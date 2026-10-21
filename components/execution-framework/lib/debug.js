'use strict';
/**
 * Debug mode (UiPath "debug" parity for browser flows). Pure option building + one small interactive helper.
 *
 *   AMX_HEADED=1             headed browser (watch it work)
 *   AMX_SLOWMO=<ms>          slow every Playwright action down (0..5000)
 *   AMX_PAUSE_ON_ERROR=1     on failure keep the browser open (default 10 min, AMX_PAUSE_MINUTES) - interactive TTY only
 *   AMX_DEBUG_SELECTORS=1    log every locator with its match count (event `selector_debug`)
 *   AMX_TRACE=1              Playwright trace, kept as evidence/trace.zip ONLY when the run fails (passive: honoured always)
 *   AMX_DEBUG_ALLOW_SCHEDULED=1   allow the interactive options above in a scheduled run (never needed in production)
 *
 * The interactive options (headed / slowMo / pause / debug-selectors) are IGNORED in scheduled runs: the runner exports
 * AUTOMAGIX_SLOT for every run, and any slot other than dev/adhoc/manual/debug counts as scheduled. Unattended machines must
 * never open windows or wait for a person.
 */

const TRUTHY = new Set(['1', 'true', 'yes', 'on']);
const MANUAL_SLOTS = new Set(['dev', 'adhoc', 'manual', 'debug', 'local']);

const truthy = (v) => TRUTHY.has(String(v === undefined || v === null ? '' : v).trim().toLowerCase());

function clampInt(v, min, max, dflt) {
  const n = Number.parseInt(String(v), 10);
  if (!Number.isFinite(n)) return dflt;
  return Math.min(max, Math.max(min, n));
}

/** True when AUTOMAGIX_SLOT names a real schedule slot (not an interactive/dev invocation). */
function isScheduledRun(env = process.env) {
  const slot = String(env.AUTOMAGIX_SLOT || '').trim().toLowerCase();
  return slot !== '' && !MANUAL_SLOTS.has(slot);
}

/**
 * @returns {{headed:boolean, slowMo:number, pauseOnError:boolean, pauseMinutes:number, debugSelectors:boolean, trace:boolean,
 *            scheduled:boolean, ignored:string[]}}
 */
function buildDebugOptions(env = process.env, { isTTY = Boolean(process.stdout && process.stdout.isTTY) } = {}) {
  const scheduled = isScheduledRun(env);
  const allowScheduled = truthy(env.AMX_DEBUG_ALLOW_SCHEDULED);
  const interactiveOk = !scheduled || allowScheduled;
  const ignored = [];
  const take = (name, want, extraOk = true, why = 'scheduled run') => {
    if (!want) return false;
    if (!interactiveOk) { ignored.push(`${name} (${why})`); return false; }
    if (!extraOk) { ignored.push(`${name} (no interactive terminal)`); return false; }
    return true;
  };
  const slowMoRaw = clampInt(env.AMX_SLOWMO, 0, 5000, 0);
  return {
    headed: take('AMX_HEADED', truthy(env.AMX_HEADED)),
    slowMo: take('AMX_SLOWMO', slowMoRaw > 0) ? slowMoRaw : 0,
    pauseOnError: take('AMX_PAUSE_ON_ERROR', truthy(env.AMX_PAUSE_ON_ERROR), Boolean(isTTY)),
    pauseMinutes: clampInt(env.AMX_PAUSE_MINUTES, 1, 60, 10),
    debugSelectors: take('AMX_DEBUG_SELECTORS', truthy(env.AMX_DEBUG_SELECTORS)),
    trace: truthy(env.AMX_TRACE), // passive: only produces evidence on failure
    scheduled,
    ignored,
  };
}

/**
 * Wait until the user presses Enter or `minutes` elapse. Resolves {paused:false} immediately when stdin is not a TTY, so an
 * unattended run can never hang here even if AMX_PAUSE_ON_ERROR leaked into its environment.
 */
function pauseForInspection({ minutes = 10, stdin = process.stdin, setTimeoutFn = setTimeout, clearTimeoutFn = clearTimeout, log = () => {} } = {}) {
  if (!stdin || !stdin.isTTY) return Promise.resolve({ paused: false, reason: 'no TTY' });
  return new Promise((resolve) => {
    let timer = null;
    const done = (reason) => {
      if (timer) clearTimeoutFn(timer);
      try { stdin.removeListener('data', onData); if (typeof stdin.pause === 'function') stdin.pause(); } catch (_) { /* ignore */ }
      resolve({ paused: true, reason });
    };
    const onData = () => done('enter');
    log(`AMX_PAUSE_ON_ERROR: browser kept open for up to ${minutes} min - press Enter to continue and close it`);
    timer = setTimeoutFn(() => done('timeout'), minutes * 60 * 1000);
    if (typeof stdin.resume === 'function') stdin.resume();
    stdin.on('data', onData);
  });
}

module.exports = { buildDebugOptions, isScheduledRun, pauseForInspection, truthy, clampInt, MANUAL_SLOTS };
