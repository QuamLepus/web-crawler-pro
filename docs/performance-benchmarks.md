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
production latency.

### SQLite aggregate query retest (2026-10-23)

A same-sandbox before/after run of the harness motivated and evaluated a query
change: the aggregate values, matched-document count, and group count now come
from one grouped CTE rather than separate full-table count/group-count scans.
The endpoint still scans the matching SQLite rows and remains single-process;
this is not a claim about production or concurrent workloads. Each value below
is p50 / p95 milliseconds from 10 iterations at 50k rows and 5 at 500k rows.
The pre-change and post-change runs used the same Python 3.11.2 and Linux
6.1.158+ x86_64 / glibc 2.36 sandbox, but timing noise remains possible.

| Documents | Profile | Pre-change p50 / p95 | Post-change p50 / p95 |
|---:|---|---:|---:|
| 50,000 | Source document count | 27.193 / 30.180 | 14.596 / 15.368 |
| 50,000 | Host mean authority | 38.615 / 39.766 | 29.117 / 30.306 |
| 50,000 | Filtered host mean quality (source=wiki) | 24.828 / 25.292 | 15.922 / 23.971 |
| 500,000 | Source document count | 343.131 / 366.034 | 186.284 / 189.949 |
| 500,000 | Host mean authority | 411.713 / 418.245 | 335.557 / 363.511 |
| 500,000 | Filtered host mean quality (source=wiki) | 277.536 / 290.344 | 165.715 / 173.888 |

The post-change temporary database sizes were unchanged at 23,107,544 / 168,188,664
bytes; synthetic builds took 0.941 / 15.592 s. The optimization preserves
aggregate counts even on empty result pages; tests cover offsets beyond the last
group and filters matching no documents. The small iteration counts and
synthetic, warm-cache corpus mean these results should guide further measurement,
not be treated as an SLO or production capacity estimate.

## In-process Data Studio API baseline (2026-10-24)

Run the authenticated FastAPI/TestClient benchmark against a temporary synthetic
SQLite index with:

```sh
python scripts/bench-studio-api.py --docs 50000 --iterations 10 --warmup 2
```

Observed in this development sandbox using Python 3.11.2 on Linux 6.1.158+
x86_64 / glibc 2.36. The endpoint is called through the full in-process API
stack with an API key; the index is a synthetic local SQLite DocStore and the
benchmark supplies fakeredis. Results are p50 / p95 milliseconds:

| Profile | Matched docs / groups | Response rows | p50 / p95 |
|---|---:|---:|---:|
| Source document count | 50,000 / 3 | 3 | 19.463 / 21.963 |
| Host mean authority | 50,000 / 500 | 50 | 33.439 / 38.984 |
| Filtered host mean quality (source=wiki) | 16,667 / 500 | 50 | 18.102 / 51.843 |

Synthetic build took 0.723 s; the SQLite file was 15,736,832 bytes. Request
latency includes local TestClient/ASGI dispatch, API-key authentication,
middleware, the aggregate query, and JSON serialization. It excludes TCP/TLS,
reverse proxies, production Redis, browser work, and concurrent clients. The
small sequential sample is sensitive to local runtime noise (notably the
filtered profile's p95) and should be compared only with matching conditions;
it is an API-path baseline, not a production capacity claim or SLO. The script
prints its environment, query profiles, and limitations as JSON and removes
the temporary database on exit.
