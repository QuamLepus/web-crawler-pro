"""Orchestration + Redis-backed job records for topic discovery.

A discovery job moves through :class:`DiscoveryState`::

    queued ──discover──> discovering ──rank──> awaiting_approval
                                          └──(auto=true)──┐
    awaiting_approval ──approve──> crawling ─────────────┘──> succeeded

Redis layout (same ``wcp:v1:*`` namespace the REST API already uses)
--------------------------------------------------------------------
``wcp:v1:discovery:{job_id}``
    Hash: topic, count, state, seeds, queries, providers, totals, error...
``wcp:v1:discovery:{job_id}:candidates``
    JSON list of scored :class:`~src.discovery.ranker.Candidate` dicts.
``wcp:v1:tenant_discoveries:{tenant}``
    ZSET of job ids, scored by creation time, for tenant-scoped listing.
``wcp:v1:job:{crawl_job_id}``
    One record per site, written by :func:`workers.crawl_tasks.run_crawl` —
    :meth:`DiscoveryService.results` aggregates these.

Everything is injectable (``redis_client``, ``fetcher``, ``providers``,
``llm``, ``scheduler``) so the whole flow is exercisable offline in tests.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from src.utils.logger import setup_logger

logger = setup_logger(__name__)

DISCOVERY_KEY = "wcp:v1:discovery:{job_id}"
CANDIDATES_KEY = "wcp:v1:discovery:{job_id}:candidates"
TENANT_DISCOVERIES = "wcp:v1:tenant_discoveries:{tenant}"
CRAWL_JOB_KEY = "wcp:v1:job:{job_id}"

# Keep job records for a month — long enough to poll, short enough to expire.
_TTL_SECONDS = 60 * 60 * 24 * 30


class DiscoveryState(str, Enum):
    queued = "queued"
    discovering = "discovering"
    awaiting_approval = "awaiting_approval"
    crawling = "crawling"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class DiscoveryRequest:
    """What the caller asked for."""

    topic: str
    count: int = 10
    seeds: list[str] = field(default_factory=list)
    language: str = "en"
    auto: bool = False              # approve + crawl without a human in the loop
    max_pages_per_site: int = 5
    per_domain: int = 1             # 1 == "N distinct websites"
    obey_robots: bool = True
    stealth: bool = False
    tenant: str = ""
    job_id: str = ""

    def __post_init__(self) -> None:
        self.topic = (self.topic or "").strip()
        self.count = max(1, int(self.count or 1))
        self.seeds = [s for s in (self.seeds or []) if s]
        self.job_id = self.job_id or f"disc_{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class DiscoveryService:
    """Plan -> search -> rank -> (approve) -> crawl."""

    def __init__(self, redis_client, *, fetcher=None, providers=None,
                 scheduler=None, llm=None) -> None:
        self.r = redis_client
        self.fetcher = fetcher
        self._providers = providers
        self._scheduler = scheduler
        self.llm = llm

    # -- redis helpers ------------------------------------------------------ #
    def _hset(self, job_id: str, fields: dict) -> None:
        if self.r is None:
            return
        try:
            self.r.hset(DISCOVERY_KEY.format(job_id=job_id),
                        mapping={**{k: str(v) for k, v in fields.items()},
                                 "updated_at": utcnow_iso()})
            self.r.expire(DISCOVERY_KEY.format(job_id=job_id), _TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - progress is best-effort
            logger.warning("could not update discovery %s: %s", job_id, exc)

    def _decode(self, raw: dict) -> dict:
        return {(k.decode() if isinstance(k, bytes) else k):
                (v.decode() if isinstance(v, bytes) else v)
                for k, v in raw.items()}

    def get(self, job_id: str) -> dict | None:
        if self.r is None:
            return None
        raw = self.r.hgetall(DISCOVERY_KEY.format(job_id=job_id))
        if not raw:
            return None
        return self._decode(raw)

    # -- step 0: create ----------------------------------------------------- #
    def create(self, req: DiscoveryRequest) -> dict:
        """Persist a new job in ``queued`` and register it under the tenant."""
        now = utcnow_iso()
        record = {
            "job_id": req.job_id, "tenant": req.tenant, "topic": req.topic,
            "count": req.count, "state": DiscoveryState.queued.value,
            "language": req.language, "auto": req.auto,
            "max_pages_per_site": req.max_pages_per_site,
            "per_domain": req.per_domain,
            "obey_robots": req.obey_robots, "stealth": req.stealth,
            "seeds": json.dumps(req.seeds), "queries": "[]",
            "providers": "[]", "crawl_jobs": "{}", "per_site": "[]",
            "pages": 0, "items": 0, "sites_total": 0, "sites_succeeded": 0,
            "sites_failed": 0, "error": "",
            "created_at": now, "updated_at": now,
        }
        if self.r is not None:
            pipe = self.r.pipeline()
            pipe.hset(DISCOVERY_KEY.format(job_id=req.job_id),
                      mapping={k: str(v) for k, v in record.items()})
            pipe.expire(DISCOVERY_KEY.format(job_id=req.job_id), _TTL_SECONDS)
            if req.tenant:
                pipe.zadd(TENANT_DISCOVERIES.format(tenant=req.tenant),
                          {req.job_id: time.time()})
            pipe.execute()
        return record

    # -- step 1: discover --------------------------------------------------- #
    def discover(self, job_id: str) -> dict:
        """Plan queries, search every provider, rank and store candidates."""
        record = self.get(job_id)
        if record is None:
            raise KeyError(f"unknown discovery job {job_id!r}")

        req = DiscoveryRequest(
            topic=record.get("topic", ""),
            count=int(record.get("count", 10) or 10),
            seeds=json.loads(record.get("seeds") or "[]"),
            language=record.get("language", "en"),
            auto=str(record.get("auto", "False")) == "True",
            max_pages_per_site=int(record.get("max_pages_per_site", 5) or 5),
            per_domain=int(record.get("per_domain", 1) or 1),
            obey_robots=str(record.get("obey_robots", "True")) == "True",
            stealth=str(record.get("stealth", "False")) == "True",
            tenant=record.get("tenant", ""), job_id=job_id,
        )

        self._hset(job_id, {"state": DiscoveryState.discovering.value})
        try:
            from src.config import get_config

            cfg = get_config()
            plan = plan_queries(req.topic,
                                max_queries=cfg.DISCOVERY_MAX_QUERIES,
                                llm=self.llm)
            providers = self._providers
            if providers is None:
                providers = build_providers(seeds=req.seeds, fetcher=self.fetcher,
                                            llm=self.llm)
            per_query = max(5, min(20, req.count * 2))
            hits = collect_hits(providers, plan.queries, limit=per_query)

            from src.discovery.ranker import Ranker

            ranker = Ranker(req.topic, language=req.language,
                            blocklist=cfg.DISCOVERY_BLOCKLIST)
            candidates = ranker.build(hits, seed_domains=req.seeds)
            scored = ranker.score(candidates)[:cfg.DISCOVERY_MAX_CANDIDATES]
            shortlist = {c.url for c in
                         ranker.select(scored, req.count, per_domain=req.per_domain)}
            for cand in scored:
                cand.selected = cand.url in shortlist

            self._store_candidates(job_id, scored)

            provider_names = sorted({p for c in scored for p in c.providers})
            # Always stop at awaiting_approval, even in auto mode: this is the
            # review checkpoint. `run_discovery_and_crawl` (and the
            # discover_and_crawl Celery task) approve on the caller's behalf
            # straight afterwards, so an auto job does not actually pause.
            self._hset(job_id, {
                "state": DiscoveryState.awaiting_approval.value,
                "queries": json.dumps(plan.queries),
                "providers": json.dumps(provider_names),
                "candidates": len(scored),
                "selected": len(shortlist),
            })
        except Exception as exc:
            logger.exception("discovery %s failed", job_id)
            self._hset(job_id, {"state": DiscoveryState.failed.value,
                                "error": str(exc)[:500]})
            raise

        record = self.get(job_id) or {}
        record["auto"] = req.auto
        return record

    def _store_candidates(self, job_id: str, candidates: Sequence[Any]) -> None:
        if self.r is None:
            return
        payload = json.dumps([c.to_dict() for c in candidates])
        self.r.set(CANDIDATES_KEY.format(job_id=job_id), payload, ex=_TTL_SECONDS)

    def candidates(self, job_id: str, *, only_selected: bool = False) -> list[dict]:
        from src.discovery.ranker import Candidate

        if self.r is None:
            return []
        raw = self.r.get(CANDIDATES_KEY.format(job_id=job_id))
        if not raw:
            return []
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        items = [Candidate.from_dict(d) for d in data]
        if only_selected:
            items = [c for c in items if c.selected and not c.rejected]
        return [c.to_dict() for c in items]

    # -- step 2: review ------------------------------------------------------ #
    def drop(self, job_id: str, domain: str) -> list[dict]:
        """Reject every candidate on ``domain`` and re-pick the shortlist."""
        from src.discovery.ranker import Candidate, Ranker

        raw = self.r.get(CANDIDATES_KEY.format(job_id=job_id)) if self.r else None
        if not raw:
            return []
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        items = [Candidate.from_dict(d) for d in data]
        target = domain.lower().lstrip(".").removeprefix("www.")
        changed = False
        for cand in items:
            hit = (cand.domain == target or cand.domain.endswith("." + target))
            if hit and not cand.rejected:
                cand.rejected = True
                cand.selected = False
                cand.reasons.append("dropped by reviewer")
                changed = True
        if changed:
            record = self.get(job_id) or {}
            ranker = Ranker(record.get("topic", ""),
                            language=record.get("language", "en"))
            keep = [c for c in items if not c.rejected]
            chosen = {c.url for c in ranker.select(
                keep, int(record.get("count", 10) or 10),
                per_domain=int(record.get("per_domain", 1) or 1))}
            for cand in items:
                cand.selected = cand.url in chosen
            self._store_candidates(job_id, items)
            self._hset(job_id, {"selected": len(chosen)})
        return [c.to_dict() for c in items]

    def approve(self, job_id: str, domains: Iterable[str] | None = None,
                *, dispatch: bool = True) -> tuple[list[dict], list[str]]:
        """Lock the shortlist and fan out one ``crawl_job`` per website.

        ``dispatch=False`` only locks the shortlist and allocates the crawl job
        ids — used by :func:`run_discovery_and_crawl`, which crawls in-process
        instead of going through the broker (otherwise the site would be
        crawled twice).

        Returns ``(selected_candidates, crawl_job_ids)``.
        """
        record = self.get(job_id)
        if record is None:
            raise KeyError(f"unknown discovery job {job_id!r}")
        if record.get("state") in (DiscoveryState.crawling.value,
                                   DiscoveryState.succeeded.value):
            # Already approved (e.g. a retried task) — replay the same ids.
            existing = json.loads(record.get("crawl_jobs") or "{}")
            if existing:
                return (self.candidates(job_id, only_selected=True),
                        list(existing.values()))

        from src.discovery.ranker import Candidate, Ranker

        raw = self.r.get(CANDIDATES_KEY.format(job_id=job_id)) if self.r else None
        items = ([Candidate.from_dict(d) for d in json.loads(
            raw.decode() if isinstance(raw, bytes) else raw)] if raw else [])

        wanted = None
        if domains is not None:
            wanted = {str(d).lower().lstrip(".").removeprefix("www.")
                      for d in domains if d}
        if wanted:
            for cand in items:
                cand.selected = (not cand.rejected) and cand.domain in wanted
        else:
            ranker = Ranker(record.get("topic", ""),
                            language=record.get("language", "en"))
            keep = [c for c in items if not c.rejected]
            chosen = {c.url for c in ranker.select(
                keep, int(record.get("count", 10) or 10),
                per_domain=int(record.get("per_domain", 1) or 1))}
            for cand in items:
                cand.selected = cand.url in chosen

        selected = [c for c in items if c.selected and not c.rejected]
        self._store_candidates(job_id, items)

        if not selected:
            self._hset(job_id, {"state": DiscoveryState.failed.value,
                                "error": "no candidates left to crawl"})
            return [], []

        job_map: dict[str, str] = {}
        for cand in selected:
            crawl_id = f"crawl_{uuid.uuid4().hex[:16]}"
            job_map[cand.domain] = crawl_id
            if dispatch:
                self._submit_crawl(record, cand, crawl_id)

        self._hset(job_id, {
            "state": DiscoveryState.crawling.value,
            "crawl_jobs": json.dumps(job_map),
            "sites_total": len(selected),
        })
        self._emit(record.get("tenant", ""), "discovery.approved", {
            "job_id": job_id, "topic": record.get("topic", ""),
            "sites": [c.domain for c in selected],
        })
        return [c.to_dict() for c in selected], list(job_map.values())

    def _submit_crawl(self, record: dict, cand, crawl_id: str) -> None:
        """Dispatch one site to Celery (or the Redis fallback queue)."""
        payload = {
            "url": cand.url, "tenant": record.get("tenant", ""),
            "job_id": crawl_id, "discovery_id": record.get("job_id", ""),
            "max_pages": int(record.get("max_pages_per_site", 5) or 5),
            "obey_robots": str(record.get("obey_robots", "True")) == "True",
            "stealth": str(record.get("stealth", "False")) == "True",
        }
        scheduler = self._scheduler
        if scheduler is None and self.r is not None:
            try:
                from src.compute.scheduler import SchedulerService

                scheduler = SchedulerService(self.r)
            except Exception as exc:  # noqa: BLE001
                logger.warning("scheduler unavailable (%s)", exc)
                return
        if scheduler is None:
            return
        try:
            from src.compute.scheduler import JobSpec

            scheduler.submit(JobSpec(kind="crawl_job", payload=payload,
                                     tenant=record.get("tenant", ""),
                                     job_id=crawl_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not dispatch crawl for %s: %s",
                           cand.domain, exc)

    # -- step 3: results ------------------------------------------------------ #
    def results(self, job_id: str) -> dict:
        """Aggregate the per-site crawl job records into one report."""
        record = self.get(job_id)
        if record is None:
            raise KeyError(f"unknown discovery job {job_id!r}")
        job_map = json.loads(record.get("crawl_jobs") or "{}")
        per_site: list[dict] = []
        pages = items = 0
        succeeded = failed = 0
        digests: list[str] = []
        for domain, crawl_id in job_map.items():
            raw = self.r.hgetall(CRAWL_JOB_KEY.format(job_id=crawl_id)) \
                if self.r else {}
            site = self._decode(raw) if raw else {}
            state = site.get("state", "unknown")
            if state in {"succeeded", "completed"}:
                succeeded += 1
            elif state == "failed":
                failed += 1
            entry = {
                "domain": domain, "job_id": crawl_id, "state": state,
                "pages": int(site.get("pages_done", 0) or 0),
                "items": int(site.get("items_collected", 0) or 0),
                "data_sha256": site.get("data_sha256", ""),
                "error": site.get("error", ""),
            }
            if entry["data_sha256"]:
                digests.append(entry["data_sha256"])
            pages += entry["pages"]
            items += entry["items"]
            per_site.append(entry)

        state = record.get("state", "")
        if job_map and succeeded + failed >= len(job_map) and (
                succeeded + failed) > 0:
            state = (DiscoveryState.succeeded.value if succeeded
                     else DiscoveryState.failed.value)
            self._hset(job_id, {"state": state, "pages": pages,
                                "items": items, "sites_succeeded": succeeded,
                                "sites_failed": failed,
                                "per_site": json.dumps(per_site)})
        return {
            "job_id": job_id, "topic": record.get("topic", ""),
            "state": state, "sites_total": len(job_map),
            "sites_succeeded": succeeded, "sites_failed": failed,
            "pages": pages, "items": items, "per_site": per_site,
            "data_sha256": _combine_digests(digests),
            "candidates": self.candidates(job_id, only_selected=True),
        }

    # -- misc ----------------------------------------------------------------- #
    def cancel(self, job_id: str) -> dict:
        self._hset(job_id, {"state": DiscoveryState.cancelled.value})
        return self.get(job_id) or {}

    def list(self, tenant: str, *, page: int = 1,
             page_size: int = 20) -> tuple[list[dict], int]:
        if self.r is None:
            return [], 0
        zkey = TENANT_DISCOVERIES.format(tenant=tenant)
        total = int(self.r.zcard(zkey) or 0)
        ids = self.r.zrevrange(zkey, (page - 1) * page_size,
                               page * page_size - 1)
        out = []
        for jid in ids:
            jid_s = jid.decode() if isinstance(jid, bytes) else jid
            rec = self.get(jid_s)
            if rec:
                out.append(rec)
        return out, total

    def _emit(self, tenant: str, event: str, data: dict) -> None:
        if not tenant:
            return
        try:
            from src.gateway.webhooks.service import WebhookService

            WebhookService(self.r).emit(tenant, event, data)
        except Exception as exc:  # noqa: BLE001
            logger.debug("webhook emit failed (%s): %s", event, exc)


def _combine_digests(digests: Sequence[str]) -> str:
    """Stable digest over a multi-site result set (empty when unknown)."""
    if not digests:
        return ""
    import hashlib

    h = hashlib.sha256()
    for d in sorted(digests):
        h.update(d.encode())
        h.update(b"\x00")
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Module-level entry points (Celery + CLI call these)
# --------------------------------------------------------------------------- #
def preview_discovery(*, topic: str, count: int = 10, seeds: Sequence[str] = (),
                      language: str = "en", per_domain: int = 1,
                      fetcher=None, providers=None, llm=None) -> dict:
    """Ranked shortlist for a topic — no job record, no crawl.

    Backs ``POST /v1/discovery/preview``: the caller sees exactly what would be
    crawled before committing to it.
    """
    from src.config import get_config

    cfg = get_config()
    plan = plan_queries(topic, max_queries=cfg.DISCOVERY_MAX_QUERIES, llm=llm)
    if providers is None:
        providers = build_providers(seeds=seeds, fetcher=fetcher, llm=llm)
    hits = collect_hits(providers, plan.queries,
                        limit=max(5, min(20, count * 2)))

    from src.discovery.ranker import Ranker

    ranker = Ranker(topic, language=language, blocklist=cfg.DISCOVERY_BLOCKLIST)
    scored = ranker.score(ranker.build(hits, seed_domains=seeds))
    scored = scored[:cfg.DISCOVERY_MAX_CANDIDATES]
    shortlist = {c.url for c in ranker.select(scored, count, per_domain=per_domain)}
    for cand in scored:
        cand.selected = cand.url in shortlist

    return {
        "topic": topic,
        "queries": plan.queries,
        "providers": sorted({p for c in scored for p in c.providers}),
        "candidates": [c.to_dict() for c in scored if c.selected],
        "alternates": [c.to_dict() for c in scored if not c.selected and not c.rejected],
        "total_found": len(scored),
    }


def run_discovery(payload: dict, redis_client=None, fetcher=None,
                  providers=None, llm=None, scheduler=None) -> dict:
    """Create/update a discovery job and run the search + ranking stage.

    Idempotent w.r.t. ``payload["job_id"]``: if the record already exists it is
    reused, so a Celery retry never duplicates work.
    """
    req = DiscoveryRequest(
        topic=payload.get("topic", ""),
        count=int(payload.get("count", 10) or 10),
        seeds=list(payload.get("seeds") or []),
        language=payload.get("language", "en"),
        auto=bool(payload.get("auto", False)),
        max_pages_per_site=int(payload.get("max_pages_per_site", 5) or 5),
        per_domain=int(payload.get("per_domain", 1) or 1),
        obey_robots=bool(payload.get("obey_robots", True)),
        stealth=bool(payload.get("stealth", False)),
        tenant=payload.get("tenant", ""),
        job_id=payload.get("job_id", ""),
    )
    svc = DiscoveryService(redis_client, fetcher=fetcher, providers=providers,
                           llm=llm, scheduler=scheduler)
    if svc.get(req.job_id) is None:
        svc.create(req)
    record = svc.discover(req.job_id)
    record["candidates"] = svc.candidates(req.job_id)
    return record


def run_discovery_and_crawl(payload: dict, redis_client=None, fetcher=None,
                            crawl_fetcher=None, providers=None, llm=None,
                            scheduler=None) -> dict:
    """Discover, then crawl every shortlisted site **in this process**.

    This is what the CLI uses (and what the Celery task wraps) when the caller
    wants the data now rather than a job id to poll.
    """
    record = run_discovery(payload, redis_client=redis_client,
                           fetcher=fetcher, providers=providers, llm=llm,
                           scheduler=scheduler)
    job_id = record["job_id"]
    svc = DiscoveryService(redis_client, fetcher=fetcher, providers=providers,
                           llm=llm, scheduler=scheduler)

    if record.get("state") == DiscoveryState.failed.value:
        return {**record, "items": [], "per_site": []}

    # dispatch=False: we crawl these sites right here, so no broker message —
    # otherwise every site would be crawled twice (once in-process, once by a
    # worker picking up the queued crawl_job).
    # dispatch=False: we crawl these sites right here, so no broker message —
    # otherwise every site would be crawled twice (once in-process, once by a
    # worker picking up the queued crawl_job).
    selected, _ = svc.approve(job_id, dispatch=False)
    if not selected:
        record["error"] = record.get("error") or (
            "no websites found for this topic")
        record.update({"state": DiscoveryState.failed.value, "pages": 0,
                       "items": 0, "sites_total": 0, "sites_succeeded": 0,
                       "sites_failed": 0, "per_site": [], "crawled_items": []})
        return record

    url_by_domain = {c["domain"]: c["url"] for c in selected}
    # approve() just persisted the shortlist + job ids; pull them back so the
    # returned report and the persisted record agree.
    record.update(svc.get(job_id) or {})
    job_map = json.loads(record.get("crawl_jobs") or "{}")
    record["auto"] = True
    record["crawl_job_ids"] = list(job_map.values())

    per_site: list[dict] = []
    all_items: list[dict] = []
    pages = 0
    succeeded = failed = 0

    from workers.crawl_tasks import run_crawl

    for domain, crawl_id in job_map.items():
        url = url_by_domain.get(domain, "")
        entry: dict
        try:
            result = run_crawl(
                {"url": url, "tenant": record.get("tenant", ""),
                 "job_id": crawl_id, "discovery_id": job_id,
                 "max_pages": int(payload.get("max_pages_per_site", 5) or 5),
                 "obey_robots": bool(payload.get("obey_robots", True)),
                 "stealth": bool(payload.get("stealth", False))},
                redis_client=redis_client, fetcher=crawl_fetcher or fetcher)
            state = ("succeeded" if result.get("items")
                     or not result.get("failures") else "failed")
            entry = {"domain": domain, "url": url, "job_id": crawl_id,
                     "state": state, "pages": result.get("pages_done", 0),
                     "items": result.get("items", 0),
                     "data_sha256": result.get("data_sha256", ""),
                     # Only surface failures when the site actually failed —
                     # a few 404s on an otherwise good site are not an error.
                     "error": ("; ".join(result.get("failures", []))[:500]
                               if state == "failed" else "")}
            all_items.extend(result.get("collected", []))
            pages += entry["pages"]
            if state == "succeeded":
                succeeded += 1
            else:
                failed += 1
        except Exception as exc:  # noqa: BLE001 - one bad site must not abort
            logger.warning("crawl failed for %s: %s", domain, exc)
            failed += 1
            entry = {"domain": domain, "url": url, "job_id": crawl_id,
                     "state": "failed", "pages": 0, "items": 0,
                     "data_sha256": "", "error": str(exc)[:500]}
        per_site.append(entry)

    state = (DiscoveryState.succeeded.value if succeeded
             else DiscoveryState.failed.value)
    svc._hset(job_id, {
        "state": state, "pages": pages, "items": len(all_items),
        "sites_total": len(job_map), "sites_succeeded": succeeded,
        "sites_failed": failed, "per_site": json.dumps(per_site),
    })
    record.update({
        "state": state, "pages": pages, "items": len(all_items),
        "sites_total": len(job_map), "sites_succeeded": succeeded,
        "sites_failed": failed, "per_site": per_site,
        "crawled_items": all_items,
        "candidates": svc.candidates(job_id, only_selected=True),
    })
    return record


# Imported late so the module can be used without pulling the whole stack.
from src.discovery.planner import plan_queries
from src.discovery.providers import build_providers, collect_hits
