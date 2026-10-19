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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeredis
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("JWT_SECRET", "benchmark-only-not-a-deployment-secret")

from src.api.main import create_api_app
from src.workflow_run_indexes import (
    RUN_INDEX_READY,
    RUN_TTL_SECONDS,
    queue_new_run_indexes,
)


class CountingPipeline:
    def __init__(self, owner, pipeline):
        self.owner = owner
        self.pipeline = pipeline

    def get(self, key, *args, **kwargs):
        if key.startswith("wcp:v1:workflow_run:"):
            self.owner.run_records_read += 1
        self.pipeline.get(key, *args, **kwargs)
        return self

    def __getattr__(self, name):
        return getattr(self.pipeline, name)


class CountingRedis:
    """Transparent Redis proxy counting indexed IDs and run-record reads."""

    def __init__(self, redis):
        self.redis = redis
        self.reset()

    def reset(self):
        self.run_ids_returned = 0
        self.run_records_read = 0
        self.zinterstore_driver_members = 0

    def __getattr__(self, name):
        method = getattr(self.redis, name)
        if name == "pipeline":
            def counted_pipeline(*args, **kwargs):
                return CountingPipeline(self, method(*args, **kwargs))
            return counted_pipeline
        if name == "zrevrange":
            def counted_zrevrange(key, *args, **kwargs):
                values = method(key, *args, **kwargs)
                if key.startswith("wcp:v1:workflow_run"):
                    self.run_ids_returned += len(values)
                return values
            return counted_zrevrange
        if name == "zrevrangebyscore":
            def counted_range_by_score(key, *args, **kwargs):
                values = method(key, *args, **kwargs)
                if key.startswith("wcp:v1:workflow_run"):
                    self.run_ids_returned += len(values)
                return values
            return counted_range_by_score
        if name == "zscan":
            def counted_zscan(key, *args, **kwargs):
                cursor, values = method(key, *args, **kwargs)
                if key.startswith("wcp:v1:workflow_run"):
                    self.run_ids_returned += len(values)
                return cursor, values
            return counted_zscan
        if name == "zinterstore":
            def counted_intersection(destination, keys, *args, **kwargs):
                sizes = [self.redis.zcard(key) for key in keys]
                self.zinterstore_driver_members += min(sizes, default=0)
                return method(destination, keys, *args, **kwargs)
            return counted_intersection
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


def seed_runs(redis, tenant: str, start: int, stop: int) -> None:
    now = datetime.now(UTC)
    statuses = ("completed", "completed", "failed", "queued", "cancelled")
    batch_size = 250
    for offset in range(start, stop, batch_size):
        pipe = redis.pipeline()
        for index in range(offset, min(offset + batch_size, stop)):
            workflow_number = index % 1000
            run_id = f"wfrun_{index:020x}"
            created_at = now - timedelta(days=index % 30, seconds=index // 30)
            record = {
                "run_id": run_id,
                "workflow_id": f"workflow_{workflow_number:020x}",
                "tenant": tenant,
                "name": f"Workflow {workflow_number:06d}",
                "status": statuses[index % len(statuses)],
                "created_at": created_at.isoformat(),
                "execution_mode": "dag",
                "steps": [],
            }
            pipe.set(f"wcp:v1:workflow_run:{run_id}", json.dumps(record), ex=RUN_TTL_SECONDS)
            queue_new_run_indexes(pipe, record, created_at.timestamp())
        pipe.execute()
    redis.set(RUN_INDEX_READY.format(tenant=tenant), "1", ex=RUN_TTL_SECONDS)


def measure(client: TestClient, headers: dict[str, str], params: dict,
            repeats: int) -> tuple[float, int, int, int, int]:
    timings = []
    total = 0
    ids_returned = 0
    driver_members = 0
    record_reads = 0
    redis = client.app.state.services.redis
    for _ in range(repeats):
        redis.reset()
        started = time.perf_counter()
        response = client.get("/api/v1/workflows/runs", headers=headers, params=params)
        timings.append((time.perf_counter() - started) * 1000)
        response.raise_for_status()
        total = response.json()["total"]
        ids_returned = redis.run_ids_returned
        driver_members = redis.zinterstore_driver_members
        record_reads = redis.run_records_read
    return statistics.median(timings), total, ids_returned, record_reads, driver_members


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
        counter = CountingRedis(raw_redis)
        app.state.services.redis = counter

        start = 0
        filters = [
            ("unfiltered page", {}),
            ("name token search", {"q": "Workflow 000042"}),
            ("status filter", {"status": "failed"}),
            ("last-four-days date range", {
                "date_from": (datetime.now(UTC).date() - timedelta(days=3)).isoformat(),
                "date_to": datetime.now(UTC).date().isoformat(),
            }),
        ]
        print("dataset,filter,median_ms,total_matches,run_ids_returned,run_records_read,smallest_intersection_input_size")
        for size in sorted(set(args.sizes)):
            seed_runs(raw_redis, tenant, start, size)
            start = size
            for label, filter_params in filters:
                elapsed, total, ids_returned, reads, driver_members = measure(
                    client, headers, {**filter_params, "page_size": 20}, args.repeats)
                print(f"{size},{label},{elapsed:.2f},{total},{ids_returned},{reads},{driver_members}")

            patterns = [
                f"wcp:v1:workflow_runs:{tenant}:status:*",
                f"wcp:v1:workflow_runs:{tenant}:workflow:*",
                f"wcp:v1:workflow_runs:{tenant}:term:*",
            ]
            index_keys = [key for pattern in patterns for key in raw_redis.keys(pattern)]
            index_keys.append(RUN_INDEX_READY.format(tenant=tenant))
            raw_redis.delete(*index_keys)
            elapsed, total, ids_returned, reads, driver_members = measure(
                client, headers, {"q": "Workflow 000042", "page_size": 20}, 1)
            print(f"{size},first-query backfill,{elapsed:.2f},{total},{ids_returned},{reads},{driver_members}")


if __name__ == "__main__":
    main()
