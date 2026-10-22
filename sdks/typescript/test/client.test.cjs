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
