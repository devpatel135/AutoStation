'use strict';
/**
 * MCP (Model Context Protocol) server logic - stdio JSON-RPC 2.0, protocol version 2024-11-05.
 * Protocol handling and the tool registry are dependency-injected so they are testable without a browser:
 *   createToolRegistry({ getPage, allowEvaluate, allowedOrigins, allowPrivate, deps })  -> { list(), call(name, args) }
 *
 * Network policy (code, not prompt): DENY BY DEFAULT - with no allowedOrigins every tool refuses; loopback/private/link-local/metadata
 * hosts are refused even when allow-listed unless allowPrivate; the page's origin is checked before every tool, after every navigation,
 * click and press (an out-of-policy page is reset to about:blank), and navigations/private requests are aborted at request level.
 *   createServer({ registry })                                           -> { handleMessage(msg) }
 *   serveStdio(server, input, output)                                    -> Promise (resolves when input closes)
 */
const readline = require('node:readline');
const schemaLib = require('./schema');

const PROTOCOL_VERSION = '2024-11-05';
const SERVER_INFO = { name: 'amx-browser', version: require('../package.json').version };

function textContent(text) {
  return { type: 'text', text: typeof text === 'string' ? text : JSON.stringify(text, null, 2) };
}

// ---- network policy -----------------------------------------------------------------------------------------
/** inet_aton-style IPv4 (dotted decimal/octal/hex parts or one integer) -> uint32, or null if `h` is not an IPv4 literal. */
function ipv4ToInt(h) {
  const parts = String(h).split('.');
  if (parts.length > 4 || parts.some((p) => p === '')) return null;
  const nums = parts.map((p) => {
    if (/^0x[0-9a-f]+$/i.test(p)) return parseInt(p, 16);
    if (/^0[0-7]+$/.test(p)) return parseInt(p, 8);
    if (/^\d+$/.test(p)) return parseInt(p, 10);
    return NaN;
  });
  if (nums.some((n) => Number.isNaN(n))) return null;
  const last = nums[nums.length - 1];
  if (last >= 256 ** (5 - nums.length)) return null;
  let v = 0;
  for (let i = 0; i < nums.length - 1; i += 1) {
    if (nums[i] > 255) return null;
    v += nums[i] * 256 ** (3 - i);
  }
  return v + last;
}

function privateV4(v) {
  const a = Math.floor(v / 16777216);
  const b = Math.floor(v / 65536) % 256;
  const c = Math.floor(v / 256) % 256;
  if (a === 0 || a === 10 || a === 127 || a >= 224) return true; // this-net, private, loopback, multicast/reserved/broadcast
  if (a === 100 && b >= 64 && b <= 127) return true; // CGNAT
  if (a === 169 && b === 254) return true; // link-local incl. cloud metadata 169.254.169.254
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 192 && b === 0 && c === 0) return true;
  if (a === 198 && (b === 18 || b === 19)) return true;
  return false;
}

/** IPv6 text -> 8 groups, or null. Understands `::` and an embedded dotted IPv4 tail. */
function expandV6(h) {
  let s = String(h).toLowerCase().split('%')[0];
  const dotted = /(\d+\.\d+\.\d+\.\d+)$/.exec(s);
  if (dotted) {
    const v = ipv4ToInt(dotted[1]);
    if (v === null) return null;
    s = `${s.slice(0, -dotted[1].length)}${Math.floor(v / 65536).toString(16)}:${(v % 65536).toString(16)}`;
  }
  const halves = s.split('::');
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(':') : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
  const fill = 8 - head.length - tail.length;
  if (halves.length === 1 ? head.length !== 8 : fill < 1) return null;
  const groups = [...head, ...(halves.length === 2 ? Array(fill).fill('0') : []), ...tail].map((g) => (/^[0-9a-f]{1,4}$/.test(g) ? parseInt(g, 16) : NaN));
  return groups.length === 8 && groups.every((g) => !Number.isNaN(g)) ? groups : null;
}

/** true for loopback, private, link-local, CGNAT, multicast, unspecified, cloud-metadata and intranet-style hosts (IPv4/IPv6/names). */
function isPrivateHost(hostname) {
  let h = String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '').replace(/\.+$/, '');
  if (!h) return true;
  if (h.includes(':')) {
    const g = expandV6(h);
    if (!g) return true; // unparseable: fail closed
    const v4 = (hi, lo) => hi * 65536 + lo;
    if (g.every((x) => x === 0) || (g.slice(0, 7).every((x) => x === 0) && g[7] === 1)) return true; // :: and ::1
    if (g.slice(0, 5).every((x) => x === 0) && g[5] === 0xffff) return privateV4(v4(g[6], g[7])); // ::ffff:a.b.c.d
    if (g.slice(0, 6).every((x) => x === 0)) return true; // IPv4-compatible (deprecated)
    if (g[0] === 0x64 && g[1] === 0xff9b && g.slice(2, 6).every((x) => x === 0)) return privateV4(v4(g[6], g[7])); // NAT64
    if (g[0] === 0x2002) return privateV4(v4(g[1], g[2])); // 6to4
    if ((g[0] & 0xfe00) === 0xfc00 || (g[0] & 0xffc0) === 0xfe80 || (g[0] & 0xff00) === 0xff00) return true; // ULA, link-local, multicast
    return false;
  }
  const n = ipv4ToInt(h);
  if (n !== null) return privateV4(n);
  if (!h.includes('.')) return true; // single-label intranet name (and `localhost`)
  return /(?:^|\.)(?:localhost|local|internal|intranet|lan|corp|home|localdomain|home\.arpa)$/.test(h);
}

function normalizeOrigins(list) {
  const out = new Set();
  for (const raw of Array.isArray(list) ? list : []) {
    try {
      const u = new URL(String(raw).trim());
      if (u.protocol === 'http:' || u.protocol === 'https:') out.add(u.origin);
    } catch (_) { /* an unparseable entry allows nothing */ }
  }
  return out;
}

/**
 * Policy check for one URL. DENY BY DEFAULT: an empty allow-list refuses everything. Loopback/private/link-local/metadata hosts are
 * refused even when allow-listed unless allowPrivate. `about:blank` is the neutral page and is fine once an allow-list exists.
 */
function originAllowed(url, allowedOrigins, { allowPrivate = false } = {}) {
  const allowed = allowedOrigins instanceof Set ? allowedOrigins : normalizeOrigins(allowedOrigins);
  if (!allowed.size) return { ok: false, error: 'no origins are allowed: set AMX_MCP_ALLOWED_ORIGINS (comma-separated origins) to enable browsing' };
  if (url === 'about:blank') return { ok: true };
  if (typeof url !== 'string' || /[\s\u0000-\u001f\\]/.test(url)) return { ok: false, error: 'invalid URL (whitespace, control characters or backslash)' };
  let u;
  try { u = new URL(url); } catch (_) { return { ok: false, error: 'invalid URL' }; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return { ok: false, error: 'only http(s) URLs are allowed' };
  if (u.username || u.password) return { ok: false, error: 'URLs with embedded credentials are not allowed' };
  if (!allowPrivate && isPrivateHost(u.hostname)) return { ok: false, error: `host ${u.hostname} is loopback/private/link-local (set AMX_MCP_ALLOW_PRIVATE=1 to allow)` };
  if (!allowed.has(u.origin)) return { ok: false, error: `origin ${u.origin} is not allowed` };
  return { ok: true };
}

async function defaultLookup(hostname) {
  return require('node:dns').promises.lookup(hostname, { all: true });
}

function createToolRegistry({ getPage: rawGetPage, allowEvaluate = false, allowedOrigins = [], allowPrivate = false, deps = {} } = {}) {
  const cdp = deps.cdp || require('./cdp');
  const table = deps.table || require('./adapters/generic-table');
  const lookup = deps.lookup || defaultLookup;
  const allowed = normalizeOrigins(allowedOrigins);
  const policy = (url) => originAllowed(url, allowed, { allowPrivate });
  const obj = (properties, required = []) => ({ type: 'object', properties, required, additionalProperties: false });

  // request-level enforcement (redirect hops included): navigations to a disallowed origin and ANY request to a private host are aborted
  const routed = new WeakSet();
  async function ensureRouting(page) {
    if (!page || routed.has(page) || typeof page.route !== 'function') return;
    routed.add(page);
    await page.route('**/*', (route) => {
      try {
        const req = route.request();
        const url = req.url();
        let u = null;
        try { u = new URL(url); } catch (_) { u = null; }
        if (!u || (u.protocol !== 'http:' && u.protocol !== 'https:')) {
          if (req.isNavigationRequest() && url !== 'about:blank') return route.abort('blockedbyclient');
          return route.continue();
        }
        if (!allowPrivate && isPrivateHost(u.hostname)) return route.abort('blockedbyclient');
        if (req.isNavigationRequest() && !policy(url).ok) return route.abort('blockedbyclient');
        return route.continue();
      } catch (_) {
        return route.abort('blockedbyclient');
      }
    });
  }

  /** The page every tool acts on. Refuses to act on (or read from) a page that is not on an allowed origin. */
  async function getPage(opts = {}) {
    const page = await rawGetPage();
    await ensureRouting(page);
    if (!opts.skipOriginCheck) {
      const c = policy(page.url());
      if (!c.ok) throw new Error(`current page ${cdp.safeUrl(page.url())} refused: ${c.error}`);
    }
    return page;
  }
  /** After anything that can navigate (goto, click, press): if the page ended up outside the policy, reset it and fail. */
  async function recheck(page) {
    if (typeof page.waitForLoadState === 'function') { try { await page.waitForLoadState('domcontentloaded', { timeout: 3000 }); } catch (_) { /* still loading */ } }
    const c = policy(page.url());
    if (c.ok) return;
    const where = cdp.safeUrl(page.url());
    try { await page.goto('about:blank', { timeout: 5000 }); } catch (_) { /* best effort */ }
    throw new Error(`navigation left the allowed origins (${where}: ${c.error}); page reset to about:blank`);
  }

  const tools = [
    {
      name: 'navigate', description: 'Navigate the browser to a URL (http/https, allow-listed origins only; never loopback/private addresses).',
      inputSchema: obj({ url: { type: 'string', minLength: 1 } }, ['url']),
      handler: async (a) => {
        const c = policy(a.url);
        if (!c.ok) throw new Error(c.error);
        if (!allowPrivate && a.url !== 'about:blank') {
          const host = new URL(a.url).hostname.replace(/^\[|\]$/g, '');
          if (ipv4ToInt(host) === null && !host.includes(':')) { // a name: every address it resolves to must be public
            let addrs;
            try { addrs = await lookup(host); } catch (_) { throw new Error(`cannot resolve ${host}`); }
            if (!addrs || !addrs.length || addrs.some((x) => isPrivateHost(x.address))) throw new Error(`host ${host} resolves to a loopback/private address`);
          }
        }
        const page = await getPage({ skipOriginCheck: true });
        await page.goto(a.url, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await recheck(page);
        return [textContent(`navigated to ${cdp.safeUrl(page.url())}`)];
      },
    },
    {
      name: 'snapshot', description: 'Compact snapshot of interactive elements with selectors (values redacted).',
      inputSchema: obj({ maxChars: { type: 'integer', minimum: 500, maximum: 50000 } }),
      handler: async (a) => [textContent(await cdp.domSnapshot(await getPage(), { maxChars: a.maxChars || 12000 }))],
    },
    {
      name: 'click', description: 'Click an element by CSS selector.',
      inputSchema: obj({ selector: { type: 'string', minLength: 1 } }, ['selector']),
      handler: async (a) => {
        const page = await getPage();
        await page.locator(a.selector).click({ timeout: 10000 });
        await recheck(page);
        return [textContent('clicked')];
      },
    },
    {
      name: 'fill', description: 'Fill an input by CSS selector.',
      inputSchema: obj({ selector: { type: 'string', minLength: 1 }, value: { type: 'string' } }, ['selector', 'value']),
      handler: async (a) => { await (await getPage()).locator(a.selector).fill(a.value, { timeout: 10000 }); return [textContent('filled')]; },
    },
    {
      name: 'press', description: 'Press a keyboard key, optionally focused on a selector.',
      inputSchema: obj({ key: { type: 'string', minLength: 1 }, selector: { type: 'string' } }, ['key']),
      handler: async (a) => {
        const page = await getPage();
        if (a.selector) await page.locator(a.selector).press(a.key, { timeout: 10000 });
        else await page.keyboard.press(a.key);
        await recheck(page);
        return [textContent('pressed')];
      },
    },
    {
      name: 'screenshot', description: 'PNG screenshot of the current page.',
      inputSchema: obj({ fullPage: { type: 'boolean' } }),
      handler: async (a) => [{ type: 'image', data: await cdp.screenshotB64(await getPage(), { fullPage: a.fullPage }), mimeType: 'image/png' }],
    },
    {
      name: 'extract_table', description: 'Scrape a table into an array of row objects (headers normalised).',
      inputSchema: obj({ selector: { type: 'string' }, nextSelector: { type: 'string' }, maxPages: { type: 'integer', minimum: 1, maximum: 100 } }),
      handler: async (a) => [textContent(await table.scrapeTable(await getPage(), { selector: a.selector || 'table', nextSelector: a.nextSelector, maxPages: a.maxPages || 1 }))],
    },
  ];
  if (allowEvaluate) {
    tools.push({
      name: 'evaluate', description: 'Evaluate a JavaScript expression in the page (enabled explicitly; powerful).',
      inputSchema: obj({ expression: { type: 'string', minLength: 1 } }, ['expression']),
      handler: async (a) => {
        const page = await getPage();
        const r = await page.evaluate(a.expression);
        await recheck(page);
        return [textContent(r)];
      },
    });
  }
  const byName = new Map(tools.map((t) => [t.name, t]));

  return {
    has: (name) => byName.has(name) || name === 'evaluate',
    list: () => tools.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })),
    /** Returns {content, isError}. Tool failures are results (isError) not protocol errors, per MCP. */
    async call(name, args) {
      const t = byName.get(name);
      if (!t) {
        return { content: [textContent(name === 'evaluate' ? 'evaluate is disabled (set AMX_MCP_ALLOW_EVALUATE=1 to enable)' : `unknown tool ${name}`)], isError: true };
      }
      const a = args && typeof args === 'object' ? args : {};
      const v = schemaLib.validate(a, t.inputSchema);
      if (!v.ok) return { content: [textContent(`invalid arguments: ${v.error}`)], isError: true };
      try {
        return { content: await t.handler(a), isError: false };
      } catch (err) {
        return { content: [textContent(`${t.name} failed: ${String((err && err.message) || err).split('\n')[0]}`)], isError: true };
      }
    },
  };
}

const rpcError = (id, code, message) => ({ jsonrpc: '2.0', id: id === undefined ? null : id, error: { code, message } });

function createServer({ registry, serverInfo = SERVER_INFO, protocolVersion = PROTOCOL_VERSION } = {}) {
  async function handleOne(msg) {
    if (!msg || typeof msg !== 'object' || Array.isArray(msg) || msg.jsonrpc !== '2.0' || typeof msg.method !== 'string') {
      return rpcError(msg && msg.id, -32600, 'Invalid Request');
    }
    const isNotification = msg.id === undefined || msg.id === null;
    const { id, method, params } = msg;
    if (isNotification) return null; // notifications/initialized, notifications/cancelled, ... need no reply
    switch (method) {
      case 'initialize':
        return { jsonrpc: '2.0', id, result: { protocolVersion, capabilities: { tools: { listChanged: false } }, serverInfo } };
      case 'ping':
        return { jsonrpc: '2.0', id, result: {} };
      case 'tools/list':
        return { jsonrpc: '2.0', id, result: { tools: registry.list() } };
      case 'tools/call': {
        if (!params || typeof params.name !== 'string') return rpcError(id, -32602, 'tools/call requires params.name');
        if (!registry.has(params.name)) return rpcError(id, -32602, `Unknown tool: ${params.name}`);
        const out = await registry.call(params.name, params.arguments);
        return { jsonrpc: '2.0', id, result: out };
      }
      default:
        return rpcError(id, -32601, `Method not found: ${method}`);
    }
  }

  return {
    /** One message or a batch. Resolves to a response, an array of responses, or null (notification only). */
    async handleMessage(msg) {
      if (Array.isArray(msg)) {
        if (!msg.length) return rpcError(null, -32600, 'Invalid Request');
        const out = (await Promise.all(msg.map(handleOne))).filter((r) => r !== null);
        return out.length ? out : null;
      }
      return handleOne(msg);
    },
  };
}

/** Newline-delimited JSON over streams. Messages are processed in order. */
function serveStdio(server, input, output) {
  const rl = readline.createInterface({ input, crlfDelay: Infinity });
  let chain = Promise.resolve();
  const send = (obj) => output.write(`${JSON.stringify(obj)}\n`);
  rl.on('line', (line) => {
    if (!line.trim()) return;
    chain = chain.then(async () => {
      let msg;
      try {
        msg = JSON.parse(line);
      } catch (_) {
        send(rpcError(null, -32700, 'Parse error'));
        return;
      }
      try {
        const res = await server.handleMessage(msg);
        if (res) send(res);
      } catch (err) {
        send(rpcError(msg && msg.id, -32603, `Internal error: ${(err && err.message) || err}`));
      }
    });
  });
  return new Promise((resolve) => rl.on('close', () => chain.then(resolve)));
}

module.exports = { createToolRegistry, createServer, serveStdio, PROTOCOL_VERSION, SERVER_INFO, originAllowed, isPrivateHost, normalizeOrigins };
