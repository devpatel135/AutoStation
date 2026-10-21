'use strict';
/**
 * Browser launch/attach on top of playwright-core. No browser download (airgapped): drives the machine's installed
 * Edge/Chrome via `channel`. A persistent profile keeps the user's Windows SSO / cert / cookie state (user-profile
 * credential model - the automation runs as the user, no separate vault).
 */
const path = require('node:path');
const events = require('./events');
const cdpLib = require('./cdp');

function loadPlaywright() {
  try {
    return require('playwright-core');
  } catch (err) {
    const e = new Error('playwright-core is not installed in this framework build (npm ci --omit=dev)');
    e.cause = err;
    throw e;
  }
}

function defaultProfileDir(env = process.env) {
  const name = env.AUTOMAGIX_AUTOMATION || 'adhoc';
  return path.join(events.amxHome(env), 'browser-profile', name);
}

/** Pure: compute the launch options (unit-testable). */
function buildLaunchOptions(opts = {}, env = process.env) {
  const args = [...(opts.args || [])];
  if (opts.cdpPort) args.push(`--remote-debugging-port=${opts.cdpPort}`);
  const o = {
    headless: opts.headless !== false,
    args,
    viewport: opts.viewport || { width: 1366, height: 900 },
    acceptDownloads: opts.acceptDownloads !== false,
    ignoreDefaultArgs: opts.ignoreDefaultArgs || undefined,
  };
  if (opts.executablePath) o.executablePath = opts.executablePath;
  else o.channel = opts.channel || 'msedge';
  return { userDataDir: opts.userDataDir || defaultProfileDir(env), options: o };
}

async function launch(opts = {}, deps = {}) {
  const pw = deps.playwright || loadPlaywright();
  const { userDataDir, options } = buildLaunchOptions(opts, deps.env || process.env);
  const context = await pw.chromium.launchPersistentContext(userDataDir, options);
  const browser = typeof context.browser === 'function' ? context.browser() : null;
  const page = context.pages()[0] || (await context.newPage());
  const cdp = await cdpLib.createCdp(page);
  return {
    browser, context, page, cdp,
    close: async () => { try { await cdp.detach(); } catch (_) { /* already gone */ } await context.close(); },
  };
}

async function attach(cdpUrl, deps = {}) {
  const pw = deps.playwright || loadPlaywright();
  const browser = await pw.chromium.connectOverCDP(cdpUrl);
  const context = browser.contexts()[0] || (await browser.newContext());
  const page = context.pages()[0] || (await context.newPage());
  const cdp = await cdpLib.createCdp(page);
  return { browser, context, page, cdp, close: async () => browser.close() };
}

module.exports = { launch, attach, buildLaunchOptions, defaultProfileDir };
