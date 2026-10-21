"""Celery orchestration for topic-to-cited-report research."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from src.celery_app import app
from src.config import get_config
from redis.exceptions import WatchError

from src.utils.logger import setup_logger

logger = setup_logger(__name__)
cfg = get_config()


def _redis():
    import redis
    return redis.Redis.from_url(cfg.REDIS_URL, decode_responses=True)


def _atomic_update(redis, research_id: str, update) -> dict:
    key = f"wcp:v1:research:{research_id}"
    for _ in range(10):
        with redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                if not raw:
                    return {}
                value = raw.decode() if isinstance(raw, bytes) else raw
                record = json.loads(value)
                if record.get("status") == "cancelled":
                    return record
                update(record)
                pipe.multi()
                pipe.set(key, json.dumps(record, separators=(",", ":")),
                         ex=60 * 60 * 24 * 30)
                pipe.execute()
                return record
            except WatchError:
                continue
    raise RuntimeError(f"research job {research_id} changed repeatedly")


def _set_stage(redis, research_id: str, name: str, state: str, *,
               progress: int, status: str = "running", **extra) -> dict:
    def update(record: dict) -> None:
        stages = record.get("stages", {})
        if name in stages:
            stages[name] = state
        record.update({"status": status, "stage": name, "progress": progress,
                       "stages": stages, **extra,
                       "updated_at": datetime.now(timezone.utc).isoformat()})

    return _atomic_update(redis, research_id, update)


def _check_cancelled(redis, research_id: str) -> None:
    raw = redis.get(f"wcp:v1:research:{research_id}")
    if raw:
        value = raw.decode() if isinstance(raw, bytes) else raw
        if json.loads(value).get("status") == "cancelled":
            raise ResearchCancellationRequested()


class ResearchCancellationRequested(Exception):
    """Internal signal used to stop work at the next cooperative boundary."""


@app.task(name="workers.research_tasks.run_research", bind=True, max_retries=1,
          default_retry_delay=60)
def run_research(self, payload: dict) -> dict:
    """Discover sources, crawl/extract them, and report source-backed excerpts."""
    from src.discovery.service import (DiscoveryService, DiscoveryState,
                                       run_discovery)
    from src.research.report import make_report
    from workers.crawl_tasks import run_crawl

    redis = _redis()
    research_id = payload["research_id"]
    tenant = payload["tenant"]
    discovery_id = f"{research_id}_discovery"
    try:
        _check_cancelled(redis, research_id)
        _set_stage(redis, research_id, "planning", "completed", progress=5)
        _check_cancelled(redis, research_id)
        _set_stage(redis, research_id, "discovery", "running", progress=10)
        _check_cancelled(redis, research_id)
        discovery_payload = {**payload, "job_id": discovery_id, "auto": True}
        discovery = run_discovery(discovery_payload, redis_client=redis)
        _check_cancelled(redis, research_id)
        if discovery.get("state") == DiscoveryState.failed.value:
            raise RuntimeError(discovery.get("error") or "source discovery failed")
        svc = DiscoveryService(redis)
        selected, crawl_ids = svc.approve(discovery_id, dispatch=False)
        if not selected:
            raise RuntimeError("no permitted candidate sources were discovered")
        _set_stage(redis, research_id, "discovery", "completed", progress=30,
                   discovered_sources=len(selected))
        _set_stage(redis, research_id, "crawl_extract", "running", progress=35)

        items: list[dict] = []
        per_site: list[dict] = []
        total = len(selected)
        for index, (candidate, crawl_id) in enumerate(zip(selected, crawl_ids)):
            _check_cancelled(redis, research_id)
            url = candidate["url"]
            try:
                result = run_crawl({"url": url, "tenant": tenant, "job_id": crawl_id,
                                    "discovery_id": discovery_id,
                                    "max_pages": int(payload.get("max_pages_per_site", 3)),
                                    "obey_robots": True, "stealth": False},
                                   redis_client=redis)
                site_items = list(result.get("collected") or [])
                items.extend(site_items)
                per_site.append({"url": url, "pages": result.get("pages_done", 0),
                                 "items": len(site_items), "state": "succeeded" if site_items else "empty"})
            except Exception as exc:  # one failed host must not erase other evidence
                logger.warning("research crawl failed for %s: %s", url, exc)
                per_site.append({"url": url, "pages": 0, "items": 0,
                                 "state": "failed", "error": str(exc)[:300]})
            pct = 35 + int(45 * (index + 1) / total)
            _set_stage(redis, research_id, "crawl_extract", "running", progress=pct,
                       crawled_sources=index + 1, source_results=per_site)
            _check_cancelled(redis, research_id)

        discovery_state = (DiscoveryState.succeeded.value if any(
            row["state"] == "succeeded" for row in per_site) else DiscoveryState.failed.value)
        svc._hset(discovery_id, {"state": discovery_state,
                                 "sites_total": len(per_site),
                                 "sites_succeeded": sum(r["state"] == "succeeded" for r in per_site),
                                 "sites_failed": sum(r["state"] == "failed" for r in per_site),
                                 "per_site": json.dumps(per_site)})
        _set_stage(redis, research_id, "crawl_extract", "completed", progress=80,
                   crawled_sources=total, page_records=len(items), source_results=per_site)
        _check_cancelled(redis, research_id)
        _set_stage(redis, research_id, "source_check", "running", progress=85)
        _check_cancelled(redis, research_id)
        report = make_report(payload["topic"], items[:200])
        _check_cancelled(redis, research_id)
        try:
            discovery_queries = json.loads(discovery.get("queries") or "[]")
        except (TypeError, ValueError):
            discovery_queries = []
        report["plan"] = {
            "objective": payload["topic"],
            "source_target": int(payload.get("count", 5)),
            "max_pages_per_source": int(payload.get("max_pages_per_site", 3)),
            "stages": ["planning", "source discovery", "robots-aware crawl and extraction",
                       "exact-excerpt source check", "report generation"],
            "discovery_queries": discovery_queries,
        }
        report["source_selection"] = [
            {key: candidate.get(key) for key in ("url", "domain", "score", "reasons", "providers")}
            for candidate in selected
        ]
        _set_stage(redis, research_id, "source_check", "completed", progress=95)
        final_record = _set_stage(redis, research_id, "report", "completed", progress=100,
                                  status="completed", report=report, error=None,
                                  discovery_id=discovery_id)
        if final_record.get("status") == "cancelled":
            return {"research_id": research_id, "status": "cancelled"}
        try:
            from src.gateway.webhooks.service import WebhookService
            WebhookService(redis).emit(tenant, "research.completed",
                                       {"research_id": research_id,
                                        "topic": payload["topic"],
                                        "claims": len(report["claims"]),
                                        "sources": len(report["sources"])})
        except Exception as exc:
            logger.warning("research completion webhook failed: %s", exc)
        return {"research_id": research_id, "status": "completed",
                "claims": len(report["claims"]), "sources": len(report["sources"])}
    except ResearchCancellationRequested:
        return {"research_id": research_id, "status": "cancelled"}
    except Exception as exc:
        logger.exception("research job %s failed", research_id)

        def mark_failed(record: dict) -> None:
            stages = record.get("stages", {})
            for name, stage_state in list(stages.items()):
                if stage_state == "running":
                    stages[name] = "failed"
            record.update(status="failed", stage="failed", progress=100,
                          error=str(exc)[:500], stages=stages,
                          updated_at=datetime.now(timezone.utc).isoformat())

        failed_record = _atomic_update(redis, research_id, mark_failed)
        if failed_record.get("status") == "cancelled":
            return {"research_id": research_id, "status": "cancelled"}
        try:
            from src.gateway.webhooks.service import WebhookService
            WebhookService(redis).emit(tenant, "research.failed",
                                       {"research_id": research_id,
                                        "error": str(exc)[:300]})
        except Exception:
            pass
        raise
