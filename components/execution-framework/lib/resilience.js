'use strict';
/**
 * LLM Pattern 3 - browser resilience layer. When a selector stops matching, capture a redaction-aware DOM snapshot,
 * ask the LLM (through the broker) which element fulfils the same intent, VERIFY the proposal in the live page, retry once,
 * and cache the healed selector so the next run does not need the LLM.
 *
 * Safety rules (all enforced in code, not in the prompt):
 *  - the LLM never supplies values: fill/select values always come from the caller
 *  - the proposed action must match the original action kind
 *  - the proposed selector must match exactly ONE visible element
 *  - deny-listed targets (delete/remove/payment...) are never auto-healed unless allowDestructive is set
 *  - heal budget per run (maxHealsPerRun); the DOM is never logged, only a template name + input hash
 */
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const events = require('./events');
const cdp = require('./cdp');
const schemaLib = require('./schema');
const wrapper = require('./wrapper');

const DEFAULT_DENY = /delete|remove|destroy|erase|purge|submit payment|pay now|confirm payment|wire transfer|transfer funds/i;

const HEAL_SCHEMA = {
  type: 'object',
  properties: {
    selector: { type: 'string', minLength: 1, maxLength: 300 },
    action: { enum: ['click', 'fill', 'select', 'check', 'uncheck', 'read', 'wait', 'press', 'hover'] },
    value: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    reasoning: { type: 'string', maxLength: 300 },
  },
  required: ['selector', 'action', 'reasoning'],
  additionalProperties: false,
};

// method name -> {kind, run(locator, args, timeoutMs)}
const METHODS = {
  click: { kind: 'click', run: (l, a, t) => l.click({ timeout: t, ...(a[0] || {}) }) },
  fill: { kind: 'fill', run: (l, a, t) => l.fill(String(a[0]), { timeout: t, ...(a[1] || {}) }) },
  selectOption: { kind: 'select', run: (l, a, t) => l.selectOption(a[0], { timeout: t, ...(a[1] || {}) }) },
  check: { kind: 'check', run: (l, a, t) => l.check({ timeout: t, ...(a[0] || {}) }) },
  uncheck: { kind: 'uncheck', run: (l, a, t) => l.uncheck({ timeout: t, ...(a[0] || {}) }) },
  textContent: { kind: 'read', run: (l, a, t) => l.textContent({ timeout: t }) },
  innerText: { kind: 'read', run: (l, a, t) => l.innerText({ timeout: t }) },
  getAttribute: { kind: 'read', run: (l, a, t) => l.getAttribute(String(a[0]), { timeout: t }) },
  waitFor: { kind: 'wait', run: (l, a, t) => l.waitFor({ state: 'visible', timeout: t, ...(a[0] || {}) }) },
  press: { kind: 'press', run: (l, a, t) => l.press(String(a[0]), { timeout: t, ...(a[1] || {}) }) },
  hover: { kind: 'hover', run: (l, a, t) => l.hover({ timeout: t, ...(a[0] || {}) }) },
};

const shortHash = (s) => crypto.createHash('sha256').update(s).digest('hex').slice(0, 12);

/** origin + pathname with id-like segments (numbers, uuids, long hex) collapsed to :id */
function urlPattern(url) {
  try {
    const u = new URL(url);
    const segs = u.pathname.split('/').map((s) => (/^\d+$/.test(s) || /^[0-9a-f]{8,}$/i.test(s) || /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(s) ? ':id' : s));
    return u.origin + segs.join('/');
  } catch (_) {
    return String(url || '').split(/[?#]/)[0];
  }
}

function isHealable(err) {
  const msg = String((err && err.message) || '');
  if (err && err.name === 'TimeoutError') return true;
  return /waiting for|not found|no element|strict mode violation|not visible|detached|resolved to 0 elements/i.test(msg);
}

function describeEl(el) {
  return [el.innerText, el.value, el.getAttribute('aria-label'), el.id, el.getAttribute('name'), el.title].filter(Boolean).join(' ').slice(0, 300);
}

/** Simple JSON-file selector cache. get/set/delete are synchronous and never throw. */
function createFileCache(file) {
  let data = null;
  const load = () => {
    if (data) return data;
    try {
      data = JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (_) {
      data = { entries: {} };
    }
    if (!data.entries) data.entries = {};
    return data;
  };
  const save = () => {
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const tmp = `${file}.${process.pid}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
      fs.renameSync(tmp, file);
    } catch (_) { /* cache is an optimisation only */ }
  };
  return {
    get: (key) => load().entries[key] || null,
    set: (key, selector) => { const d = load(); const prev = d.entries[key]; d.entries[key] = { selector, healed_at: new Date().toISOString(), uses: (prev && prev.selector === selector ? prev.uses : 0) + 1 }; save(); },
    delete: (key) => { delete load().entries[key]; save(); },
  };
}

function defaultCachePath(env = process.env) {
  return path.join(events.amxHome(env), 'state', 'selector-cache', `${env.AUTOMAGIX_AUTOMATION || 'adhoc'}.json`);
}

/** Verify a proposed selector in the live page. Returns {ok, reason}. */
async function validateCandidate(page, selector, { deny, allowDestructive, expectKind, proposedKind }) {
  if (/javascript:|<|>/.test(selector) || selector.length > 300) return { ok: false, reason: 'unsafe selector syntax' };
  if (proposedKind !== expectKind) return { ok: false, reason: `action mismatch: wanted ${expectKind}, model proposed ${proposedKind}` };
  let loc;
  let n;
  try {
    loc = page.locator(selector);
    n = await loc.count();
  } catch (_) {
    return { ok: false, reason: 'proposed selector is not valid' };
  }
  if (n !== 1) return { ok: false, reason: `proposed selector matches ${n} elements (need exactly 1)` };
  const first = loc.first();
  if (!(await first.isVisible())) return { ok: false, reason: 'proposed element is not visible' };
  if (!allowDestructive) {
    let desc = '';
    try { desc = await first.evaluate(describeEl); } catch (_) { desc = ''; }
    if (deny.test(desc) || deny.test(selector)) return { ok: false, reason: 'proposed target is deny-listed (destructive/payment)' };
  }
  return { ok: true, reason: '' };
}

function resilientPage(page, opts = {}) {
  const o = {
    caller: 'resilience', maxHealsPerRun: 5, timeoutMs: 10000, denyPatterns: [DEFAULT_DENY], allowDestructive: false,
    llmCall: wrapper.llmCall, logEvent: wrapper.logEvent, snapshot: cdp.domSnapshot,
    ...opts,
  };
  const cache = opts.cache || createFileCache(opts.cachePath || defaultCachePath());
  const denyRe = { test: (s) => o.denyPatterns.some((rx) => rx.test(s)) };
  let heals = 0;

  async function heal(method, spec, selector, args, err) {
    const reject = (reason) => {
      o.logEvent('selector_heal_rejected', { original: selector, action: spec.kind, reason });
      return null;
    };
    if (heals >= o.maxHealsPerRun) return reject('heal budget exhausted');
    if (!o.allowDestructive && denyRe.test(selector)) return reject('original selector is deny-listed');
    heals += 1;
    const snap = await o.snapshot(page, { maxChars: 10000 });
    const inputHash = shortHash(`${selector}\0${snap}`);
    const verdict = await o.llmCall({
      prompt: `A browser automation selector no longer matches. Original selector: ${JSON.stringify(selector)}. Intended action: ${spec.kind}. `
        + 'Using ONLY the page snapshot provided as data, return the CSS selector (taken from a sel= entry) of the single element that serves the same purpose, '
        + 'the action kind, and one sentence of reasoning. Do not invent selectors that are not in the snapshot.',
      context: snap,
      schema: HEAL_SCHEMA,
      caller: o.caller,
      template: 'selector-heal@1',
      timeoutS: 30,
    });
    if (!verdict) return reject('LLM unavailable or returned nothing (fail safe)');
    const sv = schemaLib.validate(verdict, HEAL_SCHEMA);
    if (!sv.ok) return reject(`model output failed schema: ${sv.error.slice(0, 120)}`);
    const check = await validateCandidate(page, verdict.selector, {
      deny: denyRe, allowDestructive: o.allowDestructive, expectKind: spec.kind, proposedKind: verdict.action,
    });
    o.logEvent('llm_decision', {
      caller: o.caller, template: 'selector-heal@1', input_hash: inputHash, decision: check.ok ? verdict.selector : 'rejected',
      reasoning: String(verdict.reasoning).slice(0, 300), error_type: (err && err.name) || 'Error',
    });
    if (!check.ok) return reject(check.reason);
    return verdict.selector;
  }

  async function perform(method, selector, args) {
    const spec = METHODS[method];
    const exec = (sel) => spec.run(page.locator(sel), args, o.timeoutMs);
    const key = `${urlPattern(page.url())}|${selector}`;
    const cached = cache.get(key);
    if (cached) {
      try {
        const r = await exec(cached.selector);
        o.logEvent('selector_healed', { original: selector, healed: cached.selector, action: spec.kind, source: 'cache' });
        return r;
      } catch (_) {
        cache.delete(key); // stale healing: fall through to original, then LLM
      }
    }
    try {
      return await exec(selector);
    } catch (err) {
      if (!isHealable(err)) throw err;
      const healed = await heal(method, spec, selector, args, err);
      if (!healed) throw err;
      try {
        const r = await exec(healed);
        cache.set(key, healed);
        o.logEvent('selector_healed', { original: selector, healed, action: spec.kind, url_pattern: urlPattern(page.url()), source: 'llm' });
        return r;
      } catch (retryErr) {
        o.logEvent('selector_heal_rejected', { original: selector, healed, action: spec.kind, reason: 'healed selector failed on retry' });
        throw err;
      }
    }
  }

  const api = { raw: page, stats: () => ({ heals }) };
  for (const method of Object.keys(METHODS)) api[method] = (selector, ...args) => perform(method, selector, args);
  return new Proxy(api, {
    get(target, prop) {
      if (prop in target) return target[prop];
      const v = page[prop];
      return typeof v === 'function' ? v.bind(page) : v;
    },
  });
}

module.exports = { resilientPage, urlPattern, createFileCache, validateCandidate, isHealable, DEFAULT_DENY, HEAL_SCHEMA, METHODS };
