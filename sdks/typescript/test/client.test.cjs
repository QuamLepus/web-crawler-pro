const { test } = require('node:test');
const assert = require('node:assert/strict');
const { WebIntelligenceClient, WebIntelligenceError } = require('../dist/client.js');

test('ask sends auth and the documented request shape', async () => {
  let captured;
  const client = new WebIntelligenceClient({
    apiKey: 'test-key', baseUrl: 'https://api.example/',
    fetch: async (url, init) => {
      captured = { url, init };
      return new Response(JSON.stringify({ answer: 'source-backed', citations: [] }),
                          { status: 200, headers: { 'content-type': 'application/json' } });
    },
  });
  const result = await client.ask('question', { maxPages: 12, topK: 4 });
  assert.equal(result.answer, 'source-backed');
  assert.equal(captured.url, 'https://api.example/api/v1/ask');
  assert.equal(captured.init.headers['X-API-Key'], 'test-key');
  assert.deepEqual(JSON.parse(captured.init.body), {
    question: 'question', budget: { max_pages: 12, max_seconds: 60 }, top_k: 4,
  });
});

test('crawl job listing forwards pagination and an optional state filter', async () => {
  let requestUrl;
  const client = new WebIntelligenceClient({ apiKey: 'crawl-key', baseUrl: 'https://api.example',
    fetch: async (url) => {
      requestUrl = url;
      return new Response(JSON.stringify({ items: [], total: 0, page: 2, page_size: 7 }),
                          { status: 200 });
    } });
  const result = await client.listJobs(2, 7, 'running');
  assert.equal(result.page, 2);
  assert.equal(requestUrl, 'https://api.example/api/v1/jobs?page=2&page_size=7&state=running');
});

test('crawl cancellation uses the tenant job cancel endpoint', async () => {
  let request;
  const client = new WebIntelligenceClient({ apiKey: 'crawl-key', baseUrl: 'https://api.example',
    fetch: async (url, init) => {
      request = { url, method: init.method };
      return new Response(JSON.stringify({ job_id: 'job_abc', state: 'cancelled' }), { status: 200 });
    } });
  const response = await client.cancelJob('job_aaaaaaaaaaaaaaaaaaaa');
  assert.equal(response.state, 'cancelled');
  assert.deepEqual(request, {
    url: 'https://api.example/api/v1/jobs/job_aaaaaaaaaaaaaaaaaaaa/cancel',
    method: 'POST',
  });
});

test('watch snapshot retrieval uses the tenant snapshots endpoint', async () => {
  let requestUrl;
  const client = new WebIntelligenceClient({ apiKey: 'watch-key', baseUrl: 'https://api.example',
    fetch: async (url) => {
      requestUrl = url;
      return new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 });
    } });
  const response = await client.getWatchSnapshots('watch_aaaaaaaaaaaaaaaaaaaa');
  assert.deepEqual(response, { items: [], total: 0 });
  assert.equal(requestUrl,
    'https://api.example/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa/snapshots');
});

test('watch change and snapshot methods forward optional result limits', async () => {
  const urls = [];
  const client = new WebIntelligenceClient({ apiKey: 'watch-key', baseUrl: 'https://api.example',
    fetch: async (url) => {
      urls.push(url);
      return new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 });
    } });
  await client.getWatchChanges('watch_aaaaaaaaaaaaaaaaaaaa', 12);
  await client.getWatchSnapshots('watch_aaaaaaaaaaaaaaaaaaaa', 6);
  assert.deepEqual(urls, [
    'https://api.example/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa/changes?limit=12',
    'https://api.example/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa/snapshots?limit=6',
  ]);
});

test('watch listing uses the authenticated tenant endpoint', async () => {
  let request;
  const client = new WebIntelligenceClient({ apiKey: 'watch-key', baseUrl: 'https://api.example',
    fetch: async (url, init) => {
      request = { url, method: init.method };
      return new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 });
    } });
  assert.deepEqual(await client.listWatches(), { items: [], total: 0 });
  assert.deepEqual(request, { url: 'https://api.example/api/v1/watch', method: 'GET' });
});

test('watch deletion accepts the API 204 response and encodes the watch id', async () => {
  let request;
  const client = new WebIntelligenceClient({ apiKey: 'watch-key', baseUrl: 'https://api.example',
    fetch: async (url, init) => {
      request = { url, method: init.method };
      return new Response(null, { status: 204 });
    } });
  assert.deepEqual(await client.deleteWatch('watch_aaaaaaaaaaaaaaaaaaaa'), {});
  assert.deepEqual(request, {
    url: 'https://api.example/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa',
    method: 'DELETE',
  });
});

test('research request uses stable snake-case API fields', async () => {
  let parsed;
  const client = new WebIntelligenceClient({ apiKey: 'key', fetch: async (_url, init) => {
    parsed = JSON.parse(init.body);
    return new Response(JSON.stringify({ research_id: 'r1', status: 'queued' }), { status: 202 });
  } });
  assert.equal((await client.createResearch({ topic: 'AI market', sources: 3 })).research_id, 'r1');
  assert.equal(parsed.max_pages_per_source, 3);
  assert.equal(parsed.sources, 3);
});

test('research listing forwards pagination to the tenant endpoint', async () => {
  let requestUrl;
  const client = new WebIntelligenceClient({ apiKey: 'research-key',
    baseUrl: 'https://api.example', fetch: async (url) => {
      requestUrl = url;
      return new Response(JSON.stringify({ items: [], total: 0, page: 2, page_size: 8 }),
                          { status: 200 });
    } });
  const result = await client.listResearch(2, 8);
  assert.equal(result.page, 2);
  assert.equal(requestUrl, 'https://api.example/api/v1/research?page=2&page_size=8');
});

test('research cancellation uses the tenant-scoped cancel endpoint', async () => {
  let request;
  const client = new WebIntelligenceClient({ apiKey: 'research-key',
    baseUrl: 'https://api.example', fetch: async (url, init) => {
      request = { url, method: init.method };
      return new Response(JSON.stringify({ status: 'cancelled' }), { status: 200 });
    } });
  assert.equal((await client.cancelResearch('research_aaaaaaaaaaaaaaaaaaaa')).status, 'cancelled');
  assert.deepEqual(request, {
    url: 'https://api.example/api/v1/research/research_aaaaaaaaaaaaaaaaaaaa/cancel',
    method: 'POST',
  });
});

test('non-success response becomes a typed error without disclosing key', async () => {
  const client = new WebIntelligenceClient({ apiKey: 'secret-token', fetch: async () =>
    new Response(JSON.stringify({ error: { message: 'authentication required' } }), { status: 401 }) });
  await assert.rejects(client.ask('x'), (error) => {
    assert.ok(error instanceof WebIntelligenceError);
    assert.equal(error.statusCode, 401);
    assert.equal(error.message, 'authentication required');
    assert.equal(error.message.includes('secret-token'), false);
    return true;
  });
});

test('workflow create, status, and cancellation use the tenant API contracts', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'workflow-key', baseUrl: 'https://api.example',
    fetch: async (url, init) => {
      calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
      return new Response(JSON.stringify({ status: 'queued' }), { status: 202 });
    } });
  await client.createWorkflow('Morning refresh', [{ url: 'https://example.org', maxPages: 4 }], 60);
  await client.runWorkflow('workflow_a/b');
  await client.getWorkflowRun('wfrun_abc');
  await client.cancelWorkflowRun('wfrun_abc');
  assert.deepEqual(calls.map((call) => call.url), [
    'https://api.example/api/v1/workflows',
    'https://api.example/api/v1/workflows/workflow_a%2Fb/runs',
    'https://api.example/api/v1/workflows/runs/wfrun_abc',
    'https://api.example/api/v1/workflows/runs/wfrun_abc/cancel',
  ]);
  assert.deepEqual(calls[0].body, { name: 'Morning refresh', schedule_interval_minutes: 60,
    steps: [{ url: 'https://example.org', max_pages: 4, obey_robots: true }] });
});

test('workflow list and read methods forward pagination and encoded IDs', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'workflow-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method });
    return new Response(JSON.stringify({ items: [] }), { status: 200 });
  } });
  await client.listWorkflows(2, 15);
  await client.getWorkflow('workflow/a');
  await client.listWorkflowRuns(3, 8);
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/workflows?page=2&page_size=15', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/workflows/workflow%2Fa', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/workflows/runs?page=3&page_size=8', method: 'GET' },
  ]);
});

test('workflow client preserves explicit step IDs and DAG dependencies', async () => {
  let parsed;
  const client = new WebIntelligenceClient({ apiKey: 'workflow-dag-key', fetch: async (_url, init) => {
    parsed = JSON.parse(init.body);
    return new Response(JSON.stringify({ workflow_id: 'workflow_abcd' }), { status: 201 });
  } });
  await client.createWorkflow('Branched recipe', [
    { url: 'https://root.example', stepId: 'root', dependsOn: [] },
    { url: 'https://child.example', stepId: 'child', dependsOn: ['root'], maxPages: 2 },
  ]);
  assert.deepEqual(parsed.steps, [
    { url: 'https://root.example', max_pages: 3, obey_robots: true,
      step_id: 'root', depends_on: [] },
    { url: 'https://child.example', max_pages: 2, obey_robots: true,
      step_id: 'child', depends_on: ['root'] },
  ]);
});

test('workflow update and deletion use encoded tenant endpoints and snake-case fields', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'workflow-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    return new Response(JSON.stringify({ workflow_id: 'workflow_abcd', deleted: true }), { status: 200 });
  } });
  await client.updateWorkflow('workflow/a', { name: 'Updated',
    steps: [{ url: 'https://example.org', maxPages: 5, obeyRobots: false }],
    scheduleIntervalMinutes: null });
  await client.deleteWorkflow('workflow/a');
  assert.equal(calls[0].url, 'http://localhost:8000/api/v1/workflows/workflow%2Fa');
  assert.equal(calls[0].method, 'PUT');
  assert.deepEqual(calls[0].body, { name: 'Updated',
    steps: [{ url: 'https://example.org', max_pages: 5, obey_robots: false }],
    schedule_interval_minutes: null });
  assert.equal(calls[1].url, 'http://localhost:8000/api/v1/workflows/workflow%2Fa');
  assert.equal(calls[1].method, 'DELETE');
});

test('own search-index levels method reads roadmap metadata', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'index-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method });
    return new Response(JSON.stringify({ levels: [{ level: 0, name: 'starter' }] }), { status: 200 });
  } });
  assert.deepEqual(await client.getSearchIndexLevels(), { levels: [{ level: 0, name: 'starter' }] });
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/search-index/levels', method: 'GET' },
  ]);
});

test('own search-index query forwards bounded source and reranking controls', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'index-query-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    return new Response(JSON.stringify({ count: 1,
      results: [{ source: 'wikipedia', snippet: 'Solar energy.' }] }), { status: 200 });
  } });
  const result = await client.searchOwnIndex('solar energy', { limit: 4, sources: ['wikipedia'],
    collapseHost: false, rerank: 'none', spell: false });
  assert.equal(result.count, 1);
  assert.deepEqual(calls, [{
    url: 'http://localhost:8000/api/v1/search-index/query', method: 'POST',
    body: { q: 'solar energy', limit: 4, sources: ['wikipedia'],
      collapse_host: false, rerank: 'none', spell: false },
  }]);
});

test('own search-index stats method reads availability metrics', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'index-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method });
    return new Response(JSON.stringify({ available: true, docs: 42, sources: { curlie: 42 } }),
      { status: 200 });
  } });
  const stats = await client.getSearchIndexStats();
  assert.equal(stats.available, true);
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/search-index/stats', method: 'GET' },
  ]);
});

test('own search-index spell method URL-encodes bounded query text', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'spell-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method });
    return new Response(JSON.stringify({ query: 'web inteligence', corrected_query: 'web intelligence',
      corrections: [{ from: 'inteligence', to: 'intelligence' }], trained: true, terms: 100 }),
    { status: 200 });
  } });
  const result = await client.correctSearchIndexQuery('web inteligence');
  assert.equal(result.corrected_query, 'web intelligence');
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/search-index/spell?q=web+inteligence', method: 'GET' },
  ]);
});

test('billing methods read plans, usage, and current invoice estimate', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'billing-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method });
    const payload = url.endsWith('/billing/plans') ? { tiers: { starter: {} } }
      : url.endsWith('/billing/invoice') ? { tenant: 'tenant-1', period: '2026-10', total_cents: 900 }
        : { tenant: 'tenant-1', requests: 12 };
    return new Response(JSON.stringify(payload), { status: 200 });
  } });
  const estimate = await client.getBillingInvoiceEstimate();
  const plans = await client.listBillingPlans();
  const usage = await client.getBillingUsage();
  assert.deepEqual(estimate, { tenant: 'tenant-1', period: '2026-10', total_cents: 900 });
  assert.deepEqual(plans, { tiers: { starter: {} } });
  assert.deepEqual(usage, { tenant: 'tenant-1', requests: 12 });
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/billing/invoice', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/billing/plans', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/billing/usage', method: 'GET' },
  ]);
});

test('marketplace methods search published datasets and encode dataset IDs', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'marketplace-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method });
    return new Response(JSON.stringify([{ dataset_id: 'ds_public', status: 'published' }]),
      { status: 200 });
  } });
  await client.listMarketplaceBounties(7);
  await client.listMarketplaceBountySubmissions('bty_0123456789ab', 4, 3);
  await client.searchMarketplaceDatasets({ q: 'climate change', tag: 'energy',
    maxPriceCents: 500, limit: 5 });
  await client.getMarketplaceDataset('dataset:1');
  await client.listMyMarketplaceDatasets({ status: 'draft', limit: 5, offset: 10 });
  await client.getMarketplaceDelivery('dlv_0123456789ab');
  await client.listMarketplacePurchases(12, 5);
  await client.listMarketplaceReviews('dataset:1', 12, 4);
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/marketplace/bounties?limit=7', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/marketplace/bounties/bty_0123456789ab/submissions?limit=4&offset=3', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/marketplace/datasets?q=climate+change&tag=energy&max_price_cents=500&limit=5', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/marketplace/datasets/dataset%3A1', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/marketplace/my/datasets?status=draft&limit=5&offset=10', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/marketplace/deliveries/dlv_0123456789ab', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/marketplace/purchases?limit=12&offset=5', method: 'GET' },
    { url: 'http://localhost:8000/api/v1/marketplace/datasets/dataset%3A1/reviews?limit=12&offset=4', method: 'GET' },
  ]);
});

test('provenance methods store, retrieve, and export tenant records', async () => {
  const calls = [];
  const record = { record_id: 'record:one', source_url: 'https://example.org/data',
    fetched_at: '2026-10-23T00:00:00Z', content_sha256: 'a'.repeat(64) };
  const client = new WebIntelligenceClient({ apiKey: 'provenance-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    return new Response(JSON.stringify({ record_id: 'record:one', stored: true }), { status: 200 });
  } });
  await client.storeProvenanceRecord(record);
  await client.getProvenanceRecord('record:one');
  await client.exportProvenanceManifest('dataset-1', 9);
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/provenance/records', method: 'POST', body: record },
    { url: 'http://localhost:8000/api/v1/provenance/records/record%3Aone',
      method: 'GET', body: undefined },
    { url: 'http://localhost:8000/api/v1/provenance/manifest?limit=9', method: 'POST',
      body: { dataset_id: 'dataset-1' } },
  ]);
});

test('discovery job methods keep crawling behind explicit approval', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'discovery-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    const payload = url.includes('/candidates') ? [{ domain: 'example.org' }]
      : { job_id: 'disc_aaaaaaaaaaaa', state: 'awaiting_approval' };
    return new Response(JSON.stringify(payload), { status: init.method === 'POST'
      && url.endsWith('/discovery') ? 202 : 200 });
  } });
  await client.createDiscovery('AI medical devices', { count: 7,
    seeds: ['https://example.org'], language: 'en', maxPagesPerSite: 3, perDomain: 2 });
  await client.listDiscoveries(2, 7);
  await client.getDiscovery('disc_aaaaaaaaaaaa');
  await client.discoveryCandidates('disc_aaaaaaaaaaaa', true);
  await client.cancelDiscovery('disc_aaaaaaaaaaaa');
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/discovery', method: 'POST',
      body: { topic: 'AI medical devices', count: 7, seeds: ['https://example.org'],
        language: 'en', auto: false, max_pages_per_site: 3, per_domain: 2,
        obey_robots: true } },
    { url: 'http://localhost:8000/api/v1/discovery?page=2&page_size=7',
      method: 'GET', body: undefined },
    { url: 'http://localhost:8000/api/v1/discovery/disc_aaaaaaaaaaaa',
      method: 'GET', body: undefined },
    { url: 'http://localhost:8000/api/v1/discovery/disc_aaaaaaaaaaaa/candidates?only_selected=true',
      method: 'GET', body: undefined },
    { url: 'http://localhost:8000/api/v1/discovery/disc_aaaaaaaaaaaa/cancel',
      method: 'POST', body: undefined },
  ]);
});

test('discovery review methods remove a candidate, approve explicit domains, and read results', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'discovery-review-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    const payload = url.endsWith('/results') ? { sites_total: 1 }
      : url.includes('/candidates') ? [{ domain: 'example.org', rejected: true }]
        : { job_id: 'disc_aaaaaaaaaaaa', state: 'crawling' };
    return new Response(JSON.stringify(payload), { status: 200 });
  } });
  await client.dropDiscoveryCandidate('disc_aaaaaaaaaaaa', 'example.org');
  await client.approveDiscovery('disc_aaaaaaaaaaaa', ['example.org']);
  await client.getDiscoveryResults('disc_aaaaaaaaaaaa');
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/discovery/disc_aaaaaaaaaaaa/candidates/example.org',
      method: 'DELETE', body: undefined },
    { url: 'http://localhost:8000/api/v1/discovery/disc_aaaaaaaaaaaa/approve',
      method: 'POST', body: { domains: ['example.org'] } },
    { url: 'http://localhost:8000/api/v1/discovery/disc_aaaaaaaaaaaa/results',
      method: 'GET', body: undefined },
  ]);
});

test('discovery preview forwards dry-run controls without creating a job', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'discovery-preview-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ topic: 'AI medical devices', total_found: 0,
      candidates: [] }), { status: 200 });
  } });
  await client.previewDiscovery('AI medical devices', { count: 7,
    seeds: ['https://example.org'], language: 'en', perDomain: 2 });
  await client.previewDiscovery('clean energy');
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/discovery/preview', method: 'POST',
      body: { topic: 'AI medical devices', count: 7, seeds: ['https://example.org'],
        language: 'en', per_domain: 2 } },
    { url: 'http://localhost:8000/api/v1/discovery/preview', method: 'POST',
      body: { topic: 'clean energy' } },
  ]);
});

test('knowledge-graph methods map IDs, limits, and payload fields', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'graph-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    return new Response(JSON.stringify({ entity_id: 'ent_aaaaaaaaaaaaaaaaaaaaaaaa' }),
      { status: init.method === 'POST' && url.endsWith('/relations') ? 201 : 200 });
  } });
  await client.listGraph(12);
  await client.upsertGraphEntity({ name: 'Open Knowledge', entityType: 'Project',
    aliases: ['OK'], properties: { region: 'global' } });
  await client.getGraphEntity('ent_aaaaaaaaaaaaaaaaaaaaaaaa');
  await client.addGraphRelation({ sourceId: 'ent_aaaaaaaaaaaaaaaaaaaaaaaa',
    targetId: 'ent_bbbbbbbbbbbbbbbbbbbbbbbb', relation: 'supports',
    properties: { confidence: 0.9 } });
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/graph?limit=12', method: 'GET', body: undefined },
    { url: 'http://localhost:8000/api/v1/graph/entities', method: 'POST',
      body: { name: 'Open Knowledge', entity_type: 'Project', aliases: ['OK'],
        properties: { region: 'global' } } },
    { url: 'http://localhost:8000/api/v1/graph/entities/ent_aaaaaaaaaaaaaaaaaaaaaaaa',
      method: 'GET', body: undefined },
    { url: 'http://localhost:8000/api/v1/graph/relations', method: 'POST',
      body: { source_id: 'ent_aaaaaaaaaaaaaaaaaaaaaaaa',
        target_id: 'ent_bbbbbbbbbbbbbbbbbbbbbbbb', relation: 'supports',
        properties: { confidence: 0.9 } } },
  ]);
});

test('RAG query URL-encodes input and forwards optional source controls', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'rag-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method });
    return new Response(JSON.stringify({ answer: 'A supported extractive answer',
      engine: 'extractive', sources: [] }), { status: 200 });
  } });
  await client.ragQuery('solar panels & storage', { topK: 7, includeSources: false });
  await client.ragQuery('clean energy');
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/search/rag?q=solar+panels+%26+storage&top_k=7&include_sources=false',
      method: 'GET' },
    { url: 'http://localhost:8000/api/v1/search/rag?q=clean+energy', method: 'GET' },
  ]);
});

test('semantic search forwards bounded query and optional metadata filters', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'search-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ results: [], took_ms: 1 }), { status: 200 });
  } });
  await client.semanticSearch('solar panels', { topK: 25, filter: { language: 'en', year: 2025 } });
  await client.semanticSearch('clean energy');
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/search', method: 'POST',
      body: { query: 'solar panels', top_k: 25, filter: { language: 'en', year: 2025 } } },
    { url: 'http://localhost:8000/api/v1/search', method: 'POST',
      body: { query: 'clean energy' } },
  ]);
});

test('webhook methods preserve tenant API contracts and array responses', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'webhook-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    if (url.endsWith('/webhooks') && init.method === 'POST') {
      return new Response(JSON.stringify({ id: '012345abcdef', secret: 'whsec-test-only' }),
                         { status: 201 });
    }
    if (url.endsWith('/webhooks/test')) {
      return new Response(JSON.stringify({ queued_deliveries: ['dlv_test'] }), { status: 200 });
    }
    if (init.method === 'DELETE') {
      return new Response(JSON.stringify({ deleted: '012345abcdef' }), { status: 200 });
    }
    return new Response(JSON.stringify([{ id: '012345abcdef' }]), { status: 200 });
  } });
  const created = await client.createWebhook({ url: 'https://hooks.example/receiver',
    events: ['research.completed'], description: 'Research' });
  const listed = await client.listWebhooks();
  const tested = await client.testWebhook('012345abcdef', { event: 'research.completed',
    data: { research_id: 'research_test' } });
  const deliveries = await client.listWebhookDeliveries('012345abcdef', 12);
  const deleted = await client.deleteWebhook('012345abcdef');
  assert.equal(created.id, '012345abcdef');
  assert.deepEqual(listed, [{ id: '012345abcdef' }]);
  assert.deepEqual(tested, { queued_deliveries: ['dlv_test'] });
  assert.deepEqual(deliveries, [{ id: '012345abcdef' }]);
  assert.deepEqual(deleted, { deleted: '012345abcdef' });
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/webhooks', method: 'POST',
      body: { url: 'https://hooks.example/receiver', events: ['research.completed'],
        description: 'Research' } },
    { url: 'http://localhost:8000/api/v1/webhooks', method: 'GET', body: undefined },
    { url: 'http://localhost:8000/api/v1/webhooks/test', method: 'POST',
      body: { subscription_id: '012345abcdef', event: 'research.completed',
        data: { research_id: 'research_test' } } },
    { url: 'http://localhost:8000/api/v1/webhooks/012345abcdef/deliveries?limit=12',
      method: 'GET', body: undefined },
    { url: 'http://localhost:8000/api/v1/webhooks/012345abcdef', method: 'DELETE',
      body: undefined },
  ]);
});

test('Studio saved queries map fields, paginate, and handle 204 deletion', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'saved-query-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    return init.method === 'DELETE' ? new Response(null, { status: 204 })
      : new Response(JSON.stringify({ items: [], total: 0 }),
        { status: init.method === 'POST' ? 201 : 200 });
  } });
  await client.saveStudioQuery({ name: 'Solar', q: 'solar panels', limit: 30,
    sources: ['news'], groupBy: 'source', metric: 'avg_score' });
  await client.listStudioQueries(2, 7);
  assert.deepEqual(await client.deleteStudioQuery('saved/a'), {});
  assert.deepEqual(calls, [
    { url: 'http://localhost:8000/api/v1/studio/saved-queries', method: 'POST',
      body: { name: 'Solar', q: 'solar panels', limit: 30, sources: ['news'],
        group_by: 'source', metric: 'avg_score' } },
    { url: 'http://localhost:8000/api/v1/studio/saved-queries?page=2&page_size=7',
      method: 'GET', body: undefined },
    { url: 'http://localhost:8000/api/v1/studio/saved-queries/saved%2Fa',
      method: 'DELETE', body: undefined },
  ]);
});

test('Studio snapshots use bounded row payloads and handle 204 deletion', async () => {
  const calls = [];
  const client = new WebIntelligenceClient({ apiKey: 'studio-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    return init.method === 'DELETE' ? new Response(null, { status: 204 })
      : new Response(JSON.stringify({ snapshot_id: 'snapshot_abcd' }), { status: 201 });
  } });
  const saved = await client.createStudioSnapshot({ name: 'Solar sample', q: 'solar',
    groupBy: 'source', metric: 'avg_score', rows: [{ rank: 1, url: 'https://example.org', score: 0.8 }] });
  assert.equal(saved.snapshot_id, 'snapshot_abcd');
  assert.deepEqual(await client.deleteStudioSnapshot('snapshot/a'), {});
  assert.equal(calls[0].url, 'http://localhost:8000/api/v1/studio/snapshots');
  assert.deepEqual(calls[0].body, { name: 'Solar sample', q: 'solar', sources: [],
    group_by: 'source', metric: 'avg_score', rows: [{ rank: 1, url: 'https://example.org', score: 0.8 }] });
  assert.equal(calls[1].url, 'http://localhost:8000/api/v1/studio/snapshots/snapshot%2Fa');
});

test('Studio analytics URL-encodes full-corpus filters', async () => {
  let captured;
  const client = new WebIntelligenceClient({ apiKey: 'analytics-key', fetch: async (url, init) => {
    captured = { url, method: init.method };
    return new Response(JSON.stringify({ matched_documents: 42, rows: [] }), { status: 200 });
  } });
  const response = await client.studioAnalytics({ groupBy: 'host', metric: 'avg_quality',
    source: 'wiki & docs', lang: 'en', indexedFrom: '2026-01-01', limit: 50, offset: 10 });
  assert.equal(response.matched_documents, 42);
  assert.equal(captured.method, 'GET');
  const url = new URL(captured.url);
  assert.equal(url.pathname, '/api/v1/studio/analytics');
  assert.equal(url.searchParams.get('group_by'), 'host');
  assert.equal(url.searchParams.get('source'), 'wiki & docs');
  assert.equal(url.searchParams.get('indexed_from'), '2026-01-01');
  assert.equal(url.searchParams.get('offset'), '10');
});
