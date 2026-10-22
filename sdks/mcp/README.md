# MCP server

This stdio MCP server exposes Web Intelligence OS APIs as tools for MCP-compatible
clients. Requires Node.js 18+, a reachable Web Intelligence OS API, and an API
key with appropriate permissions.

Tools: `wio_ask`, `wio_search`, `wio_research_create`, `wio_research_list`, `wio_research_status`, `wio_research_cancel`,
`wio_crawl_create`, `wio_crawl_list`, `wio_crawl_status`, `wio_crawl_cancel`, `wio_watch_create`, `wio_watch_list`, `wio_watch_run`, `wio_watch_changes`, `wio_watch_snapshots`, `wio_watch_delete`,
`wio_workflow_create`, `wio_workflow_list`, `wio_workflow_get`,
`wio_workflow_update`, `wio_workflow_delete`, `wio_workflow_runs`,
`wio_workflow_run`, `wio_workflow_status`, `wio_workflow_cancel`,
`wio_webhook_create`, `wio_webhook_list`, `wio_webhook_delete`, `wio_webhook_test`, `wio_webhook_deliveries`,
`wio_studio_analytics`, `wio_studio_query_save`, `wio_studio_query_list`, `wio_studio_query_delete`, `wio_studio_snapshot_create`, `wio_studio_snapshot_list`, `wio_studio_snapshot_get`, and `wio_studio_snapshot_delete`. Saved queries are tenant-scoped and allow result limits from 1 to 100. `wio_ask` accepts a `top_k` limit from 1 to 20; `wio_search` supports up to 100 results and at most 50 scalar metadata filters. Webhook creation returns its signing secret once; store it securely and do not share or log it. Test-event payloads are limited to 64 KiB, and delivery history reads to 200 attempts. Snapshot creation is capped at 100 rows and 1 MB by the API. Watch change reads accept a limit up to 100;
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
