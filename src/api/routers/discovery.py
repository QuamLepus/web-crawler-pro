"""v1 router: topic-driven website discovery.

The one flow the platform used to be missing: instead of handing the crawler a
list of URLs, hand it a **topic** and a **number of websites**. The service
searches the web, ranks and de-duplicates what it finds into a shortlist of
distinct domains, and (after optional human approval) crawls each one through
the existing ``crawl_job`` pipeline.

    POST   /v1/discovery                     create a discovery job
    POST   /v1/discovery/preview             synchronous dry-run, no crawl
    GET    /v1/discovery                     list your discovery jobs
    GET    /v1/discovery/{job_id}            status
    GET    /v1/discovery/{job_id}/candidates the ranked shortlist
    DELETE /v1/discovery/{job_id}/candidates/{domain}   drop one website
    POST   /v1/discovery/{job_id}/approve    lock the list, fan out crawls
    GET    /v1/discovery/{job_id}/results    aggregated per-site report
    POST   /v1/discovery/{job_id}/cancel     cancel

Quota: a discovery job is charged ``count`` requests up front (see
``SchedulerService._QUOTA_GATES``), so a large shortlist cannot be used to
bypass the crawl budget.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request, status

from src.api.routers.jobs import CtxDep, get_redis
from src.api.schemas import (
    CandidateOut,
    DiscoveryApprove,
    DiscoveryCreate,
    DiscoveryListOut,
    DiscoveryOut,
    DiscoveryPreviewOut,
    DiscoveryResultsOut,
    DiscoveryState,
    SiteResultOut,
)
from src.discovery.service import (
    DiscoveryRequest,
    DiscoveryService,
    preview_discovery,
)
from src.gateway.billing.quotas import QuotaExceededError

logger = logging.getLogger("wcp.api.discovery")
router = APIRouter(prefix="/discovery", tags=["discovery"])


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _to_out(record: dict) -> DiscoveryOut:
    return DiscoveryOut(
        job_id=record.get("job_id", ""),
        tenant=record.get("tenant", ""),
        topic=record.get("topic", ""),
        count=_as_int(record.get("count"), 10),
        state=DiscoveryState(record.get("state", "queued")),
        language=record.get("language", "en"),
        auto=_as_bool(record.get("auto")),
        max_pages_per_site=_as_int(record.get("max_pages_per_site"), 5),
        per_domain=_as_int(record.get("per_domain"), 1),
        seeds=_as_list(record.get("seeds")),
        queries=_as_list(record.get("queries")),
        providers=_as_list(record.get("providers")),
        candidates=_as_int(record.get("candidates")),
        selected=_as_int(record.get("selected")),
        pages=_as_int(record.get("pages")),
        items=_as_int(record.get("items")),
        sites_total=_as_int(record.get("sites_total")),
        sites_succeeded=_as_int(record.get("sites_succeeded")),
        sites_failed=_as_int(record.get("sites_failed")),
        created_at=record.get("created_at"),
        updated_at=record.get("updated_at"),
        error=record.get("error") or None,
    )


def _load(request: Request, job_id: str) -> tuple[DiscoveryService, dict]:
    r = get_redis(request)
    svc = DiscoveryService(r)
    record = svc.get(job_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "discovery job not found")
    if record.get("tenant") != request.state.ctx.get("tenant"):
        # tenant isolation: never leak the existence of another tenant's job
        raise HTTPException(status.HTTP_404_NOT_FOUND, "discovery job not found")
    return svc, record


# --------------------------------------------------------------------------- #
# create / list
# --------------------------------------------------------------------------- #
@router.post("", response_model=DiscoveryOut, status_code=status.HTTP_202_ACCEPTED,
             summary="Find and (optionally) crawl the best N websites on a topic")
async def create_discovery(body: DiscoveryCreate, request: Request, ctx: CtxDep,
                           ) -> DiscoveryOut:
    svc = request.app.state.services
    tenant = ctx["tenant"]
    r = get_redis(request)

    req = DiscoveryRequest(
        topic=body.topic, count=body.count, seeds=list(body.seeds),
        language=body.language, auto=body.auto,
        max_pages_per_site=body.max_pages_per_site,
        per_domain=body.per_domain, obey_robots=body.obey_robots,
        stealth=body.stealth, tenant=tenant,
    )

    # quota gate: a discovery job costs `count` requests
    try:
        svc.quotas.consume(tenant, "requests", body.count)
    except QuotaExceededError as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc))
    except Exception as exc:  # noqa: BLE001 - quota service optional
        logger.debug("quota pre-check skipped (%s)", exc)

    record = DiscoveryService(r).create(req)

    from src.compute.scheduler import JobSpec

    kind = "discover_and_crawl" if body.auto else "discover"
    payload = {**req.to_dict(), "tenant": tenant}
    try:
        svc.scheduler.submit(JobSpec(kind=kind, payload=payload, tenant=tenant,
                                     job_id=req.job_id))
    except QuotaExceededError as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc))
    except Exception as exc:
        logger.exception("discovery dispatch failed")
        DiscoveryService(r)._hset(req.job_id,
                                  {"state": DiscoveryState.failed.value,
                                   "error": str(exc)[:500]})
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"discovery backend unavailable: {exc}")

    if body.webhook_url:
        try:
            svc.webhooks.subscribe(tenant, body.webhook_url,
                                   ["discovery.completed", "discovery.approved",
                                    "crawl.completed", "crawl.failed"])
        except ValueError:
            pass  # invalid URL — the job still runs

    return _to_out(record)


@router.get("", response_model=DiscoveryListOut, summary="List discovery jobs")
async def list_discoveries(ctx: CtxDep, request: Request,
                           page: int = Query(1, ge=1),
                           page_size: int = Query(20, ge=1, le=100),
                           ) -> DiscoveryListOut:
    svc = DiscoveryService(get_redis(request))
    records, total = svc.list(ctx["tenant"], page=page, page_size=page_size)
    return DiscoveryListOut(items=[_to_out(r) for r in records], total=total,
                            page=page, page_size=page_size)


# --------------------------------------------------------------------------- #
# synchronous preview (must stay above /{job_id})
# --------------------------------------------------------------------------- #
@router.post("/preview", response_model=DiscoveryPreviewOut,
             summary="Dry-run: show what would be crawled, without crawling")
async def preview(body: DiscoveryCreate, ctx: CtxDep) -> DiscoveryPreviewOut:
    """Rank now, crawl later.

    Discovery needs outbound network access from the API process; when it is
    unavailable (offline/CI) every provider degrades to an empty result and the
    endpoint still answers — with ``total_found: 0`` rather than a 5xx.
    """
    try:
        result = preview_discovery(
            topic=body.topic, count=body.count, seeds=list(body.seeds),
            language=body.language, per_domain=body.per_domain)
    except Exception as exc:
        logger.exception("discovery preview failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"discovery providers unavailable: {exc}")
    return DiscoveryPreviewOut(
        topic=result["topic"], queries=result["queries"],
        providers=result["providers"],
        candidates=[CandidateOut(**c) for c in result["candidates"]],
        total_found=result["total_found"])


# --------------------------------------------------------------------------- #
# per-job operations
# --------------------------------------------------------------------------- #
@router.get("/{job_id}", response_model=DiscoveryOut,
            summary="Get discovery job status")
async def get_discovery(job_id: str, request: Request,
                        ctx: CtxDep) -> DiscoveryOut:
    _svc, record = _load(request, job_id)
    return _to_out(record)


@router.get("/{job_id}/candidates", response_model=list[CandidateOut],
            summary="Ranked candidate websites")
async def get_candidates(job_id: str, request: Request, ctx: CtxDep,
                         only_selected: bool = Query(
                             False, description="Only the shortlisted sites"),
                         ) -> list[CandidateOut]:
    svc, _record = _load(request, job_id)
    return [CandidateOut(**c)
            for c in svc.candidates(job_id, only_selected=only_selected)]


@router.delete("/{job_id}/candidates/{domain}", response_model=list[CandidateOut],
               summary="Drop one website from the shortlist")
async def drop_candidate(job_id: str, domain: str, request: Request,
                         ctx: CtxDep) -> list[CandidateOut]:
    svc, record = _load(request, job_id)
    if record.get("state") in (DiscoveryState.crawling.value,
                               DiscoveryState.succeeded.value):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "discovery already approved; cannot edit the list")
    return [CandidateOut(**c) for c in svc.drop(job_id, domain)]


@router.post("/{job_id}/approve", response_model=DiscoveryOut,
             summary="Approve the shortlist and crawl every website")
async def approve_discovery(job_id: str, request: Request, ctx: CtxDep,
                            body: DiscoveryApprove | None = None,
                            ) -> DiscoveryOut:
    svc, record = _load(request, job_id)
    if record.get("state") in (DiscoveryState.crawling.value,
                               DiscoveryState.succeeded.value):
        raise HTTPException(status.HTTP_409_CONFLICT, "already approved")
    if record.get("state") == DiscoveryState.cancelled.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "job was cancelled")
    if record.get("state") == DiscoveryState.queued.value:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "discovery has not produced candidates yet")

    domains = body.domains if body is not None else None
    try:
        selected, crawl_ids = svc.approve(job_id, domains=domains)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "discovery job not found")
    if not selected:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "no candidates left to crawl")
    logger.info("discovery %s approved: %d sites, %d crawl jobs", job_id,
                len(selected), len(crawl_ids))
    return _to_out(svc.get(job_id) or record)


@router.get("/{job_id}/results", response_model=DiscoveryResultsOut,
            summary="Aggregated crawl report across every website")
async def get_results(job_id: str, request: Request,
                      ctx: CtxDep) -> DiscoveryResultsOut:
    svc, _record = _load(request, job_id)
    try:
        report = svc.results(job_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "discovery job not found")
    return DiscoveryResultsOut(
        job_id=report["job_id"], topic=report["topic"],
        state=report["state"], sites_total=report["sites_total"],
        sites_succeeded=report["sites_succeeded"],
        sites_failed=report["sites_failed"], pages=report["pages"],
        items=report["items"],
        per_site=[SiteResultOut(**{k: s[k] for k in
                                   ("domain", "state", "pages", "items",
                                    "data_sha256", "error")})
                  for s in report["per_site"]],
        data_sha256=report["data_sha256"],
        candidates=[CandidateOut(**c) for c in report["candidates"]],
    )


@router.post("/{job_id}/cancel", response_model=DiscoveryOut,
             summary="Cancel a discovery job")
async def cancel_discovery(job_id: str, request: Request,
                           ctx: CtxDep) -> DiscoveryOut:
    svc, record = _load(request, job_id)
    if record.get("state") in (DiscoveryState.succeeded.value,
                               DiscoveryState.cancelled.value):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"job already {record.get('state')}")
    return _to_out(svc.cancel(job_id))
