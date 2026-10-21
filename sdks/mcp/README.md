# MCP server

This stdio MCP server exposes Web Intelligence OS APIs as tools for MCP-compatible
clients. Requires Node.js 18+, a reachable Web Intelligence OS API, and an API
key with appropriate permissions.

Tools: `wio_ask`, `wio_research_create`, `wio_research_list`, `wio_research_status`, `wio_research_cancel`,
`wio_crawl_create`, `wio_crawl_list`, `wio_crawl_status`, `wio_crawl_cancel`, `wio_watch_create`, `wio_watch_changes`, `wio_watch_snapshots`, `wio_watch_delete`,
`wio_workflow_create`, `wio_workflow_list`, `wio_workflow_get`,
`wio_workflow_update`, `wio_workflow_delete`, `wio_workflow_runs`,
`wio_workflow_run`, `wio_workflow_status`, `wio_workflow_cancel`,
`wio_studio_analytics`, and `wio_studio_snapshot_get`. Research cancellation
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
