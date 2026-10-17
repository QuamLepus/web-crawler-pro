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

## Persisted provenance ledger

The authenticated `POST /api/v1/provenance/records` stores caller-provided
passports in Redis under a tenant-specific key. `GET
/api/v1/provenance/records/{record_id}` reads only that tenant's record, and
`POST /api/v1/provenance/manifest` exports the tenant's stored records. Manifest
signing is disabled unless `WCP_PROVENANCE_SIGNING_KEY` is configured; use a
long random secret from a secret manager. Records are caller-supplied and this
ledger does not prove that a crawl happened or independently verify a source.

## Entity graph and Python SDK

The authenticated `/api/v1/graph/entities`, `/api/v1/graph/relations`, and
`/api/v1/graph/entities/{entity_id}` endpoints store a small Redis-backed graph
per tenant. Resolution only matches Unicode-normalized, case-folded exact names
and aliases; ambiguous aliases produce a conflict instead of silently merging.
This is a graph foundation, not an extraction pipeline or fuzzy resolver.

The installable SDK under `sdks/python/` currently supports `ask`, crawl job
creation, and job retrieval:

```python
from webintelligence import Client

with Client(api_key="YOUR_API_KEY", base_url="https://your-host") as client:
    answer = client.ask("What do indexed sources say about the topic?")
    print(answer["citations"])
```

Install for local development with `pip install -e sdks/python`.

These APIs and helpers do not claim to deliver autonomous crawling, calibrated
confidence, legal advice, signed public-key attestations, or enterprise
certification. Keep source access policy and tenant isolation in the calling
workflow. Run the focused suite with `JWT_SECRET=test-only-secret-do-not-use-in-production .venv/bin/pytest tests/test_provenance.py tests/test_provenance_api.py tests/test_python_sdk.py tests/test_research_ui.py`.
