#!/usr/bin/env node
'use strict';
/**
 * Stdio MCP server driving the machine's Edge/Chrome through the execution-framework.
 *   node bin/mcp-browser.js
 * Env: AMX_MCP_HEADLESS=0 (show the browser), AMX_MCP_ALLOWED_ORIGINS=https://a,https://b, AMX_MCP_ALLOW_EVALUATE=1,
 *      AMX_MCP_CDP_URL=http://127.0.0.1:9222 (attach instead of launch), AMX_MCP_PROFILE=<userDataDir>
 *      AMX_MCP_ALLOW_PRIVATE=1 (also allow loopback/private/link-local hosts; never needed for normal portals)
 * DENY BY DEFAULT: with AMX_MCP_ALLOWED_ORIGINS empty/unset every browser tool refuses. Loopback, private, link-local and cloud-metadata
 * addresses are blocked even when listed, unless AMX_MCP_ALLOW_PRIVATE=1.
 * stdout carries ONLY protocol messages; diagnostics go to stderr.
 */
const { createToolRegistry, createServer, serveStdio } = require('../lib/mcp-server');
const browserLib = require('../lib/browser');

let session = null;
async function getPage() {
  if (!session) {
    session = process.env.AMX_MCP_CDP_URL
      ? await browserLib.attach(process.env.AMX_MCP_CDP_URL)
      : await browserLib.launch({ headless: process.env.AMX_MCP_HEADLESS !== '0', userDataDir: process.env.AMX_MCP_PROFILE || undefined });
    process.stderr.write('[amx-mcp] browser ready\n');
  }
  return session.page;
}

const allowedOrigins = (process.env.AMX_MCP_ALLOWED_ORIGINS || '').split(',').map((s) => s.trim()).filter(Boolean);
if (!allowedOrigins.length) process.stderr.write('[amx-mcp] AMX_MCP_ALLOWED_ORIGINS is empty: deny-by-default, every browser tool will refuse\n');

const registry = createToolRegistry({
  getPage,
  allowEvaluate: process.env.AMX_MCP_ALLOW_EVALUATE === '1',
  allowPrivate: process.env.AMX_MCP_ALLOW_PRIVATE === '1',
  allowedOrigins,
});

serveStdio(createServer({ registry }), process.stdin, process.stdout)
  .then(async () => { if (session) await session.close().catch(() => {}); })
  .catch((err) => { process.stderr.write(`[amx-mcp] fatal: ${err && err.stack}\n`); process.exitCode = 1; });
