"""Data structures for the provenance ledger (the "data passport").

Every scraped record the platform produces can carry a *passport*: where it
came from, under what licence, whether robots.txt allowed it, which code and
which model touched it, and when it was last verified. That is the difference
between "we scraped a million pages" and "here is the legal proof for every
record we will sell you".

The model is deliberately boring and JSON-round-trippable: a ledger that
cannot be exported, diffed, hashed and re-verified years later is not a ledger.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# controlled vocabularies — kept small on purpose; a free-text field here is a
# field nobody can query later.
# --------------------------------------------------------------------------- #
ROBOTS_VERDICTS = ("allowed", "disallowed", "unknown", "skipped", "error")
COLLECTION_METHODS = ("http", "browser", "api", "dataset", "manual", "seed")
LICENSE_IDS = (
    "cc-by",
    "cc-by-sa",
    "cc-by-nc",
    "cc0",
    "public-domain",
    "mit",
    "apache-2.0",
    "proprietary",
    "unknown",
)

# Licences we are willing to call "safe to redistribute in a dataset".
REDISTRIBUTABLE = {"cc-by", "cc-by-sa", "cc0", "public-domain", "mit", "apache-2.0"}


def now() -> float:
    return time.time()


def content_hash(data: bytes | str) -> str:
    """``sha256`` of the bytes, with the algorithm prefix the manifest uses."""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON — the exact bytes that get hashed and signed.

    ``sort_keys`` plus compact separators means two processes producing the
    same logical record produce the same digest, which is the whole point of
    a verifiable chain.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def new_record_id() -> str:
    return "pr_" + uuid.uuid4().hex[:20]


@dataclass
class ChainStep:
    """One transformation applied to a record, in order.

    The chain is what makes an AI pipeline auditable: you can see that a field
    was parsed by recipe ``bdnews24-article@2``, then cleaned, then had PII
    redacted, then translated — and by *which* model at each step.
    """

    step: str                      # fetched|extracted|cleaned|deduped|pii_redacted|...
    at: float = field(default_factory=now)
    tool: str = ""                 # module or recipe that did it
    version: str = ""
    model: str = ""                # LLM used at this step, if any
    prompt_hash: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "step": self.step,
            "at": round(float(self.at), 3),
            "tool": self.tool,
        }
        if self.version:
            out["version"] = self.version
        if self.model:
            out["model"] = self.model
        if self.prompt_hash:
            out["prompt_hash"] = self.prompt_hash
        if self.detail:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ChainStep:
        return cls(
            step=row.get("step", "unknown"),
            at=float(row.get("at", 0.0)),
            tool=row.get("tool", ""),
            version=row.get("version", ""),
            model=row.get("model", ""),
            prompt_hash=row.get("prompt_hash", ""),
            detail=dict(row.get("detail") or {}),
        )


@dataclass
class ProvenanceRecord:
    """One collected record, with everything a buyer or a regulator asks."""

    source_url: str
    tenant: str = ""
    record_id: str = ""
    fetched_at: float = field(default_factory=now)
    http_status: int = 0
    robots_verdict: str = "unknown"
    license: str = "unknown"
    license_url: str = ""
    content_sha256: str = ""
    snapshot_id: str = ""          # content-addressed raw copy (Time Machine)
    dataset: str = ""
    dataset_version: str = ""
    extractor: str = ""
    extractor_version: str = ""
    llm_model_id: str = ""
    prompt_hash: str = ""
    collection_method: str = "http"
    confidence: float = 0.0
    last_verified_at: float | None = None
    chain: list[ChainStep] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    # filled by the ledger, not by callers
    prev_hash: str = ""
    record_hash: str = ""
    seq: int = 0
    recorded_at: float = field(default_factory=now)

    def __post_init__(self) -> None:
        if not self.record_id:
            self.record_id = new_record_id()
        if self.robots_verdict not in ROBOTS_VERDICTS:
            self.robots_verdict = "unknown"
        if self.collection_method not in COLLECTION_METHODS:
            self.collection_method = "http"
        self.license = (self.license or "unknown").lower()
        if self.license not in LICENSE_IDS:
            self.license = "unknown"
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    # -- derived ------------------------------------------------------------- #
    @property
    def redistributable(self) -> bool:
        return self.license in REDISTRIBUTABLE

    @property
    def age_seconds(self) -> float:
        return max(0.0, now() - float(self.fetched_at))

    @property
    def stale(self) -> bool:
        """Verified more than 90 days ago (a licence/terms re-check is due)."""
        ref = self.last_verified_at or self.fetched_at
        return (now() - float(ref)) > 90 * 86400

    @property
    def content_id(self) -> str:
        """The ``sha256:<hex>`` without the prefix — handy for URLs."""
        return self.content_sha256.split(":", 1)[-1] if self.content_sha256 else ""

    def to_dict(self, *, include_hashes: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "record_id": self.record_id,
            "tenant": self.tenant,
            "source_url": self.source_url,
            "fetched_at": round(float(self.fetched_at), 3),
            "http_status": int(self.http_status),
            "robots_verdict": self.robots_verdict,
            "license": self.license,
            "content_sha256": self.content_sha256,
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "extractor": self.extractor,
            "extractor_version": self.extractor_version,
            "collection_method": self.collection_method,
            "confidence": round(float(self.confidence), 4),
            "chain": [s.to_dict() for s in self.chain],
            "recorded_at": round(float(self.recorded_at), 3),
        }
        if self.license_url:
            out["license_url"] = self.license_url
        if self.snapshot_id:
            out["snapshot_id"] = self.snapshot_id
        if self.llm_model_id:
            out["llm_model_id"] = self.llm_model_id
        if self.prompt_hash:
            out["prompt_hash"] = self.prompt_hash
        if self.last_verified_at:
            out["last_verified_at"] = round(float(self.last_verified_at), 3)
        if self.extra:
            out["extra"] = self.extra
        if include_hashes:
            out["prev_hash"] = self.prev_hash
            out["record_hash"] = self.record_hash
            out["seq"] = self.seq
        return out

    def unsigned_dict(self) -> dict[str, Any]:
        """Everything except the ledger's own fields — what gets hashed."""
        d = self.to_dict(include_hashes=False)
        return d

    @property
    def payload_hash(self) -> str:
        return "sha256:" + hashlib.sha256(canonical_json(self.unsigned_dict())).hexdigest()

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ProvenanceRecord:
        rec = cls(
            source_url=row.get("source_url", ""),
            tenant=row.get("tenant", ""),
            record_id=row.get("record_id", ""),
            fetched_at=float(row.get("fetched_at", 0.0)),
            http_status=int(row.get("http_status", 0)),
            robots_verdict=row.get("robots_verdict", "unknown"),
            license=row.get("license", "unknown"),
            license_url=row.get("license_url", ""),
            content_sha256=row.get("content_sha256", ""),
            snapshot_id=row.get("snapshot_id", ""),
            dataset=row.get("dataset", ""),
            dataset_version=row.get("dataset_version", ""),
            extractor=row.get("extractor", ""),
            extractor_version=row.get("extractor_version", ""),
            llm_model_id=row.get("llm_model_id", ""),
            prompt_hash=row.get("prompt_hash", ""),
            collection_method=row.get("collection_method", "http"),
            confidence=float(row.get("confidence", 0.0)),
            last_verified_at=(
                float(row["last_verified_at"]) if row.get("last_verified_at") else None
            ),
            chain=[ChainStep.from_dict(c) for c in row.get("chain") or []],
            extra=dict(row.get("extra") or {}),
            prev_hash=row.get("prev_hash", ""),
            record_hash=row.get("record_hash", ""),
            seq=int(row.get("seq", 0)),
            recorded_at=float(row.get("recorded_at", 0.0)),
        )
        return rec


@dataclass
class LedgerStats:
    records: int = 0
    urls: int = 0
    datasets: int = 0
    redistributable: int = 0
    unknown_license: int = 0
    robots_disallowed: int = 0
    oldest_fetched_at: float = 0.0
    newest_fetched_at: float = 0.0
    by_license: dict[str, int] = field(default_factory=dict)
    by_extractor: dict[str, int] = field(default_factory=dict)
    by_method: dict[str, int] = field(default_factory=dict)
    chain_ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "urls": self.urls,
            "datasets": self.datasets,
            "redistributable": self.redistributable,
            "unknown_license": self.unknown_license,
            "robots_disallowed": self.robots_disallowed,
            "oldest_fetched_at": self.oldest_fetched_at or None,
            "newest_fetched_at": self.newest_fetched_at or None,
            "by_license": dict(sorted(self.by_license.items())),
            "by_extractor": dict(sorted(self.by_extractor.items())),
            "by_method": dict(sorted(self.by_method.items())),
            "chain_ok": self.chain_ok,
        }
