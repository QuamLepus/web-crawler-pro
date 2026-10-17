# MCP server

This stdio MCP server exposes Web Intelligence OS APIs as tools for MCP-compatible
clients. Requires Node.js 18+, a reachable Web Intelligence OS API, and an API
key with appropriate permissions.

Tools: `wio_ask`, `wio_research_create`, `wio_research_status`,
`wio_crawl_create`, `wio_watch_create`, and `wio_watch_changes`.

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
