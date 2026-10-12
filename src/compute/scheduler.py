"""Priority job scheduler built on Celery beat + Redis.

Job classes map onto Celery queues with worker pools sized per priority:
  critical  (crawl frontier drain, billing)   - dedicated always-on workers
  high      (user-triggered jobs)
  normal    (scheduled source refreshes)
  low       (bulk CC ingestion, marketplace previews)
  bulk      (tokenization/sharding overnight runs)

``SchedulerService`` publishes periodic schedules dynamically (registered
data sources with cron strings) by writing Celery beat schedule entries into
Redis, and exposes quota-aware enqueueing: jobs above tenant quota are
rejected with a structured error instead of silently queued.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

logger = logging.getLogger(__name__)


class JobPriority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BULK = 4

    @property
    def queue(self) -> str:
        return {
            JobPriority.CRITICAL: "critical",
            JobPriority.HIGH: "high",
            JobPriority.NORMAL: "default",
            JobPriority.LOW: "low",
            JobPriority.BULK: "bulk",
        }[self]


@dataclass
class JobSpec:
    kind: str                     # crawl_job | ingest_source | tokenize | dedupe | shard_upload
    payload: dict[str, Any]
    tenant: str
    priority: JobPriority = JobPriority.NORMAL
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    max_retries: int = 3
    eta_epoch: float | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        d = {**{k: v for k, v in self.__dict__.items()}, "priority": int(self.priority)}
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "JobSpec":
        d = json.loads(raw)
        d["priority"] = JobPriority(d["priority"])
        return cls(**d)


QUOTA_KEY = "quota:{tenant}:{metric}"
JOB_STATE_KEY = "job:{job_id}"
JOB_TENANT_KEY = "jobs:tenant:{tenant}"


class QuotaExceeded(Exception):
    def __init__(self, tenant: str, metric: str, limit: float, used: float) -> None:
        super().__init__(f"tenant {tenant}: {metric} quota exceeded ({used}/{limit})")
        self.tenant, self.metric, self.limit, self.used = tenant, metric, limit, used


class SchedulerService:
    """Enqueues jobs through Celery when available, else Redis list queues."""

    def __init__(self, redis_client, celery_app=None) -> None:
        self.r = redis_client
        self.app = celery_app

    # -- quotas --------------------------------------------------------------- #
    def set_quota(self, tenant: str, metric: str, limit: float) -> None:
        self.r.set(QUOTA_KEY.format(tenant=tenant, metric=metric), limit)

    def consume(self, tenant: str, metric: str, amount: float = 1) -> None:
        """Increment usage; raise QuotaExceeded past the configured limit."""
        key = QUOTA_KEY.format(tenant=tenant, metric=metric)
        used = self.r.incrbyfloat(key, amount)
        limit = self.r.get(key.replace("quota:", "quota-limit:") ) or self.r.get(f"quota:{tenant}:{metric}:limit")
        # convenience: limits stored under separate key by billing service
        if limit:
            lim = float(limit)
            if used > lim:
                self.r.incrbyfloat(key, -amount)  # refund
                raise QuotaExceeded(tenant, metric, lim, used)

    def usage(self, tenant: str, metric: str) -> float:
        return float(self.r.get(QUOTA_KEY.format(tenant=tenant, metric=metric)) or 0)

    # -- enqueue ---------------------------------------------------------------- #
    def submit(self, spec: JobSpec) -> str:
        # per-kind quota gates
        gates = {
            "crawl_job": ("requests_month", spec.payload.get("max_pages", 1)),
            "ingest_source": ("requests_month", 1),
            "tokenize": ("tokens_processed_month", spec.payload.get("est_tokens", 1)),
            "dedupe": ("storage_gb", 0),
        }
        gate = gates.get(spec.kind)
        if gate and gate[1]:
            self.consume(spec.tenant, gate[0], float(gate[1]))
        state = {
            "job_id": spec.job_id, "kind": spec.kind, "tenant": spec.tenant,
            "priority": int(spec.priority), "status": "queued",
            "created_at": spec.created_at, "queue": spec.priority.queue,
        }
        pipe = self.r.pipeline()
        pipe.set(JOB_STATE_KEY.format(job_id=spec.job_id), json.dumps(state), ex=86400 * 14)
        pipe.rpush(JOB_TENANT_KEY.format(tenant=spec.tenant), spec.job_id)
        pipe.execute()
        if self.app is not None:
            self._submit_celery(spec)
        else:
            self.r.lpush(f"queue:{spec.priority.queue}", spec.to_json())
        logger.info("submitted job %s (%s, prio=%s)", spec.job_id, spec.kind, spec.priority.name)
        return spec.job_id

    def _submit_celery(self, spec: JobSpec) -> None:  # pragma: no cover - needs broker
        task_map = {
            "crawl_job": "workers.crawl_tasks.crawl_domain",
            "ingest_source": "workers.crawl_tasks.run_source_refresh",
            "tokenize": "workers.crawl_tasks.tokenize_stage",
            "dedupe": "workers.crawl_tasks.dedupe_stage",
        }
        name = task_map.get(spec.kind)
        if not name:
            raise ValueError(f"no celery task mapped for kind {spec.kind}")
        self.app.send_task(name, args=[spec.payload], queue=spec.priority.queue,
                           retries=spec.max_retries)

    def status(self, job_id: str) -> dict | None:
        raw = self.r.get(JOB_STATE_KEY.format(job_id=job_id))
        return json.loads(raw) if raw else None

    def mark(self, job_id: str, status: str, **extra: Any) -> None:
        raw = self.r.get(JOB_STATE_KEY.format(job_id=job_id))
        if not raw:
            return
        state = json.loads(raw)
        state.update({"status": status, "updated_at": time.time()}, **extra)
        self.r.set(JOB_STATE_KEY.format(job_id=job_id), json.dumps(state), keepttl=True) \
            if hasattr(self.r, "set") else None

    # -- dynamic beat schedules --------------------------------------------------- #
    def sync_source_schedules(self, registry, kinds_to_cron: dict[str, str]) -> int:
        """Materialize cron schedules for registered sources into Redis so the
        beat extension (or our poller task) can pick them up."""
        n = 0
        for tenant_hash in self.r.scan_iter("sources:*"):
            tenant = tenant_hash.decode().split(":", 1)[1] if isinstance(tenant_hash, bytes) else tenant_hash.split(":", 1)[1]
            for rec in registry.list_sources(tenant, enabled_only=True):
                cron = rec.schedule_cron or kinds_to_cron.get(rec.kind.value)
                if not cron:
                    continue
                self.r.hset("beat:schedules", rec.id, json.dumps({
                    "tenant": tenant, "kind": rec.kind.value, "cron": cron,
                }))
                n += 1
        return n
