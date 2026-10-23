export type JsonObject = Record<string, unknown>;
export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export class WebIntelligenceError extends Error {
  readonly statusCode?: number;
  readonly response?: unknown;
  constructor(message: string, statusCode?: number, response?: unknown) {
    super(message);
    this.name = "WebIntelligenceError";
    this.statusCode = statusCode;
    this.response = response;
  }
}

export interface ClientOptions {
  apiKey: string;
  baseUrl?: string;
  timeoutMs?: number;
  fetch?: FetchLike;
}

export interface AskOptions {
  maxPages?: number;
  maxSeconds?: number;
  topK?: number;
}

export interface SemanticSearchOptions {
  topK?: number;
  filter?: JsonObject;
}

export interface RagOptions {
  topK?: number;
  includeSources?: boolean;
}

export interface GraphEntityOptions {
  name: string;
  entityType?: string;
  aliases?: string[];
  properties?: JsonObject;
}

export interface GraphRelationOptions {
  sourceId: string;
  targetId: string;
  relation: string;
  properties?: JsonObject;
}

export type CrawlJobState = "queued" | "running" | "completed" | "failed" | "cancelled";
export type WebhookEvent = "crawl.completed" | "crawl.failed" | "crawl.cancelled" |
  "data.ready" | "export.ready" | "quota.warning" | "dataset.sold" |
  "discovery.completed" | "discovery.approved" | "web.changed" |
  "research.completed" | "research.failed" | "research.cancelled";

export interface WebhookCreateOptions {
  url: string;
  events: WebhookEvent[];
  description?: string;
}

export interface ResearchCreateOptions {
  topic: string;
  sources?: number;
  maxPagesPerSource?: number;
  seeds?: string[];
}

export interface DiscoveryPreviewOptions {
  count?: number;
  seeds?: string[];
  language?: string;
  perDomain?: number;
}

export interface DiscoveryCreateOptions {
  count?: number;
  seeds?: string[];
  language?: string;
  maxPagesPerSite?: number;
  perDomain?: number;
}

export interface WatchCreateOptions {
  url: string;
  intervalMinutes?: number;
}

export interface WorkflowCrawlStep {
  url: string;
  maxPages?: number;
  obeyRobots?: boolean;
  stepId?: string;
  dependsOn?: string[];
}

export interface MarketplaceSearchOptions {
  q?: string;
  tag?: string;
  maxPriceCents?: number;
  limit?: number;
}

export type OwnSearchIndexSource = "curlie" | "wikipedia" | "commoncrawl";
export interface OwnSearchIndexQueryOptions {
  limit?: number;
  sources?: OwnSearchIndexSource[];
  collapseHost?: boolean;
  rerank?: "none" | "embedding" | "cross-encoder";
  spell?: boolean;
}

export interface StudioAnalyticsOptions {
  groupBy?: "source" | "host" | "lang" | "snapshot" | "day";
  metric?: "count" | "avg_authority" | "avg_quality" | "avg_body_chars";
  source?: string;
  host?: string;
  lang?: string;
  indexedFrom?: string;
  indexedTo?: string;
  limit?: number;
  offset?: number;
}

export interface StudioSavedQueryOptions {
  name: string;
  q: string;
  limit?: number;
  sources?: string[];
  groupBy?: "host" | "source";
  metric?: "count" | "avg_score";
}

export interface StudioSnapshotCreateOptions {
  name: string;
  q: string;
  sources?: string[];
  groupBy?: "host" | "source";
  metric?: "count" | "avg_score";
  rows: Array<{ rank: number; url: string; title?: string; host?: string;
    snippet?: string; score?: number; source?: string }>;
}

export class WebIntelligenceClient {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: FetchLike;

  constructor(options: ClientOptions) {
    if (!options.apiKey?.trim()) throw new TypeError("apiKey must not be empty");
    this.apiKey = options.apiKey;
    this.baseUrl = (options.baseUrl ?? "http://localhost:8000").replace(/\/+$/, "");
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.fetchImpl = options.fetch ?? fetch;
  }

  private async request<T = JsonObject>(method: string, path: string,
                                         body?: JsonObject): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetchImpl(`${this.baseUrl}/api/v1/${path.replace(/^\/+/, "")}`, {
        method,
        headers: { "X-API-Key": this.apiKey, Accept: "application/json",
                   ...(body ? { "Content-Type": "application/json" } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}),
        signal: controller.signal,
      });
      if (response.status === 204) return {} as T;
      let payload: unknown;
      try { payload = await response.json(); }
      catch { payload = null; }
      if (!response.ok) {
        let message = response.statusText || `HTTP ${response.status}`;
        if (payload && typeof payload === "object") {
          const data = payload as Record<string, unknown>;
          const error = data.error && typeof data.error === "object"
            ? (data.error as Record<string, unknown>).message : undefined;
          message = String(error ?? data.detail ?? message);
        }
        throw new WebIntelligenceError(message, response.status, payload);
      }
      if (!payload || typeof payload !== "object") {
        throw new WebIntelligenceError("API returned an unsupported JSON response", response.status, payload);
      }
      return payload as T;
    } catch (error) {
      if (error instanceof WebIntelligenceError) throw error;
      if (error instanceof Error && error.name === "AbortError") {
        throw new WebIntelligenceError("API request timed out");
      }
      throw new WebIntelligenceError(`API request failed: ${error instanceof Error ? error.message : "network error"}`);
    } finally {
      clearTimeout(timer);
    }
  }

  listGraph(limit = 100): Promise<JsonObject> {
    return this.request("GET", `graph?limit=${limit}`);
  }

  upsertGraphEntity(options: GraphEntityOptions): Promise<JsonObject> {
    return this.request("POST", "graph/entities", {
      name: options.name, entity_type: options.entityType ?? "Entity",
      aliases: options.aliases ?? [], properties: options.properties ?? {},
    });
  }

  getGraphEntity(entityId: string): Promise<JsonObject> {
    return this.request("GET", `graph/entities/${encodeURIComponent(entityId)}`);
  }

  addGraphRelation(options: GraphRelationOptions): Promise<JsonObject> {
    return this.request("POST", "graph/relations", {
      source_id: options.sourceId, target_id: options.targetId,
      relation: options.relation, properties: options.properties ?? {},
    });
  }

  ragQuery(query: string, options: RagOptions = {}): Promise<JsonObject> {
    const params = new URLSearchParams({ q: query });
    if (options.topK !== undefined) params.set("top_k", String(options.topK));
    if (options.includeSources !== undefined) {
      params.set("include_sources", String(options.includeSources));
    }
    return this.request("GET", `search/rag?${params.toString()}`);
  }

  semanticSearch(query: string, options: SemanticSearchOptions = {}): Promise<JsonObject> {
    return this.request("POST", "search", {
      query,
      ...(options.topK === undefined ? {} : { top_k: options.topK }),
      ...(options.filter === undefined ? {} : { filter: options.filter }),
    });
  }

  ask(question: string, options: AskOptions = {}): Promise<JsonObject> {
    return this.request("POST", "ask", { question,
      budget: { max_pages: options.maxPages ?? 50, max_seconds: options.maxSeconds ?? 60 },
      top_k: options.topK ?? 8 });
  }

  createCrawl(url: string, options: { maxPages?: number; obeyRobots?: boolean } = {}): Promise<JsonObject> {
    return this.request("POST", "jobs/crawl", { url,
      max_pages: options.maxPages ?? 10, obey_robots: options.obeyRobots ?? true });
  }

  listJobs(page = 1, pageSize = 20, state?: CrawlJobState): Promise<JsonObject> {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (state !== undefined) params.set("state", state);
    return this.request("GET", `jobs?${params.toString()}`);
  }

  getJob(jobId: string): Promise<JsonObject> {
    return this.request("GET", `jobs/${encodeURIComponent(jobId)}`);
  }

  cancelJob(jobId: string): Promise<JsonObject> {
    return this.request("POST", `jobs/${encodeURIComponent(jobId)}/cancel`);
  }

  getSearchIndexLevels(): Promise<JsonObject> {
    return this.request("GET", "search-index/levels");
  }

  getSearchIndexStats(): Promise<JsonObject> {
    return this.request("GET", "search-index/stats");
  }

  correctSearchIndexQuery(query: string): Promise<JsonObject> {
    const params = new URLSearchParams({ q: query });
    return this.request("GET", `search-index/spell?${params.toString()}`);
  }

  searchOwnIndex(query: string, options: OwnSearchIndexQueryOptions = {}): Promise<JsonObject> {
    const body: JsonObject = { q: query, limit: options.limit ?? 10 };
    if (options.sources !== undefined) body.sources = options.sources;
    if (options.collapseHost !== undefined) body.collapse_host = options.collapseHost;
    if (options.rerank !== undefined) body.rerank = options.rerank;
    if (options.spell !== undefined) body.spell = options.spell;
    return this.request("POST", "search-index/query", body);
  }

  listBillingPlans(): Promise<JsonObject> {
    return this.request("GET", "billing/plans");
  }

  getBillingUsage(): Promise<JsonObject> {
    return this.request("GET", "billing/usage");
  }

  getBillingInvoiceEstimate(): Promise<JsonObject> {
    return this.request("GET", "billing/invoice");
  }

  listMyMarketplaceDatasets(options: {
    status?: "draft" | "published" | "suspended" | "archived";
    limit?: number;
    offset?: number;
  } = {}): Promise<JsonObject[]> {
    const params = new URLSearchParams();
    if (options.status !== undefined) params.set("status", options.status);
    if (options.limit !== undefined) params.set("limit", String(options.limit));
    if (options.offset !== undefined) params.set("offset", String(options.offset));
    const query = params.toString();
    return this.request<JsonObject[]>("GET",
      `marketplace/my/datasets${query ? `?${query}` : ""}`);
  }

  searchMarketplaceDatasets(options: MarketplaceSearchOptions = {}): Promise<JsonObject[]> {
    const params = new URLSearchParams();
    if (options.q !== undefined) params.set("q", options.q);
    if (options.tag !== undefined) params.set("tag", options.tag);
    if (options.maxPriceCents !== undefined) params.set("max_price_cents", String(options.maxPriceCents));
    if (options.limit !== undefined) params.set("limit", String(options.limit));
    const query = params.toString();
    return this.request<JsonObject[]>("GET", `marketplace/datasets${query ? `?${query}` : ""}`);
  }

  getMarketplaceDataset(datasetId: string): Promise<JsonObject> {
    return this.request("GET", `marketplace/datasets/${encodeURIComponent(datasetId)}`);
  }

  listMarketplaceBounties(limit = 50): Promise<JsonObject[]> {
    return this.request<JsonObject[]>("GET", `marketplace/bounties?limit=${limit}`);
  }

  listMarketplaceBountySubmissions(bountyId: string, limit = 50, offset = 0): Promise<string[]> {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return this.request<string[]>("GET",
      `marketplace/bounties/${encodeURIComponent(bountyId)}/submissions?${query}`);
  }

  getMarketplaceDelivery(deliveryId: string): Promise<JsonObject> {
    return this.request("GET", `marketplace/deliveries/${encodeURIComponent(deliveryId)}`);
  }

  listMarketplacePurchases(limit = 50, offset = 0): Promise<JsonObject[]> {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return this.request<JsonObject[]>("GET", `marketplace/purchases?${query}`);
  }

  listMarketplaceReviews(datasetId: string, limit = 50): Promise<JsonObject[]> {
    return this.request<JsonObject[]>("GET",
      `marketplace/datasets/${encodeURIComponent(datasetId)}/reviews?limit=${limit}`);
  }

  storeProvenanceRecord(record: JsonObject): Promise<JsonObject> {
    return this.request("POST", "provenance/records", record);
  }

  getProvenanceRecord(recordId: string): Promise<JsonObject> {
    return this.request("GET", `provenance/records/${encodeURIComponent(recordId)}`);
  }

  exportProvenanceManifest(datasetId: string, limit = 1000): Promise<JsonObject> {
    return this.request("POST", `provenance/manifest?limit=${limit}`, { dataset_id: datasetId });
  }

  createDiscovery(topic: string, options: DiscoveryCreateOptions = {}): Promise<JsonObject> {
    return this.request("POST", "discovery", {
      topic, count: options.count ?? 10, seeds: options.seeds ?? [],
      language: options.language ?? "en", auto: false,
      max_pages_per_site: options.maxPagesPerSite ?? 5,
      per_domain: options.perDomain ?? 1, obey_robots: true,
    });
  }

  listDiscoveries(page = 1, pageSize = 20): Promise<JsonObject> {
    return this.request("GET", `discovery?page=${page}&page_size=${pageSize}`);
  }

  getDiscovery(jobId: string): Promise<JsonObject> {
    return this.request("GET", `discovery/${encodeURIComponent(jobId)}`);
  }

  discoveryCandidates(jobId: string, onlySelected = false): Promise<JsonObject[]> {
    const params = onlySelected ? "?only_selected=true" : "";
    return this.request<JsonObject[]>("GET",
      `discovery/${encodeURIComponent(jobId)}/candidates${params}`);
  }

  cancelDiscovery(jobId: string): Promise<JsonObject> {
    return this.request("POST", `discovery/${encodeURIComponent(jobId)}/cancel`);
  }

  dropDiscoveryCandidate(jobId: string, domain: string): Promise<JsonObject[]> {
    return this.request<JsonObject[]>("DELETE",
      `discovery/${encodeURIComponent(jobId)}/candidates/${encodeURIComponent(domain)}`);
  }

  approveDiscovery(jobId: string, domains: string[]): Promise<JsonObject> {
    return this.request("POST", `discovery/${encodeURIComponent(jobId)}/approve`, { domains });
  }

  getDiscoveryResults(jobId: string): Promise<JsonObject> {
    return this.request("GET", `discovery/${encodeURIComponent(jobId)}/results`);
  }

  previewDiscovery(topic: string, options: DiscoveryPreviewOptions = {}): Promise<JsonObject> {
    return this.request("POST", "discovery/preview", {
      topic,
      ...(options.count === undefined ? {} : { count: options.count }),
      ...(options.seeds === undefined ? {} : { seeds: options.seeds }),
      ...(options.language === undefined ? {} : { language: options.language }),
      ...(options.perDomain === undefined ? {} : { per_domain: options.perDomain }),
    });
  }

  createResearch(options: ResearchCreateOptions): Promise<JsonObject> {
    return this.request("POST", "research", {
      topic: options.topic, sources: options.sources ?? 5,
      max_pages_per_source: options.maxPagesPerSource ?? 3,
      seeds: options.seeds ?? [],
    });
  }

  getResearch(researchId: string): Promise<JsonObject> {
    return this.request("GET", `research/${encodeURIComponent(researchId)}`);
  }

  listResearch(page = 1, pageSize = 20): Promise<JsonObject> {
    return this.request("GET", `research?page=${page}&page_size=${pageSize}`);
  }

  cancelResearch(researchId: string): Promise<JsonObject> {
    return this.request("POST", `research/${encodeURIComponent(researchId)}/cancel`);
  }

  listWatches(): Promise<JsonObject> {
    return this.request("GET", "watch");
  }

  createWatch(options: WatchCreateOptions): Promise<JsonObject> {
    return this.request("POST", "watch", {
      url: options.url, interval_minutes: options.intervalMinutes ?? 60,
    });
  }

  runWatch(watchId: string): Promise<JsonObject> {
    return this.request("POST", `watch/${encodeURIComponent(watchId)}/run`);
  }

  getWatchChanges(watchId: string, limit?: number): Promise<JsonObject> {
    const query = limit === undefined ? "" : `?limit=${limit}`;
    return this.request("GET", `watch/${encodeURIComponent(watchId)}/changes${query}`);
  }

  getWatchSnapshots(watchId: string, limit?: number): Promise<JsonObject> {
    const query = limit === undefined ? "" : `?limit=${limit}`;
    return this.request("GET", `watch/${encodeURIComponent(watchId)}/snapshots${query}`);
  }

  deleteWatch(watchId: string): Promise<JsonObject> {
    return this.request("DELETE", `watch/${encodeURIComponent(watchId)}`);
  }

  createWorkflow(name: string, steps: WorkflowCrawlStep[],
                 scheduleIntervalMinutes?: number): Promise<JsonObject> {
    return this.request("POST", "workflows", {
      name, steps: steps.map((step) => ({
        url: step.url, max_pages: step.maxPages ?? 3,
        obey_robots: step.obeyRobots ?? true,
        ...(step.stepId === undefined ? {} : { step_id: step.stepId }),
        ...(step.dependsOn === undefined ? {} : { depends_on: step.dependsOn }),
      })),
      ...(scheduleIntervalMinutes === undefined ? {} :
        { schedule_interval_minutes: scheduleIntervalMinutes }),
    });
  }

  listWorkflows(page = 1, pageSize = 20): Promise<JsonObject> {
    return this.request("GET", `workflows?page=${page}&page_size=${pageSize}`);
  }

  getWorkflow(workflowId: string): Promise<JsonObject> {
    return this.request("GET", `workflows/${encodeURIComponent(workflowId)}`);
  }

  listWorkflowRuns(page = 1, pageSize = 20): Promise<JsonObject> {
    return this.request("GET", `workflows/runs?page=${page}&page_size=${pageSize}`);
  }

  updateWorkflow(workflowId: string, options: {
    name?: string; steps?: WorkflowCrawlStep[]; scheduleIntervalMinutes?: number | null;
  }): Promise<JsonObject> {
    return this.request("PUT", `workflows/${encodeURIComponent(workflowId)}`, {
      ...(options.name === undefined ? {} : { name: options.name }),
      ...(options.steps === undefined ? {} : { steps: options.steps.map((step) => ({
        url: step.url, max_pages: step.maxPages ?? 3,
        obey_robots: step.obeyRobots ?? true,
        ...(step.stepId === undefined ? {} : { step_id: step.stepId }),
        ...(step.dependsOn === undefined ? {} : { depends_on: step.dependsOn }),
      })) }),
      ...(options.scheduleIntervalMinutes === undefined ? {} :
        { schedule_interval_minutes: options.scheduleIntervalMinutes }),
    });
  }

  deleteWorkflow(workflowId: string): Promise<JsonObject> {
    return this.request("DELETE", `workflows/${encodeURIComponent(workflowId)}`);
  }

  runWorkflow(workflowId: string): Promise<JsonObject> {
    return this.request("POST", `workflows/${encodeURIComponent(workflowId)}/runs`);
  }

  getWorkflowRun(runId: string): Promise<JsonObject> {
    return this.request("GET", `workflows/runs/${encodeURIComponent(runId)}`);
  }

  cancelWorkflowRun(runId: string): Promise<JsonObject> {
    return this.request("POST", `workflows/runs/${encodeURIComponent(runId)}/cancel`);
  }

  studioAnalytics(options: StudioAnalyticsOptions = {}): Promise<JsonObject> {
    const params = new URLSearchParams();
    const fields: Record<string, string | number | undefined> = {
      group_by: options.groupBy, metric: options.metric, source: options.source,
      host: options.host, lang: options.lang, indexed_from: options.indexedFrom,
      indexed_to: options.indexedTo, limit: options.limit, offset: options.offset,
    };
    for (const [key, value] of Object.entries(fields)) {
      if (value !== undefined && value !== "") params.set(key, String(value));
    }
    const query = params.toString();
    return this.request("GET", `studio/analytics${query ? `?${query}` : ""}`);
  }

  createWebhook(options: WebhookCreateOptions): Promise<JsonObject> {
    return this.request("POST", "webhooks", {
      url: options.url, events: options.events, description: options.description ?? "",
    });
  }

  listWebhooks(): Promise<JsonObject[]> {
    return this.request<JsonObject[]>("GET", "webhooks");
  }

  deleteWebhook(subscriptionId: string): Promise<JsonObject> {
    return this.request("DELETE", `webhooks/${encodeURIComponent(subscriptionId)}`);
  }

  testWebhook(subscriptionId: string, options: {
    event?: WebhookEvent; data?: JsonObject;
  } = {}): Promise<JsonObject> {
    return this.request("POST", "webhooks/test", {
      subscription_id: subscriptionId,
      ...(options.event === undefined ? {} : { event: options.event }),
      ...(options.data === undefined ? {} : { data: options.data }),
    });
  }

  listWebhookDeliveries(subscriptionId: string, limit = 50): Promise<JsonObject[]> {
    return this.request<JsonObject[]>("GET",
      `webhooks/${encodeURIComponent(subscriptionId)}/deliveries?limit=${limit}`);
  }

  saveStudioQuery(options: StudioSavedQueryOptions): Promise<JsonObject> {
    return this.request("POST", "studio/saved-queries", {
      name: options.name, q: options.q, limit: options.limit ?? 25,
      sources: options.sources ?? [], group_by: options.groupBy ?? "host",
      metric: options.metric ?? "count",
    });
  }

  listStudioQueries(page = 1, pageSize = 20): Promise<JsonObject> {
    return this.request("GET", `studio/saved-queries?page=${page}&page_size=${pageSize}`);
  }

  deleteStudioQuery(savedId: string): Promise<JsonObject> {
    return this.request("DELETE", `studio/saved-queries/${encodeURIComponent(savedId)}`);
  }

  createStudioSnapshot(options: StudioSnapshotCreateOptions): Promise<JsonObject> {
    return this.request("POST", "studio/snapshots", {
      name: options.name, q: options.q, sources: options.sources ?? [],
      group_by: options.groupBy ?? "host", metric: options.metric ?? "count",
      rows: options.rows,
    });
  }

  listStudioSnapshots(): Promise<JsonObject> {
    return this.request("GET", "studio/snapshots");
  }

  getStudioSnapshot(snapshotId: string): Promise<JsonObject> {
    return this.request("GET", `studio/snapshots/${encodeURIComponent(snapshotId)}`);
  }

  deleteStudioSnapshot(snapshotId: string): Promise<JsonObject> {
    return this.request("DELETE", `studio/snapshots/${encodeURIComponent(snapshotId)}`);
  }
}
