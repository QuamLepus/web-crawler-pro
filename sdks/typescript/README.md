# TypeScript SDK

Install from this repository workspace after the package is published or use a
local workspace link during development. Requires Node.js 18+ (uses global
`fetch`).

```ts
import { WebIntelligenceClient } from "@webintelligence/sdk";

const client = new WebIntelligenceClient({
  apiKey: process.env.WCP_API_KEY!,
  baseUrl: "https://your-web-intelligence-host",
});

const job = await client.createResearch({ topic: "AI chip market 2026", sources: 5 });
const answer = await client.ask("What do indexed sources say about AI chips?");
console.log(job.research_id, answer.citations);
```

Available methods cover indexed `ask`, crawl create/status, research create/status,
watch create/run/change retrieval, sequential workflow create/run/status/cancel,
and bounded Data Studio snapshot create/list/get/delete. `ask` searches the existing
index and does not start a crawl. Workflow cancellation is cooperative between
crawl steps; recurring workflows support intervals of at least 60 minutes and
require Celery Beat/workers. Snapshots hold at most 100 result rows. The SDK does not retry
automatically, so callers can choose retry behavior appropriate to their environment.

For local development from the repository root:

```sh
cd sdks
npm install
npm run test --workspace @webintelligence/sdk
```
