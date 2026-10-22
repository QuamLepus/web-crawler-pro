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
    'wio_ask', 'wio_crawl_cancel', 'wio_crawl_create', 'wio_crawl_list', 'wio_crawl_status',
    'wio_research_cancel', 'wio_research_create', 'wio_research_list', 'wio_research_status',
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
