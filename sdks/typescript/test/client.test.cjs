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
