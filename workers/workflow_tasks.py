"""Celery execution for bounded, sequential crawl workflows."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from src.celery_app import app
from src.config import get_config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)
RUN_KEY = "wcp:v1:workflow_run:{run_id}"


def _redis():
    import redis
    return redis.Redis.from_url(get_config().REDIS_URL, decode_responses=True)


def _save(redis, run_id: str, record: dict) -> None:
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    redis.set(RUN_KEY.format(run_id=run_id), json.dumps(record, separators=(",", ":")), ex=30 * 86400)


def _read(redis, key: str) -> dict:
    raw = redis.get(key)
    return json.loads(raw) if raw else {}


def _finish_cancelled(redis, run_id: str, key: str, record: dict) -> dict:
    latest = _read(redis, key)
    record["cancel_requested"] = False
    record["status"] = "cancelled"
    record["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    for step in record.get("steps", []):
        if step.get("status") == "queued":
            step["status"] = "cancelled"
    _save(redis, run_id, record)
    return {"run_id": run_id, "status": "cancelled",
            "step_index": record.get("step_index", 0),
            "cancel_requested_at": latest.get("cancel_requested_at")}


@app.task(name="workers.workflow_tasks.run_workflow", bind=True, max_retries=0)
def run_workflow(self, payload: dict) -> dict:
    """Execute crawl steps in order, honoring cancellation between steps.

    A crawl already running is not interrupted mid-request; a cancellation is
    applied immediately after that crawl returns and before another step starts.
    """
    from workers.crawl_tasks import run_crawl

    redis = _redis()
    run_id = payload["run_id"]
    key = RUN_KEY.format(run_id=run_id)
    record = _read(redis, key)
    if not record:
        raise RuntimeError("workflow run record expired or missing")
    if record.get("status") == "cancelled" or record.get("cancel_requested"):
        return _finish_cancelled(redis, run_id, key, record)
    record.update(status="running", error=None)
    _save(redis, run_id, record)
    try:
        steps = payload.get("steps", [])
        for index, definition in enumerate(steps):
            latest = _read(redis, key)
            if latest.get("status") == "cancelled" or latest.get("cancel_requested"):
                return _finish_cancelled(redis, run_id, key, {**record, **latest})
            record["step_index"] = index
            record["steps"][index].update(status="running", started_at=datetime.now(timezone.utc).isoformat())
            _save(redis, run_id, record)
            result = run_crawl({
                "url": definition["url"], "max_pages": int(definition.get("max_pages", 3)),
                "obey_robots": bool(definition.get("obey_robots", True)),
                "stealth": False, "tenant": payload["tenant"],
                "job_id": f"{run_id}_{index + 1}",
            }, redis_client=redis)
            latest = _read(redis, key)
            record["cancel_requested"] = bool(latest.get("cancel_requested"))
            failed = bool(result.get("failures")) and not result.get("collected")
            record["steps"][index].update(
                status="failed" if failed else "completed",
                pages_done=int(result.get("pages_done", 0)),
                items_collected=int(result.get("items", 0)),
                data_sha256=result.get("data_sha256"),
                error="; ".join(result.get("failures", []))[:500] or None,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            if failed:
                raise RuntimeError(record["steps"][index]["error"] or "crawl step failed")
            _save(redis, run_id, record)
            if record["cancel_requested"]:
                return _finish_cancelled(redis, run_id, key, record)
        record.update(status="completed", step_index=len(steps), error=None,
                      completed_at=datetime.now(timezone.utc).isoformat())
        _save(redis, run_id, record)
        return {"run_id": run_id, "status": "completed", "steps": len(steps)}
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = str(exc)[:500]
        for step in record.get("steps", []):
            if step.get("status") == "running":
                step["status"] = "failed"
                step["error"] = record["error"]
        _save(redis, run_id, record)
        logger.exception("workflow run %s failed", run_id)
        raise


SCHEDULED_WORKFLOWS = "wcp:v1:workflow_schedules"
WORKFLOW_KEY = "wcp:v1:workflow:{workflow_id}"
TENANT_RUNS = "wcp:v1:workflow_runs:{tenant}"
SCHEDULE_LOCK = "wcp:v1:workflow_schedule_tick_lock"


@app.task(name="workers.workflow_tasks.schedule_tick", bind=True, max_retries=0)
def schedule_tick(self, batch: int = 50) -> dict:
    """Queue due tenant workflows once per interval; Celery Beat calls each minute."""
    import time
    import uuid

    from src.compute.scheduler import JobPriority, JobSpec, SchedulerService

    redis = _redis()
    if not redis.set(SCHEDULE_LOCK, uuid.uuid4().hex, nx=True, ex=55):
        return {"queued": 0, "locked": True}
    now = time.time()
    due = redis.zrangebyscore(SCHEDULED_WORKFLOWS, 0, now, start=0, num=max(1, min(batch, 100)))
    queued = 0
    for raw_id in due:
        workflow_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        raw = redis.get(WORKFLOW_KEY.format(workflow_id=workflow_id))
        if not raw:
            redis.zrem(SCHEDULED_WORKFLOWS, workflow_id)
            continue
        definition = json.loads(raw)
        interval = definition.get("schedule_interval_minutes")
        if not interval:
            redis.zrem(SCHEDULED_WORKFLOWS, workflow_id)
            continue
        tenant = str(definition["tenant"])
        run_id = "wfrun_" + uuid.uuid4().hex[:20]
        now_iso = datetime.now(timezone.utc).isoformat()
        record = {
            "run_id": run_id, "workflow_id": workflow_id, "tenant": tenant,
            "name": definition["name"], "status": "queued", "step_index": 0,
            "steps": [{"url": step["url"], "status": "queued", "pages_done": 0,
                       "error": None} for step in definition["steps"]],
            "created_at": now_iso, "updated_at": now_iso, "error": None,
            "scheduled": True,
        }
        key = RUN_KEY.format(run_id=run_id)
        redis.set(key, json.dumps(record, separators=(",", ":")), ex=30 * 86400)
        redis.zadd(TENANT_RUNS.format(tenant=tenant), {run_id: now})
        try:
            scheduler = SchedulerService(redis, celery_app=app)
            scheduler.submit(JobSpec(
                kind="workflow", tenant=tenant, job_id=run_id,
                priority=JobPriority.NORMAL,
                payload={"run_id": run_id, "workflow_id": workflow_id,
                         "tenant": tenant, "steps": definition["steps"],
                         "estimated_pages": int(definition.get("max_pages", 1))},
            ))
            queued += 1
        except Exception as exc:  # keep a failed run record rather than silently dropping it
            record.update(status="failed", error=f"scheduled dispatch failed: {exc}"[:500],
                          updated_at=datetime.now(timezone.utc).isoformat())
            redis.set(key, json.dumps(record, separators=(",", ":")), ex=30 * 86400)
        next_run = now + int(interval) * 60
        definition["next_run_at"] = datetime.fromtimestamp(next_run, timezone.utc).isoformat()
        redis.set(WORKFLOW_KEY.format(workflow_id=workflow_id),
                  json.dumps(definition, separators=(",", ":")), ex=30 * 86400)
        redis.zadd(SCHEDULED_WORKFLOWS, {workflow_id: next_run})
    return {"queued": queued, "due": len(due)}
