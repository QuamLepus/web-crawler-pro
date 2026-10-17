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


@app.task(name="workers.workflow_tasks.run_workflow", bind=True, max_retries=0)
def run_workflow(self, payload: dict) -> dict:
    """Execute crawl steps in order, persisting per-step progress and results."""
    from workers.crawl_tasks import run_crawl

    redis = _redis()
    run_id = payload["run_id"]
    key = RUN_KEY.format(run_id=run_id)
    raw = redis.get(key)
    if not raw:
        raise RuntimeError("workflow run record expired or missing")
    record = json.loads(raw)
    record.update(status="running", error=None)
    _save(redis, run_id, record)
    try:
        steps = payload.get("steps", [])
        for index, definition in enumerate(steps):
            record["step_index"] = index
            record["steps"][index].update(status="running", started_at=datetime.now(timezone.utc).isoformat())
            _save(redis, run_id, record)
            result = run_crawl({
                "url": definition["url"], "max_pages": int(definition.get("max_pages", 3)),
                "obey_robots": bool(definition.get("obey_robots", True)),
                "stealth": False, "tenant": payload["tenant"],
                "job_id": f"{run_id}_{index + 1}",
            }, redis_client=redis)
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
