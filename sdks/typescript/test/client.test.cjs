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
