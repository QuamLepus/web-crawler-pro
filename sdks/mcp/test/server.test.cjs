const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
const { InMemoryTransport } = require('@modelcontextprotocol/sdk/inMemory.js');
const { WebIntelligenceClient } = require('@webintelligence/sdk');
const { buildServer } = require('../dist/server.js');

test('MCP ask tool forwards safely to the API client and returns citations', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'test-key', fetch: async (url, init) => {
    request = { url, headers: init.headers, body: JSON.parse(init.body) };
    return new Response(JSON.stringify({ answer: 'evidence', citations: [{ url: 'https://source.example' }] }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'test-client', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  const response = await client.callTool({ name: 'wio_ask', arguments: {
    question: 'what is known?', max_pages: 30, max_seconds: 120, top_k: 4,
  } });
  assert.equal(request.url, 'http://localhost:8000/api/v1/ask');
  assert.equal(request.headers['X-API-Key'], 'test-key');
  assert.deepEqual(request.body, {
    question: 'what is known?', budget: { max_pages: 30, max_seconds: 120 }, top_k: 4,
  });
  assert.equal(response.isError, undefined);
  assert.match(response.content[0].text, /source\.example/);
  await client.close();
  await server.close();
});

test('MCP billing tools read public plans and the authenticated tenant usage', async () => {
  const calls = [];
  const api = new WebIntelligenceClient({ apiKey: 'billing-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, key: init.headers['X-API-Key'] });
    const payload = url.endsWith('/billing/plans') ? { tiers: { starter: {} } }
      : { tenant: 'tenant-1', period: '2026-10', requests: 12 };
    return new Response(JSON.stringify(payload), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'billing-tools-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const plans = await client.callTool({ name: 'wio_billing_plans', arguments: {} });
    const usage = await client.callTool({ name: 'wio_billing_usage', arguments: {} });
    assert.equal(plans.isError, undefined);
    assert.equal(usage.isError, undefined);
    assert.match(plans.content[0].text, /starter/);
    assert.match(usage.content[0].text, /tenant-1/);
    assert.deepEqual(calls, [
      { url: 'http://localhost:8000/api/v1/billing/plans', method: 'GET', key: 'billing-key' },
      { url: 'http://localhost:8000/api/v1/billing/usage', method: 'GET', key: 'billing-key' },
    ]);
  } finally {
    await client.close();
    await server.close();
  }
});

test('stdio MCP transport initializes, lists tools, and forwards an authenticated API call', async (t) => {
  const http = require('node:http');
  const { StdioClientTransport } = require('@modelcontextprotocol/sdk/client/stdio.js');
  const requests = [];
  const mockApi = http.createServer((req, res) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', () => {
      requests.push({ path: req.url, key: req.headers['x-api-key'], body: body ? JSON.parse(body) : null });
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ answer: 'grounded answer', citations: [{ url: 'https://evidence.example' }] }));
    });
  });
  await new Promise((resolve) => mockApi.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => mockApi.close(resolve)));
  const address = mockApi.address();
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [require.resolve('../dist/server.js')],
    env: { ...process.env, WCP_API_KEY: 'stdio-test-key',
      WCP_BASE_URL: `http://127.0.0.1:${address.port}` },
  });
  const client = new Client({ name: 'stdio-integration-test', version: '1.0.0' });
  t.after(() => client.close());
  await client.connect(transport);
  const tools = await client.listTools();
  assert.deepEqual(tools.tools.map((tool) => tool.name).sort(), [
    'wio_ask', 'wio_billing_plans', 'wio_billing_usage', 'wio_crawl_cancel', 'wio_crawl_create', 'wio_crawl_list', 'wio_crawl_status',
    'wio_discovery_approve', 'wio_discovery_cancel', 'wio_discovery_candidate_drop',
    'wio_discovery_candidates', 'wio_discovery_create', 'wio_discovery_list',
    'wio_discovery_preview', 'wio_discovery_results', 'wio_discovery_status',
    'wio_graph_entity_get', 'wio_graph_entity_upsert', 'wio_graph_list',
    'wio_graph_relation_add', 'wio_marketplace_bounties_list', 'wio_marketplace_dataset_get',
    'wio_marketplace_dataset_search', 'wio_provenance_manifest_export',
    'wio_provenance_record_get', 'wio_provenance_record_store', 'wio_rag',
    'wio_research_cancel', 'wio_research_create', 'wio_research_list', 'wio_research_status',
    'wio_search',
    'wio_studio_analytics', 'wio_studio_query_delete', 'wio_studio_query_list',
    'wio_studio_query_save', 'wio_studio_snapshot_create', 'wio_studio_snapshot_delete',
    'wio_studio_snapshot_get', 'wio_studio_snapshot_list',
    'wio_watch_changes', 'wio_watch_create',
    'wio_watch_delete', 'wio_watch_list', 'wio_watch_run', 'wio_watch_snapshots',
    'wio_webhook_create', 'wio_webhook_delete', 'wio_webhook_deliveries',
    'wio_webhook_list', 'wio_webhook_test',
    'wio_workflow_cancel', 'wio_workflow_create', 'wio_workflow_delete',
    'wio_workflow_get', 'wio_workflow_list', 'wio_workflow_run',
    'wio_workflow_runs', 'wio_workflow_status', 'wio_workflow_update',
  ]);
  const result = await client.callTool({ name: 'wio_ask', arguments: { question: 'What is cited?' } });
  assert.equal(result.isError, undefined);
  assert.match(result.content[0].text, /evidence\.example/);
  assert.equal(requests[0].path, '/api/v1/ask');
  assert.equal(requests[0].key, 'stdio-test-key');
  assert.equal(requests[0].body.question, 'What is cited?');
});

test('MCP tool returns a protocol error when the API rejects a call', async () => {
  const api = new WebIntelligenceClient({ apiKey: 'test-key', fetch: async () =>
    new Response(JSON.stringify({ detail: 'index unavailable' }), { status: 503 }) });
  const server = buildServer(api);
  const client = new Client({ name: 'failure-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_ask', arguments: { question: 'test query' } });
    assert.equal(response.isError, true);
    assert.match(response.content[0].text, /index unavailable/);
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP research list tool forwards tenant pagination', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'research-list-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(JSON.stringify({ items: [], total: 0, page: 3, page_size: 4 }),
                        { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'research-list-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_research_list', arguments: {
      page: 3, page_size: 4,
    } });
    assert.equal(response.isError, undefined);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/research?page=3&page_size=4',
      method: 'GET',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP research cancellation tool forwards a validated research id', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'research-cancel-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(JSON.stringify({ research_id: 'research_aaaaaaaaaaaaaaaaaaaa',
      status: 'cancelled' }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'research-cancel-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_research_cancel', arguments: {
      research_id: 'research_aaaaaaaaaaaaaaaaaaaa',
    } });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /cancelled/);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/research/research_aaaaaaaaaaaaaaaaaaaa/cancel',
      method: 'POST',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP watch run tool queues a validated watch run', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'watch-run-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(JSON.stringify({ watch_id: 'watch_aaaaaaaaaaaaaaaaaaaa',
      job_id: 'watchrun_aaaaaaaaaaaaaaaaaaaa', status: 'queued' }), { status: 202 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'watch-run-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_watch_run', arguments: {
      watch_id: 'watch_aaaaaaaaaaaaaaaaaaaa',
    } });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /watchrun_aaaaaaaaaaaaaaaaaaaa/);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa/run',
      method: 'POST',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP watch list forwards the authenticated tenant endpoint', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'watch-list-key', fetch: async (url, init) => {
    request = { url, method: init.method, key: init.headers['X-API-Key'] };
    return new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'watch-list-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_watch_list', arguments: {} });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /"items": \[\]/);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/watch',
      method: 'GET',
      key: 'watch-list-key',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP watch delete tool stops a validated watch and handles 204', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'watch-delete-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(null, { status: 204 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'watch-delete-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_watch_delete', arguments: {
      watch_id: 'watch_aaaaaaaaaaaaaaaaaaaa',
    } });
    assert.equal(response.isError, undefined);
    assert.equal(response.content[0].text, '{}');
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa',
      method: 'DELETE',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP crawl list tool forwards tenant pagination and state filters', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'crawl-list-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(JSON.stringify({ items: [], total: 0, page: 2, page_size: 7 }),
                        { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'crawl-list-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_crawl_list', arguments: {
      page: 2, page_size: 7, state: 'running',
    } });
    assert.equal(response.isError, undefined);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/jobs?page=2&page_size=7&state=running',
      method: 'GET',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP crawl status tool retrieves a validated job id', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'crawl-status-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(JSON.stringify({ job_id: 'job_aaaaaaaaaaaaaaaaaaaa', state: 'running',
      pages_done: 2 }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'crawl-status-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_crawl_status', arguments: {
      job_id: 'job_aaaaaaaaaaaaaaaaaaaa',
    } });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /running/);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/jobs/job_aaaaaaaaaaaaaaaaaaaa',
      method: 'GET',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP crawl cancellation tool forwards a validated job id', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'crawl-tool-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(JSON.stringify({ job_id: 'job_aaaaaaaaaaaaaaaaaaaa', state: 'cancelled' }),
                        { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'crawl-cancel-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_crawl_cancel', arguments: {
      job_id: 'job_aaaaaaaaaaaaaaaaaaaa',
    } });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /cancelled/);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/jobs/job_aaaaaaaaaaaaaaaaaaaa/cancel',
      method: 'POST',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP workflow tool submits bounded sequential steps through the SDK', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'workflow-tool-key', fetch: async (url, init) => {
    request = { url, key: init.headers['X-API-Key'], body: JSON.parse(init.body) };
    return new Response(JSON.stringify({ workflow_id: 'workflow_aaaaaaaaaaaaaaaaaaaa', status: 'created' }),
                        { status: 201 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'workflow-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_workflow_create', arguments: {
      name: 'Morning sources', steps: [{ url: 'https://example.org', max_pages: 2 }],
      schedule_interval_minutes: 60,
    } });
    assert.equal(response.isError, undefined);
    assert.equal(request.url, 'http://localhost:8000/api/v1/workflows');
    assert.equal(request.key, 'workflow-tool-key');
    assert.deepEqual(request.body.steps, [{ url: 'https://example.org', max_pages: 2, obey_robots: true }]);
    assert.equal(request.body.schedule_interval_minutes, 60);
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP workflow update and delete tools preserve API semantics', async () => {
  const requests = [];
  const api = new WebIntelligenceClient({ apiKey: 'workflow-edit-key', fetch: async (url, init) => {
    requests.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    return new Response(JSON.stringify({ workflow_id: 'workflow_aaaaaaaaaaaaaaaaaaaa', deleted: true }),
                        { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'workflow-edit-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const updated = await client.callTool({ name: 'wio_workflow_update', arguments: {
      workflow_id: 'workflow_aaaaaaaaaaaaaaaaaaaa', name: 'Revised',
      steps: [
        { url: 'https://example.net', max_pages: 5, obey_robots: false,
          step_id: 'seed', depends_on: [] },
        { url: 'https://result.example', step_id: 'result', depends_on: ['seed'] },
      ],
      schedule_interval_minutes: null,
    } });
    const deleted = await client.callTool({ name: 'wio_workflow_delete', arguments: {
      workflow_id: 'workflow_aaaaaaaaaaaaaaaaaaaa',
    } });
    assert.equal(updated.isError, undefined);
    assert.equal(deleted.isError, undefined);
    assert.equal(requests[0].method, 'PUT');
    assert.equal(requests[0].url,
      'http://localhost:8000/api/v1/workflows/workflow_aaaaaaaaaaaaaaaaaaaa');
    assert.deepEqual(requests[0].body, { name: 'Revised',
      steps: [
        { url: 'https://example.net', max_pages: 5, obey_robots: false,
          step_id: 'seed', depends_on: [] },
        { url: 'https://result.example', max_pages: 3, obey_robots: true,
          step_id: 'result', depends_on: ['seed'] },
      ],
      schedule_interval_minutes: null });
    assert.equal(requests[1].method, 'DELETE');
    assert.equal(requests[1].url, requests[0].url);
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP workflow list, get, and run-history tools forward pagination', async () => {
  const requests = [];
  const api = new WebIntelligenceClient({ apiKey: 'workflow-read-key', fetch: async (url, init) => {
    requests.push({ url, method: init.method });
    return new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'workflow-read-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const listed = await client.callTool({ name: 'wio_workflow_list', arguments: {
      page: 2, page_size: 10,
    } });
    const fetched = await client.callTool({ name: 'wio_workflow_get', arguments: {
      workflow_id: 'workflow_aaaaaaaaaaaaaaaaaaaa',
    } });
    const runs = await client.callTool({ name: 'wio_workflow_runs', arguments: {
      page: 1, page_size: 5,
    } });
    assert.equal(listed.isError, undefined);
    assert.equal(fetched.isError, undefined);
    assert.equal(runs.isError, undefined);
    assert.deepEqual(requests, [
      { url: 'http://localhost:8000/api/v1/workflows?page=2&page_size=10', method: 'GET' },
      { url: 'http://localhost:8000/api/v1/workflows/workflow_aaaaaaaaaaaaaaaaaaaa', method: 'GET' },
      { url: 'http://localhost:8000/api/v1/workflows/runs?page=1&page_size=5', method: 'GET' },
    ]);
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP discovery job tools queue only human-reviewed candidates and expose status', async () => {
  const calls = [];
  const api = new WebIntelligenceClient({ apiKey: 'discovery-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    const payload = url.includes('/candidates') ? [{ domain: 'example.org' }]
      : { job_id: 'disc_aaaaaaaaaaaa', state: 'awaiting_approval' };
    return new Response(JSON.stringify(payload), { status: init.method === 'POST'
      && url.endsWith('/discovery') ? 202 : 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'discovery-tools-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const created = await client.callTool({ name: 'wio_discovery_create', arguments: {
      topic: 'AI medical devices', count: 7, seeds: ['https://example.org'],
      language: 'en', max_pages_per_site: 3, per_domain: 2,
    } });
    const listed = await client.callTool({ name: 'wio_discovery_list', arguments: {
      page: 2, page_size: 7,
    } });
    const status = await client.callTool({ name: 'wio_discovery_status', arguments: {
      job_id: 'disc_aaaaaaaaaaaa',
    } });
    const candidates = await client.callTool({ name: 'wio_discovery_candidates', arguments: {
      job_id: 'disc_aaaaaaaaaaaa', only_selected: true,
    } });
    const cancelled = await client.callTool({ name: 'wio_discovery_cancel', arguments: {
      job_id: 'disc_aaaaaaaaaaaa',
    } });
    for (const response of [created, listed, status, candidates, cancelled]) {
      assert.equal(response.isError, undefined);
    }
    assert.match(created.content[0].text, /awaiting_approval/);
    assert.match(candidates.content[0].text, /example.org/);
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
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP discovery review requires explicit domains before starting crawls', async () => {
  const calls = [];
  const api = new WebIntelligenceClient({ apiKey: 'discovery-review-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    const payload = url.endsWith('/results') ? { sites_total: 1 }
      : url.includes('/candidates') ? [{ domain: 'example.org', rejected: true }]
        : { job_id: 'disc_aaaaaaaaaaaa', state: 'crawling' };
    return new Response(JSON.stringify(payload), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'discovery-review-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const dropped = await client.callTool({ name: 'wio_discovery_candidate_drop', arguments: {
      job_id: 'disc_aaaaaaaaaaaa', domain: 'example.org',
    } });
    const approved = await client.callTool({ name: 'wio_discovery_approve', arguments: {
      job_id: 'disc_aaaaaaaaaaaa', domains: ['example.org'],
    } });
    const report = await client.callTool({ name: 'wio_discovery_results', arguments: {
      job_id: 'disc_aaaaaaaaaaaa',
    } });
    for (const response of [dropped, approved, report]) {
      assert.equal(response.isError, undefined);
    }
    assert.match(report.content[0].text, /sites_total/);
    assert.deepEqual(calls, [
      { url: 'http://localhost:8000/api/v1/discovery/disc_aaaaaaaaaaaa/candidates/example.org',
        method: 'DELETE', body: undefined },
      { url: 'http://localhost:8000/api/v1/discovery/disc_aaaaaaaaaaaa/approve',
        method: 'POST', body: { domains: ['example.org'] } },
      { url: 'http://localhost:8000/api/v1/discovery/disc_aaaaaaaaaaaa/results',
        method: 'GET', body: undefined },
    ]);
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP discovery preview is a bounded dry-run and submits no crawl job', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'discovery-preview-key', fetch: async (url, init) => {
    request = { url, method: init.method, body: JSON.parse(init.body) };
    return new Response(JSON.stringify({ topic: 'AI medical devices', total_found: 0,
      candidates: [] }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'discovery-preview-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_discovery_preview', arguments: {
      topic: 'AI medical devices', count: 7, seeds: ['https://example.org'],
      language: 'en', per_domain: 2,
    } });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /total_found/);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/discovery/preview', method: 'POST',
      body: { topic: 'AI medical devices', count: 7, seeds: ['https://example.org'],
        language: 'en', per_domain: 2 },
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP marketplace tools search public listings and fetch a safe dataset ID', async () => {
  const calls = [];
  const api = new WebIntelligenceClient({ apiKey: 'marketplace-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method });
    return new Response(JSON.stringify([{ dataset_id: 'ds_public', status: 'published' }]),
      { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'marketplace-tools-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const bounties = await client.callTool({ name: 'wio_marketplace_bounties_list', arguments: {
      limit: 7,
    } });
    const search = await client.callTool({ name: 'wio_marketplace_dataset_search', arguments: {
      q: 'climate change', tag: 'energy', max_price_cents: 500, limit: 5,
    } });
    const get = await client.callTool({ name: 'wio_marketplace_dataset_get', arguments: {
      dataset_id: 'dataset:1',
    } });
    assert.equal(bounties.isError, undefined);
    assert.equal(search.isError, undefined);
    assert.equal(get.isError, undefined);
    assert.deepEqual(calls, [
      { url: 'http://localhost:8000/api/v1/marketplace/bounties?limit=7', method: 'GET' },
      { url: 'http://localhost:8000/api/v1/marketplace/datasets?q=climate+change&tag=energy&max_price_cents=500&limit=5', method: 'GET' },
      { url: 'http://localhost:8000/api/v1/marketplace/datasets/dataset%3A1', method: 'GET' },
    ]);
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP provenance tools store, retrieve, and export caller metadata', async () => {
  const calls = [];
  const api = new WebIntelligenceClient({ apiKey: 'provenance-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    return new Response(JSON.stringify({ record_id: 'record:one', stored: true }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'provenance-tools-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const stored = await client.callTool({ name: 'wio_provenance_record_store', arguments: {
      record_id: 'record:one', source_url: 'https://example.org/data',
      fetched_at: '2026-10-23T00:00:00Z', content_sha256: 'a'.repeat(64),
    } });
    const record = await client.callTool({ name: 'wio_provenance_record_get', arguments: {
      record_id: 'record:one',
    } });
    const manifest = await client.callTool({ name: 'wio_provenance_manifest_export', arguments: {
      dataset_id: 'dataset-1', limit: 9,
    } });
    for (const response of [stored, record, manifest]) assert.equal(response.isError, undefined);
    assert.deepEqual(calls, [
      { url: 'http://localhost:8000/api/v1/provenance/records', method: 'POST',
        body: { record_id: 'record:one', source_url: 'https://example.org/data',
          fetched_at: '2026-10-23T00:00:00Z', content_sha256: 'a'.repeat(64) } },
      { url: 'http://localhost:8000/api/v1/provenance/records/record%3Aone',
        method: 'GET', body: undefined },
      { url: 'http://localhost:8000/api/v1/provenance/manifest?limit=9', method: 'POST',
        body: { dataset_id: 'dataset-1' } },
    ]);
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP knowledge-graph tools forward tenant-scoped bounded contracts', async () => {
  const calls = [];
  const api = new WebIntelligenceClient({ apiKey: 'graph-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    return new Response(JSON.stringify({ entity_id: 'ent_aaaaaaaaaaaaaaaaaaaaaaaa' }),
      { status: init.method === 'POST' && url.endsWith('/relations') ? 201 : 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'graph-tools-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const listed = await client.callTool({ name: 'wio_graph_list', arguments: { limit: 12 } });
    const entity = await client.callTool({ name: 'wio_graph_entity_upsert', arguments: {
      name: 'Open Knowledge', entity_type: 'Project', aliases: ['OK'],
      properties: { region: 'global' },
    } });
    const fetched = await client.callTool({ name: 'wio_graph_entity_get', arguments: {
      entity_id: 'ent_aaaaaaaaaaaaaaaaaaaaaaaa',
    } });
    const relation = await client.callTool({ name: 'wio_graph_relation_add', arguments: {
      source_id: 'ent_aaaaaaaaaaaaaaaaaaaaaaaa',
      target_id: 'ent_bbbbbbbbbbbbbbbbbbbbbbbb', relation: 'supports',
      properties: { confidence: 0.9 },
    } });
    assert.equal(listed.isError, undefined);
    assert.equal(entity.isError, undefined);
    assert.equal(fetched.isError, undefined);
    assert.equal(relation.isError, undefined);
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
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP RAG tool forwards bounded query and retrieval controls', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'rag-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(JSON.stringify({ answer: 'Supported answer', engine: 'extractive',
      sources: [] }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'rag-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_rag', arguments: {
      q: 'solar panels & storage', top_k: 7, include_sources: false,
    } });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /Supported answer/);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/search/rag?q=solar+panels+%26+storage&top_k=7&include_sources=false',
      method: 'GET',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP semantic search forwards tenant query, top-k, and metadata filters', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'semantic-search-key', fetch: async (url, init) => {
    request = { url, method: init.method, body: JSON.parse(init.body) };
    return new Response(JSON.stringify({ results: [], took_ms: 1,
      embedding_backend: 'test-backend' }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'semantic-search-tool-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_search', arguments: {
      query: 'solar panels', top_k: 25, filter: { language: 'en', year: 2025 },
    } });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /test-backend/);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/search', method: 'POST',
      body: { query: 'solar panels', top_k: 25, filter: { language: 'en', year: 2025 } },
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP webhook tools preserve subscription, test, and delivery contracts', async () => {
  const calls = [];
  const api = new WebIntelligenceClient({ apiKey: 'webhook-key', fetch: async (url, init) => {
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
  const server = buildServer(api);
  const client = new Client({ name: 'webhook-tools-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const created = await client.callTool({ name: 'wio_webhook_create', arguments: {
      url: 'https://hooks.example/receiver', events: ['research.completed'],
      description: 'Research',
    } });
    const listed = await client.callTool({ name: 'wio_webhook_list', arguments: {} });
    const tested = await client.callTool({ name: 'wio_webhook_test', arguments: {
      subscription_id: '012345abcdef', event: 'research.completed',
      data: { research_id: 'research_test' },
    } });
    const deliveries = await client.callTool({ name: 'wio_webhook_deliveries', arguments: {
      subscription_id: '012345abcdef', limit: 12,
    } });
    const deleted = await client.callTool({ name: 'wio_webhook_delete', arguments: {
      subscription_id: '012345abcdef',
    } });
    assert.equal(created.isError, undefined);
    assert.equal(listed.isError, undefined);
    assert.equal(tested.isError, undefined);
    assert.equal(deliveries.isError, undefined);
    assert.equal(deleted.isError, undefined);
    assert.match(created.content[0].text, /"secret"/);
    assert.match(listed.content[0].text, /012345abcdef/);
    assert.match(tested.content[0].text, /dlv_test/);
    assert.match(deliveries.content[0].text, /012345abcdef/);
    assert.match(deleted.content[0].text, /"deleted"/);
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
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP Studio saved-query tools save, paginate, and delete tenant queries', async () => {
  const calls = [];
  const api = new WebIntelligenceClient({ apiKey: 'studio-query-key', fetch: async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    if (init.method === 'DELETE') return new Response(null, { status: 204 });
    return new Response(JSON.stringify(init.method === 'POST'
      ? { saved_id: 'saved_aaaaaaaaaaaaaaaaaaaa' } : { items: [], total: 0 }),
    { status: init.method === 'POST' ? 201 : 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'studio-query-tools-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const saved = await client.callTool({ name: 'wio_studio_query_save', arguments: {
      name: 'Solar', q: 'solar panels', limit: 30, sources: ['news'],
      group_by: 'source', metric: 'avg_score',
    } });
    const listed = await client.callTool({ name: 'wio_studio_query_list', arguments: {
      page: 2, page_size: 7,
    } });
    const deleted = await client.callTool({ name: 'wio_studio_query_delete', arguments: {
      saved_id: 'saved_aaaaaaaaaaaaaaaaaaaa',
    } });
    assert.equal(saved.isError, undefined);
    assert.equal(listed.isError, undefined);
    assert.equal(deleted.isError, undefined);
    assert.match(saved.content[0].text, /saved_aaaaaaaaaaaaaaaaaaaa/);
    assert.equal(deleted.content[0].text, '{}');
    assert.deepEqual(calls, [
      { url: 'http://localhost:8000/api/v1/studio/saved-queries', method: 'POST',
        body: { name: 'Solar', q: 'solar panels', limit: 30, sources: ['news'],
          group_by: 'source', metric: 'avg_score' } },
      { url: 'http://localhost:8000/api/v1/studio/saved-queries?page=2&page_size=7',
        method: 'GET', body: undefined },
      { url: 'http://localhost:8000/api/v1/studio/saved-queries/saved_aaaaaaaaaaaaaaaaaaaa',
        method: 'DELETE', body: undefined },
    ]);
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP Studio snapshot list tool returns tenant-owned saved samples', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'studio-snapshot-list-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(JSON.stringify({ items: [{ snapshot_id: 'snapshot_aaaaaaaaaaaaaaaaaaaa' }],
      total: 1 }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'studio-snapshot-list-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_studio_snapshot_list', arguments: {} });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /snapshot_aaaaaaaaaaaaaaaaaaaa/);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/studio/snapshots', method: 'GET',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP Studio snapshot delete tool removes a validated tenant snapshot', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'studio-snapshot-delete-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(null, { status: 204 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'studio-snapshot-delete-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_studio_snapshot_delete', arguments: {
      snapshot_id: 'snapshot_aaaaaaaaaaaaaaaaaaaa',
    } });
    assert.equal(response.isError, undefined);
    assert.equal(response.content[0].text, '{}');
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/studio/snapshots/snapshot_aaaaaaaaaaaaaaaaaaaa',
      method: 'DELETE',
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP Studio snapshot create tool submits a bounded result sample', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'studio-snapshot-key', fetch: async (url, init) => {
    request = { url, method: init.method, body: JSON.parse(init.body) };
    return new Response(JSON.stringify({ snapshot_id: 'snapshot_aaaaaaaaaaaaaaaaaaaa' }),
                        { status: 201 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'studio-snapshot-create-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_studio_snapshot_create', arguments: {
      name: 'Solar sample', q: 'solar', rows: [{ rank: 1,
        url: 'https://example.org/article', score: 0.8 }],
    } });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /snapshot_aaaaaaaaaaaaaaaaaaaa/);
    assert.deepEqual(request, {
      url: 'http://localhost:8000/api/v1/studio/snapshots',
      method: 'POST',
      body: { name: 'Solar sample', q: 'solar', sources: [], group_by: 'host', metric: 'count',
        rows: [{ rank: 1, url: 'https://example.org/article', score: 0.8 }] },
    });
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP Studio analytics tool forwards bounded full-corpus filters', async () => {
  let request;
  const api = new WebIntelligenceClient({ apiKey: 'analytics-key', fetch: async (url, init) => {
    request = { url, method: init.method };
    return new Response(JSON.stringify({ matched_documents: 10, group_count: 2, rows: [] }), { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'studio-analytics-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_studio_analytics', arguments: {
      group_by: 'source', metric: 'avg_quality', source: 'wiki', limit: 10,
    } });
    assert.equal(response.isError, undefined);
    assert.equal(request.method, 'GET');
    const url = new URL(request.url);
    assert.equal(url.pathname, '/api/v1/studio/analytics');
    assert.equal(url.searchParams.get('group_by'), 'source');
    assert.equal(url.searchParams.get('metric'), 'avg_quality');
    assert.equal(url.searchParams.get('source'), 'wiki');
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP watch changes tool forwards the bounded result limit', async () => {
  let requestUrl;
  const api = new WebIntelligenceClient({ apiKey: 'watch-key', fetch: async (url) => {
    requestUrl = url;
    return new Response(JSON.stringify({ items: [], watch_id: 'watch_aaaaaaaaaaaaaaaaaaaa' }),
                        { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'watch-changes-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_watch_changes', arguments: {
      watch_id: 'watch_aaaaaaaaaaaaaaaaaaaa', limit: 15,
    } });
    assert.equal(response.isError, undefined);
    assert.equal(requestUrl,
      'http://localhost:8000/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa/changes?limit=15');
  } finally {
    await client.close();
    await server.close();
  }
});

test('MCP watch snapshots tool retrieves the bounded tenant history', async () => {
  let requestUrl;
  const api = new WebIntelligenceClient({ apiKey: 'watch-key', fetch: async (url) => {
    requestUrl = url;
    return new Response(JSON.stringify({ items: [{ snapshot_id: 's1', text: 'baseline' }], total: 1 }),
                        { status: 200 });
  } });
  const server = buildServer(api);
  const client = new Client({ name: 'watch-snapshots-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const response = await client.callTool({ name: 'wio_watch_snapshots', arguments: {
      watch_id: 'watch_aaaaaaaaaaaaaaaaaaaa', limit: 7,
    } });
    assert.equal(response.isError, undefined);
    assert.match(response.content[0].text, /baseline/);
    assert.equal(requestUrl,
      'http://localhost:8000/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa/snapshots?limit=7');
  } finally {
    await client.close();
    await server.close();
  }
});
