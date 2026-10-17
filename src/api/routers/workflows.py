"""Tenant-scoped sequential crawl recipes and asynchronous executions.

This is intentionally a small, typed workflow runner: a workflow is an ordered
list of permitted HTTP(S) crawl steps. It is not a general DAG/script engine.
"""
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

router = APIRouter(prefix="/workflows", tags=["workflows"])
WORKFLOW_KEY = "wcp:v1:workflow:{workflow_id}"
RUN_KEY = "wcp:v1:workflow_run:{run_id}"
TENANT_WORKFLOWS = "wcp:v1:workflows:{tenant}"
TENANT_RUNS = "wcp:v1:workflow_runs:{tenant}"
TTL = 30 * 24 * 60 * 60


class CrawlStep(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    max_pages: int = Field(default=3, ge=1, le=25)
    obey_robots: bool = True

    @field_validator("url")
    @classmethod
    def valid_http_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("workflow crawl URLs must be absolute HTTP(S) URLs")
        return value


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    steps: list[CrawlStep] = Field(min_length=1, max_length=10)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.split())

    @property
    def max_pages(self) -> int:
        return sum(step.max_pages for step in self.steps)


def _decode(raw) -> dict:
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


def _workflow(request: Request, workflow_id: str, tenant: str) -> dict:
    if not re.fullmatch(r"workflow_[a-f0-9]{20}", workflow_id):
        raise HTTPException(404, "workflow not found")
    raw = request.app.state.services.redis.get(WORKFLOW_KEY.format(workflow_id=workflow_id))
    if not raw:
        raise HTTPException(404, "workflow not found")
    record = _decode(raw)
    if record.get("tenant") != tenant:
        raise HTTPException(404, "workflow not found")
    return record


@router.post("", status_code=201, summary="Create a sequential crawl workflow")
async def create_workflow(body: WorkflowCreate, request: Request, ctx: CtxDep) -> dict:
    tenant = str(ctx["tenant"])
    workflow_id = "workflow_" + uuid.uuid4().hex[:20]
    now = datetime.now(timezone.utc).isoformat()
    record = {"workflow_id": workflow_id, "tenant": tenant, "name": body.name,
              "steps": [step.model_dump() for step in body.steps],
              "max_pages": body.max_pages, "created_at": now}
    redis = request.app.state.services.redis
    pipe = redis.pipeline()
    pipe.set(WORKFLOW_KEY.format(workflow_id=workflow_id), json.dumps(record), ex=TTL)
    pipe.zadd(TENANT_WORKFLOWS.format(tenant=tenant), {workflow_id: time.time()})
    pipe.execute()
    return record


@router.get("", summary="List this tenant's workflows")
async def list_workflows(request: Request, ctx: CtxDep,
                         page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    key = TENANT_WORKFLOWS.format(tenant=tenant)
    ids = redis.zrevrange(key, (page - 1) * page_size, page * page_size - 1)
    items = []
    for raw_id in ids:
        wid = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        try:
            items.append(_workflow(request, wid, tenant))
        except HTTPException:
            continue
    return {"items": items, "total": int(redis.zcard(key) or 0), "page": page, "page_size": page_size}


@router.get("/runs", summary="List recent workflow runs for this tenant")
async def list_workflow_runs(request: Request, ctx: CtxDep,
                             page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    key = TENANT_RUNS.format(tenant=tenant)
    ids = redis.zrevrange(key, (page - 1) * page_size, page * page_size - 1)
    items = []
    for raw_id in ids:
        run_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        raw = redis.get(RUN_KEY.format(run_id=run_id))
        if not raw:
            continue
        record = _decode(raw)
        if record.get("tenant") == tenant:
            items.append(record)
    return {"items": items, "total": int(redis.zcard(key) or 0),
            "page": page, "page_size": page_size}


@router.get("/{workflow_id}", summary="Get a workflow definition")
async def get_workflow(workflow_id: str, request: Request, ctx: CtxDep) -> dict:
    return _workflow(request, workflow_id, str(ctx["tenant"]))


@router.post("/{workflow_id}/runs", status_code=202, summary="Run a workflow")
async def start_workflow(workflow_id: str, request: Request, ctx: CtxDep) -> dict:
    tenant = str(ctx["tenant"])
    definition = _workflow(request, workflow_id, tenant)
    run_id = "wfrun_" + uuid.uuid4().hex[:20]
    now = datetime.now(timezone.utc).isoformat()
    record = {"run_id": run_id, "workflow_id": workflow_id, "tenant": tenant,
              "name": definition["name"], "status": "queued", "step_index": 0,
              "steps": [{"url": s["url"], "status": "queued", "pages_done": 0, "error": None}
                        for s in definition["steps"]],
              "created_at": now, "updated_at": now, "error": None}
    redis = request.app.state.services.redis
    redis.set(RUN_KEY.format(run_id=run_id), json.dumps(record), ex=TTL)
    redis.zadd(TENANT_RUNS.format(tenant=tenant), {run_id: time.time()})
    payload = {"run_id": run_id, "workflow_id": workflow_id, "tenant": tenant,
               "steps": definition["steps"]}
    spec = JobSpec(kind="workflow", tenant=tenant, job_id=run_id,
                   priority=JobPriority.NORMAL,
                   payload={**payload, "estimated_pages": definition["max_pages"]})
    try:
        request.app.state.services.scheduler.submit(spec)
    except Exception as exc:
        record.update(status="failed", error="workflow backend unavailable",
                      updated_at=datetime.now(timezone.utc).isoformat())
        redis.set(RUN_KEY.format(run_id=run_id), json.dumps(record), ex=TTL)
        from src.compute.scheduler import QuotaExceeded
        if isinstance(exc, QuotaExceeded):
            raise HTTPException(402, str(exc)) from exc
        raise HTTPException(503, "workflow backend unavailable") from exc
    return {"run_id": run_id, "workflow_id": workflow_id, "status": "queued",
            "status_url": f"/api/v1/workflows/runs/{run_id}"}


@router.get("/runs/{run_id}", summary="Get workflow run status")
async def get_workflow_run(run_id: str, request: Request, ctx: CtxDep) -> dict:
    if not re.fullmatch(r"wfrun_[a-f0-9]{20}", run_id):
        raise HTTPException(404, "workflow run not found")
    raw = request.app.state.services.redis.get(RUN_KEY.format(run_id=run_id))
    if not raw:
        raise HTTPException(404, "workflow run not found")
    record = _decode(raw)
    if record.get("tenant") != str(ctx["tenant"]):
        raise HTTPException(404, "workflow run not found")
    return record
