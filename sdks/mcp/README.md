# MCP server

This stdio MCP server exposes Web Intelligence OS APIs as tools for MCP-compatible
clients. Requires Node.js 18+, a reachable Web Intelligence OS API, and an API
key with appropriate permissions.

Tools: `wio_ask`, `wio_billing_invoice_estimate`, `wio_billing_plans`, `wio_billing_usage`, `wio_rag`, `wio_search`, `wio_search_index_levels`, `wio_search_index_query`, `wio_search_index_spell`, `wio_search_index_stats`, `wio_research_create`, `wio_research_list`, `wio_research_status`, `wio_research_cancel`,
`wio_crawl_create`, `wio_crawl_list`, `wio_crawl_status`, `wio_crawl_cancel`,
`wio_discovery_create`, `wio_discovery_list`, `wio_discovery_status`, `wio_discovery_candidates`, `wio_discovery_candidate_drop`, `wio_discovery_cancel`, `wio_discovery_approve`, `wio_discovery_results`, `wio_discovery_preview`,
`wio_graph_list`, `wio_graph_entity_upsert`, `wio_graph_entity_get`, `wio_graph_relation_add`, `wio_marketplace_bounties_list`, `wio_marketplace_bounty_submissions_list`, `wio_marketplace_dataset_search`, `wio_marketplace_dataset_get`, `wio_marketplace_my_datasets_list`, `wio_marketplace_delivery_get`, `wio_marketplace_purchases_list`, `wio_marketplace_reviews_list`, `wio_provenance_record_store`, `wio_provenance_record_get`, `wio_provenance_manifest_export`, `wio_watch_create`, `wio_watch_list`, `wio_watch_run`, `wio_watch_changes`, `wio_watch_snapshots`, `wio_watch_delete`,
`wio_workflow_create`, `wio_workflow_list`, `wio_workflow_get`,
`wio_workflow_update`, `wio_workflow_delete`, `wio_workflow_runs`,
`wio_workflow_run`, `wio_workflow_status`, `wio_workflow_cancel`,
`wio_webhook_create`, `wio_webhook_list`, `wio_webhook_delete`, `wio_webhook_test`, `wio_webhook_deliveries`,
`wio_studio_analytics`, `wio_studio_query_save`, `wio_studio_query_list`, `wio_studio_query_delete`, `wio_studio_snapshot_create`, `wio_studio_snapshot_list`, `wio_studio_snapshot_get`, and `wio_studio_snapshot_delete`. Discovery creation queues candidate generation for human review (the requested count consumes quota up front); it does not crawl sites until separately approved. The MCP tool keeps robots.txt enabled. Only `wio_discovery_approve` starts crawls, and it requires an explicit list of shortlisted domains (maximum 50); review `wio_discovery_candidates` before approval. `wio_discovery_preview` queries configured discovery providers without creating a job; provider unavailability can yield no candidates. Billing plans are public; usage is the authenticated tenant's current-period summary. The invoice tool returns the API's current-period estimate, not a Stripe-issued invoice. These are read-only and do not alter a plan, start checkout, or collect payment. Marketplace listing search returns published datasets only; listing reads expose only published datasets or drafts owned by the authenticated tenant; the seller inventory tool lists all of the authenticated tenant's listings with optional status filtering and pagination. Open-bounty listing is read-only and paginated; the submission-list tool returns submitted dataset IDs only to the authenticated bounty poster. No purchase, submission, or award tool is exposed. Delivery manifests can be re-read only by their buyer tenant and are subject to the API's expiry. The purchase-history tool returns bounded pages of records belonging only to the authenticated tenant. Public review reads return bounded pages only for published datasets (or the authenticated tenant's own listings) and omit buyer tenant IDs. Provenance records store caller-supplied metadata only; they do not fetch URLs or infer permissions. Manifest export requires the API's signing key to be configured. Knowledge-graph entities and relations are tenant-scoped; alias resolution is normalized exact matching, not fuzzy or AI resolution. Graph list reads return at most 500 entities. Saved queries are tenant-scoped and allow result limits from 1 to 100. `wio_ask` and `wio_rag` accept retrieval limits from 1 to 20; RAG uses an extractive fallback when no LLM is configured and is not independent fact-checking. `wio_search` supports up to 100 results and at most 50 scalar metadata filters. `wio_search_index_levels` returns the platform index's configured roadmap/cost tiers only; it does not query the index. `wio_search_index_stats` reports availability and build metrics without internal filesystem paths or document content. `wio_search_index_query` searches the platform's prebuilt index (not tenant documents), with up to 100 results; output snippets may carry source-specific license and attribution requirements. `wio_search_index_spell` suggests query corrections using the index vocabulary and requires a built index; it never returns document content. Webhook creation returns its signing secret once; store it securely and do not share or log it. Test-event payloads are limited to 64 KiB, and delivery history reads to 200 attempts. Snapshot creation is capped at 100 rows and 1 MB by the API. Watch change reads accept a limit up to 100;
snapshot reads are capped at 20. Research cancellation
stops at source boundaries, so an active source crawl may finish first. Workflow
tools operate on bounded sequential or dependency-DAG crawl recipes; DAG steps run serially,
and failed steps skip descendants while independent branches continue. Workflow
cancellation takes effect after the active crawl step completes. Creation can optionally schedule runs (minimum 60 minutes),
which requires Celery Beat and workflow workers to be running.

```sh
cd sdks
npm install
WCP_API_KEY='your-api-key' WCP_BASE_URL='http://localhost:8000' \
  npm run build --workspace @webintelligence/mcp-server
WCP_API_KEY='your-api-key' WCP_BASE_URL='http://localhost:8000' \
  npm run start --workspace @webintelligence/mcp-server
```

Configure the client to launch `node` with `sdks/mcp/dist/server.js` as a stdio
MCP process, passing `WCP_API_KEY` and `WCP_BASE_URL` through its environment.
Do not place secrets in checked-in client configuration. The API key remains in
the MCP process and is never returned in tool output. Research reports are
extractive and citations are supplied by the backend; this server does not add
independent fact verification.
