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

Available methods cover read-only billing plans/usage/current-period invoice estimate, platform search-index build levels, sanitized stats, spelling suggestions and prebuilt-index queries, indexed `ask`, tenant-scoped semantic search and RAG, discovery preview and human-reviewed job create/list/status/candidates/drop/cancel/approve/results (approval starts crawls for explicitly selected domains), crawl create/list/status/cancel, research create/list/status/cancel,
watch list/create/run/delete/change and retained snapshot retrieval, workflow create/list/get/update/delete,
schedule/run/history/status/cancel, tenant knowledge-graph, read-only marketplace listing search/get and tenant-owned inventory reads, open-bounty listing and poster-scoped bounty submission reads, buyer-scoped delivery retrieval, paginated tenant-scoped purchase history, and paginated public reviews without buyer tenant IDs, provenance record/manifest, and webhook subscription/delivery operations, and Data Studio aggregate,
saved-query, and snapshot operations. Billing plan, current-period usage and invoice-estimate methods are read-only; the estimate is not a Stripe-issued invoice, and these methods do not create checkout sessions or change a plan. Marketplace search exposes published listings only; listing reads are limited to published datasets or drafts owned by the authenticated tenant. Open-bounty listing, buyer-scoped unexpired delivery retrieval, and visible-dataset review listing are read-only; review responses omit buyer tenant IDs, and the SDK does not submit or award bounty work. It exposes no marketplace purchase method. Webhook creation returns the signing secret once; store it securely and avoid logging it.
Watch change/snapshot reads accept optional limits (up to 100 changes or 20 snapshots).
Workflow step IDs and `dependsOn` dependencies enable bounded DAG branching; nodes
still run serially, failed nodes skip descendants, and independent branches continue.
`ask` searches the tenant's existing index and does not start a crawl. `searchOwnIndex` searches the platform's prebuilt public-source index, not tenant documents; results may contain licensed snippets, so follow source-specific reuse and attribution terms. Crawl-job cancellation
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
