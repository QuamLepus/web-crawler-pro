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
