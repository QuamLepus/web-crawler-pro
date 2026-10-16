"""Celery tasks for topic-driven discovery.

Two entry points, matching the two ways callers want the feature:

``workers.discovery_tasks.run_discovery``
    Plan -> search -> rank, then stop at ``awaiting_approval``. A human (or an
    automation) reviews the shortlist and calls ``POST /v1/discovery/{id}/approve``.
``workers.discovery_tasks.discover_then_crawl``
    The fully autonomous path: discover, then crawl every shortlisted site in
    the worker process and write the aggregated report.

Both delegate to :mod:`src.discovery.service`, which is Celery-agnostic and
therefore directly testable.
"""

from __future__ import annotations

from src.celery_app import app
from src.config import get_config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)
cfg = get_config()


def _redis():
    """Client for the ``wcp:v1:discovery:*`` job records."""
    import redis as redis_lib

    return redis_lib.Redis.from_url(cfg.REDIS_URL, decode_responses=True)


@app.task(name="workers.discovery_tasks.run_discovery", bind=True,
          max_retries=2, default_retry_delay=30)
def run_discovery_task(self, payload: dict) -> dict:
    """Discovery stage only — leaves the job awaiting approval."""
    from src.discovery.service import run_discovery

    try:
        result = run_discovery(payload, redis_client=_redis())
    except Exception as exc:
        logger.exception("discovery task failed")
        try:
            from src.discovery.service import DiscoveryService, DiscoveryState

            DiscoveryService(_redis())._hset(
                payload.get("job_id", ""),
                {"state": DiscoveryState.failed.value, "error": str(exc)[:500]})
        except Exception as inner:  # noqa: BLE001 - never mask the real error
            logger.warning("could not record discovery failure: %s", inner)
        raise self.retry(exc=exc)
    # Celery results must stay small; the ranker's candidate list lives in Redis.
    result.pop("candidates", None)
    return result


@app.task(name="workers.discovery_tasks.discover_then_crawl", bind=True,
          max_retries=2, default_retry_delay=30)
def discover_then_crawl_task(self, payload: dict) -> dict:
    """Discover and immediately crawl every shortlisted website."""
    from src.discovery.service import run_discovery_and_crawl

    payload = {**payload, "auto": True}
    try:
        result = run_discovery_and_crawl(payload, redis_client=_redis())
    except Exception as exc:
        logger.exception("discover+crawl task failed")
        try:
            from src.discovery.service import DiscoveryService, DiscoveryState

            DiscoveryService(_redis())._hset(
                payload.get("job_id", ""),
                {"state": DiscoveryState.failed.value, "error": str(exc)[:500]})
        except Exception as inner:  # noqa: BLE001
            logger.warning("could not record discovery failure: %s", inner)
        raise self.retry(exc=exc)
    result.pop("crawled_items", None)
    result.pop("candidates", None)
    return result


@app.task(name="workers.discovery_tasks.crawl_shortlist", bind=True)
def crawl_shortlist(self, payload: dict) -> dict:
    """Crawl an already-approved shortlist (used by the API approve flow)."""
    from src.discovery.service import DiscoveryService

    svc = DiscoveryService(_redis())
    selected, job_ids = svc.approve(payload.get("job_id", ""),
                                    domains=payload.get("domains"))
    return {"job_id": payload.get("job_id", ""), "sites": len(selected),
            "crawl_job_ids": job_ids}
