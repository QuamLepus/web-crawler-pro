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

Available methods cover billing plans/usage, current or historical invoice estimates, and paid-plan checkout-session creation, platform search-index build levels, sanitized stats, spelling suggestions and prebuilt-index queries, indexed `ask`, tenant-scoped semantic search and RAG, discovery preview and job create/list/status/candidates/drop/cancel/approve/results (manual review remains the default; `createDiscovery(topic, { auto: true })` opts into automatic robots-aware crawling without a separate approval call; approval can start crawls for explicitly selected domains), crawl create/list/status/cancel, research create/list/status/cancel,
watch list/create/run/delete/change and retained snapshot retrieval, workflow create/list/get/update/delete,
schedule/run/history/status/cancel, tenant knowledge-graph, paginated marketplace listing search/get, explicit free-listing purchase, bounded sanitized crawl-sample previews (not dataset downloads), and tenant-owned inventory reads, `updateMarketplaceDataset()` for partial seller-owned listing updates, paginated open-bounty listing, `createMarketplaceBounty()` for dataset requests (the reward amount is recorded but not transferred), poster-visible bounty details and cancellation of the tenant's open bounties, poster-scoped bounty submission reads, `submitMarketplaceBounty()` for this tenant's published datasets, and `awardMarketplaceBounty()` for poster-only selection of a valid submitted dataset, buyer-scoped signed purchase-metadata retrieval and `downloadMarketplaceDelivery()` for linked artifacts (pass the manifest SHA-256 to verify returned bytes), paginated tenant-scoped purchase history, paginated public reviews without buyer tenant IDs, and `addMarketplaceReview()` to post a single purchase-verified review, provenance record/manifest, and webhook subscription/delivery operations, and Data Studio aggregate,
saved-query, and snapshot operations. Billing plan, current-period usage and invoice-estimate methods are read-only; `getBillingInvoiceEstimate(period?)` supports the current or a specified historical period, and its estimates are not Stripe-issued invoices. `createBillingCheckout(plan, successUrl, cancelUrl)` requests a paid-plan Stripe Checkout session and returns its URL. The API deployment needs Stripe configured with the matching price. Creating the session does not itself activate a subscription; activation follows the provider's verified webhook flow. The invoice estimate is not a Stripe-issued invoice. `getSsoOidcConfig()` reads public OIDC configuration and whether a client secret is configured without returning the secret; `listSsoAuditEvents(limit)` reads bounded audit events and relies on API owner/admin authorization. Marketplace search exposes published listings only; listing reads are limited to published datasets or drafts owned by the authenticated tenant. Open-bounty listing, buyer-scoped unexpired delivery retrieval, and visible-dataset review listing are read-only; review responses omit buyer tenant IDs. `addMarketplaceReview()` requires a purchase by this tenant and permits one review per dataset (rating 1–5; comment 5–2,000 characters). `purchaseFreeMarketplaceDataset()` explicitly records a purchase of an available free listing and returns signed delivery metadata; paid settlement is unavailable and paid listings return `503`. `submitMarketplaceBounty()` is restricted by the API to this tenant's published datasets and open, unexpired bounties. `awardMarketplaceBounty()` is poster-only and selects an already-submitted, still-published dataset; it records the winner but does not transfer funds. Webhook creation returns the signing secret once; store it securely and avoid logging it.
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
