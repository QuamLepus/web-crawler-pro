# Grounded intelligence and provenance

This increment adds two building blocks without replacing the FastAPI/Jinja2
architecture:

* `POST /api/v1/ask` (and the existing `/v1/ask` compatibility prefix) answers
  from the authenticated tenant's existing search index, returns source
  citations and a heuristic confidence indicator, and does **not** start a
  crawl when no context exists. Example:

  ```sh
  curl -X POST http://localhost:8000/api/v1/ask \
    -H 'X-API-Key: YOUR_KEY' -H 'Content-Type: application/json' \
    -d '{"question":"What changed in smartphone pricing?","budget":{"max_pages":50,"max_seconds":60}}'
  ```

  The budget is a ceiling reserved for future retrieval expansion, not a claim
  that the endpoint crawls those pages. `cost_usd` is null because no measured
  cost model exists. Confidence is an explicitly heuristic coverage signal,
  not calibrated probability. Current answers use extractive retrieval, not
  model-verified synthesis.

* `src.api.provenance` produces JSON-ready data passports and signed dataset
  manifests. The manifest uses HMAC-SHA256; configure and protect the signing
  key outside the repository. HMAC establishes integrity only for parties
  sharing the secret, and does not establish source licensing or independent
  non-repudiation. License and robots values default to `unknown` rather than
  making unsupported assumptions. Callers must persist passports alongside
  their collected records; this change does not retroactively annotate data.

## Asynchronous topic research

`POST /api/v1/research` creates a queued tenant-scoped research job with a
source/page budget. Workers run query planning and topic discovery, crawl the
selected candidate pages using the existing `obey_robots` crawl path, and store
an extractive report in Redis. Poll `GET /api/v1/research/{research_id}` or
list jobs with `GET /api/v1/research`. The console offers this under
**Intelligence → Research reports**. Celery workers and Redis must be running;
source discovery needs one or more configured network providers.

Every report claim is a verbatim sentence from a crawled text record, with its
source URL, collection timestamp, and SHA-256 of the extracted text. Confidence
is a term-overlap heuristic, not calibrated probability. The source check only
verifies exact excerpt presence; it is not independent fact-checking, and this
version does not synthesize an LLM narrative. Failed domains are retained as
per-source crawl results while other sources can still contribute evidence.

## Persisted provenance ledger

The authenticated `POST /api/v1/provenance/records` stores caller-provided
passports in Redis under a tenant-specific key. `GET
/api/v1/provenance/records/{record_id}` reads only that tenant's record, and
`POST /api/v1/provenance/manifest` exports the tenant's stored records. Manifest
signing is disabled unless `WCP_PROVENANCE_SIGNING_KEY` is configured; use a
long random secret from a secret manager. Records are caller-supplied and this
ledger does not prove that a crawl happened or independently verify a source.

## Scheduled web watches

Authenticated `POST /api/v1/watch` creates a tenant watch, and `POST
/api/v1/watch/{watch_id}/run` queues an immediate, one-page crawl with
`obey_robots=true`. Celery Beat polls due watches every minute and dispatches
the same crawl path. Snapshots are hashed and change events are available from
`GET /api/v1/watch/{watch_id}/changes`; optional webhook subscribers can choose
`web.changed`. A first snapshot establishes a baseline. Price-like strings are
reported as text matches only—not verified prices—and ordinary content changes
are not semantically classified. Scheduled execution requires Redis, Celery
workers, and Beat. Watch API validation blocks credentials, obvious localhost,
private literal IPs, and `.local` names; this is not a complete defense against
DNS rebinding or unsafe redirects, so deployment network egress policy remains
necessary.

## Entity graph and Python SDK

The authenticated `/api/v1/graph/entities`, `/api/v1/graph/relations`, and
`/api/v1/graph/entities/{entity_id}` endpoints store a small Redis-backed graph
per tenant. Resolution only matches Unicode-normalized, case-folded exact names
and aliases; ambiguous aliases produce a conflict instead of silently merging.
This is a graph foundation, not an extraction pipeline or fuzzy resolver.

The Python SDK under `sdks/python/` currently supports `ask`, research jobs,
crawl status, and basic watch operations. Early TypeScript SDK and MCP packages
are under `sdks/typescript/` and `sdks/mcp/`.

```python
from webintelligence import Client

with Client(api_key="YOUR_API_KEY", base_url="https://your-host") as client:
    job = client.research.create("AI chip market 2026", sources=5,
                                 max_pages_per_source=3)
    report_job = client.research.get(job["research_id"])
    answer = client.ask("What do indexed sources say about the topic?")
    print(answer["citations"])
```

Install for local Python development with `pip install -e sdks/python`. For TypeScript SDK and MCP builds, run `cd sdks && npm install`; use `npm run test --workspace @webintelligence/sdk` and `npm run build --workspace @webintelligence/mcp-server`. Setup instructions and environment requirements are in the SDK-specific README files.

These APIs and helpers do not claim to deliver autonomous crawling, calibrated
confidence, legal advice, signed public-key attestations, or enterprise
certification. Keep source access policy and tenant isolation in the calling
workflow. Run the focused suite with `JWT_SECRET=test-only-secret-do-not-use-in-production .venv/bin/pytest tests/test_provenance.py tests/test_provenance_api.py tests/test_python_sdk.py tests/test_research_ui.py`.
