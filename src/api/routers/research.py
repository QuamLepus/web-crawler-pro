"""Asynchronous topic-to-cited-report research jobs."""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from src.api.routers.jobs import CtxDep
from src.compute.scheduler import JobPriority, JobSpec

router = APIRouter(prefix="/research", tags=["research"])
JOB = "wcp:v1:research:{research_id}"
TENANT_JOBS = "wcp:v1:research:tenant:{tenant}"
TTL = 60 * 60 * 24 * 30


class ResearchCreate(BaseModel):
    topic: str = Field(min_length=3, max_length=512)
    sources: int = Field(default=5, ge=1, le=20)
    max_pages_per_source: int = Field(default=3, ge=1, le=10)
    seeds: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("topic")
    @classmethod
    def normalize_topic(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("topic must contain at least three non-whitespace characters")
        return value

    @field_validator("seeds")
    @classmethod
    def validate_seeds(cls, values: list[str]) -> list[str]:
        for value in values:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("seeds must be absolute HTTP(S) URLs")
        return values


@router.post("", status_code=202, summary="Start a source-cited web research job")
async def create_research(body: ResearchCreate, ctx: CtxDep, request: Request) -> dict:
    tenant = str(ctx["tenant"])
    research_id = "research_" + uuid.uuid4().hex[:20]
    now = datetime.now(timezone.utc).isoformat()
    record = {"research_id": research_id, "tenant": tenant,
              "topic": body.topic.strip(), "sources": body.sources,
              "max_pages_per_source": body.max_pages_per_source,
              "status": "queued", "stage": "queued", "progress": 0,
              "stages": {"planning": "queued", "discovery": "queued",
                         "crawl_extract": "queued", "source_check": "queued",
                         "report": "queued"},
              "created_at": now, "updated_at": now, "error": None,
              "report": None}
    redis = request.app.state.services.redis
    key = JOB.format(research_id=research_id)
    pipe = redis.pipeline()
    pipe.set(key, json.dumps(record, separators=(",", ":")), ex=TTL)
    pipe.zadd(TENANT_JOBS.format(tenant=tenant), {research_id: time.time()})
    pipe.execute()
    spec = JobSpec(kind="research", tenant=tenant, job_id=research_id,
                   priority=JobPriority.NORMAL,
                   payload={"research_id": research_id, "tenant": tenant,
                            "topic": record["topic"], "count": body.sources,
                            "max_pages_per_site": body.max_pages_per_source,
                            "estimated_pages": body.sources * body.max_pages_per_source,
                            "seeds": body.seeds, "obey_robots": True})
    try:
        request.app.state.services.scheduler.submit(spec)
    except Exception as exc:
        record.update(status="failed", stage="dispatch", error="research backend unavailable")
        redis.set(key, json.dumps(record, separators=(",", ":")), ex=TTL)
        from src.compute.scheduler import QuotaExceeded
        if isinstance(exc, QuotaExceeded):
            raise HTTPException(402, str(exc)) from exc
        raise HTTPException(503, "research backend unavailable") from exc
    return {"research_id": research_id, "status": "queued", "stage": "queued",
            "status_url": f"/api/v1/research/{research_id}"}


def _load(request: Request, research_id: str, tenant: str) -> dict:
    if not re.fullmatch(r"research_[a-f0-9]{20}", research_id):
        raise HTTPException(404, "research job not found")
    raw = request.app.state.services.redis.get(JOB.format(research_id=research_id))
    if not raw:
        raise HTTPException(404, "research job not found")
    value = raw.decode() if isinstance(raw, bytes) else raw
    record = json.loads(value)
    if record.get("tenant") != tenant:
        raise HTTPException(404, "research job not found")
    return record


@router.get("/{research_id}", summary="Get research job status and cited report")
async def get_research(research_id: str, ctx: CtxDep, request: Request) -> dict:
    return _load(request, research_id, str(ctx["tenant"]))


@router.get("", summary="List research jobs for this tenant")
async def list_research(ctx: CtxDep, request: Request,
                        page: int = Query(1, ge=1),
                        page_size: int = Query(20, ge=1, le=100)) -> dict:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    zkey = TENANT_JOBS.format(tenant=tenant)
    total = int(redis.zcard(zkey) or 0)
    ids = redis.zrevrange(zkey, (page - 1) * page_size, page * page_size - 1)
    out = []
    for raw_id in ids:
        rid = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        try:
            rec = _load(request, rid, tenant)
            rec.pop("report", None)
            out.append(rec)
        except HTTPException:
            continue
    return {"items": out, "total": total, "page": page, "page_size": page_size}
