#!/usr/bin/env python3
"""Reproducible synthetic, in-process API benchmark for Data Studio aggregates.

Measures authenticated GET requests through FastAPI's TestClient against a
synthetic SQLite DocStore. This includes application routing, auth, middleware,
SQLite and JSON serialization, but not TCP/TLS, a reverse proxy, browser work,
production Redis, or concurrent traffic. It is a local microbenchmark, not an
SLO or capacity test. All temporary data is removed when the process exits.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=int, default=50_000,
                        help="number of synthetic indexed documents")
    parser.add_argument("--iterations", type=int, default=10,
                        help="measured API calls per profile")
    parser.add_argument("--warmup", type=int, default=2,
                        help="unmeasured API calls per profile")
    args = parser.parse_args()
    if args.docs < 1 or args.iterations < 1 or args.warmup < 0:
        parser.error("docs and iterations must be positive; warmup cannot be negative")

    # The API requires a signing secret even for this disposable local app.
    # Generate an ephemeral key only when no caller-provided value exists.
    os.environ.setdefault("JWT_SECRET", secrets.token_hex(32))

    try:
        import fakeredis
        from fastapi.testclient import TestClient
        from src.api.main import create_api_app
        from src.api.routers import search_index
        from src.search_index.models import Doc
        from src.search_index.store import DocStore
    except ImportError as exc:
        parser.error(f"missing API/development dependency: {exc.name}; install requirements.txt")

    with tempfile.TemporaryDirectory(prefix="wcp-studio-api-bench-") as tmp:
        db_path = Path(tmp) / "docs.sqlite3"
        store = DocStore(db_path)
        build_started = time.perf_counter()
        batch: list[Doc] = []
        for i in range(args.docs):
            batch.append(Doc(
                url=f"https://host-{i % 500}.example/{i}", title=f"Page {i}",
                source=("wiki", "curlie", "commoncrawl")[i % 3],
                authority=(i % 100) / 10, quality=(i % 10) / 10,
                lang=("en", "fr")[i % 2], body="sample indexed body text " * 4,
            ))
            if len(batch) == 1000:
                store.add_batch(batch)
                batch.clear()
        if batch:
            store.add_batch(batch)
        build_seconds = time.perf_counter() - build_started
        store.close()

        # _index is the app's normal lazy index-provider hook. For this harness,
        # point it at the generated DocStore so the actual authenticated route
        # runs without loading the search shards or contacting external services.
        analytics_store = DocStore(db_path, readonly=True)
        search_index._index = lambda _request: analytics_store
        redis = fakeredis.FakeStrictRedis(decode_responses=True)
        app = create_api_app(redis_client=redis, enable_graphql=False)
        profiles = {
            "source_count": {"group_by": "source", "metric": "count", "limit": 20},
            "host_mean_authority": {"group_by": "host", "metric": "avg_authority", "limit": 50},
            "filtered_host_mean_quality": {
                "group_by": "host", "metric": "avg_quality", "source": "wiki", "limit": 50},
        }
        results = {}
        filtered_matches = (args.docs + 2) // 3
        expected_profiles = {
            "source_count": (args.docs, min(3, args.docs), 20),
            "host_mean_authority": (args.docs, min(500, args.docs), 50),
            "filtered_host_mean_quality": (
                filtered_matches, min(500, filtered_matches), 50),
        }
        try:
            with TestClient(app) as client:
                email = f"studio-bench-{uuid.uuid4().hex[:12]}@example.test"
                signup = client.post("/auth/signup", json={
                    "org_name": "Synthetic Studio Benchmark", "email": email,
                    "password": "benchmark-only-password-123"})
                if signup.status_code != 200:
                    raise RuntimeError(f"benchmark signup failed: HTTP {signup.status_code}")
                key_response = client.post(
                    "/keys", headers={"Authorization": f"Bearer {signup.json()['access_token']}"},
                    json={"name": "studio-benchmark", "scopes": ["read"]})
                if key_response.status_code != 200:
                    raise RuntimeError(
                        f"benchmark API key setup failed: HTTP {key_response.status_code}")
                headers = {"X-API-Key": key_response.json()["key"]}

                for name, params in profiles.items():
                    durations: list[float] = []
                    last_payload = None
                    for iteration in range(args.warmup + args.iterations):
                        started = time.perf_counter()
                        response = client.get("/api/v1/studio/analytics",
                                              headers=headers, params=params)
                        elapsed_ms = (time.perf_counter() - started) * 1000
                        if response.status_code != 200:
                            raise RuntimeError(
                                f"{name} returned HTTP {response.status_code}: "
                                f"{response.text[:200]}")
                        last_payload = response.json()
                        if iteration >= args.warmup:
                            durations.append(elapsed_ms)
                    expected_matches, expected_groups, expected_limit = expected_profiles[name]
                    expected_rows = min(expected_groups, expected_limit)
                    actual = (last_payload["matched_documents"], last_payload["group_count"],
                              len(last_payload["rows"]))
                    if actual != (expected_matches, expected_groups, expected_rows):
                        raise RuntimeError(
                            f"{name} returned unexpected benchmark result counts: {actual}")
                    results[name] = {
                        "matched_documents": last_payload["matched_documents"],
                        "groups": last_payload["group_count"],
                        "response_rows": len(last_payload["rows"]),
                        "p50_ms": round(statistics.median(durations), 3),
                        "p95_ms": round(percentile(durations, 0.95), 3),
                        "mean_ms": round(statistics.mean(durations), 3),
                    }
        finally:
            analytics_store.close()

        payload = {
            "benchmark": "synthetic in-process FastAPI Data Studio aggregate requests",
            "python": sys.version.split()[0], "platform": platform.platform(),
            "documents": args.docs, "iterations_per_profile": args.iterations,
            "warmup_per_profile": args.warmup,
            "sqlite_bytes": store.size_bytes(),
            "build_seconds": round(build_seconds, 3),
            "profiles": results,
            "limitations": [
                "in-process TestClient; no TCP/TLS or reverse proxy",
                "synthetic SQLite corpus; no production Redis",
                "single sequential client; no concurrent load",
                "warm-up requests; local cache state",
                "not an SLO or production capacity estimate",
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
