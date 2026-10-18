"""Measure filtered workflow-run listing against an isolated fakeredis dataset.

Run with: python scripts/benchmark_workflow_run_filters.py
The measurements include FastAPI/TestClient overhead and are not production Redis
latency estimates; counters report run records examined by each filtered request.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("JWT_SECRET", "benchmark-only-not-a-deployment-secret")

from src.api.main import create_api_app  # noqa: E402


class CountingRedis:
    """Transparent Redis proxy counting run-index IDs and run-record reads."""

    def __init__(self, redis):
        self.redis = redis
        self.reset()

    def reset(self):
        self.index_ids_examined = 0
        self.run_records_read = 0

    def __getattr__(self, name):
        method = getattr(self.redis, name)
        if name == "zrevrange":
            def counted_zrevrange(key, *args, **kwargs):
                values = method(key, *args, **kwargs)
                if key.startswith("wcp:v1:workflow_runs:"):
                    self.index_ids_examined += len(values)
                return values
            return counted_zrevrange
        if name == "get":
            def counted_get(key, *args, **kwargs):
                if key.startswith("wcp:v1:workflow_run:"):
                    self.run_records_read += 1
                return method(key, *args, **kwargs)
            return counted_get
        return method


def tenant_api_key(client: TestClient) -> str:
    signup = client.post("/auth/signup", json={
        "org_name": "Filter benchmark", "email": "filter-bench@example.test",
        "password": "correct-horse-123",
    })
    signup.raise_for_status()
    token = signup.json()["access_token"]
    response = client.post("/keys", headers={"Authorization": f"Bearer {token}"},
                           json={"name": "benchmark", "scopes": ["read", "crawl"]})
    response.raise_for_status()
    return response.json()["key"]


def seed_runs(redis, tenant: str, workflow_id: str, start: int, stop: int) -> None:
    now = datetime.now(timezone.utc)
    score_now = time.time()
    statuses = ("completed", "completed", "failed", "queued", "cancelled")
    for index in range(start, stop):
        run_id = f"wfrun_{index:020x}"
        record = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "tenant": tenant,
            "name": f"Workflow run {index:06d}",
            "status": statuses[index % len(statuses)],
            "created_at": (now - timedelta(days=index % 30)).isoformat(),
            "execution_mode": "dag",
            "steps": [],
        }
        redis.set(f"wcp:v1:workflow_run:{run_id}", json.dumps(record), ex=30 * 86400)
        redis.zadd(f"wcp:v1:workflow_runs:{tenant}", {run_id: score_now - index})


def measure(client: TestClient, headers: dict[str, str], params: dict,
            repeats: int) -> tuple[float, int, int, int]:
    timings = []
    total = 0
    index_count = 0
    record_reads = 0
    redis = client.app.state.services.redis
    for _ in range(repeats):
        redis.reset()
        started = time.perf_counter()
        response = client.get("/api/v1/workflows/runs", headers=headers, params=params)
        timings.append((time.perf_counter() - started) * 1000)
        response.raise_for_status()
        total = response.json()["total"]
        index_count = redis.index_ids_examined
        record_reads = redis.run_records_read
    return statistics.median(timings), total, index_count, record_reads


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[1_000, 10_000, 50_000])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and repeats must be positive")

    raw_redis = fakeredis.FakeStrictRedis(decode_responses=True)
    app = create_api_app(raw_redis, enable_graphql=False)
    with TestClient(app) as client:
        key = tenant_api_key(client)
        headers = {"X-API-Key": key}
        definition = client.post("/api/v1/workflows", headers=headers, json={
            "name": "Filter benchmark", "steps": [{"url": "https://example.org"}],
        })
        definition.raise_for_status()
        tenant = definition.json()["tenant"]
        workflow_id = definition.json()["workflow_id"]
        counter = CountingRedis(raw_redis)
        app.state.services.redis = counter

        start = 0
        filters = [
            ("unfiltered page", {}),
            ("sparse name search", {"q": "Workflow run 000042"}),
            ("status filter", {"status": "failed"}),
            ("last-four-days date range", {
                "date_from": (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat(),
                "date_to": datetime.now(timezone.utc).date().isoformat(),
            }),
        ]
        print("dataset,filter,median_ms,total_matches,index_ids_examined,run_records_read")
        for size in sorted(set(args.sizes)):
            seed_runs(raw_redis, tenant, workflow_id, start, size)
            start = size
            for label, filter_params in filters:
                elapsed, total, indexes, reads = measure(
                    client, headers, {**filter_params, "page_size": 20}, args.repeats)
                print(f"{size},{label},{elapsed:.2f},{total},{indexes},{reads}")


if __name__ == "__main__":
    main()
