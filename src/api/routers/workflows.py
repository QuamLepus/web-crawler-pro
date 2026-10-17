"""Tenant-scoped bounded crawl recipes and asynchronous executions.

Workflows contain up to ten permitted HTTP(S) steps. Legacy definitions execute
sequentially; explicit dependencies enable bounded DAG branching, still executed
serially by one worker. This is not a general DAG/script engine.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from src.api.routers.jobs import CtxDep
from src.compute.scheduler import JobPriority, JobSpec

router = APIRouter(prefix="/workflows", tags=["workflows"])
WORKFLOW_KEY = "wcp:v1:workflow:{workflow_id}"
RUN_KEY = "wcp:v1:workflow_run:{run_id}"
TENANT_WORKFLOWS = "wcp:v1:workflows:{tenant}"
TENANT_RUNS = "wcp:v1:workflow_runs:{tenant}"
SCHEDULED_WORKFLOWS = "wcp:v1:workflow_schedules"
WORKFLOW_LOCK = "wcp:v1:workflow_lock:{workflow_id}"
ACTIVE_WORKFLOW_RUNS = "wcp:v1:workflow_active_runs:{workflow_id}"
TTL = 30 * 24 * 60 * 60


class CrawlStep(BaseModel):
    step_id: str | None = Field(default=None, min_length=1, max_length=40)
    depends_on: list[str] | None = Field(default=None, max_length=9,
                                         description="Step IDs that must complete first; omitted means the previous step")
    url: str = Field(min_length=1, max_length=2048)
    max_pages: int = Field(default=3, ge=1, le=25)
    obey_robots: bool = True

    @field_validator("step_id")
    @classmethod
    def valid_step_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", value):
            raise ValueError("step_id must start with a letter and contain only letters, digits, _ or -")
        return value

    @field_validator("depends_on")
    @classmethod
    def unique_dependencies(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("depends_on cannot contain duplicate step IDs")
        return value

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
    schedule_interval_minutes: int | None = Field(default=None, ge=60, le=10080)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.split())

    @property
    def max_pages(self) -> int:
        return sum(step.max_pages for step in self.steps)


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    steps: list[CrawlStep] | None = Field(default=None, min_length=1, max_length=10)
    schedule_interval_minutes: int | None = Field(default=None, ge=60, le=10080,
                                                   description="null disables schedule")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else None

    @model_validator(mode="after")
    def require_patch(self):
        if not self.model_fields_set:
            raise ValueError("at least one workflow field must be changed")
        if "name" in self.model_fields_set and not self.name:
            raise ValueError("workflow name must not be blank")
        if "steps" in self.model_fields_set and self.steps is None:
            raise ValueError("steps must be a non-empty list")
        return self


class WorkflowScheduleUpdate(BaseModel):
    interval_minutes: int | None = Field(default=None, ge=60, le=10080,
                                         description="null disables the schedule")


def _decode(raw) -> dict:
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


def _prepare_steps(steps: list[CrawlStep]) -> tuple[list[dict], str]:
    """Normalize IDs/default ordering and validate an acyclic dependency graph."""
    ids = [step.step_id or f"step_{index + 1}" for index, step in enumerate(steps)]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "step_id values must be unique")
    known = set(ids)
    prepared = []
    has_explicit_graph = False
    for index, (step, step_id) in enumerate(zip(steps, ids)):
        explicit = "depends_on" in step.model_fields_set and step.depends_on is not None
        has_explicit_graph |= explicit
        dependencies = (step.depends_on if explicit else (ids[index - 1:index] if index else []))
        if step_id in dependencies:
            raise HTTPException(422, f"step {step_id} cannot depend on itself")
        unknown = set(dependencies) - known
        if unknown:
            raise HTTPException(422, f"step {step_id} depends on unknown IDs: {', '.join(sorted(unknown))}")
        prepared.append({"step_id": step_id, "depends_on": dependencies, **step.model_dump(
            exclude={"step_id", "depends_on"})})

    pending = {step["step_id"]: set(step["depends_on"]) for step in prepared}
    resolved = set()
    while pending:
        ready = {step_id for step_id, dependencies in pending.items() if dependencies <= resolved}
        if not ready:
            raise HTTPException(422, "workflow step dependencies must form an acyclic graph")
        resolved.update(ready)
        for step_id in ready:
            del pending[step_id]
    return prepared, "dag" if has_explicit_graph else "sequential"


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


def _acquire_workflow_lock(redis, workflow_id: str) -> str:
    token = uuid.uuid4().hex
    if not redis.set(WORKFLOW_LOCK.format(workflow_id=workflow_id), token, nx=True, ex=30):
        raise HTTPException(409, "workflow is being changed or scheduled; retry shortly")
    return token


def _release_workflow_lock(redis, workflow_id: str, token: str) -> None:
    key = WORKFLOW_LOCK.format(workflow_id=workflow_id)
    try:
        redis.eval("if redis.call('get', KEYS[1]) == ARGV[1] then "
                   "return redis.call('del', KEYS[1]) else return 0 end", 1, key, token)
    except Exception as exc:  # noqa: BLE001 - fakeredis may not implement Lua
        if redis.get(key) == token:
            redis.delete(key)
        else:
            import logging
            logging.getLogger(__name__).warning("workflow lock cleanup failed: %s", exc)


def _active_workflow_runs(redis, workflow_id: str) -> list[str]:
    index = ACTIVE_WORKFLOW_RUNS.format(workflow_id=workflow_id)
    active = []
    for raw_id in redis.zrange(index, 0, -1):
        run_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        raw = redis.get(RUN_KEY.format(run_id=run_id))
        if not raw:
            redis.zrem(index, run_id)
            continue
        record = _decode(raw)
        if record.get("status") in {"queued", "running"}:
            active.append(run_id)
        else:
            redis.zrem(index, run_id)
    return active


@router.post("", status_code=201, summary="Create a bounded crawl workflow")
async def create_workflow(body: WorkflowCreate, request: Request, ctx: CtxDep) -> dict:
    tenant = str(ctx["tenant"])
    workflow_id = "workflow_" + uuid.uuid4().hex[:20]
    now = datetime.now(timezone.utc).isoformat()
    next_run = (time.time() + body.schedule_interval_minutes * 60
                if body.schedule_interval_minutes else None)
    steps, execution_mode = _prepare_steps(body.steps)
    record = {"workflow_id": workflow_id, "tenant": tenant, "name": body.name,
              "steps": steps, "execution_mode": execution_mode,
              "max_pages": body.max_pages,
              "schedule_interval_minutes": body.schedule_interval_minutes,
              "next_run_at": (datetime.fromtimestamp(next_run, timezone.utc).isoformat()
                              if next_run else None),
              "created_at": now}
    redis = request.app.state.services.redis
    pipe = redis.pipeline()
    pipe.set(WORKFLOW_KEY.format(workflow_id=workflow_id), json.dumps(record), ex=TTL)
    pipe.zadd(TENANT_WORKFLOWS.format(tenant=tenant), {workflow_id: time.time()})
    if next_run:
        pipe.zadd(SCHEDULED_WORKFLOWS, {workflow_id: next_run})
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


@router.put("/{workflow_id}", summary="Edit a workflow definition")
async def update_workflow(workflow_id: str, body: WorkflowUpdate,
                          request: Request, ctx: CtxDep) -> dict:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    _workflow(request, workflow_id, tenant)
    token = _acquire_workflow_lock(redis, workflow_id)
    try:
        record = _workflow(request, workflow_id, tenant)
        if "name" in body.model_fields_set:
            record["name"] = body.name
        if "steps" in body.model_fields_set:
            steps, execution_mode = _prepare_steps(body.steps)
            record["steps"] = steps
            record["execution_mode"] = execution_mode
            record["max_pages"] = sum(step["max_pages"] for step in steps)
        if "schedule_interval_minutes" in body.model_fields_set:
            interval = body.schedule_interval_minutes
            next_run = time.time() + interval * 60 if interval else None
            record["schedule_interval_minutes"] = interval
            record["next_run_at"] = (datetime.fromtimestamp(next_run, timezone.utc).isoformat()
                                     if next_run else None)
            if next_run is None:
                redis.zrem(SCHEDULED_WORKFLOWS, workflow_id)
            else:
                redis.zadd(SCHEDULED_WORKFLOWS, {workflow_id: next_run})
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        redis.set(WORKFLOW_KEY.format(workflow_id=workflow_id),
                  json.dumps(record, separators=(",", ":")), ex=TTL)
        return record
    finally:
        _release_workflow_lock(redis, workflow_id, token)


@router.delete("/{workflow_id}", summary="Delete a workflow definition")
async def delete_workflow(workflow_id: str, request: Request, ctx: CtxDep) -> dict:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    _workflow(request, workflow_id, tenant)
    token = _acquire_workflow_lock(redis, workflow_id)
    try:
        record = _workflow(request, workflow_id, tenant)
        active = _active_workflow_runs(redis, workflow_id)
        if active:
            raise HTTPException(409, "cancel or wait for active runs before deleting this workflow")
        pipe = redis.pipeline()
        pipe.delete(WORKFLOW_KEY.format(workflow_id=workflow_id))
        pipe.zrem(TENANT_WORKFLOWS.format(tenant=tenant), workflow_id)
        pipe.zrem(SCHEDULED_WORKFLOWS, workflow_id)
        pipe.execute()
        return {"workflow_id": workflow_id, "deleted": True,
                "historical_runs_retained": True, "name": record["name"]}
    finally:
        _release_workflow_lock(redis, workflow_id, token)


@router.patch("/{workflow_id}/schedule", summary="Set or disable a recurring workflow schedule")
async def update_workflow_schedule(workflow_id: str, body: WorkflowScheduleUpdate,
                                   request: Request, ctx: CtxDep) -> dict:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    _workflow(request, workflow_id, tenant)
    token = _acquire_workflow_lock(redis, workflow_id)
    try:
        record = _workflow(request, workflow_id, tenant)
        interval = body.interval_minutes
        next_run = time.time() + interval * 60 if interval else None
        record["schedule_interval_minutes"] = interval
        record["next_run_at"] = (datetime.fromtimestamp(next_run, timezone.utc).isoformat()
                                 if next_run else None)
        redis.set(WORKFLOW_KEY.format(workflow_id=workflow_id),
                  json.dumps(record, separators=(",", ":")), ex=TTL)
        if next_run is None:
            redis.zrem(SCHEDULED_WORKFLOWS, workflow_id)
        else:
            redis.zadd(SCHEDULED_WORKFLOWS, {workflow_id: next_run})
        return record
    finally:
        _release_workflow_lock(redis, workflow_id, token)


@router.post("/{workflow_id}/runs", status_code=202, summary="Run a workflow")
async def start_workflow(workflow_id: str, request: Request, ctx: CtxDep) -> dict:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    _workflow(request, workflow_id, tenant)
    lock_token = _acquire_workflow_lock(redis, workflow_id)
    try:
        definition = _workflow(request, workflow_id, tenant)
        run_id = "wfrun_" + uuid.uuid4().hex[:20]
        now = datetime.now(timezone.utc).isoformat()
        step_states = []
        for index, step in enumerate(definition["steps"]):
            step_id = step.get("step_id", f"step_{index + 1}")
            dependencies = step.get("depends_on", [f"step_{index}"] if index else [])
            step_states.append({"step_id": step_id, "depends_on": dependencies,
                                "url": step["url"], "status": "queued",
                                "pages_done": 0, "error": None})
        record = {"run_id": run_id, "workflow_id": workflow_id, "tenant": tenant,
                  "name": definition["name"], "status": "queued", "step_index": 0,
                  "execution_mode": definition.get("execution_mode", "sequential"),
                  "steps": step_states,
                  "created_at": now, "updated_at": now, "error": None}
        run_key = RUN_KEY.format(run_id=run_id)
        now_ts = time.time()
        pipe = redis.pipeline()
        pipe.set(run_key, json.dumps(record), ex=TTL)
        pipe.zadd(TENANT_RUNS.format(tenant=tenant), {run_id: now_ts})
        pipe.zadd(ACTIVE_WORKFLOW_RUNS.format(workflow_id=workflow_id), {run_id: now_ts})
        pipe.execute()
        payload = {"run_id": run_id, "workflow_id": workflow_id, "tenant": tenant,
                   "steps": definition["steps"],
                   "execution_mode": definition.get("execution_mode", "sequential")}
        spec = JobSpec(kind="workflow", tenant=tenant, job_id=run_id,
                       priority=JobPriority.NORMAL,
                       payload={**payload, "estimated_pages": definition["max_pages"]})
        try:
            request.app.state.services.scheduler.submit(spec)
        except Exception as exc:
            record.update(status="failed", error="workflow backend unavailable",
                          updated_at=datetime.now(timezone.utc).isoformat())
            pipe = redis.pipeline()
            pipe.set(run_key, json.dumps(record), ex=TTL)
            pipe.zrem(ACTIVE_WORKFLOW_RUNS.format(workflow_id=workflow_id), run_id)
            pipe.execute()
            from src.compute.scheduler import QuotaExceeded
            if isinstance(exc, QuotaExceeded):
                raise HTTPException(402, str(exc)) from exc
            raise HTTPException(503, "workflow backend unavailable") from exc
        return {"run_id": run_id, "workflow_id": workflow_id, "status": "queued",
                "status_url": f"/api/v1/workflows/runs/{run_id}"}
    finally:
        _release_workflow_lock(redis, workflow_id, lock_token)


@router.post("/runs/{run_id}/cancel", summary="Cancel a queued or running workflow")
async def cancel_workflow_run(run_id: str, request: Request, ctx: CtxDep) -> dict:
    if not re.fullmatch(r"wfrun_[a-f0-9]{20}", run_id):
        raise HTTPException(404, "workflow run not found")
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    key = RUN_KEY.format(run_id=run_id)
    raw = redis.get(key)
    if not raw:
        raise HTTPException(404, "workflow run not found")
    record = _decode(raw)
    if record.get("tenant") != tenant:
        raise HTTPException(404, "workflow run not found")
    if record.get("status") not in {"queued", "running"}:
        raise HTTPException(409, f"workflow run is already {record.get('status')}")
    now = datetime.now(timezone.utc).isoformat()
    if record["status"] == "queued":
        record.update(status="cancelled", cancelled_at=now,
                      cancel_requested=False)
        request.app.state.services.scheduler.mark(run_id, "cancelled",
                                                  cancelled_by=tenant)
        message = "queued workflow cancelled"
    else:
        record.update(cancel_requested=True, cancel_requested_at=now)
        message = "cancellation requested; the active crawl step will finish first"
    record["updated_at"] = now
    redis.set(key, json.dumps(record, separators=(",", ":")), ex=TTL)
    if record["status"] == "cancelled":
        redis.zrem(ACTIVE_WORKFLOW_RUNS.format(workflow_id=record["workflow_id"]), run_id)
    return {"run_id": run_id, "status": record["status"],
            "cancel_requested": record.get("cancel_requested", False),
            "message": message}


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
