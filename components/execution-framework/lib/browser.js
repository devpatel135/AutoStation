'use strict';
/**
 * Browser launch/attach on top of playwright-core. No browser download (airgapped): drives the machine's installed
 * Edge/Chrome via `channel`. A persistent profile keeps the user's Windows SSO / cert / cookie state (user-profile
 * credential model - the automation runs as the user, no separate vault).
 *
 * Every session is registered with the evidence module, so a failure anywhere in the run (retries exhausted, selector heal
 * exhausted, session lapse, uncaught error in `entrypoint`) captures a screenshot + redacted DOM snapshot + context.
 * Use `session.guard(async (s) => {...})` to capture on ANY error thrown inside, even when you close the browser in a
 * `finally` before the error reaches `entrypoint`.
 */
const path = require('node:path');
const events = require('./events');
const cdpLib = require('./cdp');
const evidence = require('./evidence');
const debug = require('./debug');

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

/**
 * Pure: compute the launch options (unit-testable). Debug options (AMX_HEADED / AMX_SLOWMO) apply only in interactive
 * invocations - see lib/debug.js. `deps.debug` may be passed to override env-derived debug options.
 */
function buildLaunchOptions(opts = {}, env = process.env, dbg = null) {
  const d = dbg || debug.buildDebugOptions(env);
  const args = [...(opts.args || [])];
  if (opts.cdpPort) args.push(`--remote-debugging-port=${opts.cdpPort}`);
  const o = {
    headless: opts.headless !== false && !d.headed,
    args,
    viewport: opts.viewport || { width: 1366, height: 900 },
    acceptDownloads: opts.acceptDownloads !== false,
    ignoreDefaultArgs: opts.ignoreDefaultArgs || undefined,
  };
  const slowMo = opts.slowMo !== undefined ? opts.slowMo : d.slowMo;
  if (slowMo > 0) o.slowMo = slowMo;
  if (opts.executablePath) o.executablePath = opts.executablePath;
  else o.channel = opts.channel || 'msedge';
  return { userDataDir: opts.userDataDir || defaultProfileDir(env), options: o };
}

/** Wrap a context+page into the session object returned by launch/attach (also used by tests with fakes). */
function createSession({ browser, context, page, cdp, close, trace = false, name = 'browser' }) {
  const record = { page, context, trace, name, traceStopped: false };
  const unregister = evidence.registerSession(record);
  let closed = false;
  const session = {
    browser, context, page, cdp, evidence: record,
    /** Capture failure evidence now (idempotent per error object). Never throws. */
    captureFailure: (err, reason) => evidence.captureSafe(err, { reason: reason || 'failure', page, context }),
    /** Run fn(session); on any error capture evidence, then rethrow the ORIGINAL error. */
    guard: async (fn) => {
      try {
        return await fn(session);
      } catch (err) {
        await evidence.captureSafe(err, { reason: 'error inside session.guard', page, context });
        throw err;
      }
    },
    close: async () => {
      if (closed) return;
      closed = true;
      unregister();
      await evidence.stopTrace(record); // success path: discard the trace
      await close();
    },
  };
  return session;
}

async function launch(opts = {}, deps = {}) {
  const env = deps.env || process.env;
  const pw = deps.playwright || loadPlaywright();
  const dbg = deps.debug || debug.buildDebugOptions(env);
  const { userDataDir, options } = buildLaunchOptions(opts, env, dbg);
  if (dbg.ignored.length && deps.logEvent) deps.logEvent('debug_options_ignored', { options: dbg.ignored });
  const context = await pw.chromium.launchPersistentContext(userDataDir, options);
  const browser = typeof context.browser === 'function' ? context.browser() : null;
  const page = context.pages()[0] || (await context.newPage());
  let trace = false;
  if (dbg.trace && context.tracing) {
    try {
      await context.tracing.start({ screenshots: true, snapshots: true });
      trace = true;
    } catch (_) { trace = false; }
  }
  const cdp = await (deps.createCdp || cdpLib.createCdp)(page);
  return createSession({
    browser, context, page, cdp, trace, name: 'launch',
    close: async () => { try { await cdp.detach(); } catch (_) { /* already gone */ } await context.close(); },
  });
}

async function attach(cdpUrl, deps = {}) {
  const pw = deps.playwright || loadPlaywright();
  const browser = await pw.chromium.connectOverCDP(cdpUrl);
  const context = browser.contexts()[0] || (await browser.newContext());
  const page = context.pages()[0] || (await context.newPage());
  const cdp = await (deps.createCdp || cdpLib.createCdp)(page);
  return createSession({ browser, context, page, cdp, name: 'attach', close: async () => browser.close() });
}

module.exports = { launch, attach, buildLaunchOptions, defaultProfileDir, createSession };
