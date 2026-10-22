#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { WebIntelligenceClient } from "@webintelligence/sdk";
import { z } from "zod";

function result(value: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }] };
}

function failure(error: unknown) {
  return { content: [{ type: "text" as const,
    text: error instanceof Error ? error.message : "Web Intelligence request failed" }],
    isError: true };
}

const webhookEventSchema = z.enum([
  "crawl.completed", "crawl.failed", "crawl.cancelled", "data.ready",
  "export.ready", "quota.warning", "dataset.sold", "discovery.completed",
  "discovery.approved", "web.changed", "research.completed", "research.failed",
  "research.cancelled",
]);

const webhookTestDataSchema = z.record(z.unknown()).refine((data) => {
  try { return Buffer.byteLength(JSON.stringify(data), "utf8") <= 65536; }
  catch { return false; }
}, "test data must be JSON and at most 64 KiB");

const graphPropertiesSchema = z.record(z.string().max(128), z.unknown()).refine((data) => {
  try { return Buffer.byteLength(JSON.stringify(data), "utf8") <= 65536; }
  catch { return false; }
}, "graph properties must be JSON and at most 64 KiB");

const graphEntityIdSchema = z.string().regex(/^ent_[a-f0-9]{24}$/);

const semanticSearchFilterSchema = z.record(z.string().min(1).max(128),
  z.union([z.string().max(2048), z.number().finite(), z.boolean(), z.null()]))
  .refine((filter) => Object.keys(filter).length <= 50,
    "filter supports at most 50 metadata fields");

const studioSnapshotRowSchema = z.object({
  rank: z.number().int().min(1).max(100),
  url: z.string().url().max(2048).refine((value) => /^https?:\/\//i.test(value),
    "result URLs must use HTTP or HTTPS"),
  title: z.string().max(512).optional(),
  host: z.string().max(253).optional(),
  snippet: z.string().max(4000).optional(),
  score: z.number().finite().optional(),
  source: z.string().max(80).optional(),
});

const workflowStepSchema = z.object({
  step_id: z.string().regex(/^[A-Za-z][A-Za-z0-9_-]{0,39}$/).optional(),
  depends_on: z.array(z.string().regex(/^[A-Za-z][A-Za-z0-9_-]{0,39}$/)).max(9)
    .refine((ids) => new Set(ids).size === ids.length, "depends_on IDs must be unique").optional(),
  url: z.string().url(),
  max_pages: z.number().int().min(1).max(25).optional(),
  obey_robots: z.boolean().optional(),
});

export function buildServer(client: WebIntelligenceClient) {
  const server = new McpServer({ name: "web-intelligence-os", version: "0.1.0" });

  server.tool("wio_ask", "Answer from the authenticated tenant's existing indexed sources; this does not start a crawl.", {
    question: z.string().min(3).max(4096),
    max_pages: z.number().int().min(1).max(500).optional(),
    max_seconds: z.number().int().min(1).max(3600).optional(),
    top_k: z.number().int().min(1).max(20).optional(),
  }, async ({ question, max_pages, max_seconds, top_k }) => {
    try { return result(await client.ask(question, {
      maxPages: max_pages, maxSeconds: max_seconds, topK: top_k,
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_rag", "Retrieve context from the authenticated tenant's index and synthesize an answer; uses extractive fallback when no LLM is configured and is not independent fact-checking.", {
    q: z.string().min(3).max(4096),
    top_k: z.number().int().min(1).max(20).optional(),
    include_sources: z.boolean().optional(),
  }, async ({ q, top_k, include_sources }) => {
    try { return result(await client.ragQuery(q, {
      topK: top_k, includeSources: include_sources,
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_provenance_record_store", "Store caller-supplied provenance metadata for the authenticated tenant; this does not fetch a URL or verify permissions.", {
    record_id: z.string().min(1).max(160).regex(/^[A-Za-z0-9._:-]+$/),
    source_url: z.string().url().max(4096).refine((value) =>
      ["http:", "https:"].includes(new URL(value).protocol),
      "source_url must use HTTP or HTTPS"),
    fetched_at: z.string().min(1).max(80),
    http_status: z.number().int().min(100).max(599).optional(),
    robots_verdict: z.string().max(64).optional(),
    license: z.string().max(128).optional(),
    content_sha256: z.string().regex(/^[a-fA-F0-9]{64}$/),
    extraction_method: z.string().max(128).optional(),
    transformations: z.array(z.string().max(512)).max(64).optional(),
    extractor_version: z.string().max(128).optional(),
    model_id: z.string().max(256).optional(),
    prompt_hash: z.string().regex(/^[a-fA-F0-9]{64}$/).optional(),
    confidence: z.number().finite().min(0).max(1).optional(),
  }, async (record) => {
    try { return result(await client.storeProvenanceRecord(record)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_provenance_record_get", "Retrieve a provenance record belonging to the authenticated tenant.", {
    record_id: z.string().min(1).max(160).regex(/^[A-Za-z0-9._:-]+$/),
  }, async ({ record_id }) => {
    try { return result(await client.getProvenanceRecord(record_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_provenance_manifest_export", "Export a signed manifest for the authenticated tenant's provenance records; the API must have a signing key configured.", {
    dataset_id: z.string().min(1).max(160).regex(/^[A-Za-z0-9._:-]+$/),
    limit: z.number().int().min(1).max(10000).optional(),
  }, async ({ dataset_id, limit }) => {
    try { return result(await client.exportProvenanceManifest(dataset_id, limit)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_search", "Run semantic search against the authenticated tenant's indexed documents; results include matching text and metadata.", {
    query: z.string().min(1).max(4096),
    top_k: z.number().int().min(1).max(100).optional(),
    filter: semanticSearchFilterSchema.optional(),
  }, async ({ query, top_k, filter }) => {
    try { return result(await client.semanticSearch(query, { topK: top_k, filter })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_research_create", "Start an asynchronous, source-cited research job. Reports contain extractive citations, not independent fact-checking.", {
    topic: z.string().min(3).max(512),
    sources: z.number().int().min(1).max(20).optional(),
    max_pages_per_source: z.number().int().min(1).max(10).optional(),
  }, async ({ topic, sources, max_pages_per_source }) => {
    try { return result(await client.createResearch({ topic, sources,
      maxPagesPerSource: max_pages_per_source })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_research_status", "Get a research job's stage, report, and citations.", {
    research_id: z.string().regex(/^research_[a-f0-9]{20}$/),
  }, async ({ research_id }) => {
    try { return result(await client.getResearch(research_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_research_list", "List research jobs belonging to the authenticated tenant.", {
    page: z.number().int().min(1).optional(),
    page_size: z.number().int().min(1).max(100).optional(),
  }, async ({ page, page_size }) => {
    try { return result(await client.listResearch(page, page_size)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_research_cancel", "Request cancellation of a research job; an active source crawl may finish first.", {
    research_id: z.string().regex(/^research_[a-f0-9]{20}$/),
  }, async ({ research_id }) => {
    try { return result(await client.cancelResearch(research_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_crawl_create", "Queue a crawl job. Robots.txt is obeyed by default.", {
    url: z.string().url(), max_pages: z.number().int().min(1).max(1000).optional(),
  }, async ({ url, max_pages }) => {
    try { return result(await client.createCrawl(url, { maxPages: max_pages, obeyRobots: true })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_crawl_list", "List crawl jobs belonging to the authenticated tenant, optionally filtered by state.", {
    page: z.number().int().min(1).optional(),
    page_size: z.number().int().min(1).max(100).optional(),
    state: z.enum(["queued", "running", "completed", "failed", "cancelled"]).optional(),
  }, async ({ page, page_size, state }) => {
    try { return result(await client.listJobs(page, page_size, state)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_crawl_status", "Get a crawl job's state and progress.", {
    job_id: z.string().regex(/^job_[a-f0-9]{20}$/),
  }, async ({ job_id }) => {
    try { return result(await client.getJob(job_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_crawl_cancel", "Request cooperative cancellation of a crawl job; an active page may finish first.", {
    job_id: z.string().regex(/^job_[a-f0-9]{20}$/),
  }, async ({ job_id }) => {
    try { return result(await client.cancelJob(job_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_discovery_preview", "Query configured discovery providers for ranked candidates without starting a crawl or creating a job; provider unavailability can yield no candidates.", {
    topic: z.string().min(2).max(400),
    count: z.number().int().min(1).max(500).optional(),
    seeds: z.array(z.string().url().refine((value) =>
      ["http:", "https:"].includes(new URL(value).protocol),
      "seed URLs must use HTTP or HTTPS")).max(50).optional(),
    language: z.string().regex(/^[a-z]{2}(-[A-Z]{2})?$/).optional(),
    per_domain: z.number().int().min(1).max(25).optional(),
  }, async ({ topic, count, seeds, language, per_domain }) => {
    try { return result(await client.previewDiscovery(topic, {
      count, seeds, language, perDomain: per_domain,
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_discovery_create", "Queue candidate discovery for human review; this does not start crawls. The requested count consumes quota up front, and robots.txt remains enabled.", {
    topic: z.string().min(2).max(400),
    count: z.number().int().min(1).max(50).optional(),
    seeds: z.array(z.string().url().refine((value) =>
      ["http:", "https:"].includes(new URL(value).protocol),
      "seed URLs must use HTTP or HTTPS")).max(50).optional(),
    language: z.string().regex(/^[a-z]{2}(-[A-Z]{2})?$/).optional(),
    max_pages_per_site: z.number().int().min(1).max(50).optional(),
    per_domain: z.number().int().min(1).max(5).optional(),
  }, async ({ topic, count, seeds, language, max_pages_per_site, per_domain }) => {
    try { return result(await client.createDiscovery(topic, {
      count, seeds, language, maxPagesPerSite: max_pages_per_site, perDomain: per_domain,
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_discovery_list", "List discovery jobs belonging to the authenticated tenant.", {
    page: z.number().int().min(1).max(10000).optional(),
    page_size: z.number().int().min(1).max(100).optional(),
  }, async ({ page, page_size }) => {
    try { return result(await client.listDiscoveries(page, page_size)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_discovery_status", "Read status for one tenant-owned discovery job.", {
    job_id: z.string().regex(/^disc_[a-f0-9]{12}$/),
  }, async ({ job_id }) => {
    try { return result(await client.getDiscovery(job_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_discovery_candidates", "Read ranked candidate sites for a tenant-owned discovery job; this does not crawl them.", {
    job_id: z.string().regex(/^disc_[a-f0-9]{12}$/),
    only_selected: z.boolean().optional(),
  }, async ({ job_id, only_selected }) => {
    try { return result(await client.discoveryCandidates(job_id, only_selected)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_discovery_cancel", "Cancel a queued or running discovery job before it is approved.", {
    job_id: z.string().regex(/^disc_[a-f0-9]{12}$/),
  }, async ({ job_id }) => {
    try { return result(await client.cancelDiscovery(job_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_discovery_candidate_drop", "Remove one domain from a tenant-owned shortlist before approval.", {
    job_id: z.string().regex(/^disc_[a-f0-9]{12}$/),
    domain: z.string().min(1).max(253),
  }, async ({ job_id, domain }) => {
    try { return result(await client.dropDiscoveryCandidate(job_id, domain)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_discovery_approve", "Start crawls only for the explicitly supplied shortlisted domains. This triggers crawl jobs; robots.txt policy from discovery is preserved.", {
    job_id: z.string().regex(/^disc_[a-f0-9]{12}$/),
    domains: z.array(z.string().min(1).max(253)).min(1).max(50)
      .refine((domains) => new Set(domains.map((domain) => domain.toLowerCase())).size === domains.length,
        "domains must be unique"),
  }, async ({ job_id, domains }) => {
    try { return result(await client.approveDiscovery(job_id, domains)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_discovery_results", "Read the aggregated report for a tenant-owned discovery job.", {
    job_id: z.string().regex(/^disc_[a-f0-9]{12}$/),
  }, async ({ job_id }) => {
    try { return result(await client.getDiscoveryResults(job_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_graph_list", "List up to 500 entities and relationships in the authenticated tenant's knowledge graph.", {
    limit: z.number().int().min(1).max(500).optional(),
  }, async ({ limit }) => {
    try { return result(await client.listGraph(limit)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_graph_entity_upsert", "Create or exactly resolve a tenant graph entity; aliases merge only when they resolve unambiguously.", {
    name: z.string().min(1).max(256),
    entity_type: z.string().min(1).max(80).optional(),
    aliases: z.array(z.string().max(256)).max(50).optional(),
    properties: graphPropertiesSchema.optional(),
  }, async ({ name, entity_type, aliases, properties }) => {
    try { return result(await client.upsertGraphEntity({
      name, entityType: entity_type, aliases, properties,
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_graph_entity_get", "Retrieve one tenant-owned graph entity and its relationships.", {
    entity_id: graphEntityIdSchema,
  }, async ({ entity_id }) => {
    try { return result(await client.getGraphEntity(entity_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_graph_relation_add", "Connect two existing entities in the authenticated tenant's graph.", {
    source_id: graphEntityIdSchema,
    target_id: graphEntityIdSchema,
    relation: z.string().min(1).max(80),
    properties: graphPropertiesSchema.optional(),
  }, async ({ source_id, target_id, relation, properties }) => {
    try { return result(await client.addGraphRelation({
      sourceId: source_id, targetId: target_id, relation, properties,
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_marketplace_dataset_search", "Search published marketplace listings; this read-only tool never purchases a dataset.", {
    q: z.string().max(200).optional(),
    tag: z.string().max(64).optional(),
    max_price_cents: z.number().int().min(0).max(10_000_000).optional(),
    limit: z.number().int().min(1).max(100).optional(),
  }, async ({ q, tag, max_price_cents, limit }) => {
    try { return result(await client.searchMarketplaceDatasets({
      q, tag, maxPriceCents: max_price_cents, limit,
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_marketplace_dataset_get", "Retrieve a published marketplace listing or a draft owned by the authenticated tenant.", {
    dataset_id: z.string().min(1).max(160),
  }, async ({ dataset_id }) => {
    try { return result(await client.getMarketplaceDataset(dataset_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_watch_create", "Create a recurring single-page change watch (minimum 15 minutes).", {
    url: z.string().url(), interval_minutes: z.number().int().min(15).max(10080).optional(),
  }, async ({ url, interval_minutes }) => {
    try { return result(await client.createWatch({ url, intervalMinutes: interval_minutes })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_watch_list", "List monitored page watches belonging to the authenticated tenant.", {}, async () => {
    try { return result(await client.listWatches()); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_watch_run", "Queue an immediate single-page crawl for a monitored page.", {
    watch_id: z.string().regex(/^watch_[a-f0-9]{20}$/),
  }, async ({ watch_id }) => {
    try { return result(await client.runWatch(watch_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_watch_changes", "Read detected text changes for a monitored page.", {
    watch_id: z.string().regex(/^watch_[a-f0-9]{20}$/),
    limit: z.number().int().min(1).max(100).optional(),
  }, async ({ watch_id, limit }) => {
    try { return result(await client.getWatchChanges(watch_id, limit)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_watch_snapshots", "Read retained text snapshots for a monitored page (maximum 20).", {
    watch_id: z.string().regex(/^watch_[a-f0-9]{20}$/),
    limit: z.number().int().min(1).max(20).optional(),
  }, async ({ watch_id, limit }) => {
    try { return result(await client.getWatchSnapshots(watch_id, limit)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_watch_delete", "Stop a monitored page watch; retained snapshots remain available.", {
    watch_id: z.string().regex(/^watch_[a-f0-9]{20}$/),
  }, async ({ watch_id }) => {
    try { return result(await client.deleteWatch(watch_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_workflow_create", "Create a bounded workflow with sequential defaults or explicit DAG dependencies (up to 10 steps; execution is serial).", {
    name: z.string().min(1).max(100),
    steps: z.array(workflowStepSchema).min(1).max(10),
    schedule_interval_minutes: z.number().int().min(60).max(10080).optional(),
  }, async ({ name, steps, schedule_interval_minutes }) => {
    try { return result(await client.createWorkflow(name, steps.map((step) => ({
      url: step.url, maxPages: step.max_pages, obeyRobots: step.obey_robots ?? true,
      stepId: step.step_id, dependsOn: step.depends_on,
    })), schedule_interval_minutes)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_workflow_list", "List workflows belonging to the authenticated tenant.", {
    page: z.number().int().min(1).optional(),
    page_size: z.number().int().min(1).max(100).optional(),
  }, async ({ page, page_size }) => {
    try { return result(await client.listWorkflows(page, page_size)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_workflow_get", "Read one workflow definition owned by the authenticated tenant.", {
    workflow_id: z.string().regex(/^workflow_[a-f0-9]{20}$/),
  }, async ({ workflow_id }) => {
    try { return result(await client.getWorkflow(workflow_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_workflow_runs", "List recent workflow runs belonging to the authenticated tenant.", {
    page: z.number().int().min(1).optional(),
    page_size: z.number().int().min(1).max(100).optional(),
  }, async ({ page, page_size }) => {
    try { return result(await client.listWorkflowRuns(page, page_size)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_workflow_update", "Update workflow metadata, ordered steps, or schedule. Omitted fields are unchanged; null disables recurrence. Step dependencies enable bounded serial DAG execution.", {
    workflow_id: z.string().regex(/^workflow_[a-f0-9]{20}$/),
    name: z.string().min(1).max(100).optional(),
    steps: z.array(workflowStepSchema).min(1).max(10).optional(),
    schedule_interval_minutes: z.number().int().min(60).max(10080).nullable().optional(),
  }, async ({ workflow_id, name, steps, schedule_interval_minutes }) => {
    try { return result(await client.updateWorkflow(workflow_id, {
      ...(name === undefined ? {} : { name }),
      ...(steps === undefined ? {} : { steps: steps.map((step) => ({
        url: step.url, maxPages: step.max_pages, obeyRobots: step.obey_robots ?? true,
        stepId: step.step_id, dependsOn: step.depends_on,
      })) }),
      ...(schedule_interval_minutes === undefined ? {} :
        { scheduleIntervalMinutes: schedule_interval_minutes }),
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_workflow_delete", "Delete a workflow definition and its schedule. Historical runs are retained; deletion fails while runs are queued or running.", {
    workflow_id: z.string().regex(/^workflow_[a-f0-9]{20}$/),
  }, async ({ workflow_id }) => {
    try { return result(await client.deleteWorkflow(workflow_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_workflow_run", "Queue a saved sequential crawl workflow.", {
    workflow_id: z.string().regex(/^workflow_[a-f0-9]{20}$/),
  }, async ({ workflow_id }) => {
    try { return result(await client.runWorkflow(workflow_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_workflow_status", "Read status and per-step progress for a workflow run.", {
    run_id: z.string().regex(/^wfrun_[a-f0-9]{20}$/),
  }, async ({ run_id }) => {
    try { return result(await client.getWorkflowRun(run_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_workflow_cancel", "Cancel a queued workflow or request cancellation after its active crawl step.", {
    run_id: z.string().regex(/^wfrun_[a-f0-9]{20}$/),
  }, async ({ run_id }) => {
    try { return result(await client.cancelWorkflowRun(run_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_webhook_create", "Register an HTTPS webhook for selected events. The one-time signing secret is returned; store it securely.", {
    url: z.string().url().max(2048).refine((value) => /^https:\/\//.test(value),
      "webhook URL must use HTTPS"),
    events: z.array(webhookEventSchema).min(1).max(13),
    description: z.string().max(500).optional(),
  }, async ({ url, events, description }) => {
    try { return result(await client.createWebhook({ url, events, description })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_webhook_list", "List webhook subscriptions for the authenticated tenant (secrets are never included).", {}, async () => {
    try { return result(await client.listWebhooks()); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_webhook_delete", "Delete a webhook subscription belonging to the authenticated tenant.", {
    subscription_id: z.string().regex(/^[a-f0-9]{12}$/),
  }, async ({ subscription_id }) => {
    try { return result(await client.deleteWebhook(subscription_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_webhook_test", "Queue a signed test event for a tenant-owned webhook subscription.", {
    subscription_id: z.string().regex(/^[a-f0-9]{12}$/),
    event: webhookEventSchema.optional(),
    data: webhookTestDataSchema.optional(),
  }, async ({ subscription_id, event, data }) => {
    try { return result(await client.testWebhook(subscription_id, { event, data })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_webhook_deliveries", "List recent webhook delivery attempts (maximum 200).", {
    subscription_id: z.string().regex(/^[a-f0-9]{12}$/),
    limit: z.number().int().min(1).max(200).optional(),
  }, async ({ subscription_id, limit }) => {
    try { return result(await client.listWebhookDeliveries(subscription_id, limit)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_studio_query_save", "Save a bounded Data Studio query for the authenticated tenant.", {
    name: z.string().min(1).max(100), q: z.string().min(1).max(2048),
    limit: z.number().int().min(1).max(100).optional(),
    sources: z.array(z.string().max(80)).max(10).optional(),
    group_by: z.enum(["host", "source"]).optional(),
    metric: z.enum(["count", "avg_score"]).optional(),
  }, async ({ name, q, limit, sources, group_by, metric }) => {
    try { return result(await client.saveStudioQuery({
      name, q, limit, sources, groupBy: group_by, metric,
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_studio_query_list", "List saved Data Studio queries belonging to the authenticated tenant.", {
    page: z.number().int().min(1).max(10000).optional(),
    page_size: z.number().int().min(1).max(100).optional(),
  }, async ({ page, page_size }) => {
    try { return result(await client.listStudioQueries(page, page_size)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_studio_query_delete", "Delete a saved Data Studio query belonging to the authenticated tenant.", {
    saved_id: z.string().regex(/^saved_[a-f0-9]{20}$/),
  }, async ({ saved_id }) => {
    try { return result(await client.deleteStudioQuery(saved_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_studio_analytics", "Aggregate the full indexed corpus using safe dimensions and filters; returns grouped values, not document contents.", {
    group_by: z.enum(["source", "host", "lang", "snapshot", "day"]).optional(),
    metric: z.enum(["count", "avg_authority", "avg_quality", "avg_body_chars"]).optional(),
    source: z.string().max(80).optional(), host: z.string().max(253).optional(),
    lang: z.string().max(20).optional(),
    indexed_from: z.string().regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/).optional(),
    indexed_to: z.string().regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/).optional(),
    limit: z.number().int().min(1).max(100).optional(),
    offset: z.number().int().min(0).max(5000).optional(),
  }, async (args) => {
    try { return result(await client.studioAnalytics({
      groupBy: args.group_by, metric: args.metric, source: args.source,
      host: args.host, lang: args.lang, indexedFrom: args.indexed_from,
      indexedTo: args.indexed_to, limit: args.limit, offset: args.offset,
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_studio_snapshot_create", "Save an immutable Data Studio result sample with up to 100 rows.", {
    name: z.string().min(1).max(100),
    q: z.string().min(1).max(2048),
    sources: z.array(z.string().max(256)).max(10).optional(),
    group_by: z.enum(["host", "source"]).optional(),
    metric: z.enum(["count", "avg_score"]).optional(),
    rows: z.array(studioSnapshotRowSchema).min(1).max(100),
  }, async ({ name, q, sources, group_by, metric, rows }) => {
    try { return result(await client.createStudioSnapshot({
      name, q, sources, groupBy: group_by, metric, rows,
    })); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_studio_snapshot_list", "List Data Studio result snapshots saved by the authenticated tenant.", {}, async () => {
    try { return result(await client.listStudioSnapshots()); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_studio_snapshot_get", "Retrieve an immutable bounded Data Studio result snapshot.", {
    snapshot_id: z.string().regex(/^snapshot_[a-f0-9]{20}$/),
  }, async ({ snapshot_id }) => {
    try { return result(await client.getStudioSnapshot(snapshot_id)); }
    catch (error) { return failure(error); }
  });

  server.tool("wio_studio_snapshot_delete", "Delete a saved Data Studio result snapshot belonging to the authenticated tenant.", {
    snapshot_id: z.string().regex(/^snapshot_[a-f0-9]{20}$/),
  }, async ({ snapshot_id }) => {
    try { return result(await client.deleteStudioSnapshot(snapshot_id)); }
    catch (error) { return failure(error); }
  });

  return server;
}

async function main() {
  const apiKey = process.env.WCP_API_KEY;
  if (!apiKey) throw new Error("Set WCP_API_KEY in the MCP server environment.");
  const client = new WebIntelligenceClient({ apiKey,
    baseUrl: process.env.WCP_BASE_URL ?? "http://localhost:8000" });
  const server = buildServer(client);
  await server.connect(new StdioServerTransport());
}

if (require.main === module) {
  main().catch((error) => {
    // MCP stdio reserves stdout for protocol frames.
    console.error(error instanceof Error ? error.message : "MCP server startup failed");
    process.exitCode = 1;
  });
}
