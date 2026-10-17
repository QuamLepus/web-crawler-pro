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
  const response = await client.callTool({ name: 'wio_ask', arguments: { question: 'what is known?' } });
  assert.equal(request.url, 'http://localhost:8000/api/v1/ask');
  assert.equal(request.headers['X-API-Key'], 'test-key');
  assert.equal(request.body.question, 'what is known?');
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
    'wio_ask', 'wio_crawl_create', 'wio_research_create', 'wio_research_status',
    'wio_studio_analytics', 'wio_studio_snapshot_get', 'wio_watch_changes', 'wio_watch_create',
    'wio_workflow_cancel', 'wio_workflow_create', 'wio_workflow_run', 'wio_workflow_status',
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
