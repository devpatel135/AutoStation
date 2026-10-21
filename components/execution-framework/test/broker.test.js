'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { llmCall } = require('../lib/llm');
const { eventSink } = require('./_helpers');

function startStub(handler) {
  return new Promise((resolve) => {
    const seen = [];
    const srv = http.createServer((req, res) => {
      const chunks = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => {
        const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : null;
        seen.push({ method: req.method, url: req.url, headers: req.headers, body });
        const out = handler(body, req);
        res.writeHead(out.code || 200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(out.json));
      });
    });
    srv.listen(0, '127.0.0.1', () => resolve({ url: `http://127.0.0.1:${srv.address().port}`, seen, close: () => new Promise((r) => srv.close(r)) }));
  });
}

test('llmCall posts to /llm with the bearer token and the SPEC section 8 body; returns result', async () => {
  const stub = await startStub(() => ({ json: { ok: true, status: 'success', result: { vendor: 'ACME' } } }));
  try {
    const log = eventSink();
    const out = await llmCall({ prompt: 'classify', context: 'doc', schema: { vendor: 'string' }, caller: 'invoice-router', template: 'cls@1', classification: 'internal', timeoutS: 5 },
      { env: { AUTOMAGIX_BROKER_URL: stub.url, AUTOMAGIX_BROKER_TOKEN: 'tok123' }, logEvent: log });
    assert.deepEqual(out, { vendor: 'ACME' });
    const req = stub.seen[0];
    assert.equal(req.method, 'POST');
    assert.equal(req.url, '/llm');
    assert.equal(req.headers.authorization, 'Bearer tok123');
    assert.deepEqual(Object.keys(req.body).sort(), ['caller', 'classification', 'context', 'decision_log', 'inject_context', 'prompt', 'schema', 'template', 'timeout_s']);
    assert.equal(req.body.prompt, 'classify');
    assert.equal(req.body.context, 'doc');
    assert.equal(req.body.caller, 'invoice-router');
    assert.equal(req.body.timeout_s, 5);
    assert.deepEqual(req.body.schema.required, ['vendor']); // shape converted to JSON-Schema for the broker
    assert.equal(log.list.length, 0, 'broker logs the llm_call itself; the client adds nothing on success');
  } finally {
    await stub.close();
  }
});

test('no key is ever sent or read: only the broker token is used', async () => {
  const stub = await startStub(() => ({ json: { ok: true, status: 'success', result: 'hi' } }));
  try {
    await llmCall({ prompt: 'p' }, { env: { AUTOMAGIX_BROKER_URL: stub.url, AUTOMAGIX_BROKER_TOKEN: 't', AUTOSTATION_LLM_KEY: 'sk-secret' }, logEvent: eventSink() });
    const dump = JSON.stringify(stub.seen[0]);
    assert.ok(!dump.includes('sk-secret'));
  } finally {
    await stub.close();
  }
});

test('non-success statuses, unauthorized, and null results all return null', async () => {
  for (const json of [{ ok: false, status: 'schema_failure', result: null }, { ok: false, status: 'disabled', result: null }, { ok: false, error: 'unauthorized' }, { ok: true, status: 'success', result: null }]) {
    const stub = await startStub(() => ({ code: json.error ? 401 : 200, json }));
    try {
      assert.equal(await llmCall({ prompt: 'p' }, { env: { AUTOMAGIX_BROKER_URL: stub.url, AUTOMAGIX_BROKER_TOKEN: 't' }, logEvent: eventSink() }), null);
    } finally {
      await stub.close();
    }
  }
});

test('schema-invalid result from the broker is rejected locally and logged as llm_schema_failure', async () => {
  const stub = await startStub(() => ({ json: { ok: true, status: 'success', result: { vendor: 5 } } }));
  try {
    const log = eventSink();
    const out = await llmCall({ prompt: 'p', schema: { vendor: 'string' }, caller: 'c' }, { env: { AUTOMAGIX_BROKER_URL: stub.url, AUTOMAGIX_BROKER_TOKEN: 't' }, logEvent: log });
    assert.equal(out, null);
    assert.equal(log.named('llm_schema_failure').length, 1);
  } finally {
    await stub.close();
  }
});

test('unreachable broker => null + llm_call error event; never throws', async () => {
  const log = eventSink();
  const out = await llmCall({ prompt: 'p', caller: 'c' }, { env: { AUTOMAGIX_BROKER_URL: 'http://127.0.0.1:1', AUTOMAGIX_BROKER_TOKEN: 't' }, logEvent: log });
  assert.equal(out, null);
  const ev = log.named('llm_call')[0].data;
  assert.equal(ev.status, 'error');
  assert.match(ev.detail, /broker unreachable/);
});

test('missing broker env => null + llm_call disabled event', async () => {
  const log = eventSink();
  assert.equal(await llmCall({ prompt: 'p', caller: 'c' }, { env: {}, logEvent: log }), null);
  assert.equal(log.named('llm_call')[0].data.status, 'disabled');
});

test('fetch that throws synchronously or rejects is swallowed', async () => {
  const env = { AUTOMAGIX_BROKER_URL: 'http://x', AUTOMAGIX_BROKER_TOKEN: 't' };
  assert.equal(await llmCall({ prompt: 'p' }, { env, logEvent: eventSink(), fetch: () => { throw new Error('sync boom'); } }), null);
  assert.equal(await llmCall({ prompt: 'p' }, { env, logEvent: eventSink(), fetch: async () => { throw new Error('async boom'); } }), null);
});

test('timeout aborts the request (never hangs the automation)', async () => {
  const env = { AUTOMAGIX_BROKER_URL: 'http://x', AUTOMAGIX_BROKER_TOKEN: 't' };
  let aborted = false;
  const fetchFn = (url, init) => new Promise((resolve, reject) => {
    init.signal.addEventListener('abort', () => { aborted = true; reject(Object.assign(new Error('aborted'), { name: 'AbortError' })); });
  });
  // timeoutS=-15 makes the abort timer fire immediately without a real wait
  const out = await llmCall({ prompt: 'p', timeoutS: -15 }, { env, logEvent: eventSink(), fetch: fetchFn });
  assert.equal(out, null);
  assert.equal(aborted, true);
});
