'use strict';
// MCP browser server network policy: deny by default, never loopback/private/link-local/metadata, origin re-checked after actions.
const test = require('node:test');
const assert = require('node:assert/strict');
const mcp = require('../lib/mcp-server');

const PORTAL = 'https://portal.example.com';
const PUBLIC_DNS = async () => [{ address: '93.184.216.34' }];

function fakePage(state = { url: `${PORTAL}/home` }) {
  const log = [];
  const page = {
    log,
    state,
    url: () => state.url,
    goto: async (u) => { log.push(['goto', u]); state.url = state.redirectTo && u !== 'about:blank' ? state.redirectTo : u; },
    keyboard: { press: async (k) => { log.push(['keyboard', k]); if (state.onAction) state.url = state.onAction; } },
    screenshot: async () => { log.push(['screenshot']); return Buffer.from('PNG'); },
    evaluate: async () => { log.push(['evaluate']); return { url: state.url, title: 'T', nodes: [] }; },
    locator: (sel) => ({
      click: async () => { log.push(['click', sel]); if (state.onAction) state.url = state.onAction; },
      fill: async (v) => { log.push(['fill', sel, v]); },
      press: async (k) => { log.push(['press', sel, k]); if (state.onAction) state.url = state.onAction; },
      count: async () => 0,
    }),
    waitForLoadState: async () => {},
  };
  return page;
}

function setup(opts = {}, page = fakePage()) {
  const registry = mcp.createToolRegistry({ getPage: async () => page, deps: { lookup: PUBLIC_DNS }, ...opts });
  const server = mcp.createServer({ registry });
  const call = async (name, args = {}) => (await server.handleMessage({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } })).result;
  return { page, call, registry };
}
const text = (r) => r.content[0].text;

test('isPrivateHost: loopback, private, link-local, CGNAT, metadata, odd encodings, IPv6 forms and intranet names are private', () => {
  const priv = ['localhost', 'LOCALHOST.', 'foo.localhost', '127.0.0.1', '127.1', '2130706433', '0x7f000001', '0x7f.0.0.1', '017700000001', '10.0.0.5', '172.16.0.1', '172.31.255.255',
    '192.168.1.1', '169.254.169.254', '100.64.0.1', '0.0.0.0', '224.0.0.1', '255.255.255.255', '192.0.0.8', '198.18.0.1', '[::1]', '::1', '::', 'fe80::1', 'fe80::1%eth0', 'fc00::1', 'fd12:3456::1',
    'ff02::1', '::ffff:127.0.0.1', '::ffff:7f00:1', '::ffff:a00:1', '64:ff9b::7f00:1', '2002:7f00:1::', '::127.0.0.1', 'metadata.google.internal', 'wiki', 'printer.local', 'host.corp', 'db.lan', '', 'not:an:ip'];
  for (const h of priv) assert.equal(mcp.isPrivateHost(h), true, JSON.stringify(h));
  const pub = ['93.184.216.34', '8.8.8.8', '1.1.1.1', '172.32.0.1', '172.15.0.1', '100.63.0.1', '100.128.0.1', 'portal.example.com', 'develop.deloitte.com', '2606:2800:220:1:248:1893:25c8:1946', '::ffff:5db8:d822'];
  for (const h of pub) assert.equal(mcp.isPrivateHost(h), false, h);
});

test('originAllowed is deny-by-default: an empty or invalid allow-list refuses everything', () => {
  for (const list of [undefined, [], [''], ['not a url'], ['ftp://portal.example.com'], ['javascript:alert(1)']]) {
    const r = mcp.originAllowed(`${PORTAL}/x`, list);
    assert.equal(r.ok, false, JSON.stringify(list));
    assert.match(r.error, /no origins are allowed/);
  }
  assert.equal(mcp.originAllowed('about:blank', []).ok, false);
  assert.equal(mcp.originAllowed(`${PORTAL}/x`, [PORTAL]).ok, true);
  assert.equal(mcp.originAllowed('about:blank', [PORTAL]).ok, true);
});

test('originAllowed: look-alike hosts, other ports/schemes and embedded credentials are refused', () => {
  const list = [PORTAL];
  for (const u of ['https://portal.example.com.evil.net/', 'https://portal.example.com@evil.net/', 'https://evil.net/https://portal.example.com', 'http://portal.example.com/',
    'https://portal.example.com:8443/', 'https://user:pw@portal.example.com/', 'https://portal.example.com\\@evil.net/', 'file:///c:/secrets', 'javascript:alert(1)', 'data:text/html,x', 'ftp://portal.example.com/']) {
    assert.equal(mcp.originAllowed(u, list).ok, false, u);
  }
});

test('with no AMX_MCP_ALLOWED_ORIGINS every tool refuses and never touches the page', async () => {
  for (const opts of [{}, { allowedOrigins: [] }, { allowedOrigins: [''] }]) {
    const { page, call } = setup({ ...opts, allowEvaluate: true });
    for (const [name, args] of [['navigate', { url: `${PORTAL}/x` }], ['snapshot', {}], ['click', { selector: '#a' }], ['fill', { selector: '#a', value: 'v' }], ['press', { key: 'Enter' }],
      ['screenshot', {}], ['extract_table', {}], ['evaluate', { expression: '1' }]]) {
      const r = await call(name, args);
      assert.equal(r.isError, true, name);
      assert.match(text(r), /no origins are allowed|refused/, name);
    }
    assert.deepEqual(page.log.filter((l) => l[0] !== 'goto'), []);
    assert.deepEqual(page.log.filter((l) => l[0] === 'goto'), [], 'no navigation either');
  }
});

test('loopback/private/link-local/metadata hosts are refused even when allow-listed, unless AMX_MCP_ALLOW_PRIVATE', async () => {
  const listed = ['http://127.0.0.1:8080', 'http://localhost:3000', 'http://169.254.169.254', 'http://10.1.2.3', 'http://192.168.0.10', 'http://2130706433'];
  const { page, call } = setup({ allowedOrigins: listed }, fakePage({ url: 'about:blank' }));
  for (const u of ['http://127.0.0.1:8080/admin', 'http://localhost:3000/', 'http://169.254.169.254/latest/meta-data/', 'http://10.1.2.3/', 'http://192.168.0.10/', 'http://2130706433/']) {
    const r = await call('navigate', { url: u });
    assert.equal(r.isError, true, u);
    assert.match(text(r), /loopback|private/, u);
  }
  assert.deepEqual(page.log, []);
  const ok = setup({ allowedOrigins: ['http://127.0.0.1:8080'], allowPrivate: true }, fakePage({ url: 'about:blank' }));
  assert.equal((await ok.call('navigate', { url: 'http://127.0.0.1:8080/x' })).isError, false);
});

test('a public name that resolves to a private address (any of its addresses) is refused; unresolvable names fail closed', async () => {
  for (const addrs of [[{ address: '10.0.0.7' }], [{ address: '93.184.216.34' }, { address: '169.254.169.254' }], [{ address: '::1' }], []]) {
    const { page, call } = setup({ deps: { lookup: async () => addrs }, allowedOrigins: [PORTAL] }, fakePage({ url: 'about:blank' }));
    const r = await call('navigate', { url: `${PORTAL}/x` });
    assert.equal(r.isError, true, JSON.stringify(addrs));
    assert.match(text(r), /resolves to a loopback\/private/);
    assert.deepEqual(page.log, []);
  }
  const failing = setup({ deps: { lookup: async () => { throw new Error('ENOTFOUND'); } }, allowedOrigins: [PORTAL] }, fakePage({ url: 'about:blank' }));
  assert.match(text(await failing.call('navigate', { url: `${PORTAL}/x` })), /cannot resolve/);
});

test('click and press that navigate off the allowed origins reset the page to about:blank and report an error', async () => {
  for (const [tool, args] of [['click', { selector: '#link' }], ['press', { key: 'Enter' }], ['press', { key: 'Enter', selector: '#q' }]]) {
    const state = { url: `${PORTAL}/home`, onAction: 'https://evil.example.net/phish?token=abc' };
    const page = fakePage(state);
    const { call } = setup({ allowedOrigins: [PORTAL] }, page);
    const r = await call(tool, args);
    assert.equal(r.isError, true, tool);
    assert.match(text(r), /left the allowed origins/);
    assert.ok(!text(r).includes('abc'), 'query values are masked');
    assert.deepEqual(page.log[page.log.length - 1], ['goto', 'about:blank']);
    assert.equal(state.url, 'about:blank');
  }
  const stay = fakePage({ url: `${PORTAL}/home`, onAction: `${PORTAL}/next` });
  assert.equal((await setup({ allowedOrigins: [PORTAL] }, stay).call('click', { selector: '#ok' })).isError, false);
});

test('a redirect after navigate that lands outside the policy is reset; a landing on a private address too', async () => {
  for (const redirectTo of ['https://evil.example.net/', 'http://169.254.169.254/latest/', 'http://127.0.0.1:9222/json']) {
    const state = { url: 'about:blank', redirectTo };
    const page = fakePage(state);
    const { call } = setup({ allowedOrigins: [PORTAL, 'http://169.254.169.254', 'http://127.0.0.1:9222'] }, page);
    const r = await call('navigate', { url: `${PORTAL}/start` });
    assert.equal(r.isError, true, redirectTo);
    assert.match(text(r), /left the allowed origins/);
    assert.equal(state.url, 'about:blank');
  }
});

test('every page tool refuses to act on or read from a page that is not on an allowed origin', async () => {
  const state = { url: 'https://evil.example.net/page' };
  const page = fakePage(state);
  const { call } = setup({ allowedOrigins: [PORTAL], allowEvaluate: true }, page);
  for (const [name, args] of [['snapshot', {}], ['click', { selector: '#a' }], ['fill', { selector: '#a', value: 'v' }], ['press', { key: 'Tab' }], ['screenshot', {}], ['extract_table', {}], ['evaluate', { expression: '1' }]]) {
    const r = await call(name, args);
    assert.equal(r.isError, true, name);
    assert.match(text(r), /refused/, name);
  }
  assert.deepEqual(page.log, []);
  // navigate is the way back: it is judged by its target, not by where the page currently is
  const back = await call('navigate', { url: `${PORTAL}/home` });
  assert.equal(back.isError, false);
});

test('evaluate stays off unless explicitly enabled (and then still obeys the origin policy)', async () => {
  const off = setup({ allowedOrigins: [PORTAL] });
  assert.match(text(await off.call('evaluate', { expression: '1' })), /disabled/);
  assert.ok(!off.registry.list().some((t) => t.name === 'evaluate'));
});

test('request-level enforcement: navigations off-policy and any private-host request are aborted; ordinary subresources continue', async () => {
  const page = fakePage();
  let handler = null;
  let routes = 0;
  page.route = async (pattern, fn) => { routes += 1; handler = fn; assert.equal(pattern, '**/*'); };
  const { call } = setup({ allowedOrigins: [PORTAL] }, page);
  await call('click', { selector: '#a' });
  await call('click', { selector: '#b' });
  assert.equal(routes, 1, 'routing is installed once per page');
  const decide = (url, nav) => {
    const out = [];
    handler({ request: () => ({ url: () => url, isNavigationRequest: () => nav }), abort: (why) => out.push(['abort', why]), continue: () => out.push(['continue']) });
    return out[0][0];
  };
  assert.equal(decide('https://evil.example.net/', true), 'abort');
  assert.equal(decide(`${PORTAL}/next`, true), 'continue');
  assert.equal(decide('http://127.0.0.1:9222/json', true), 'abort');
  assert.equal(decide('http://169.254.169.254/latest/', false), 'abort');
  assert.equal(decide('http://192.168.1.1/img.png', false), 'abort');
  assert.equal(decide('https://cdn.example.org/app.js', false), 'continue');
  assert.equal(decide('data:image/png;base64,AAAA', false), 'continue');
  assert.equal(decide('file:///c:/secrets.txt', true), 'abort');
  assert.equal(decide('not a url', false), 'continue');
});
