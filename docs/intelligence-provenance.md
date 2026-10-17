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

These APIs and helpers do not claim to deliver external web crawling, calibrated
confidence, legal advice, signed public-key attestations, or enterprise
certification. Keep source access policy and tenant isolation in the calling
workflow. Run the focused suite with `python -m pytest tests/test_provenance.py`.
