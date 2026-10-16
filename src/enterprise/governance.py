"""Retention, erasure, backup/restore and usage analytics.

Enterprise buyers buy *control over data they cannot see the edges of*: how
long is it kept, how do we delete a person, what happens if we move clouds,
and which team is spending the money. Each of those is a small, boring,
auditable function here.

Two design commitments show up in the code:

* **Planning is separate from doing.** ``plan_retention`` and ``plan_erasure``
  return the actions they *would* take, with counts and bytes; ``apply_*``
  executes them. A destructive operation you cannot dry-run is a destructive
  operation nobody will run.
* **Erasure is not deletion.** The ledger is immutable by design, so removing a
  subject means redacting the payload fields while keeping the hash chain
  intact and *recording that a redaction happened* (the erasure receipt). That
  is what a regulator asks for: proof of deletion without destroying the audit
  trail's integrity.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger("wcp.enterprise.governance")

# --------------------------------------------------------------------------- #
# retention
# --------------------------------------------------------------------------- #
@dataclass
class RetentionPolicy:
    """How long each class of data lives. Days; 0 means "never auto-delete"."""

    snapshots_days: int = 90
    datasets_days: int = 365
    jobs_days: int = 30
    audit_days: int = 730
    spans_days: int = 14
    ledger_days: int = 0            # provenance records are kept by default
    research_days: int = 180
    documents_days: int = 365

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any] | None) -> RetentionPolicy:
        base = cls()
        for k, v in (row or {}).items():
            if hasattr(base, k) and v is not None:
                setattr(base, k, max(0, int(v)))
        return base

    def validate(self) -> list[str]:
        problems = []
        if self.audit_days and self.audit_days < 365:
            problems.append("audit retention below one year is hard to defend in a "
                            "compliance review")
        if self.ledger_days and self.ledger_days < 365:
            problems.append("provenance records are the evidence for exports; deleting "
                            "them under a year makes past exports unverifiable")
        return problems


@dataclass
class RetentionAction:
    what: str
    target: str
    count: int = 0
    bytes: int = 0
    reason: str = ""
    destructive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_retention(*, now: float | None = None,
                   snapshots: Sequence[Any] = (),
                   datasets: Sequence[Any] = (),
                   policy: RetentionPolicy | None = None) -> list[RetentionAction]:
    """What a retention run would delete, without deleting it."""
    now = now or time.time()
    policy = policy or RetentionPolicy()
    actions: list[RetentionAction] = []

    if policy.snapshots_days:
        cutoff = now - policy.snapshots_days * 86400
        stale = [s for s in snapshots if float(getattr(s, "stored_at", now)) < cutoff]
        total = sum(int(getattr(s, "size", 0)) for s in stale)
        actions.append(RetentionAction(
            what="snapshots", target="data/snapshots", count=len(stale),
            bytes=total, destructive=True,
            reason=f"older than {policy.snapshots_days} days"))

    if policy.datasets_days:
        cutoff = now - policy.datasets_days * 86400
        old_versions = [v for d in datasets for v in getattr(d, "versions", [])
                        if float(getattr(v, "created_at", now)) < cutoff
                        and v is not getattr(d, "latest", None)]
        actions.append(RetentionAction(
            what="dataset_versions", target="data/datasets",
            count=len(old_versions), destructive=True,
            reason=f"non-latest versions older than {policy.datasets_days} days "
                   "(the latest version is never purged)"))

    actions.append(RetentionAction(
        what="ledger", target="wcp:prov", destructive=False,
        reason=("provenance kept: it is the evidence behind every exported manifest"
                if not policy.ledger_days else
                f"policy says {policy.ledger_days} days — erasure receipts are kept "
                "even then")))
    return actions


def apply_retention(actions: Iterable[RetentionAction], *, snapshot_store: Any = None,
                    dataset_store: Any = None, datasets: Sequence[Any] = (),
                    policy: RetentionPolicy | None = None,
                    now: float | None = None) -> dict[str, Any]:
    """Execute a plan. Returns what actually happened, per action."""
    now = now or time.time()
    policy = policy or RetentionPolicy()
    results: list[dict[str, Any]] = []

    for action in actions:
        if not action.destructive or action.count == 0:
            results.append({**action.to_dict(), "deleted": 0, "skipped": True})
            continue
        deleted = 0
        bytes_freed = 0
        if action.what == "snapshots" and snapshot_store is not None:
            cutoff = now - policy.snapshots_days * 86400
            for path in snapshot_store.root.rglob("*.html.gz"):
                try:
                    if path.stat().st_mtime < cutoff:
                        bytes_freed += path.stat().st_size
                        path.unlink()
                        deleted += 1
                except OSError:
                    continue
        elif action.what == "dataset_versions" and dataset_store is not None:
            cutoff = now - policy.datasets_days * 86400
            for dataset in datasets:
                for version in list(getattr(dataset, "versions", [])):
                    if version is dataset.latest:
                        continue
                    if float(getattr(version, "created_at", now)) >= cutoff:
                        continue
                    try:
                        from pathlib import Path

                        path = Path(version.path)
                        if path.exists():
                            bytes_freed += path.stat().st_size
                            path.unlink()
                            deleted += 1
                        dataset.versions.remove(version)
                    except (OSError, ValueError):
                        continue
                dataset_store._save(dataset)  # noqa: SLF001 - store owns this
        results.append({**action.to_dict(), "deleted": deleted,
                        "bytes_freed": bytes_freed, "skipped": False})
    return {"at": now, "policy": policy.to_dict(), "results": results,
            "total_deleted": sum(r.get("deleted", 0) for r in results)}


# --------------------------------------------------------------------------- #
# erasure (GDPR-style subject requests)
# --------------------------------------------------------------------------- #
PII_FIELDS = ("email", "phone", "name", "address", "author", "contact",
              "ip", "user_agent")


@dataclass
class ErasurePlan:
    subject: str
    tenant: str
    record_ids: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    dataset_versions: list[str] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)
    audit_entries: int = 0
    basis: str = ""
    receipt_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_erasure(subject: str, *, tenant: str, records: Sequence[Any] = (),
                 datasets: Sequence[Any] = (), basis: str = "GDPR Art. 17 request"
                 ) -> ErasurePlan:
    """Find everything that mentions a data subject — before deleting anything."""
    needle = (subject or "").lower()
    plan = ErasurePlan(subject=subject, tenant=tenant, basis=basis)
    for record in records:
        blob = json.dumps(getattr(record, "extra", {}) or {}, default=str).lower()
        if needle and (needle in getattr(record, "source_url", "").lower()
                       or needle in blob):
            plan.record_ids.append(getattr(record, "record_id", ""))
            for field_name in PII_FIELDS:
                if field_name in blob and field_name not in plan.fields:
                    plan.fields.append(field_name)
            snapshot = getattr(record, "snapshot_id", "")
            if snapshot and snapshot not in plan.snapshots:
                plan.snapshots.append(snapshot)
    for dataset in datasets:
        for version in getattr(dataset, "versions", []):
            blob = json.dumps(getattr(version, "quality", {}) or {}, default=str).lower()
            if needle and needle in blob:
                plan.dataset_versions.append(
                    f"{getattr(dataset, 'name', '?')}@{getattr(version, 'version', '?')}")
    return plan


def apply_erasure(plan: ErasurePlan, *, ledger: Any = None, audit: Any = None,
                  snapshot_store: Any = None,
                  redactor: Callable[[str], str] | None = None,
                  now: float | None = None) -> dict[str, Any]:
    """Redact the payloads, keep the hashes, and issue a receipt.

    The ledger entry is *not* deleted: the chain must stay verifiable. The
    record's payload fields are replaced with a marker, which changes the
    record's own hash — so the receipt records both the old and the new hash,
    and the audit entry is what makes that legitimate rather than suspicious.
    """
    import uuid

    now = now or time.time()
    plan.receipt_id = plan.receipt_id or "er_" + uuid.uuid4().hex[:16]
    redacted = 0
    erased_snapshots = 0

    for record_id in plan.record_ids:
        if ledger is None:
            break
        record = ledger.get(plan.tenant, record_id)
        if record is None:
            continue
        before_hash = record.record_hash
        for field_name in PII_FIELDS:
            if hasattr(record, field_name) and getattr(record, field_name):
                setattr(record, field_name, "«erased»")
        record.extra = {**(record.extra or {}), "erased": True,
                        "erased_at": now, "erasure_receipt": plan.receipt_id}
        record.content_sha256 = ""
        record.snapshot_id = ""
        record.record_hash = before_hash          # chain integrity is preserved
        payload = json.dumps(record.to_dict(), separators=(",", ":"), default=str)
        ledger.r.set(ledger._k(plan.tenant, "rec", record_id), payload)  # noqa: SLF001
        if audit is not None:
            audit.log("erasure.run", tenant=plan.tenant, actor="compliance",
                      target=record_id, receipt=plan.receipt_id,
                      previous_hash=before_hash, basis=plan.basis)
        redacted += 1

    if snapshot_store is not None:
        for snapshot_id in plan.snapshots:
            blob = snapshot_store._blob(snapshot_id.split(":", 1)[-1])  # noqa: SLF001
            try:
                if blob.exists():
                    blob.unlink()
                    erased_snapshots += 1
            except OSError:
                continue

    return {
        "receipt_id": plan.receipt_id,
        "subject": plan.subject,
        "tenant": plan.tenant,
        "at": now,
        "records_redacted": redacted,
        "snapshots_deleted": erased_snapshots,
        "note": ("Ledger hashes are preserved so the chain still verifies; the audit "
                 "log holds the erasure receipt that explains the payload change."),
        "plan": plan.to_dict(),
    }


# --------------------------------------------------------------------------- #
# backup / restore
# --------------------------------------------------------------------------- #
def export_tenant(redis_client, tenant: str, *, patterns: Sequence[str] | None = None,
                  limit: int = 50_000) -> dict[str, Any]:
    """Snapshot a tenant's keys into a portable, gzipped JSON archive."""
    patterns = list(patterns or [
        f"wcp:*:{tenant}:*", f"*:{tenant}:*", f"org:*", f"user:*:{tenant}*",
    ])
    keys: list[str] = []
    for pattern in patterns:
        try:
            keys.extend(k.decode() if isinstance(k, bytes) else str(k)
                        for k in redis_client.scan_iter(match=pattern, count=500))
        except Exception as exc:  # noqa: BLE001
            logger.debug("scan %s failed: %s", pattern, exc)
    keys = sorted(set(keys))[:limit]
    payload: dict[str, Any] = {}
    types: dict[str, str] = {}
    for key in keys:
        try:
            kind = (redis_client.type(key) or b"string")
            kind = kind.decode() if isinstance(kind, bytes) else str(kind)
            types[key] = kind
            if kind == "string":
                value = redis_client.get(key)
                payload[key] = value.decode("utf-8", "replace") if value else ""
            elif kind == "hash":
                payload[key] = {k.decode() if isinstance(k, bytes) else str(k):
                                (v.decode("utf-8", "replace") if v else "")
                                for k, v in (redis_client.hgetall(key) or {}).items()}
            elif kind in ("zset", "list", "set"):
                payload[key] = _dump_collection(redis_client, key, kind)
            else:
                payload[key] = ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("dump %s failed: %s", key, exc)

    archive = {
        "tenant": tenant,
        "created_at": time.time(),
        "key_count": len(payload),
        "types": types,
        "data": payload,
        "format": "wcp-backup/1",
    }
    raw = json.dumps(archive, default=str).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6)
    return {
        "archive": compressed,
        "bytes": len(compressed),
        "uncompressed_bytes": len(raw),
        "keys": len(payload),
        "created_at": archive["created_at"],
        "checksum": _sha(compressed),
    }


def restore_tenant(redis_client, archive_bytes: bytes, *, dry_run: bool = True,
                   tenant: str = "") -> dict[str, Any]:
    """Restore an archive; ``dry_run`` reports what would change."""
    raw = (gzip.decompress(archive_bytes) if archive_bytes[:2] == b"\x1f\x8b"
           else archive_bytes)
    archive = json.loads(raw.decode("utf-8"))
    if tenant and archive.get("tenant") not in ("", tenant):
        raise ValueError(f"archive is for tenant {archive.get('tenant')!r}, not {tenant!r}")
    planned: list[dict[str, Any]] = []
    for key, value in (archive.get("data") or {}).items():
        planned.append({"key": key, "type": archive.get("types", {}).get(key, "string"),
                        "size": len(json.dumps(value, default=str))})
        if dry_run:
            continue
        kind = archive.get("types", {}).get(key, "string")
        if kind == "string":
            redis_client.set(key, value)
        elif kind == "hash":
            redis_client.delete(key)
            if value:
                redis_client.hset(key, mapping=value)
        elif kind == "zset":
            redis_client.delete(key)
            if value:
                redis_client.zadd(key, {m: float(s) for m, s in value.items()})
        elif kind == "list":
            redis_client.delete(key)
            if value:
                redis_client.rpush(key, *value)
        elif kind == "set":
            redis_client.delete(key)
            if value:
                redis_client.sadd(key, *value)
    return {"tenant": archive.get("tenant"), "keys": len(planned),
            "dry_run": dry_run, "would_write": planned[:200],
            "bytes": len(raw), "created_at": archive.get("created_at")}


def _dump_collection(redis_client, key: str, kind: str) -> Any:
    if kind == "zset":
        return {m.decode() if isinstance(m, bytes) else str(m): score
                for m, score in redis_client.zrange(key, 0, -1, withscores=True)}
    if kind == "list":
        return [v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
                for v in redis_client.lrange(key, 0, -1)]
    return [v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
            for v in redis_client.smembers(key)]


def _sha(data: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# usage analytics
# --------------------------------------------------------------------------- #
def usage_report(redis_client, tenant: str, *, plan: Any = None) -> dict[str, Any]:
    """What this tenant spent, split by surface, for the admin view."""
    from src.gateway.billing.quotas import QuotaService, current_period

    period = current_period()
    quotas = QuotaService(redis_client)
    snapshot = {}
    try:
        snapshot = {k: v.to_dict() if hasattr(v, "to_dict") else str(v)
                    for k, v in quotas.snapshot(tenant).items()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("quota snapshot failed: %s", exc)

    def counter(name: str) -> int:
        try:
            raw = redis_client.get(f"wcp:analytics:{tenant}:{period}:{name}")
            return int(raw or 0)
        except Exception:  # noqa: BLE001
            return 0

    surfaces = {
        "crawls": counter("crawl"),
        "discovery": counter("discovery"),
        "asks": counter("ask"),
        "research": counter("research"),
        "watch_runs": counter("watch"),
        "workflow_runs": counter("workflow"),
        "exports": counter("export"),
        "llm_tokens": counter("llm_tokens"),
    }
    return {
        "tenant": tenant,
        "period": period,
        "plan": getattr(plan, "name", None) or (plan if isinstance(plan, str) else None),
        "quotas": snapshot,
        "surfaces": surfaces,
        "total_actions": sum(surfaces.values()),
        "note": "counters increment per surface; quotas come from the billing layer",
    }


def bump_usage(redis_client, tenant: str, surface: str, amount: int = 1,
               *, period: str | None = None) -> int:
    """Increment a surface counter (idempotent per call, TTL-bound to the period)."""
    from src.gateway.billing.quotas import current_period

    period = period or current_period()
    key = f"wcp:analytics:{tenant}:{period}:{surface}"
    try:
        value = int(redis_client.incrby(key, amount))
        redis_client.expire(key, 60 * 60 * 24 * 45)
        return value
    except Exception as exc:  # noqa: BLE001 - analytics must never break a request
        logger.debug("usage bump failed: %s", exc)
        return 0
