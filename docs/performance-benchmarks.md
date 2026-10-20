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

### Sandbox retest (2026-10-21)

The same harness was rerun in the current sandbox at both 50,000 documents
(10 iterations) and 500,000 documents (5 iterations). It reported Python 3.11.2
on Linux 6.1.158+ x86_64 / glibc 2.36. Results are p50 / p95 milliseconds:

| Documents | Profile | Matched docs / groups | p50 / p95 |
|---:|---|---:|---:|
| 50,000 | Source document count | 50,000 / 3 | 26.642 / 27.137 ms |
| 50,000 | Host mean authority | 50,000 / 500 | 38.219 / 40.982 ms |
| 50,000 | Filtered host mean quality (source=wiki) | 16,667 / 500 | 23.990 / 25.716 ms |
| 500,000 | Source document count | 500,000 / 3 | 364.122 / 375.051 ms |
| 500,000 | Host mean authority | 500,000 / 500 | 436.078 / 443.870 ms |
| 500,000 | Filtered host mean quality (source=wiki) | 166,667 / 500 | 288.163 / 293.333 ms |

The 50k/500k temporary databases were 23,107,544 / 168,188,664 bytes and
synthetic builds took 0.776 / 14.108 s. These remain warm-cache, single-process
DocStore-only numbers, not API or production-capacity results. In particular,
the 500k p95 uses only five measured iterations and is a rough summary, not a
tail-latency estimate. The larger run shows sub-second aggregates at half a
million synthetic rows in this sandbox, but is not enough evidence to promise
production latency or justify an optimization without workload-specific
measurements.
