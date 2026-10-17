# Performance benchmark notes

## Synthetic own-index query baseline

Run locally with:

```sh
python scripts/bench-search-index.py --docs 1000 --iterations 30 --warmup 5 --shard-docs 250
```

Observed in this development sandbox on 2026-10-18:

| Measure | Result |
|---|---:|
| Python / platform | 3.11.2 / Linux x86_64, glibc 2.36 |
| Synthetic indexed documents / shards | 1,000 / 4 |
| Index build time | 0.133 s |
| Query p50 / p95 / p99 | 3.452 / 3.585 / 3.603 ms |
| Mean query time | 3.469 ms |
| Approximate sequential query throughput | 288.17 queries/s |
| Result count | 10 for each of three query profiles |
| Query configuration | result limit 10, BM25, spelling and reranking disabled |

This is an actual local run of the checked-in harness, but only a small,
deterministic synthetic microbenchmark. It does not measure API/network overhead,
large production corpora, cold-cache behavior, concurrent load, semantic
reranking, browser LCP/CLS, or cloud costs. Do not interpret it as an SLO or
production capacity claim. Re-run on the target machine and compare only runs
with the same corpus, parameters, software, and hardware. The script emits JSON
including its runtime environment and full parameters for reproducibility.

## Full-corpus Data Studio aggregate baseline

Run the checked-in synthetic SQLite aggregate harness with:

```sh
python scripts/bench-studio-analytics.py --docs 50000 --iterations 10
```

Observed in the same development sandbox on 2026-10-18:

| Profile | Matched docs / groups | p50 / p95 |
|---|---:|---:|
| Source document count | 50,000 / 3 | 23.590 / 25.297 ms |
| Host mean authority | 50,000 / 500 | 34.332 / 38.694 ms |
| Filtered host mean quality (source=wiki) | 16,667 / 500 | 22.129 / 23.129 ms |

The temporary SQLite file was 23,107,544 bytes; synthetic data build took
0.689 s. These are warm-cache, single-process DocStore measurements on 50,000
synthetic rows; they exclude HTTP/UI overhead and concurrency and must not be
extrapolated to production-scale corpora. The harness reports its environment
and limitations as JSON and removes the temporary database after each run.
