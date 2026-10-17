#!/usr/bin/env python3
"""Reproducible synthetic SQLite benchmark for full-corpus Studio aggregates.

This measures the local DocStore aggregate path only. It is not an API,
concurrency, or production-capacity benchmark. The temporary database is removed
when the process exits.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.search_index.models import Doc
from src.search_index.store import DocStore


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
    parser.add_argument("--docs", type=int, default=50_000)
    parser.add_argument("--iterations", type=int, default=10)
    args = parser.parse_args()
    if args.docs < 1 or args.iterations < 1:
        parser.error("docs and iterations must be positive")

    with tempfile.TemporaryDirectory(prefix="wcp-studio-bench-") as tmp:
        store = DocStore(Path(tmp) / "docs.sqlite3")
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

        profiles = {
            "source_count": {"group_by": "source", "metric": "count"},
            "host_mean_authority": {"group_by": "host", "metric": "avg_authority"},
            "filtered_host_mean_quality": {
                "group_by": "host", "metric": "avg_quality", "source": "wiki"},
        }
        results = {}
        for name, profile in profiles.items():
            store.analytics(limit=50, **profile)  # warmup
            durations = []
            last = None
            for _ in range(args.iterations):
                started = time.perf_counter()
                last = store.analytics(limit=50, **profile)
                durations.append((time.perf_counter() - started) * 1000)
            results[name] = {
                "matched_documents": last["matched_documents"],
                "groups": last["group_count"],
                "p50_ms": round(statistics.median(durations), 3),
                "p95_ms": round(percentile(durations, 0.95), 3),
                "mean_ms": round(statistics.mean(durations), 3),
            }
        payload = {
            "benchmark": "synthetic SQLite DocStore full-corpus aggregates",
            "python": sys.version.split()[0], "platform": platform.platform(),
            "documents": args.docs, "iterations": args.iterations,
            "sqlite_bytes": store.size_bytes(),
            "build_seconds": round(build_seconds, 3),
            "profiles": results,
            "limitations": ["single process", "warm-cache", "no HTTP/API overhead",
                            "no concurrent load", "synthetic documents"],
        }
        store.close()
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
