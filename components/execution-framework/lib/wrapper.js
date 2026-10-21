'use strict';
/**
 * Node SDK mirroring Python `wrapper_core` (same events.jsonl, same error.json, same result.json).
 * See README "Node SDK reference" for the Python equivalents table.
 */
const fs = require('node:fs');
const path = require('node:path');
const events = require('./events');
const E = require('./errors');
const retry = require('./retry');
const { llmCall } = require('./llm');

const OUTCOMES = new Set(['success', 'skipped', 'failed']);

function logEvent(event, data = {}, opts = {}) {
  const payload = { ...(data || {}) };
  const keys = [...(opts.sensitiveKeys || [])];
  if (opts.sensitive) keys.push(...Object.keys(payload));
  return events.current().append(event, payload, keys.length ? keys : null);
}

/** fields: any extra data; special keys durationMs/duration_ms, sensitive, sensitiveKeys. */
function logItem(itemId, outcome, fields = {}) {
  if (!OUTCOMES.has(outcome)) throw new Error('outcome must be success|skipped|failed');
  const { durationMs, duration_ms: durSnake, sensitive, sensitiveKeys, ...rest } = fields || {};
  const data = { item_id: String(itemId), outcome };
  const dur = durationMs ?? durSnake;
  if (dur !== undefined && dur !== null) data.duration_ms = Math.trunc(dur);
  Object.assign(data, rest);
  const keys = [...(sensitiveKeys || [])];
  if (sensitive) keys.push(...Object.keys(data).filter((k) => k !== 'outcome'));
  return events.current().append('item_processed', data, keys.length ? keys : null);
}

/** Time one item and log it. fn receives a mutable state {outcome:'success'}; set it.outcome='skipped' etc. to override. */
async function trackItem(itemId, fn, fields = {}) {
  const state = { outcome: 'success' };
  const t0 = Date.now();
  let result;
  try {
    result = await fn(state);
  } catch (err) {
    const { outcome, ...extra } = state;
    logItem(itemId, 'failed', {
      durationMs: Date.now() - t0, ...fields, error_category: (err && err.name) || 'Error',
      detail: String((err && err.message) || err).slice(0, 300), ...extra,
    });
    throw err;
  }
  const { outcome, ...extra } = state;
  logItem(itemId, outcome, { durationMs: Date.now() - t0, ...fields, ...extra });
  return result;
}

function runWithRetry(fn, policy, opts = {}) {
  return retry.runWithRetry(fn, policy, opts);
}

function runItem(itemId, fn, policy, opts = {}) {
  return retry.runItem(itemId, fn, policy, { logItem, ...opts });
}

function params(env = process.env) {
  const pre = 'AUTOMATION_PARAM_';
  const out = {};
  for (const [k, v] of Object.entries(env)) if (k.startsWith(pre)) out[k.slice(pre.length).toLowerCase()] = v;
  return out;
}

function param(name, dflt = undefined, env = process.env) {
  const p = params(env);
  const k = String(name).toLowerCase();
  return k in p ? p[k] : dflt;
}

function isDryRun(env = process.env) {
  return env.AUTOMAGIX_DRY_RUN === '1';
}

function setResult(obj) {
  events.current().writeResult(obj || {});
}

function assets(env = process.env) {
  try {
    return JSON.parse(fs.readFileSync(path.join(events.amxHome(env), 'config', 'assets.json'), 'utf8')) || {};
  } catch (_) {
    return {};
  }
}

function emitTrigger(target, trigParams = {}, reason = '') {
  return logEvent('trigger', { target, params: trigParams || {}, reason });
}

/**
 * Run `main(fw)`; classify uncaught errors for the runner (error.json) like Python's @entrypoint.
 * Resolves to the exit code and sets process.exitCode. `fw` is injected by index.js.
 */
async function entrypoint(main, fw, io = {}) {
  const stderr = io.stderr || process.stderr;
  const setExit = io.setExitCode || ((c) => { process.exitCode = c; });
  try {
    await main(fw);
    setExit(0);
    return 0;
  } catch (exc) {
    const esc = exc instanceof E.Escalation ? exc : null;
    const cause = esc && esc.cause ? esc.cause : exc;
    const cat = esc ? esc.category : E.classifyException(exc);
    const stack = String((exc && exc.stack) || exc);
    try {
      events.current().writeError({
        category: cat,
        type: (cause && cause.name) || 'Error',
        message: String((cause && cause.message) || cause).slice(0, 500),
        traceback_tail: stack.split('\n').map((s) => s.trim()).filter(Boolean).slice(-6),
        escalated: esc !== null,
        attempts: esc ? esc.attempts : 1,
      });
    } catch (_) { /* never mask the original failure */ }
    stderr.write(`${stack}\n`);
    setExit(1);
    return 1;
  }
}

module.exports = {
  logEvent, logItem, trackItem, runItem, runWithRetry, llmCall, setResult, params, param, isDryRun, assets, emitTrigger,
  entrypoint, errors: { Category: E.Category, AutomationError: E.AutomationError, NetworkError: E.NetworkError,
    AuthExpired: E.AuthExpired, SystemUnavailable: E.SystemUnavailable, DataError: E.DataError, LogicError: E.LogicError,
    Escalation: E.Escalation },
  RetryPolicy: retry.RetryPolicy,
};
