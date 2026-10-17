# Web Intelligence OS implementation status

This status describes the code in this repository. It is not a promise that the
full long-term product plan is already shipped.

| Area | Current status | Important limitation |
|---|---|---|
| Topic discovery, crawling, search, APIs, web console | Existing foundation | Existing project behavior; not re-certified by the incremental tests in this change. |
| Grounded `/ask` and citations | Implemented against existing tenant index | Extractive answer only; no autonomous discovery/crawl, LLM verification, or calibrated confidence. |
| Provenance ledger and manifest | Implemented with tenant-keyed Redis storage; optional HMAC export | Caller supplies provenance; older data is not backfilled; HMAC is not a public-key signature. |
| Knowledge graph | Redis entity/relationship endpoints and exact Unicode/case-normalized aliases | Not fuzzy/AI entity resolution; no graph visualization or automated extraction. |
| Python SDK | Early installable package for ask, crawl/status, and watch operations | No TypeScript SDK yet; limited endpoint coverage. |
| Research console page | Implemented as a UI over `/ask` | It is not a multi-stage autonomous research agent. |
| Web change monitoring | Scheduled URL watches, one-page crawl snapshots, content hashes, change history, basic text-based price mentions, and `web.changed` webhook event | Not semantic change classification; no DNS-rebinding defense, structured product matching, version replay UI, or customer-specific schedules beyond the minimum interval. Celery Beat and workers must run. |
| Workflow builder, Data Studio, agent marketplace | Not implemented | Existing dataset marketplace is separate. |
| Enterprise connectors, SSO, compliance | Not implemented as a complete offering | Do not claim SOC 2/ISO certification or enterprise-ready private integrations. |
| Performance targets | Documented as targets | No new latency, LCP, CLS, throughput, or cost benchmarks were measured here. |
| TypeScript SDK, MCP server | Not implemented | Requires separate packaging, protocol tests, and release work. |

The implemented endpoints are tenant authenticated and tested with fakeredis.
Production deployments must configure Redis and protect
`WCP_PROVENANCE_SIGNING_KEY` using their secret-management system. The signing
key is intentionally optional: manifest export returns `503` if it is absent.
