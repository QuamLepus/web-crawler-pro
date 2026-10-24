# Agent marketplace (catalog phase)

The initial agent marketplace provides a tenant-authenticated catalog for
versioned, declarative agent cards. It is deliberately separate from the
existing dataset marketplace.

## API

All routes are under `/api/v1/agent-marketplace` and require a tenant API key.

- `POST /agents` creates a draft card owned by the caller's tenant.
- `GET /my/agents` lists the caller's cards, with optional status, bounded limit,
  and offset.
- `PATCH /agents/{agent_id}` lets only the owner edit the manifest, tags,
  license, or visibility status.
- `GET /agents` searches published cards by name/summary substring and tag.
- `GET /agents/{agent_id}` returns published cards to other tenants and draft,
  suspended, or archived cards only to their owner.

A manifest declares a name, semantic version, summary, capability labels, and
bounded JSON input/output schemas. New cards start as drafts and are not visible
to other tenants until their owner publishes them. Manifests are limited to
16 KiB, each schema to 8 KiB, and tags/capabilities are bounded. Unknown manifest
fields are rejected, including fields that might otherwise imply a runtime or
publisher code payload.

## Console

Authenticated users can browse published cards, search by name/summary or tag,
create draft cards, edit their own listing metadata, publish or unpublish
cards, browse/search results and owner listings in 20-card pages, and inspect
listing metadata plus declared input/output schemas at `/app/agents`. The
console caps its UI offsets at the API's 5,000-item bound. These console actions
call the catalog API using the
user's session. The UI does not accept code uploads or execute publisher code;
editing a card's version is metadata-only and does not provision a backend.

## Managed server-hosted agent

The first server-managed agent is `data-curator`:

- `GET /managed/agents` lists the allow-listed server implementation.
- `POST /managed/agents/data-curator/runs` accepts 1–20 strings or JSON objects;
  each encoded record is limited to 8 KiB and the combined request records to
  40,000 bytes.
- The run uses the deterministic offline heuristic scorer, regardless of any
  configured model key. It reports scores and decisions, but not the submitted
  record contents. Inputs and outputs are not persisted by this path.

This is an illustrative heuristic, not a calibrated quality model or reliable
PII/safety detector. Its scores and decisions should not be treated as a
security, compliance, or content-moderation guarantee. API key authentication
and the normal API rate limits apply.

## Execution and marketplace boundary

Seller-published cards remain declarative metadata only; publishing a card does
not register code or grant execution. The managed endpoint uses an explicit
server-side allow-list and does not execute seller-provided content. This first
managed implementation is free and has no paid access or settlement path.

Publisher-supplied executable agents remain deferred until the WASM/WASI,
sandboxed-Kubernetes, and dedicated-microVM backends are implemented and each
validated against the launch gates in `docs/agent-runtime-security.md`. Each
agent version must pin one backend, with no automatic downgrade. The catalog
alone is not a trust or security review of a listing.
