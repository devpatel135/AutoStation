'use strict';
/**
 * Failure evidence (UiPath's "screenshot on error"). Files land in <AUTOMAGIX_RUN_DIR>/evidence/; wrapper-core's runner
 * collects that folder after the run and ships it as artefact kind `evidence`.
 *
 * Hard rules, enforced here (not by convention):
 *  - caps: at most 8 files and 1.5 MB in total; anything over is DROPPED with an `evidence_dropped` event (never truncated silently)
 *  - allowed types only: png, jpeg, html, txt, json, zip
 *  - never cookies / storage / credentials: such names are refused, and text evidence is scrubbed (bearer tokens, JWTs,
 *    password=/token= values, SSNs, card numbers)
 *  - every capture step is individually try/caught: evidence can never mask or replace the real error
 *  - a given error object is captured at most once; at most MAX_CAPTURES failure captures per run
 */
const fs = require('node:fs');
const path = require('node:path');
const events = require('./events');
const redact = require('./redact');
const cdp = require('./cdp');
const debug = require('./debug');

const LIMITS = { maxFiles: 8, maxTotalBytes: 1572864, maxCaptures: 2 };
const TYPES = { png: 'image/png', jpg: 'image/jpeg', html: 'text/html', txt: 'text/plain', json: 'application/json', zip: 'application/zip' };
const TEXT_EXT = new Set(['html', 'txt', 'json']);
const FORBIDDEN_NAME = /cookie|storage[-_ .]?state|local[-_ .]?storage|session[-_ .]?storage|credential|password|passwd|secret|token/i;

function extFromContentType(ct) {
  const c = String(ct || '').split(';')[0].trim().toLowerCase();
  if (!c) return null;
  if (c === 'image/jpeg' || c === 'image/jpg') return 'jpg';
  for (const [ext, type] of Object.entries(TYPES)) if (type === c) return ext;
  return undefined; // given but not allowed
}

/** -> {name, ext, contentType} or {error}. */
function sanitizeName(name, contentType) {
  const raw = String(name === undefined || name === null ? '' : name);
  if (FORBIDDEN_NAME.test(raw)) return { error: 'name looks like cookies/storage/credentials: refused' };
  let base = raw.split(/[\\/]/).pop().replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^[._]+/, '').slice(0, 64);
  const dot = base.lastIndexOf('.');
  const nameExt = dot > 0 ? base.slice(dot + 1).toLowerCase() : '';
  const ctExt = extFromContentType(contentType);
  if (ctExt === undefined) return { error: `content type ${String(contentType).slice(0, 40)} is not allowed (png, jpeg, html, txt, json, zip)` };
  let ext = ctExt || (nameExt === 'jpeg' ? 'jpg' : nameExt);
  if (!ext || !Object.prototype.hasOwnProperty.call(TYPES, ext)) return { error: `file type ".${nameExt || '?'}" is not allowed (png, jpeg, html, txt, json, zip)` };
  const stem = (dot > 0 ? base.slice(0, dot) : base).replace(/[._-]+$/, '') || 'evidence';
  return { name: `${stem}.${ext}`, ext, contentType: TYPES[ext] };
}

function luhnOk(digits) {
  let sum = 0;
  let alt = false;
  for (let i = digits.length - 1; i >= 0; i -= 1) {
    let n = digits.charCodeAt(i) - 48;
    if (alt) { n *= 2; if (n > 9) n -= 9; }
    sum += n;
    alt = !alt;
  }
  return sum % 10 === 0;
}

/** Mask secrets in free text (error messages, event lines, html). Best effort - belt and braces on top of key-based redaction. */
function scrubText(text) {
  let s = String(text === undefined || text === null ? '' : text);
  s = s.replace(/Bearer\s+[A-Za-z0-9._~+/=-]{8,}/gi, 'Bearer [REDACTED]');
  s = s.replace(/eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g, '[REDACTED-JWT]');
  s = s.replace(/(password|passwd|pwd|secret|token|api[_-]?key|authorization)(["']?\s*[:=]\s*)(["']?)[^\s"'&,;]{3,}/gi, '$1$2$3[REDACTED]');
  s = s.replace(/\b\d{3}-\d{2}-\d{4}\b/g, '[REDACTED-SSN]');
  s = s.replace(/\b(?:\d[ -]?){13,19}\b/g, (m) => {
    const digits = m.replace(/[ -]/g, '');
    return digits.length >= 13 && digits.length <= 19 && luhnOk(digits) ? '[REDACTED-CARD]' : m;
  });
  return s;
}

const byteLength = (d) => (typeof d === 'string' ? Buffer.byteLength(d, 'utf8') : (d.byteLength !== undefined ? d.byteLength : d.length));

/**
 * The per-run evidence folder. `fsMod`/`logEvent` are injectable for tests.
 */
function createStore({ runDir, fsMod = fs, logEvent = () => {}, limits = LIMITS } = {}) {
  const dir = path.join(runDir, 'evidence');

  function list() {
    let names;
    try { names = fsMod.readdirSync(dir); } catch (_) { return []; }
    return names.map((n) => {
      let size = 0;
      try { size = fsMod.statSync(path.join(dir, n)).size; } catch (_) { size = 0; }
      return { name: n, size };
    });
  }

  function drop(name, reason) {
    try { logEvent('evidence_dropped', { name: String(name).slice(0, 64), reason }); } catch (_) { /* never throw from evidence */ }
    return { ok: false, reason };
  }

  /** add(name, bufferOrString, contentType?) -> {ok:true,name,size,contentType} | {ok:false,reason}. Never throws. */
  function add(name, data, contentType, opts = {}) {
    try {
      if (data === undefined || data === null) return drop(name, 'no data');
      const s = sanitizeName(name, contentType);
      if (s.error) return drop(name, s.error);
      let payload = data;
      if (TEXT_EXT.has(s.ext) && opts.scrub !== false) payload = scrubText(typeof data === 'string' ? data : Buffer.from(data).toString('utf8'));
      else if (typeof payload === 'string') payload = Buffer.from(payload, 'utf8');
      const size = byteLength(payload);
      const existing = list();
      if (existing.length >= limits.maxFiles) return drop(name, `file limit reached (${limits.maxFiles})`);
      const used = existing.reduce((a, f) => a + f.size, 0);
      if (used + size > limits.maxTotalBytes) return drop(name, `size limit reached (${used + size} > ${limits.maxTotalBytes} bytes)`);
      const taken = new Set(existing.map((f) => f.name));
      let finalName = s.name;
      for (let i = 2; taken.has(finalName); i += 1) finalName = s.name.replace(/(\.[a-z]+)$/, `-${i}$1`);
      fsMod.mkdirSync(dir, { recursive: true });
      fsMod.writeFileSync(path.join(dir, finalName), payload);
      try { logEvent('evidence_added', { name: finalName, size, content_type: s.contentType }); } catch (_) { /* ignore */ }
      return { ok: true, name: finalName, size, contentType: s.contentType };
    } catch (err) {
      return drop(name, `write failed: ${String((err && err.message) || err).slice(0, 120)}`);
    }
  }

  return { dir, list, add, limits };
}

// ---- live browser sessions -------------------------------------------------------------------------------
const sessions = new Set();

/** browser.launch registers {page, context, trace, name}; returns an unregister function. */
function registerSession(s) {
  sessions.add(s);
  return () => { sessions.delete(s); };
}

function activeSessions() {
  return [...sessions];
}

const captured = new WeakSet();

function withTimeout(promise, ms, setT = setTimeout, clearT = clearTimeout) {
  let timer;
  const t = new Promise((_, reject) => { timer = setT(() => reject(new Error(`timed out after ${ms} ms`)), ms); });
  return Promise.race([promise, t]).finally(() => clearT(timer));
}

async function stopTrace(session, { keepPath } = {}) {
  if (!session || !session.trace || session.traceStopped || !session.context || !session.context.tracing) return false;
  session.traceStopped = true;
  try {
    await session.context.tracing.stop(keepPath ? { path: keepPath } : undefined);
    return true;
  } catch (_) {
    return false;
  }
}

function contextText({ err, reason, page, events: evs, env }) {
  const lines = [
    `automation: ${env.AUTOMAGIX_AUTOMATION || ''}`,
    `run_id: ${env.AUTOMAGIX_RUN_ID || ''}`,
    `time: ${new Date().toISOString()}`,
    `reason: ${reason || 'failure'}`,
    `error: ${scrubText(`${(err && err.name) || 'Error'}: ${(err && err.message) || err}`).slice(0, 1000)}`,
  ];
  if (page) {
    let url = '';
    try { url = cdp.safeUrl(page.url()); } catch (_) { url = '(unavailable)'; }
    lines.push(`url: ${url}`);
  }
  lines.push('', '-- last 20 events (sensitive fields redacted) --');
  for (const e of redact.redactEvents(evs.slice(-20))) lines.push(scrubText(JSON.stringify(e)));
  return `${lines.join('\n')}\n`;
}

/**
 * Capture screenshot + redacted DOM snapshot + context.txt (+ trace.zip) for the live page(s). Resolves to
 * {captured:[names], dropped:[reasons], skipped?:reason}; NEVER rejects.
 * deps (all optional, for tests): store, sessions, env, readEvents, snapshot, logEvent, pause, page, stepTimeoutMs.
 */
async function captureFailure(err, opts = {}) {
  const result = { captured: [], dropped: [] };
  try {
    const env = opts.env || process.env;
    if (err && typeof err === 'object' && captured.has(err)) return { ...result, skipped: 'already captured' };
    const pool = opts.sessions || activeSessions();
    // opts.page narrows to the session that owns that page (a page that was not launched through the framework has no session)
    const live = opts.page ? pool.filter((x) => x.page === opts.page || x.page === opts.page.raw) : pool;
    if (!live.length) return { ...result, skipped: 'no browser session' };
    if (err && typeof err === 'object') captured.add(err);

    const runCtx = opts.store ? null : events.current(env);
    const logEvent = opts.logEvent || (runCtx ? (n, d) => runCtx.append(n, d) : () => {});
    const store = opts.store || createStore({ runDir: runCtx.runDir, logEvent });
    const have = store.list().filter((f) => /^fail\d+-context\.txt$/.test(f.name)).length;
    if (have >= store.limits.maxCaptures) return { ...result, skipped: 'capture limit reached' };
    const prefix = `fail${have + 1}-`;
    const stepMs = opts.stepTimeoutMs || 8000;
    const readEvents = opts.readEvents || (() => (runCtx || events.current(env)).readEvents());
    const snapshot = opts.snapshot || cdp.domSnapshot;
    const record = (r) => { if (r.ok) result.captured.push(r.name); else result.dropped.push(r.reason); };

    const s = live[live.length - 1];
    const page = s.page;
    try { // 1. screenshot
      const png = await withTimeout(Promise.resolve(page.screenshot({ type: 'png', timeout: stepMs })), stepMs + 1000);
      record(store.add(`${prefix}screenshot.png`, png, 'image/png'));
    } catch (e) { result.dropped.push(`screenshot failed: ${String((e && e.message) || e).slice(0, 80)}`); }
    try { // 2. redacted DOM snapshot (text only; form VALUES are never part of it)
      const snap = await withTimeout(Promise.resolve(snapshot(page, { maxChars: 20000 })), stepMs + 1000);
      record(store.add(`${prefix}dom-snapshot.txt`, snap, 'text/plain'));
    } catch (e) { result.dropped.push(`dom snapshot failed: ${String((e && e.message) || e).slice(0, 80)}`); }
    try { // 3. context.txt
      record(store.add(`${prefix}context.txt`, contextText({ err, reason: opts.reason, page, events: readEvents(), env }), 'text/plain'));
    } catch (e) { result.dropped.push(`context failed: ${String((e && e.message) || e).slice(0, 80)}`); }
    try { // 4. trace (last: it is the biggest and the first to be dropped by the size cap)
      if (s.trace) {
        const tmp = path.join(store.dir, '..', `.trace-${process.pid}.zip`);
        if (await stopTrace(s, { keepPath: tmp })) {
          const buf = fs.readFileSync(tmp);
          try { fs.unlinkSync(tmp); } catch (_) { /* ignore */ }
          record(store.add('trace.zip', buf, 'application/zip'));
        }
      }
    } catch (e) { result.dropped.push(`trace failed: ${String((e && e.message) || e).slice(0, 80)}`); }

    try { logEvent('evidence_captured', { reason: opts.reason || 'failure', files: result.captured, dropped: result.dropped }); } catch (_) { /* ignore */ }

    try { // 5. optional interactive pause (TTY + AMX_PAUSE_ON_ERROR, never in scheduled runs)
      const dbg = opts.debug || debug.buildDebugOptions(env);
      if (dbg.pauseOnError) await (opts.pause || debug.pauseForInspection)({ minutes: dbg.pauseMinutes, log: (m) => process.stderr.write(`${m}\n`) });
    } catch (_) { /* ignore */ }
  } catch (_) { /* evidence must never throw */ }
  return result;
}

module.exports = {
  LIMITS, TYPES, createStore, sanitizeName, scrubText, registerSession, activeSessions, captureFailure, stopTrace, withTimeout, luhnOk,
  /** Convenience used by hooks: same as captureFailure (which never rejects). */
  captureSafe: captureFailure,
};
