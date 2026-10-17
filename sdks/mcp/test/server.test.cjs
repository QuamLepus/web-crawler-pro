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
    'wio_watch_changes', 'wio_watch_create',
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
