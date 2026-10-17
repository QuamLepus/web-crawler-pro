# Web Intelligence OS implementation status

This status describes the code in this repository. It is not a promise that the
full long-term product plan is already shipped.

| Area | Current status | Important limitation |
|---|---|---|
| Topic discovery, crawling, search, APIs, web console | Existing foundation | Existing project behavior; not re-certified by the incremental tests in this change. |
| Grounded `/ask` and citations | Implemented against existing tenant index | Extractive answer only; not an autonomous crawl or independently verified answer. |
| Topic-to-report research | Async discovery + crawl jobs produce verbatim, timestamped, hashed excerpts with per-claim citations | Not LLM synthesis, independent source verification, or calibrated confidence; needs Celery workers/Beat and discovery providers. |
| Provenance ledger and manifest | Implemented with tenant-keyed Redis storage; optional HMAC export | Caller supplies provenance; older data is not backfilled; HMAC is not a public-key signature. |
| Knowledge graph | Tenant-isolated Redis entity/relationship API, bounded graph listing, and console canvas explorer | Exact alias resolution only; no fuzzy/AI resolution or automated extraction. |
| Python SDK | Early installable package for ask, crawl/status, watch, and research operations | Limited endpoint coverage; release and compatibility policy remain early. |
| Research console page | Implemented as a UI over `/ask` | It is not a multi-stage autonomous research agent. |
| Web change monitoring | Scheduled URL watches, one-page crawl snapshots, content hashes, change history, basic text-based price mentions, and `web.changed` webhook event | Not semantic change classification; no DNS-rebinding defense, structured product matching, version replay UI, or customer-specific schedules beyond the minimum interval. Celery Beat and workers must run. |
| Data Studio | Authenticated own-index query explorer with bounded search preview/export, tenant-scoped saved queries and immutable result snapshots, plus full-corpus SQLite aggregates (source/host/language/snapshot/day; count and mean authority/quality/body length), filters, pagination, charts, and aggregate CSV | Full-corpus analysis returns aggregates rather than document rows (100 groups/page; offset capped at 5,000) and may scan SQLite; no arbitrary SQL or full-corpus materialization. Search-result snapshots cover at most 100 hits, expire after 90 days, and are capped per tenant/size. A 50k-row synthetic warm-cache baseline is recorded; production-scale analytics latency remains unverified. |
| Workflow builder | Tenant-scoped sequential crawl recipes with create/list/run/status APIs, console controls, Celery execution, per-step progress, robots checks, request-budget accounting, cooperative cancellation, and recurring schedules | Crawl-only (up to 10 steps / 25 pages each); no DAG branching, arbitrary code, or editing/deletion. Schedules require Celery Beat plus workers; cancellation waits for the active crawl step to finish. |
| Agent marketplace | Not implemented | Existing dataset marketplace is separate. |
| Enterprise connectors, SSO, compliance | Not implemented as a complete offering | Do not claim SOC 2/ISO certification or enterprise-ready private integrations. |
| Performance measurement | Reproducible synthetic own-index query benchmark and a small sandbox baseline are recorded | No production-scale, concurrent/API/browser, reranking, LCP/CLS, or cloud-cost benchmarks; targets remain unverified. |
| TypeScript SDK + MCP server | Early packages expose ask, research, crawl, watch, workflow control, and Studio snapshot operations; CI builds/tests both and checks package tarballs; MCP stdio protocol smoke tests cover initialize/list/call/error paths | Not published to npm; live API integration, complete endpoint coverage, and a stable compatibility/release policy remain. |

The implemented endpoints are tenant authenticated and tested with fakeredis.
Production deployments must configure Redis and protect
`WCP_PROVENANCE_SIGNING_KEY` using their secret-management system. The signing
key is intentionally optional: manifest export returns `503` if it is absent.
