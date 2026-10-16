"""Audit log — who changed what, in an append-only hash chain.

An audit log that can be edited is a liability: in a dispute, "the log says
so" only holds if the log cannot say anything else. So entries are chained
exactly like provenance records — each entry hashes its predecessor — and
``verify()`` re-walks the chain on demand.

What gets logged: authentication events, key minting/revocation, role and
membership changes, plan and quota changes, job submissions and cancellations,
dataset publishing, retention/erasure runs, and secret rotations. What does
*not*: request bodies, tokens, PII payloads. An audit log is a record of
actions, not a shadow copy of user data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("wcp.audit")

#: high-signal action vocabulary; anything else is stored as ``other``
ACTIONS = (
    "auth.signup", "auth.login", "auth.failed_login", "auth.logout",
    "auth.sso_login", "auth.refresh",
    "key.create", "key.revoke", "key.rotate",
    "member.invite", "member.remove", "member.role_change",
    "org.update", "org.plan_change", "org.sso_configure", "org.secret_rotate",
    "quota.override", "billing.checkout", "billing.invoice",
    "job.submit", "job.cancel", "job.reject_quota",
    "discovery.create", "discovery.approve", "discovery.reject",
    "watch.create", "watch.update", "watch.delete", "watch.run",
    "workflow.create", "workflow.update", "workflow.run", "workflow.delete",
    "dataset.create", "dataset.publish", "dataset.export", "dataset.delete",
    "recipe.install", "recipe.remove", "recipe.publish",
    "provenance.export", "provenance.verify",
    "retention.update", "retention.run", "erasure.run",
    "audit.export", "backup.create", "backup.restore",
    "other",
)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


@dataclass
class AuditEntry:
    action: str
    tenant: str
    actor: str = ""                 # user id or api key id
    actor_kind: str = "user"        # user | api_key | system
    target: str = ""
    at: float = field(default_factory=time.time)
    ip: str = ""
    user_agent: str = ""
    request_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    entry_id: str = ""
    seq: int = 0
    prev_hash: str = ""
    entry_hash: str = ""

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            self.action = "other"
        if not self.entry_id:
            import uuid

            self.entry_id = "au_" + uuid.uuid4().hex[:18]

    def body(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("entry_hash", "prev_hash", "seq"):
            row.pop(key, None)
        return row

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> AuditEntry:
        entry = cls(action=row.get("action", "other"), tenant=row.get("tenant", ""),
                    actor=row.get("actor", ""))
        for k, v in row.items():
            if hasattr(entry, k):
                setattr(entry, k, v)
        entry.__post_init__()
        return entry


class AuditLog:
    """Append-only, hash-chained, tenant-scoped."""

    def __init__(self, redis_client, *, prefix: str = "wcp:audit",
                 max_entries: int = 200_000) -> None:
        self.r = redis_client
        self.prefix = prefix
        self.max_entries = max_entries

    def _k(self, tenant: str, *parts: str) -> str:
        return ":".join([self.prefix, tenant or "public", *parts])

    @staticmethod
    def _dec(raw) -> str:
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    # -- writes -------------------------------------------------------------- #
    def log(self, action: str, *, tenant: str, actor: str = "",
            actor_kind: str = "user", target: str = "", ip: str = "",
            user_agent: str = "", request_id: str = "",
            **metadata: Any) -> AuditEntry:
        entry = AuditEntry(action=action, tenant=tenant, actor=actor,
                           actor_kind=actor_kind, target=target, ip=ip,
                           user_agent=user_agent, request_id=request_id,
                           metadata=_redact(metadata))
        seq = int(self.r.incr(self._k(tenant, "seq")))
        prev = self.r.get(self._k(tenant, "head"))
        entry.seq = seq
        entry.prev_hash = self._dec(prev) if prev else ""
        entry.entry_hash = "sha256:" + hashlib.sha256(
            _canonical({"prev": entry.prev_hash, "seq": seq, "entry": entry.body()})
        ).hexdigest()

        pipe = self.r.pipeline()
        pipe.set(self._k(tenant, "entry", entry.entry_id),
                 json.dumps(entry.to_dict(), separators=(",", ":"), default=str))
        pipe.set(self._k(tenant, "head"), entry.entry_hash)
        pipe.zadd(self._k(tenant, "index"), {entry.entry_id: seq})
        pipe.zadd(self._k(tenant, "action", entry.action), {entry.entry_id: seq})
        if entry.actor:
            pipe.zadd(self._k(tenant, "actor", entry.actor), {entry.entry_id: seq})
        pipe.execute()
        return entry

    def log_many(self, entries: Iterable[dict[str, Any]]) -> list[AuditEntry]:
        return [self.log(**e) for e in entries]

    # -- reads --------------------------------------------------------------- #
    def get(self, tenant: str, entry_id: str) -> AuditEntry | None:
        raw = self.r.get(self._k(tenant, "entry", entry_id))
        return AuditEntry.from_dict(json.loads(raw)) if raw else None

    def recent(self, tenant: str, *, limit: int = 100, action: str = "",
               actor: str = "") -> list[AuditEntry]:
        if action:
            key = self._k(tenant, "action", action)
        elif actor:
            key = self._k(tenant, "actor", actor)
        else:
            key = self._k(tenant, "index")
        ids = [self._dec(i) for i in self.r.zrevrange(key, 0, max(0, limit - 1))]
        return [e for e in (self.get(tenant, i) for i in ids) if e is not None]

    def counts_by_action(self, tenant: str, *, limit: int = 5000) -> dict[str, int]:
        rows = self.recent(tenant, limit=limit)
        out: dict[str, int] = {}
        for entry in rows:
            out[entry.action] = out.get(entry.action, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def verify(self, tenant: str, *, limit: int = 20_000) -> dict[str, Any]:
        ids = [self._dec(i) for i in self.r.zrange(self._k(tenant, "index"), 0,
                                                   limit - 1)]
        prev = ""
        problems: list[dict[str, Any]] = []
        for entry_id in ids:
            entry = self.get(tenant, entry_id)
            if entry is None:
                problems.append({"entry_id": entry_id, "reason": "missing"})
                continue
            expect = "sha256:" + hashlib.sha256(
                _canonical({"prev": prev, "seq": entry.seq, "entry": entry.body()})
            ).hexdigest()
            if entry.prev_hash != prev or entry.entry_hash != expect:
                problems.append({"entry_id": entry_id,
                                 "reason": "chain_break" if entry.prev_hash != prev
                                 else "hash_mismatch",
                                 "expected": expect, "found": entry.entry_hash})
            prev = entry.entry_hash
        return {"tenant": tenant, "entries": len(ids), "ok": not problems,
                "problems": problems[:20], "problem_count": len(problems)}

    def export(self, tenant: str, *, limit: int = 10_000) -> dict[str, Any]:
        entries = [e.to_dict() for e in self.recent(tenant, limit=limit)]
        return {
            "tenant": tenant,
            "exported_at": time.time(),
            "entries": entries,
            "verification": self.verify(tenant, limit=limit),
            "note": "Entries are hash-chained; verification re-walks the chain.",
        }


_SECRET_KEYS = ("password", "token", "secret", "api_key", "authorization",
                "cookie", "key")


def _redact(metadata: dict[str, Any]) -> dict[str, Any]:
    """Never store credentials in an audit entry, even by accident."""
    out: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if any(s in key.lower() for s in _SECRET_KEYS):
            out[key] = "«redacted»"
        elif isinstance(value, dict):
            out[key] = _redact(value)
        elif isinstance(value, str) and len(value) > 500:
            out[key] = value[:500] + "…"
        else:
            out[key] = value
    return out


def action_catalogue() -> list[str]:
    return list(ACTIONS)
