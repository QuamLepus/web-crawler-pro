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

Available methods cover indexed `ask` and tenant-scoped semantic search, crawl create/list/status/cancel, research create/list/status/cancel,
watch list/create/run/delete/change and retained snapshot retrieval, workflow create/list/get/update/delete,
schedule/run/history/status/cancel, webhook subscription and delivery operations, and Data Studio aggregate,
saved-query, and snapshot operations. Webhook creation returns the signing secret once; store it securely and avoid logging it.
Watch change/snapshot reads accept optional limits (up to 100 changes or 20 snapshots).
Workflow step IDs and `dependsOn` dependencies enable bounded DAG branching; nodes
still run serially, failed nodes skip descendants, and independent branches continue.
`ask` searches the existing index and does not start a crawl. Crawl-job cancellation
is cooperative; an active page may finish before cancellation takes effect. Research
cancellation stops between source crawls, so an active source may finish. Workflow
cancellation is cooperative between crawl steps; recurring workflows support intervals of at
least 60 minutes and require Celery Beat/workers. Snapshots hold at most 100 result
rows. The SDK does not retry automatically, so callers can choose retry behavior
appropriate to their environment.

For local development from the repository root:

```sh
cd sdks
npm install
npm run test --workspace @webintelligence/sdk
```
