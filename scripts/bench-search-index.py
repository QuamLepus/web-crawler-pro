#!/usr/bin/env python3
"""Build a deterministic synthetic own-index corpus and measure local query latency.

No network access or external provider is used. Results are a reproducible
microbenchmark, not a production SLO: compare only runs with matching hardware,
Python, corpus size, shard size, query set, and reranker configuration.

    python scripts/bench-search-index.py --docs 10000 --iterations 200
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

from src.search_index.indexer import BuildOptions, SearchIndexBuilder
from src.search_index.models import Doc
from src.search_index.query import SearchIndex

QUERIES = (
    "solar energy storage",
    "machine learning research",
    "common crawl indexing",
)


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * p
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def docs(count: int):
    terms = (
        "solar energy storage machine learning research data indexing "
        "common crawl public science renewable computing university "
        "analysis systems web discovery engineering"
    )
    for i in range(count):
        host = ("lab.example", "journal.example", "archive.example")[i % 3]
        source = ("wikipedia", "curlie", "commoncrawl")[i % 3]
        yield Doc(
            url=f"https://{host}/papers/{i}",
            title=f"Research paper {i}: {terms.split()[i % 15]}",
            snippet=f"Synthetic indexed summary {i} about {terms}.",
            body=(terms + " ") * (1 + i % 3),
            source=source,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=int, default=5000)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--shard-docs", type=int, default=1000)
    args = parser.parse_args()
    if args.docs < 100 or args.iterations < 1 or args.warmup < 0 or args.shard_docs < 1:
        parser.error("docs >= 100, iterations >= 1, warmup >= 0 and shard-docs >= 1 required")

    with tempfile.TemporaryDirectory(prefix="wcp-search-bench-") as temp:
        data_dir = Path(temp) / "data"
        options = BuildOptions(level=0, data_dir=data_dir, sources=("seed",),
                               shard_docs=args.shard_docs, download=False,
                               quality_filter=False, authority=False,
                               keep_body=True, incremental=False)
        start = time.perf_counter()
        with SearchIndexBuilder(data_dir, options=options) as builder:
            written = builder.add_documents(docs(args.docs), source="seed")
            stats = builder.stats()
        build_seconds = time.perf_counter() - start
        index = SearchIndex(data_dir / "index", collapse_host=False,
                            spell=False, rerank="none")
        try:
            query_match_counts = {
                query: len(index.search(query, 10, spell=False, rerank="none",
                                        collapse_host=False))
                for query in QUERIES
            }
            if any(count == 0 for count in query_match_counts.values()):
                raise RuntimeError(f"benchmark fixture query returned no hits: {query_match_counts}")
            for i in range(args.warmup):
                index.search(QUERIES[i % len(QUERIES)], 10, spell=False,
                             rerank="none", collapse_host=False)
            latencies: list[float] = []
            measured_wall_start = time.perf_counter()
            for i in range(args.iterations):
                query = QUERIES[i % len(QUERIES)]
                before = time.perf_counter()
                index.search(query, 10, spell=False, rerank="none",
                             collapse_host=False)
                latencies.append((time.perf_counter() - before) * 1000)
            measured_wall_seconds = time.perf_counter() - measured_wall_start
            payload = {
                "benchmark": "synthetic own-search-index query latency",
                "network": "not used",
                "environment": {"python": platform.python_version(),
                                "platform": platform.platform()},
                "corpus": {"requested_docs": args.docs, "indexed_docs": written,
                           "shards": stats.shards, "shard_docs": args.shard_docs},
                "query": {"iterations": args.iterations, "warmup": args.warmup,
                          "queries": len(QUERIES), "limit": 10,
                          "reranker": "none", "spell": False,
                          "p50_ms": round(statistics.median(latencies), 3),
                          "p95_ms": round(percentile(latencies, 0.95), 3),
                          "p99_ms": round(percentile(latencies, 0.99), 3),
                          "mean_ms": round(statistics.mean(latencies), 3),
                          "queries_per_second": round(args.iterations / measured_wall_seconds, 2)
                          if measured_wall_seconds else None,
                          "match_counts_per_query": query_match_counts},
                "build_seconds": round(build_seconds, 3),
                "caveat": "Synthetic local microbenchmark; not a production SLO or representative web corpus.",
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        finally:
            index.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
