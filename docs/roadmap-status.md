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
| Data Studio | Authenticated own-index query explorer with bounded preview, CSV export, tenant-scoped saved queries, and host/source breakdowns over the returned result sample | Exports are capped at 100 matches; breakdowns describe returned hits only, not corpus-wide aggregates. No arbitrary SQL, full chart builder, or persisted datasets. |
| Workflow builder | Early tenant-scoped sequential crawl recipes with create/list/run/status APIs, a console form, Celery execution, per-step progress, robots checks, and request-budget accounting | Crawl-only (up to 10 steps / 25 pages each); no DAG branching, arbitrary code, schedules, editing/deletion, or cancellation. Requires configured Celery workers. |
| Agent marketplace | Not implemented | Existing dataset marketplace is separate. |
| Enterprise connectors, SSO, compliance | Not implemented as a complete offering | Do not claim SOC 2/ISO certification or enterprise-ready private integrations. |
| Performance measurement | Reproducible synthetic own-index query benchmark and a small sandbox baseline are recorded | No production-scale, concurrent/API/browser, reranking, LCP/CLS, or cloud-cost benchmarks; targets remain unverified. |
| TypeScript SDK + MCP server | Early packages expose ask, research, crawl and watch operations; CI builds/tests both and checks package tarballs; MCP stdio protocol smoke tests cover initialize/list/call/error paths | Not published to npm; MCP HTTP/API integration coverage and SDK endpoint coverage remain limited; no stable compatibility/release policy yet. |

The implemented endpoints are tenant authenticated and tested with fakeredis.
Production deployments must configure Redis and protect
`WCP_PROVENANCE_SIGNING_KEY` using their secret-management system. The signing
key is intentionally optional: manifest export returns `503` if it is absent.
