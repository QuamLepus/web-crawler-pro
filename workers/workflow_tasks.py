"""Celery execution for bounded, sequential crawl workflows."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from src.celery_app import app
from src.config import get_config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)
RUN_KEY = "wcp:v1:workflow_run:{run_id}"
ACTIVE_WORKFLOW_RUNS = "wcp:v1:workflow_active_runs:{workflow_id}"


def _redis():
    import redis
    return redis.Redis.from_url(get_config().REDIS_URL, decode_responses=True)


def _save(redis, run_id: str, record: dict) -> None:
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    pipe = redis.pipeline()
    pipe.set(RUN_KEY.format(run_id=run_id), json.dumps(record, separators=(",", ":")), ex=30 * 86400)
    if record.get("status") not in {"queued", "running"} and record.get("workflow_id"):
        pipe.zrem(ACTIVE_WORKFLOW_RUNS.format(workflow_id=record["workflow_id"]), run_id)
    pipe.execute()


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
    """Execute a legacy sequence or serially dispatched dependency DAG.

    Legacy sequences are fail-fast. DAG failures skip dependent steps while
    independent branches continue; the run still ends failed. A crawl already
    running is not interrupted mid-request, and cancellation takes effect after
    that crawl returns and before another step starts.
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
        states = record.setdefault("steps", [])
        for index, definition in enumerate(steps):
            step_id = definition.get("step_id", f"step_{index + 1}")
            dependencies = definition.get("depends_on", [f"step_{index}"] if index else [])
            states[index].setdefault("step_id", step_id)
            states[index].setdefault("depends_on", dependencies)
        mode = record.get("execution_mode", payload.get("execution_mode", "sequential"))
        remaining = set(range(len(steps)))
        failed_steps = []

        while remaining:
            latest = _read(redis, key)
            if latest.get("status") == "cancelled" or latest.get("cancel_requested"):
                return _finish_cancelled(redis, run_id, key, {**record, **latest})
            status_by_id = {step.get("step_id", f"step_{index + 1}"): step.get("status")
                            for index, step in enumerate(states)}
            advanced = False
            for index in sorted(remaining):
                definition = steps[index]
                step_state = states[index]
                step_id = step_state["step_id"]
                dependencies = definition.get("depends_on", step_state.get("depends_on", []))
                dependency_statuses = [status_by_id.get(dependency, "queued")
                                       for dependency in dependencies]
                if any(status in {"failed", "skipped", "cancelled"}
                       for status in dependency_statuses):
                    step_state.update(status="skipped", error="a prerequisite step did not complete",
                                      completed_at=datetime.now(timezone.utc).isoformat())
                    remaining.remove(index)
                    _save(redis, run_id, record)
                    advanced = True
                    break
                if not all(status == "completed" for status in dependency_statuses):
                    continue

                record["step_index"] = index
                step_state.update(status="running", error=None,
                                  started_at=datetime.now(timezone.utc).isoformat())
                _save(redis, run_id, record)
                result = run_crawl({
                    "url": definition["url"],
                    "max_pages": int(definition.get("max_pages", 3)),
                    "obey_robots": bool(definition.get("obey_robots", True)),
                    "stealth": False, "tenant": payload["tenant"],
                    "job_id": f"{run_id}_{index + 1}",
                }, redis_client=redis)
                latest = _read(redis, key)
                record["cancel_requested"] = bool(latest.get("cancel_requested"))
                failed = bool(result.get("failures")) and not result.get("collected")
                error = "; ".join(result.get("failures", []))[:500] or None
                step_state.update(
                    status="failed" if failed else "completed",
                    pages_done=int(result.get("pages_done", 0)),
                    items_collected=int(result.get("items", 0)),
                    data_sha256=result.get("data_sha256"), error=error,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                remaining.remove(index)
                advanced = True
                if failed:
                    failed_steps.append(step_id)
                    if mode != "dag":
                        raise RuntimeError(error or "crawl step failed")
                _save(redis, run_id, record)
                if record["cancel_requested"]:
                    return _finish_cancelled(redis, run_id, key, record)
                break
            if not advanced:
                raise RuntimeError("workflow graph has no runnable step; definition may be invalid")

        if failed_steps:
            error = "workflow steps failed: " + ", ".join(failed_steps)
            record.update(step_index=len(steps), completed_at=datetime.now(timezone.utc).isoformat())
            raise RuntimeError(error[:500])
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
        lock_key = f"wcp:v1:workflow_lock:{workflow_id}"
        lock_token = uuid.uuid4().hex
        if not redis.set(lock_key, lock_token, nx=True, ex=30):
            continue
        try:
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
            run_steps = []
            for index, step in enumerate(definition["steps"]):
                step_id = step.get("step_id", f"step_{index + 1}")
                dependencies = step.get("depends_on", [f"step_{index}"] if index else [])
                run_steps.append({"step_id": step_id, "depends_on": dependencies,
                                  "url": step["url"], "status": "queued",
                                  "pages_done": 0, "error": None})
            record = {
                "run_id": run_id, "workflow_id": workflow_id, "tenant": tenant,
                "name": definition["name"], "status": "queued", "step_index": 0,
                "execution_mode": definition.get("execution_mode", "sequential"),
                "steps": run_steps,
                "created_at": now_iso, "updated_at": now_iso, "error": None,
                "scheduled": True,
            }
            key = RUN_KEY.format(run_id=run_id)
            redis.set(key, json.dumps(record, separators=(",", ":")), ex=30 * 86400)
            redis.zadd(TENANT_RUNS.format(tenant=tenant), {run_id: now})
            redis.zadd(ACTIVE_WORKFLOW_RUNS.format(workflow_id=workflow_id), {run_id: now})
            try:
                scheduler = SchedulerService(redis, celery_app=app)
                scheduler.submit(JobSpec(
                    kind="workflow", tenant=tenant, job_id=run_id,
                    priority=JobPriority.NORMAL,
                    payload={"run_id": run_id, "workflow_id": workflow_id,
                             "tenant": tenant, "steps": definition["steps"],
                             "execution_mode": definition.get("execution_mode", "sequential"),
                             "estimated_pages": int(definition.get("max_pages", 1))},
                ))
                queued += 1
            except Exception as exc:  # keep a failed run record rather than silently dropping it
                record.update(status="failed", error=f"scheduled dispatch failed: {exc}"[:500],
                              updated_at=datetime.now(timezone.utc).isoformat())
                redis.set(key, json.dumps(record, separators=(",", ":")), ex=30 * 86400)
                redis.zrem(ACTIVE_WORKFLOW_RUNS.format(workflow_id=workflow_id), run_id)
            next_run = now + int(interval) * 60
            definition["next_run_at"] = datetime.fromtimestamp(next_run, timezone.utc).isoformat()
            redis.set(WORKFLOW_KEY.format(workflow_id=workflow_id),
                      json.dumps(definition, separators=(",", ":")), ex=30 * 86400)
            redis.zadd(SCHEDULED_WORKFLOWS, {workflow_id: next_run})
        finally:
            try:
                redis.eval("if redis.call('get', KEYS[1]) == ARGV[1] then "
                           "return redis.call('del', KEYS[1]) else return 0 end",
                           1, lock_key, lock_token)
            except Exception as exc:  # noqa: BLE001 - fakeredis may not implement Lua
                if redis.get(lock_key) == lock_token:
                    redis.delete(lock_key)
                else:
                    logger.warning("workflow schedule lock cleanup failed: %s", exc)
    return {"queued": queued, "due": len(due)}
