'use strict';
/**
 * MCP (Model Context Protocol) server logic - stdio JSON-RPC 2.0, protocol version 2024-11-05.
 * Protocol handling and the tool registry are dependency-injected so they are testable without a browser:
 *   createToolRegistry({ getPage, allowEvaluate, allowedOrigins, deps })  -> { list(), call(name, args) }
 *   createServer({ registry })                                           -> { handleMessage(msg) }
 *   serveStdio(server, input, output)                                    -> Promise (resolves when input closes)
 */
const readline = require('node:readline');
const schemaLib = require('./schema');

const PROTOCOL_VERSION = '2024-11-05';
const SERVER_INFO = { name: 'amx-browser', version: '0.1.0' };

function textContent(text) {
  return { type: 'text', text: typeof text === 'string' ? text : JSON.stringify(text, null, 2) };
}

function originAllowed(url, allowedOrigins) {
  let u;
  try { u = new URL(url); } catch (_) { return { ok: false, error: 'invalid URL' }; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return { ok: false, error: 'only http(s) URLs are allowed' };
  if (allowedOrigins && allowedOrigins.length && !allowedOrigins.includes(u.origin)) return { ok: false, error: `origin ${u.origin} is not allowed` };
  return { ok: true };
}

function createToolRegistry({ getPage, allowEvaluate = false, allowedOrigins = [], deps = {} } = {}) {
  const cdp = deps.cdp || require('./cdp');
  const table = deps.table || require('./adapters/generic-table');
  const obj = (properties, required = []) => ({ type: 'object', properties, required, additionalProperties: false });

  const tools = [
    {
      name: 'navigate', description: 'Navigate the browser to a URL (http/https only).',
      inputSchema: obj({ url: { type: 'string', minLength: 1 } }, ['url']),
      handler: async (a) => {
        const c = originAllowed(a.url, allowedOrigins);
        if (!c.ok) throw new Error(c.error);
        const page = await getPage();
        await page.goto(a.url, { waitUntil: 'domcontentloaded', timeout: 30000 });
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
      handler: async (a) => { await (await getPage()).locator(a.selector).click({ timeout: 10000 }); return [textContent('clicked')]; },
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
      handler: async (a) => [textContent(await (await getPage()).evaluate(a.expression))],
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

module.exports = { createToolRegistry, createServer, serveStdio, PROTOCOL_VERSION, SERVER_INFO, originAllowed };
