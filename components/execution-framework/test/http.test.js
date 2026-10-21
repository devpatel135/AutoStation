'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const E = require('../lib/errors');
const { client, HttpError, buildUrl } = require('../lib/http');

function fakeTransport(responses) {
  const queue = [...responses];
  const calls = [];
  let last;
  const t = async (opts, body) => {
    calls.push({ opts, body });
    if (queue.length) last = queue.shift(); // the final response is sticky
    if (last instanceof Error) throw last;
    return last;
  };
  t.calls = calls;
  return t;
}

const ok = (body = '{"a":1}') => ({ status: 200, headers: { 'content-type': 'application/json' }, body });
const status = (s, body = 'nope') => ({ status: s, headers: {}, body });

const noSleep = async () => {};

test('buildUrl joins relative paths, sets query, and blocks cross-origin targets', () => {
  assert.equal(buildUrl('https://api.example.com/v1/', 'items', { page: 2, q: undefined }).href, 'https://api.example.com/v1/items?page=2');
  assert.equal(buildUrl('https://api.example.com', '/x?y=1').href, 'https://api.example.com/x?y=1');
  assert.throws(() => buildUrl('https://api.example.com', 'https://evil.example.net/x'), E.DataError);
});

test('2xx: parses JSON, returns status/headers/body; sends JSON bodies with Content-Type and Length', async () => {
  const transport = fakeTransport([ok('{"id":7}')]);
  const c = client({ baseUrl: 'https://api.example.com', headers: { 'X-Team': 'ap' }, transport });
  const r = await c.postJson('/invoices', { n: 1 });
  assert.deepEqual(r, { id: 7 });
  const sent = transport.calls[0];
  assert.equal(sent.opts.method, 'POST');
  assert.equal(sent.opts.path, '/invoices');
  assert.equal(sent.opts.headers['Content-Type'], 'application/json');
  assert.equal(sent.opts.headers['X-Team'], 'ap');
  assert.equal(sent.opts.headers['Content-Length'], Buffer.byteLength('{"n":1}'));
  assert.equal(sent.body, '{"n":1}');
});

test('status codes are classified into the SPEC categories (usable by runWithRetry)', async () => {
  const cases = [[401, 'AUTH_EXPIRED'], [403, 'AUTH_EXPIRED'], [503, 'SYSTEM_UNAVAILABLE'], [504, 'SYSTEM_UNAVAILABLE'], [429, 'SYSTEM_UNAVAILABLE'],
    [500, 'SYSTEM_UNAVAILABLE'], [404, 'DATA_ERROR'], [422, 'DATA_ERROR'], [400, 'DATA_ERROR']];
  for (const [s, cat] of cases) {
    const c = client({ baseUrl: 'https://api.example.com', transport: fakeTransport([status(s)]) });
    await assert.rejects(c.get('/x'), (e) => {
      assert.ok(e instanceof HttpError);
      assert.equal(e.status, s);
      assert.equal(E.classifyException(e), cat, String(s));
      assert.equal(e.message, `HTTP ${s} GET /x`);
      assert.ok(!e.message.includes('nope'), 'body must not leak into the message');
      assert.equal(e.snippet, 'nope');
      return true;
    });
  }
});

test('transport failures become NetworkError (ECONNRESET / ETIMEDOUT)', async () => {
  for (const code of ['ECONNRESET', 'ETIMEDOUT', 'ENOTFOUND']) {
    const c = client({ baseUrl: 'https://api.example.com', transport: fakeTransport([Object.assign(new Error(`boom ${code}`), { code })]) });
    await assert.rejects(c.get('/x'), (e) => e instanceof E.NetworkError && E.classifyException(e) === 'NETWORK_ERROR');
  }
});

test('built-in retry: idempotent methods retry on 503 with exponential waits; POST does not', async () => {
  const waits = [];
  const sleep = async (ms) => { waits.push(ms); };
  const t1 = fakeTransport([status(503), status(503), ok()]);
  const c1 = client({ baseUrl: 'https://api.example.com', transport: t1, retry: { retries: 3, waitMs: 100, multiplier: 2 }, sleep });
  assert.deepEqual((await c1.get('/x')).json, { a: 1 });
  assert.deepEqual(waits, [100, 200]);
  assert.equal(t1.calls.length, 3);

  const t2 = fakeTransport([status(503), ok()]);
  const c2 = client({ baseUrl: 'https://api.example.com', transport: t2, retry: { retries: 3, waitMs: 1 }, sleep: noSleep });
  await assert.rejects(c2.post('/x', { json: {} }), HttpError);
  assert.equal(t2.calls.length, 1, 'non-idempotent request must not be retried');

  const t3 = fakeTransport([status(503), ok()]);
  const c3 = client({ baseUrl: 'https://api.example.com', transport: t3, retry: { retries: 3, waitMs: 1, idempotentOnly: false }, sleep: noSleep });
  assert.equal((await c3.post('/x', { json: {} })).status, 200);
});

test('retry stops at the configured limit and never retries 4xx', async () => {
  const t = fakeTransport([status(503)]);
  const c = client({ baseUrl: 'https://api.example.com', transport: t, retry: { retries: 2, waitMs: 1 }, sleep: noSleep });
  await assert.rejects(c.get('/x'), HttpError);
  assert.equal(t.calls.length, 3);
  const t404 = fakeTransport([status(404)]);
  await assert.rejects(client({ baseUrl: 'https://api.example.com', transport: t404, retry: { retries: 5 }, sleep: noSleep }).get('/x'), HttpError);
  assert.equal(t404.calls.length, 1);
});

test('getJson throws DataError when the body is not JSON', async () => {
  const c = client({ baseUrl: 'https://api.example.com', transport: fakeTransport([{ status: 200, headers: {}, body: '<html>' }]) });
  await assert.rejects(c.getJson('/x'), E.DataError);
});

test('baseUrl is required', () => {
  assert.throws(() => client({}), /baseUrl is required/);
});

test('real HTTP round trip through the default transport (plain http on loopback)', async () => {
  const srv = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      if (req.url === '/echo') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ method: req.method, body: Buffer.concat(chunks).toString('utf8'), team: req.headers['x-team'] }));
      } else {
        res.writeHead(503);
        res.end('down');
      }
    });
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${srv.address().port}`;
  try {
    const c = client({ baseUrl: base, headers: { 'X-Team': 'ap' }, timeoutMs: 5000 });
    assert.deepEqual(await c.postJson('/echo', { k: 'v' }), { method: 'POST', body: '{"k":"v"}', team: 'ap' });
    await assert.rejects(c.get('/broken'), (e) => e.status === 503 && E.classifyException(e) === 'SYSTEM_UNAVAILABLE');
  } finally {
    await new Promise((r) => srv.close(r));
  }
  const dead = client({ baseUrl: 'http://127.0.0.1:1', timeoutMs: 2000 });
  await assert.rejects(dead.get('/x'), (e) => E.classifyException(e) === 'NETWORK_ERROR');
});
