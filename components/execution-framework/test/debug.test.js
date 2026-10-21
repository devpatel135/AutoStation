'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const debug = require('../lib/debug');
const browser = require('../lib/browser');
const evidence = require('../lib/evidence');
const { withRunDir } = require('./_helpers');

const tty = { isTTY: true };

test('defaults: nothing interactive is on', () => {
  const d = debug.buildDebugOptions({}, tty);
  assert.deepEqual([d.headed, d.slowMo, d.pauseOnError, d.debugSelectors, d.trace, d.scheduled], [false, 0, false, false, false, false]);
  assert.equal(d.pauseMinutes, 10);
  assert.deepEqual(d.ignored, []);
});

test('interactive invocation (no slot / dev / adhoc): every debug option is honoured', () => {
  const env = { AMX_HEADED: '1', AMX_SLOWMO: '250', AMX_PAUSE_ON_ERROR: 'true', AMX_DEBUG_SELECTORS: 'yes', AMX_TRACE: 'on', AMX_PAUSE_MINUTES: '5' };
  for (const slot of [undefined, '', 'dev', 'adhoc', 'manual']) {
    const d = debug.buildDebugOptions({ ...env, ...(slot === undefined ? {} : { AUTOMAGIX_SLOT: slot }) }, tty);
    assert.deepEqual([d.headed, d.slowMo, d.pauseOnError, d.debugSelectors, d.trace, d.pauseMinutes], [true, 250, true, true, true, 5], String(slot));
    assert.equal(d.scheduled, false);
  }
});

test('scheduled run: headed / slowMo / pause / debug-selectors are IGNORED (and reported); trace stays (passive)', () => {
  const env = { AMX_HEADED: '1', AMX_SLOWMO: '250', AMX_PAUSE_ON_ERROR: '1', AMX_DEBUG_SELECTORS: '1', AMX_TRACE: '1', AUTOMAGIX_SLOT: 'daily' };
  const d = debug.buildDebugOptions(env, tty);
  assert.equal(d.scheduled, true);
  assert.deepEqual([d.headed, d.slowMo, d.pauseOnError, d.debugSelectors], [false, 0, false, false]);
  assert.equal(d.trace, true);
  assert.equal(d.ignored.length, 4);
  assert.ok(d.ignored.every((x) => /scheduled run/.test(x)));
  // explicit override
  const allowed = debug.buildDebugOptions({ ...env, AMX_DEBUG_ALLOW_SCHEDULED: '1' }, tty);
  assert.deepEqual([allowed.headed, allowed.slowMo, allowed.pauseOnError, allowed.debugSelectors], [true, 250, true, true]);
});

test('pause-on-error needs an interactive terminal even in a manual run', () => {
  const d = debug.buildDebugOptions({ AMX_PAUSE_ON_ERROR: '1' }, { isTTY: false });
  assert.equal(d.pauseOnError, false);
  assert.match(d.ignored[0], /no interactive terminal/);
});

test('slowMo clamps to 5000, pauseMinutes to 1..60', () => {
  assert.equal(debug.buildDebugOptions({ AMX_SLOWMO: '99999' }, tty).slowMo, 5000);
  assert.equal(debug.buildDebugOptions({ AMX_SLOWMO: '-5' }, tty).slowMo, 0);
  assert.equal(debug.buildDebugOptions({ AMX_SLOWMO: 'abc' }, tty).slowMo, 0);
  assert.equal(debug.buildDebugOptions({ AMX_PAUSE_MINUTES: '0' }, tty).pauseMinutes, 1);
  assert.equal(debug.buildDebugOptions({ AMX_PAUSE_MINUTES: '500' }, tty).pauseMinutes, 60);
  assert.equal(debug.buildDebugOptions({ AMX_PAUSE_MINUTES: 'x' }, tty).pauseMinutes, 10);
});

test('isScheduledRun: only real slots count', () => {
  assert.equal(debug.isScheduledRun({ AUTOMAGIX_SLOT: 'daily' }), true);
  assert.equal(debug.isScheduledRun({ AUTOMAGIX_SLOT: 'Monday-weekly' }), true);
  assert.equal(debug.isScheduledRun({ AUTOMAGIX_SLOT: 'DEV' }), false);
  assert.equal(debug.isScheduledRun({}), false);
});

// ---- pause helper --------------------------------------------------------------------------------------
function fakeStdin(isTTY) {
  const handlers = [];
  return {
    isTTY,
    resumed: false,
    paused: false,
    on(ev, fn) { if (ev === 'data') handlers.push(fn); },
    removeListener(ev, fn) { const i = handlers.indexOf(fn); if (i >= 0) handlers.splice(i, 1); },
    resume() { this.resumed = true; },
    pause() { this.paused = true; },
    press() { handlers.slice().forEach((h) => h('\n')); },
    handlers,
  };
}

test('pauseForInspection: resolves immediately without a TTY (an unattended run can never hang here)', async () => {
  const r = await debug.pauseForInspection({ minutes: 10, stdin: fakeStdin(false) });
  assert.deepEqual(r, { paused: false, reason: 'no TTY' });
  assert.deepEqual(await debug.pauseForInspection({ stdin: null }), { paused: false, reason: 'no TTY' });
});

test('pauseForInspection: Enter ends the pause; so does the timeout; timers are cleaned up', async () => {
  const stdin = fakeStdin(true);
  const timers = [];
  const cleared = [];
  const setT = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  const clearT = (id) => cleared.push(id);
  const logs = [];
  const p = debug.pauseForInspection({ minutes: 2, stdin, setTimeoutFn: setT, clearTimeoutFn: clearT, log: (m) => logs.push(m) });
  assert.equal(timers[0].ms, 2 * 60 * 1000);
  assert.ok(stdin.resumed && /press Enter/.test(logs[0]));
  stdin.press();
  assert.deepEqual(await p, { paused: true, reason: 'enter' });
  assert.deepEqual(cleared, [1]);
  assert.equal(stdin.handlers.length, 0);
  assert.ok(stdin.paused);

  const stdin2 = fakeStdin(true);
  const timers2 = [];
  const p2 = debug.pauseForInspection({ minutes: 1, stdin: stdin2, setTimeoutFn: (fn) => { timers2.push(fn); return 9; }, clearTimeoutFn() {} });
  timers2[0]();
  assert.deepEqual(await p2, { paused: true, reason: 'timeout' });
});

// ---- launch options + session -------------------------------------------------------------------------
test('buildLaunchOptions: AMX_HEADED / AMX_SLOWMO apply to interactive runs only; explicit opts still win', () => {
  const env = { AUTOMAGIX_HOME: 'h', AMX_HEADED: '1', AMX_SLOWMO: '100' };
  const a = browser.buildLaunchOptions({}, env, debug.buildDebugOptions(env, tty));
  assert.equal(a.options.headless, false);
  assert.equal(a.options.slowMo, 100);
  const sched = { ...env, AUTOMAGIX_SLOT: 'daily' };
  const b = browser.buildLaunchOptions({}, sched, debug.buildDebugOptions(sched, tty));
  assert.equal(b.options.headless, true);
  assert.equal(b.options.slowMo, undefined);
  const c = browser.buildLaunchOptions({ headless: false, slowMo: 0 }, {}, debug.buildDebugOptions({}, tty));
  assert.equal(c.options.headless, false);
  assert.equal(c.options.slowMo, undefined);
});

function fakePlaywright(log) {
  const tracing = { start: async (o) => log.push(['trace.start', o]), stop: async (o) => log.push(['trace.stop', o]) };
  const page = { url: () => 'https://a.test/x', screenshot: async () => Buffer.from('PNG') };
  const context = {
    tracing,
    browser: () => null,
    pages: () => [page],
    newPage: async () => page,
    close: async () => log.push(['context.close']),
  };
  return { page, context, pw: { chromium: { launchPersistentContext: async (dir, options) => { log.push(['launch', options]); return context; } } } };
}

test('launch: registers the session for evidence, starts a trace when AMX_TRACE=1, close() discards the trace and unregisters', async () => {
  const log = [];
  const fp = fakePlaywright(log);
  const session = await browser.launch({}, { playwright: fp.pw, env: { AUTOMAGIX_HOME: 'h', AMX_TRACE: '1' }, createCdp: async () => ({ detach: async () => log.push(['cdp.detach']) }) });
  assert.ok(evidence.activeSessions().some((s) => s.page === fp.page && s.trace === true));
  assert.deepEqual(log.find((l) => l[0] === 'trace.start')[1], { screenshots: true, snapshots: true });
  await session.close();
  await session.close(); // idempotent
  assert.equal(evidence.activeSessions().some((s) => s.page === fp.page), false);
  assert.equal(log.filter((l) => l[0] === 'context.close').length, 1);
  assert.deepEqual(log.find((l) => l[0] === 'trace.stop'), ['trace.stop', undefined]);
});

test('launch reports ignored debug options in a scheduled run', async () => {
  const log = [];
  const logged = [];
  const fp = fakePlaywright(log);
  const env = { AUTOMAGIX_HOME: 'h', AMX_HEADED: '1', AUTOMAGIX_SLOT: 'daily' };
  const s = await browser.launch({}, { playwright: fp.pw, env, logEvent: (n, d) => logged.push([n, d]), createCdp: async () => ({ detach: async () => {} }) });
  assert.equal(log.find((l) => l[0] === 'launch')[1].headless, true);
  assert.equal(logged[0][0], 'debug_options_ignored');
  await s.close();
});

test('session.guard captures evidence on ANY error and rethrows the original; success passes the value through', async (t) => {
  const r = withRunDir(t);
  const log = [];
  const fp = fakePlaywright(log);
  const session = await browser.launch({}, { playwright: fp.pw, env: { AUTOMAGIX_HOME: 'h' }, createCdp: async () => ({ detach: async () => {} }) });
  assert.equal(await session.guard(async () => 42), 42);
  const boom = new Error('page exploded');
  await assert.rejects(() => session.guard(async () => { throw boom; }), (e) => e === boom);
  const fs = require('node:fs');
  const path = require('node:path');
  const files = fs.readdirSync(path.join(r.runDir, 'evidence'));
  assert.ok(files.includes('fail1-screenshot.png') && files.includes('fail1-context.txt'), files.join(','));
  await session.close();
});
