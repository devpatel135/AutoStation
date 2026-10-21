'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { PassThrough } = require('node:stream');
const mcp = require('../lib/mcp-server');

function fakePage() {
  const log = [];
  const page = {
    log,
    url: () => 'https://portal.example.com/page?token=secret',
    goto: async (u) => { log.push(['goto', u]); },
    keyboard: { press: async (k) => { log.push(['keyboard', k]); } },
    screenshot: async () => Buffer.from('PNGDATA'),
    evaluate: async (fn, arg) => {
      if (typeof fn === 'function' && arg !== undefined && typeof arg === 'number') {
        return { url: 'https://portal.example.com/page', title: 'T', nodes: [{ i: 1, tag: 'button', role: '', type: '', text: 'Go', name: '', selector: '#go', href: '', hasValue: false, checked: null, disabled: false }] };
      }
      if (typeof fn === 'string') return `eval:${fn}`;
      return { headers: ['Name', 'Qty'], rows: [['a', '1'], ['b', '2']] };
    },
    locator: (sel) => ({
      click: async () => { log.push(['click', sel]); },
      fill: async (v) => { log.push(['fill', sel, v]); },
      press: async (k) => { log.push(['press', sel, k]); },
      count: async () => 0,
    }),
    waitForLoadState: async () => {},
  };
  return page;
}

function setup(opts = {}) {
  const page = fakePage();
  const registry = mcp.createToolRegistry({ getPage: async () => page, ...opts });
  const server = mcp.createServer({ registry });
  const rpc = (method, params, id = 1) => server.handleMessage({ jsonrpc: '2.0', id, method, params });
  return { page, registry, server, rpc };
}

test('initialize handshake returns protocol 2024-11-05, tools capability and serverInfo', async () => {
  const { rpc } = setup();
  const r = await rpc('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'c', version: '1' } });
  assert.equal(r.jsonrpc, '2.0');
  assert.equal(r.id, 1);
  assert.equal(r.result.protocolVersion, '2024-11-05');
  assert.ok(r.result.capabilities.tools);
  assert.equal(r.result.serverInfo.name, 'amx-browser');
});

test('notifications get no reply; ping returns {}', async () => {
  const { server, rpc } = setup();
  assert.equal(await server.handleMessage({ jsonrpc: '2.0', method: 'notifications/initialized' }), null);
  assert.deepEqual((await rpc('ping')).result, {});
});

test('tools/list exposes the browser tools with JSON-Schema inputs; evaluate is hidden by default', async () => {
  const { rpc } = setup();
  const names = (await rpc('tools/list')).result.tools.map((t) => t.name);
  assert.deepEqual(names, ['navigate', 'snapshot', 'click', 'fill', 'press', 'screenshot', 'extract_table']);
  const nav = (await rpc('tools/list')).result.tools[0];
  assert.equal(nav.inputSchema.type, 'object');
  assert.deepEqual(nav.inputSchema.required, ['url']);
  const withEval = setup({ allowEvaluate: true });
  assert.ok((await withEval.rpc('tools/list')).result.tools.some((t) => t.name === 'evaluate'));
});

test('tools/call navigate/click/fill/press/snapshot/screenshot/extract_table', async () => {
  const { rpc, page } = setup();
  const text = (r) => r.result.content[0].text;
  assert.match(text(await rpc('tools/call', { name: 'navigate', arguments: { url: 'https://portal.example.com/x' } })), /navigated to https:\/\/portal\.example\.com\/page\?token=…/, 'URL query values are masked');
  await rpc('tools/call', { name: 'click', arguments: { selector: '#go' } });
  await rpc('tools/call', { name: 'fill', arguments: { selector: '#q', value: 'abc' } });
  await rpc('tools/call', { name: 'press', arguments: { key: 'Enter' } });
  await rpc('tools/call', { name: 'press', arguments: { key: 'Tab', selector: '#q' } });
  assert.deepEqual(page.log, [['goto', 'https://portal.example.com/x'], ['click', '#go'], ['fill', '#q', 'abc'], ['keyboard', 'Enter'], ['press', '#q', 'Tab']]);

  const snap = await rpc('tools/call', { name: 'snapshot', arguments: {} });
  assert.equal(snap.result.isError, false);
  assert.match(text(snap), /\[1\] button "Go" sel=#go/);

  const shot = (await rpc('tools/call', { name: 'screenshot', arguments: {} })).result.content[0];
  assert.deepEqual(shot, { type: 'image', data: Buffer.from('PNGDATA').toString('base64'), mimeType: 'image/png' });

  const tbl = JSON.parse(text(await rpc('tools/call', { name: 'extract_table', arguments: { selector: '#t' } })));
  assert.deepEqual(tbl.rows, [{ name: 'a', qty: '1' }, { name: 'b', qty: '2' }]);
});

test('tool failures are results with isError, not protocol errors; argument validation happens first', async () => {
  const { rpc } = setup();
  const bad = await rpc('tools/call', { name: 'fill', arguments: { selector: '#q' } });
  assert.equal(bad.result.isError, true);
  assert.match(bad.result.content[0].text, /invalid arguments/);
  const extra = await rpc('tools/call', { name: 'click', arguments: { selector: '#a', evil: 1 } });
  assert.equal(extra.result.isError, true);
  const throwing = setup();
  throwing.page.locator = () => ({ click: async () => { throw new Error('Timeout 10000ms exceeded\nCall log...'); } });
  const r = await throwing.rpc('tools/call', { name: 'click', arguments: { selector: '#x' } });
  assert.equal(r.result.isError, true);
  assert.equal(r.result.content[0].text, 'click failed: Timeout 10000ms exceeded');
});

test('navigate enforces http(s) and the origin allow-list', async () => {
  const { rpc, page } = setup({ allowedOrigins: ['https://portal.example.com'] });
  for (const url of ['file:///c:/secrets.txt', 'javascript:alert(1)', 'https://evil.example.net/']) {
    const r = await rpc('tools/call', { name: 'navigate', arguments: { url } });
    assert.equal(r.result.isError, true, url);
  }
  assert.deepEqual(page.log, []);
  assert.equal((await rpc('tools/call', { name: 'navigate', arguments: { url: 'https://portal.example.com/ok' } })).result.isError, false);
});

test('evaluate is disabled by default and enabled only explicitly', async () => {
  const off = setup();
  const r = await off.rpc('tools/call', { name: 'evaluate', arguments: { expression: '1+1' } });
  assert.equal(r.result.isError, true);
  assert.match(r.result.content[0].text, /disabled/);
  const on = setup({ allowEvaluate: true });
  const ok = await on.rpc('tools/call', { name: 'evaluate', arguments: { expression: '1+1' } });
  assert.equal(ok.result.isError, false);
  assert.match(ok.result.content[0].text, /eval:1\+1/);
});

test('protocol errors: unknown method, unknown tool, malformed requests', async () => {
  const { server, rpc } = setup();
  assert.equal((await rpc('resources/list')).error.code, -32601);
  assert.equal((await rpc('tools/call', { name: 'nope', arguments: {} })).error.code, -32602);
  assert.equal((await rpc('tools/call', {})).error.code, -32602);
  assert.equal((await server.handleMessage({ id: 1, method: 'ping' })).error.code, -32600);
  assert.equal((await server.handleMessage('junk')).error.code, -32600);
  assert.equal((await server.handleMessage([])).error.code, -32600);
});

test('batch requests return an array of responses and omit notifications', async () => {
  const { server } = setup();
  const out = await server.handleMessage([
    { jsonrpc: '2.0', id: 1, method: 'ping' },
    { jsonrpc: '2.0', method: 'notifications/initialized' },
    { jsonrpc: '2.0', id: 2, method: 'tools/list' },
  ]);
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((r) => r.id), [1, 2]);
});

test('serveStdio: newline-delimited JSON in, ordered responses out, parse errors reported, resolves on close', async () => {
  const { server } = setup();
  const input = new PassThrough();
  const output = new PassThrough();
  const chunks = [];
  output.on('data', (c) => chunks.push(c));
  const done = mcp.serveStdio(server, input, output);
  input.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })}\n`);
  input.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })}\n`);
  input.write('{not json}\n');
  input.write(`${JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list' })}\n`);
  input.end();
  await done;
  await new Promise((resolve) => setImmediate(resolve)); // let the PassThrough flush its 'data' events
  const lines = Buffer.concat(chunks).toString('utf8').trim().split('\n').map((l) => JSON.parse(l));
  assert.equal(lines.length, 3);
  assert.equal(lines[0].id, 1);
  assert.equal(lines[1].error.code, -32700);
  assert.equal(lines[2].id, 2);
  assert.ok(lines[2].result.tools.length >= 7);
});
