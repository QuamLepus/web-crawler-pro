"""Workflow storage (Redis) — definitions plus an inspectable run history."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from src.workflows.models import Workflow, WorkflowRun

logger = logging.getLogger("wcp.workflows.store")


class WorkflowStore:
    def __init__(self, redis_client, *, prefix: str = "wcp:workflow") -> None:
        self.r = redis_client
        self.prefix = prefix

    def _k(self, tenant: str, *parts: str) -> str:
        return ":".join([self.prefix, tenant or "public", *parts])

    @staticmethod
    def _dec(raw) -> str:
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    # -- workflows ----------------------------------------------------------- #
    def save(self, workflow: Workflow) -> Workflow:
        workflow.updated_at = time.time()
        self.r.set(self._k(workflow.tenant, "wf", workflow.workflow_id),
                   json.dumps(workflow.to_dict(), separators=(",", ":"), default=str))
        self.r.zadd(self._k(workflow.tenant, "index"),
                    {workflow.workflow_id: workflow.created_at})
        if workflow.state == "active" and workflow.schedule:
            self.r.zadd(self._k("_all", "scheduled"),
                        {f"{workflow.tenant}:{workflow.workflow_id}": time.time()})
        return workflow

    def get(self, tenant: str, workflow_id: str) -> Workflow | None:
        raw = self.r.get(self._k(tenant, "wf", workflow_id))
        if not raw:
            return None
        return Workflow.from_dict(json.loads(raw))

    def list(self, tenant: str, *, limit: int = 100) -> list[Workflow]:
        ids = [self._dec(i) for i in self.r.zrevrange(self._k(tenant, "index"),
                                                      0, max(0, limit - 1))]
        return [w for w in (self.get(tenant, i) for i in ids) if w is not None]

    def delete(self, tenant: str, workflow_id: str) -> bool:
        pipe = self.r.pipeline()
        pipe.delete(self._k(tenant, "wf", workflow_id))
        pipe.zrem(self._k(tenant, "index"), workflow_id)
        pipe.zrem(self._k("_all", "scheduled"), f"{tenant}:{workflow_id}")
        pipe.execute()
        return True

    # -- runs ---------------------------------------------------------------- #
    def save_run(self, run: WorkflowRun) -> WorkflowRun:
        payload = json.dumps(run.to_dict(), separators=(",", ":"), default=str)
        pipe = self.r.pipeline()
        pipe.set(self._k(run.tenant, "run", run.run_id), payload)
        pipe.zadd(self._k(run.tenant, "runs"), {run.run_id: run.started_at})
        pipe.zadd(self._k(run.tenant, "wf", run.workflow_id, "runs"),
                  {run.run_id: run.started_at})
        pipe.zremrangebyrank(self._k(run.tenant, "runs"), 0, -1001)
        pipe.execute()
        return run

    def get_run(self, tenant: str, run_id: str) -> WorkflowRun | None:
        raw = self.r.get(self._k(tenant, "run", run_id))
        return WorkflowRun.from_dict(json.loads(raw)) if raw else None

    def runs(self, tenant: str, *, workflow_id: str = "",
             limit: int = 25) -> list[WorkflowRun]:
        key = (self._k(tenant, "wf", workflow_id, "runs") if workflow_id
               else self._k(tenant, "runs"))
        ids = [self._dec(i) for i in self.r.zrevrange(key, 0, max(0, limit - 1))]
        return [r for r in (self.get_run(tenant, i) for i in ids) if r is not None]

    def stats(self, tenant: str) -> dict[str, Any]:
        workflows = self.list(tenant, limit=500)
        runs = self.runs(tenant, limit=200)
        return {
            "workflows": len(workflows),
            "active": sum(1 for w in workflows if w.state == "active"),
            "scheduled": sum(1 for w in workflows if w.schedule),
            "runs": len(runs),
            "failed_runs": sum(1 for r in runs if r.state == "failed"),
            "last_run_at": max((w.last_run_at or 0) for w in workflows) if workflows else None,
        }
