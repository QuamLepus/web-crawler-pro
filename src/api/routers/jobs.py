"""v1 router: crawl jobs (submit, status, list, cancel).

Thin façade over :class:`src.compute.scheduler.SchedulerService` with
tenant-scoped job records in Redis so the public v1 contract is stable even
if the internal engine changes.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

import redis as redis_lib
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from src.api.schemas import (CrawlJobCreate, CrawlJobOut, JobListOut, JobState)
from src.compute.scheduler import JobPriority, JobSpec
from src.gateway.billing.quotas import QuotaExceededError

logger = logging.getLogger("wcp.api.jobs")
router = APIRouter(prefix="/jobs", tags=["jobs"])

_PRIORITY_MAP = {"low": JobPriority.LOW, "normal": JobPriority.NORMAL,
                 "high": JobPriority.HIGH}

JOB_KEY = "wcp:v1:job:{job_id}"
TENANT_JOBS = "wcp:v1:tenant_jobs:{tenant}"


def get_redis(request: Request) -> redis_lib.Redis:
    return request.app.state.services.redis


def _now() -> datetime:
    return datetime.now(timezone.utc)


def authenticate(request: Request) -> dict:
    """Resolve tenant from API key / bearer token set by gateway auth."""
    ctx = getattr(request.state, "ctx", None)
    if ctx:
        return ctx
    svc = request.app.state.services
    api_key = request.headers.get("x-api-key", "")
    if api_key:
        record = svc.api_keys.lookup(api_key)
        if record is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")
        request.state.tenant = record.tenant
        return {"tenant": record.tenant, "scopes": record.scopes,
                "role": "member", "user": None}
    raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                        "authentication required",
                        headers={"WWW-Authenticate": "Bearer"})


CtxDep = Annotated[dict, Depends(authenticate)]


@router.post("/crawl", response_model=CrawlJobOut, status_code=status.HTTP_202_ACCEPTED,
             summary="Submit a crawl job")
async def submit_crawl(body: CrawlJobCreate, request: Request, ctx: CtxDep,
                       r: Annotated[redis_lib.Redis, Depends(get_redis)]) -> CrawlJobOut:
    svc = request.app.state.services
    tenant = ctx["tenant"]

    # plan quota gate (raises QuotaExceededError -> 402)
    try:
        svc.quotas.consume(tenant, "requests", body.max_pages)
    except QuotaExceededError as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc))
    except Exception as exc:
        logger.warning("quota pre-check skipped (%s)", exc)

    job_id = f"job_{uuid.uuid4().hex[:20]}"
    now = _now()
    record = {
        "job_id": job_id, "tenant": tenant, "state": JobState.queued.value,
        "url": body.url, "max_pages": body.max_pages, "pages_done": 0,
        "robots_blocked": 0, "items_collected": 0, "tokens_generated": 0,
        "created_at": now.isoformat(), "updated_at": now.isoformat(),
        "error": "",
        "options": body.model_dump(mode="json"),
    }
    pipe = r.pipeline()
    pipe.hset(JOB_KEY.format(job_id=job_id), mapping={
        k: (str(v) if not isinstance(v, (dict, list)) else str(v))
        for k, v in record.items()})
    pipe.expire(JOB_KEY.format(job_id=job_id), 60 * 60 * 24 * 30)
    pipe.zadd(TENANT_JOBS.format(tenant=tenant), {job_id: time.time()})
    pipe.execute()

    # dispatch to the distributed engine (Celery when wired, Redis queue otherwise)
    spec = JobSpec(
        kind="crawl_job",
        payload={"url": body.url, "max_pages": body.max_pages,
                 "obey_robots": body.obey_robots, "stealth": body.stealth,
                 "tenant": tenant, "job_id": job_id,
                 "options": record["options"]},
        tenant=tenant,
        priority=_PRIORITY_MAP.get(body.priority, JobPriority.NORMAL),
        job_id=job_id,
    )
    try:
        svc.scheduler.submit(spec)
    except Exception as exc:
        logger.exception("scheduler dispatch failed")
        r.hset(JOB_KEY.format(job_id=job_id),
               mapping={"state": JobState.failed.value, "error": str(exc),
                        "updated_at": _now().isoformat()})
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"crawl backend unavailable: {exc}")

    if body.webhook_url:
        try:
            svc.webhooks.subscribe(tenant, body.webhook_url,
                                   ["crawl.completed", "crawl.failed", "data.ready"])
        except ValueError:
            pass  # invalid URL — job still runs; client can fix subscription

    return CrawlJobOut(**{**record, "state": JobState(record["state"]),
                          "created_at": now, "updated_at": now})


def _to_out(record: dict) -> CrawlJobOut:
    return CrawlJobOut(
        job_id=record["job_id"], tenant=record["tenant"],
        state=JobState(record.get("state", "queued")),
        url=record["url"], max_pages=int(record.get("max_pages", 0)),
        pages_done=int(record.get("pages_done", 0)),
        robots_blocked=int(record.get("robots_blocked", 0)),
        items_collected=int(record.get("items_collected", 0)),
        tokens_generated=int(record.get("tokens_generated", 0)),
        created_at=datetime.fromisoformat(record["created_at"]),
        updated_at=datetime.fromisoformat(record["updated_at"]),
        error=record.get("error") or None,
    )


@router.get("/{job_id}", response_model=CrawlJobOut, summary="Get job status")
async def get_job(job_id: str, request: Request, ctx: CtxDep,
                  r: Annotated[redis_lib.Redis, Depends(get_redis)]) -> CrawlJobOut:
    key = JOB_KEY.format(job_id=job_id)
    raw = r.hgetall(key)
    if not raw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    record = {(k.decode() if isinstance(k, bytes) else k):
              (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}
    if record.get("tenant") != ctx["tenant"] and ctx.get("role") != "admin":
        # tenant isolation: hide existence of other tenants' jobs
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    # live-refresh from the scheduler's job store when still running
    if record.get("state") in ("queued", "running"):
        try:
            res = request.app.state.services.scheduler.status(job_id)
            if res:
                st = res.get("status")
                if st == "completed":
                    record["state"] = JobState.completed.value
                elif st == "failed":
                    record["state"] = JobState.failed.value
                    record["error"] = str(res.get("error", ""))[:500]
                elif st == "running":
                    record["state"] = JobState.running.value
                    record["pages_done"] = str(res.get("pages_done", record.get("pages_done", 0)))
                    record["items_collected"] = str(res.get("items", record.get("items_collected", 0)))
                r.hset(key, mapping={"state": record["state"],
                                     "updated_at": _now().isoformat()})
        except Exception:  # inspection is best-effort
            logger.debug("job inspect failed for %s", job_id, exc_info=True)
    return _to_out(record)


@router.get("", response_model=JobListOut, summary="List jobs (newest first)")
async def list_jobs(ctx: CtxDep, request: Request,
                    r: Annotated[redis_lib.Redis, Depends(get_redis)],
                    page: int = Query(1, ge=1),
                    page_size: int = Query(20, ge=1, le=100),
                    state: JobState | None = None) -> JobListOut:
    zkey = TENANT_JOBS.format(tenant=ctx["tenant"])
    total = r.zcard(zkey)
    ids = r.zrevrange(zkey, (page - 1) * page_size, page * page_size - 1)
    items: list[CrawlJobOut] = []
    for jid in ids:
        jid_s = jid.decode() if isinstance(jid, bytes) else jid
        raw = r.hgetall(JOB_KEY.format(job_id=jid_s))
        if not raw:
            continue
        record = {(k.decode() if isinstance(k, bytes) else k):
                  (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}
        out = _to_out(record)
        if state and out.state != state:
            continue
        items.append(out)
    return JobListOut(items=items, total=int(total), page=page, page_size=page_size)


@router.post("/{job_id}/cancel", response_model=CrawlJobOut, summary="Cancel a job")
async def cancel_job(job_id: str, request: Request, ctx: CtxDep,
                     r: Annotated[redis_lib.Redis, Depends(get_redis)]) -> CrawlJobOut:
    key = JOB_KEY.format(job_id=job_id)
    raw = r.hgetall(key)
    if not raw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    record = {(k.decode() if isinstance(k, bytes) else k):
              (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}
    if record.get("tenant") != ctx["tenant"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if record.get("state") in (JobState.completed.value, JobState.failed.value,
                                 JobState.cancelled.value):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"job already {record.get('state')}")
    try:
        request.app.state.services.scheduler.mark(job_id, "cancelled",
                                                  cancelled_by=ctx["tenant"])
    except Exception as exc:  # pragma: no cover
        logger.warning("engine mark failed (%s); marking cancelled anyway", exc)
    r.hset(key, mapping={"state": JobState.cancelled.value,
                         "updated_at": _now().isoformat()})
    record.update(state=JobState.cancelled.value, updated_at=_now().isoformat())
    return _to_out(record)
