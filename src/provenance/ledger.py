"""The append-only provenance ledger.

Storage is Redis because that is what the rest of the platform already runs
on, but the *shape* matters more than the backend: every record is written
once, linked to the hash of the record before it, and never updated. A
tampered or out-of-order record therefore breaks verification loudly instead
of quietly rewriting history.

Keys (all tenant-scoped, so one workspace can never read another's ledger)::

    wcp:prov:{tenant}:rec:{record_id}      the record JSON
    wcp:prov:{tenant}:seq                  INCR counter (ledger position)
    wcp:prov:{tenant}:head                 hash of the newest record
    wcp:prov:{tenant}:all                  ZSET seq -> record_id
    wcp:prov:{tenant}:url:{url_id}         ZSET seq -> record_id
    wcp:prov:{tenant}:ds:{dataset}         ZSET seq -> record_id
    wcp:prov:{tenant}:sha:{hex}            SET of record ids sharing content
    wcp:prov:{tenant}:stats                cached counters (best effort)

The ledger is deliberately *not* in the hot crawl path in a blocking way:
``record()`` is one pipeline of simple writes, and the crawler can batch it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Iterable, Sequence

from src.provenance.models import (
    LedgerStats,
    ProvenanceRecord,
    canonical_json,
    new_record_id,
)

logger = logging.getLogger("wcp.provenance")


def _url_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:16]


class ProvenanceLedger:
    """Append-only, hash-chained record store."""

    def __init__(self, redis_client, *, prefix: str = "wcp:prov") -> None:
        self.r = redis_client
        self.prefix = prefix

    # -- key helpers --------------------------------------------------------- #
    def _k(self, tenant: str, *parts: str) -> str:
        return ":".join([self.prefix, tenant or "public", *parts])

    def _decode(self, raw) -> str:
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    # -- writes -------------------------------------------------------------- #
    def record(self, rec: ProvenanceRecord) -> ProvenanceRecord:
        """Append one record, linking it to the current chain head."""
        tenant = rec.tenant or "public"
        if not rec.record_id:
            rec.record_id = new_record_id()

        seq = int(self.r.incr(self._k(tenant, "seq")))
        prev = self.r.get(self._k(tenant, "head"))
        rec.seq = seq
        rec.prev_hash = self._decode(prev) if prev else ""
        rec.recorded_at = rec.recorded_at or time.time()
        digest = hashlib.sha256(
            canonical_json({"prev": rec.prev_hash, "seq": seq, "rec": rec.unsigned_dict()})
        ).hexdigest()
        rec.record_hash = "sha256:" + digest

        payload = json.dumps(rec.to_dict(), separators=(",", ":"), default=str)
        pipe = self.r.pipeline()
        pipe.set(self._k(tenant, "rec", rec.record_id), payload)
        pipe.set(self._k(tenant, "head"), rec.record_hash)
        pipe.zadd(self._k(tenant, "all"), {rec.record_id: seq})
        pipe.zadd(self._k(tenant, "url", _url_id(rec.source_url)),
                  {rec.record_id: seq})
        if rec.dataset:
            pipe.zadd(self._k(tenant, "ds", rec.dataset), {rec.record_id: seq})
        if rec.content_sha256:
            pipe.sadd(self._k(tenant, "sha", rec.content_id), rec.record_id)
        pipe.execute()
        return rec

    def record_many(self, records: Sequence[ProvenanceRecord]) -> list[ProvenanceRecord]:
        """Append a batch, preserving order and chain links."""
        out = []
        for rec in records:
            out.append(self.record(rec))
        return out

    def verify(self, tenant: str) -> dict[str, Any]:
        """Re-walk the chain and prove nothing was edited or reordered.

        O(n) in the number of records, so it is an *on-demand* audit (an
        endpoint, a nightly job, or the pre-export check) rather than
        something that runs on every write.
        """
        ids = self._decode_ids(self.r.zrange(self._k(tenant, "all"), 0, -1))
        prev = ""
        problems: list[dict[str, Any]] = []
        for i, rid in enumerate(ids):
            rec = self.get(tenant, rid)
            if rec is None:
                problems.append({"record_id": rid, "reason": "missing"})
                continue
            expect = hashlib.sha256(
                canonical_json({"prev": prev, "seq": rec.seq, "rec": rec.unsigned_dict()})
            ).hexdigest()
            if rec.prev_hash != prev:
                problems.append({"record_id": rid, "reason": "chain_break",
                                 "expected_prev": prev, "found_prev": rec.prev_hash})
            elif rec.record_hash != "sha256:" + expect:
                problems.append({"record_id": rid, "reason": "hash_mismatch",
                                 "expected": "sha256:" + expect,
                                 "found": rec.record_hash})
            prev = rec.record_hash
            if i and i % 5000 == 0:
                logger.debug("verified %s records", i)
        return {
            "tenant": tenant,
            "records": len(ids),
            "ok": not problems,
            "problems": problems[:50],
            "problem_count": len(problems),
            "head": self._decode(self.r.get(self._k(tenant, "head")) or ""),
        }

    # -- reads --------------------------------------------------------------- #
    def get(self, tenant: str, record_id: str) -> ProvenanceRecord | None:
        raw = self.r.get(self._k(tenant, "rec", record_id))
        if not raw:
            return None
        return ProvenanceRecord.from_dict(json.loads(raw))

    def recent(self, tenant: str, limit: int = 50) -> list[ProvenanceRecord]:
        ids = self._decode_ids(
            self.r.zrevrange(self._k(tenant, "all"), 0, max(0, limit - 1))
        )
        return [r for r in (self.get(tenant, i) for i in ids) if r is not None]

    def for_url(self, tenant: str, url: str, limit: int = 50) -> list[ProvenanceRecord]:
        ids = self._decode_ids(
            self.r.zrevrange(self._k(tenant, "url", _url_id(url)), 0, max(0, limit - 1))
        )
        return [r for r in (self.get(tenant, i) for i in ids) if r is not None]

    def for_dataset(self, tenant: str, dataset: str, *, version: str = "",
                    limit: int = 5000) -> list[ProvenanceRecord]:
        ids = self._decode_ids(
            self.r.zrevrange(self._k(tenant, "ds", dataset), 0, max(0, limit - 1))
        )
        out = []
        for i in ids:
            rec = self.get(tenant, i)
            if rec is None:
                continue
            if version and rec.dataset_version != version:
                continue
            out.append(rec)
        return out

    def origin_of_content(self, tenant: str, content_sha256: str) -> list[ProvenanceRecord]:
        """Which records share this exact content hash (dedupe/lineage view)."""
        key = content_sha256.split(":", 1)[-1]
        ids = self._decode_ids(self.r.smembers(self._k(tenant, "sha", key)))
        return [r for r in (self.get(tenant, i) for i in ids) if r is not None]

    def stats(self, tenant: str) -> LedgerStats:
        ids = self._decode_ids(self.r.zrange(self._k(tenant, "all"), 0, 20000))
        st = LedgerStats(records=len(ids))
        seen_urls: set[str] = set()
        datasets: set[str] = set()
        for rid in ids:
            rec = self.get(tenant, rid)
            if rec is None:
                continue
            seen_urls.add(rec.source_url)
            if rec.dataset:
                datasets.add(rec.dataset)
            st.by_license[rec.license] = st.by_license.get(rec.license, 0) + 1
            if rec.license in ("cc-by", "cc-by-sa", "cc0", "public-domain", "mit",
                               "apache-2.0"):
                st.redistributable += 1
            if rec.license == "unknown":
                st.unknown_license += 1
            if rec.robots_verdict == "disallowed":
                st.robots_disallowed += 1
            key = rec.extractor or "unknown"
            st.by_extractor[key] = st.by_extractor.get(key, 0) + 1
            st.by_method[rec.collection_method] = \
                st.by_method.get(rec.collection_method, 0) + 1
            if rec.fetched_at:
                st.oldest_fetched_at = min(st.oldest_fetched_at or rec.fetched_at,
                                           rec.fetched_at)
                st.newest_fetched_at = max(st.newest_fetched_at, rec.fetched_at)
        st.urls = len(seen_urls)
        st.datasets = len(datasets)
        return st

    def counts(self, tenant: str) -> dict[str, int]:
        """Cheap counters for dashboards (no per-record reads)."""
        return {
            "records": int(self.r.zcard(self._k(tenant, "all")) or 0),
            "urls": int(self.r.zcard(self._k(tenant, "all")) or 0),
        }

    def head(self, tenant: str) -> str:
        raw = self.r.get(self._k(tenant, "head"))
        return self._decode(raw) if raw else ""

    def _decode_ids(self, raw: Iterable) -> list[str]:
        return [self._decode(x) for x in raw]


# --------------------------------------------------------------------------- #
# record builders — small helpers so call sites stay one-liners
# --------------------------------------------------------------------------- #
def record_for_page(
    *,
    tenant: str,
    url: str,
    body: bytes | str,
    status: int = 200,
    robots_verdict: str = "unknown",
    license: str = "unknown",
    license_url: str = "",
    method: str = "http",
    extractor: str = "",
    extractor_version: str = "",
    llm_model_id: str = "",
    prompt_hash: str = "",
    confidence: float = 0.0,
    dataset: str = "",
    dataset_version: str = "",
    snapshot_id: str = "",
    fetched_at: float | None = None,
    chain: Sequence | None = None,
    extra: dict[str, Any] | None = None,
) -> ProvenanceRecord:
    """Build a passport for one fetched page.

    ``confidence`` is the caller's own 0–1 judgement of extraction quality;
    it is stored rather than computed here because only the extractor knows
    whether it matched a selector, a checksum or a language model.
    """
    from src.provenance.models import ChainStep, content_hash

    digest = content_hash(body)
    steps = list(chain or [])
    if not steps:
        steps = [ChainStep(step="fetched", tool="crawler", detail={"status": status})]
    return ProvenanceRecord(
        source_url=url,
        tenant=tenant,
        fetched_at=fetched_at or time.time(),
        http_status=status,
        robots_verdict=robots_verdict,
        license=license,
        license_url=license_url,
        content_sha256=digest,
        snapshot_id=snapshot_id,
        dataset=dataset,
        dataset_version=dataset_version,
        extractor=extractor,
        extractor_version=extractor_version,
        llm_model_id=llm_model_id,
        prompt_hash=prompt_hash,
        collection_method=method,
        confidence=confidence,
        chain=steps,
        extra=dict(extra or {}),
    )
