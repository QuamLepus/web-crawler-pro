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
