'use strict';
// Resilience hardening: wider deny-list, no model-chosen targets for fill/select across origins or for sensitive values,
// and cached healings are re-validated like fresh proposals.
const test = require('node:test');
const assert = require('node:assert/strict');
const { resilientPage, urlPattern, DEFAULT_DENY } = require('../lib/resilience');
const { eventSink } = require('./_helpers');

const HOME = 'https://portal.example.com/orders/123?token=abc';
const OTHER = 'https://evil.example.net/phish';

function pwTimeout(sel) {
  return Object.assign(new Error(`locator.click: Timeout 10000ms exceeded.\nCall log:\n  - waiting for locator('${sel}')`), { name: 'TimeoutError' });
}

/** Fake page: `state.url` is mutable; a missing selector can navigate the page (state.afterFail) while it fails. */
function fakePage(dom, state) {
  const calls = [];
  const page = {
    url: () => state.url,
    locator(sel) {
      const el = dom[sel];
      const need = () => { if (!el) { if (state.afterFail) state.url = state.afterFail; throw pwTimeout(sel); } };
      const loc = {
        async click() { need(); calls.push(['click', sel]); },
        async fill(v) { need(); calls.push(['fill', sel, v]); },
        async selectOption(v) { need(); calls.push(['select', sel, v]); },
        async waitFor() { need(); calls.push(['wait', sel]); },
        async count() { return el ? (el.count === undefined ? 1 : el.count) : 0; },
        first() { return loc; },
        async isVisible() { return Boolean(el) && el.visible !== false; },
        async evaluate() { return el ? (el.desc !== undefined ? el.desc : el.text || '') : ''; },
      };
      return loc;
    },
  };
  return { page, calls };
}

function memCache() {
  const store = new Map();
  return { store, get: (k) => store.get(k) || null, set: (k, s, meta) => store.set(k, { selector: s, ...(meta || {}) }), delete: (k) => store.delete(k) };
}

function setup(dom, verdict, over = {}, state = { url: HOME }) {
  const log = eventSink();
  const llmCalls = [];
  const fp = fakePage(dom, state);
  const cache = over.cache || memCache();
  const rp = resilientPage(fp.page, {
    caller: 'x', cache, logEvent: log, snapshot: async () => 'SNAP', captureEvidence: async () => {},
    llmCall: async (args) => { llmCalls.push(args); return typeof verdict === 'function' ? verdict(args) : verdict; },
    ...over,
  });
  return { rp, log, llmCalls, calls: fp.calls, cache, state };
}

const good = (sel, action = 'click') => ({ selector: sel, action, reasoning: 'same purpose' });
const cacheKey = (sel) => `${urlPattern(HOME)}|${sel}`;

test('DEFAULT_DENY covers send/approve/buy/purchase/accept/allow/grant/authorize/agree/confirm/transfer/publish (and not look-alikes)', () => {
  for (const w of ['Send', 'Send message', 'Approve', 'Approval', 'Buy now', 'Purchase', 'Accept all', 'Allow cookies', 'Grant access', 'Authorize', 'Authorise', 'I agree', 'Confirm', 'Transfer', 'Publish', 'delete', 'Pay now']) {
    assert.ok(DEFAULT_DENY.test(w), w);
  }
  for (const w of ['Next', 'Download report', 'Search', 'Reports', 'Open', 'Filter', 'Export CSV']) assert.ok(!DEFAULT_DENY.test(w), w);
});

test('a model proposal pointing at an Approve / Send / Accept element is rejected', async () => {
  for (const desc of ['Approve invoice', 'Send payment', 'Accept terms', 'Allow access', 'Publish now']) {
    const { rp, calls, log } = setup({ '#new': { text: desc, desc } }, good('#new'));
    await assert.rejects(rp.click('#old'), /Timeout/);
    assert.deepEqual(calls, [], desc);
    assert.ok(log.named('selector_heal_rejected').some((e) => /deny-listed/.test(e.data.reason)), desc);
  }
});

test('fill/select with a sensitive value or field is never healed (the model is not even consulted)', async () => {
  const cases = [
    ['#old', '4111 1111 1111 1111'], // card number (Luhn)
    ['#old', 'eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4'], // JWT
    ['input[name=password]', 'hunter2'], // password field
    ['#otp-code-field', '123456'.repeat(0) + 'x'], // otp field name matches SENSITIVE_NAME
  ];
  for (const [sel, value] of cases) {
    const { rp, llmCalls, calls, log } = setup({ '#new': { text: 'Comment' } }, good('#new', 'fill'));
    await assert.rejects(rp.fill(sel, value), /Timeout/);
    assert.equal(llmCalls.length, 0, sel);
    assert.deepEqual(calls, [], sel);
    assert.ok(log.named('selector_heal_rejected').some((e) => /sensitive/.test(e.data.reason)), sel);
  }
  const sel = setup({ '#new': { text: 'Size' } }, good('#new', 'select'));
  await assert.rejects(sel.rp.selectOption('input[name=api_key]', 'x'), /Timeout/);
  assert.equal(sel.llmCalls.length, 0);
});

test('fill/select is not healed when the page origin changed while the action ran; click still heals on the same origin', async () => {
  const state = { url: HOME, afterFail: OTHER };
  const { rp, llmCalls, calls, log } = setup({ '#new': { text: 'Comment' } }, good('#new', 'fill'), {}, state);
  await assert.rejects(rp.fill('#comment', 'hello'), /Timeout/);
  assert.equal(llmCalls.length, 0);
  assert.deepEqual(calls, []);
  assert.ok(log.named('selector_heal_rejected').some((e) => /origin changed/.test(e.data.reason)));

  const ok = setup({ '#new': { text: 'Comment' } }, good('#new', 'fill'));
  await ok.rp.fill('#comment', 'hello');
  assert.deepEqual(ok.calls, [['fill', '#new', 'hello']]);
});

test('expectedOrigins restricts fill/select healing to the listed origins', async () => {
  const blocked = setup({ '#new': { text: 'Comment' } }, good('#new', 'fill'), { expectedOrigins: ['https://other.example.com'] });
  await assert.rejects(blocked.rp.fill('#comment', 'hello'), /Timeout/);
  assert.equal(blocked.llmCalls.length, 0);
  const ok = setup({ '#new': { text: 'Comment' } }, good('#new', 'fill'), { expectedOrigins: ['https://portal.example.com'] });
  await ok.rp.fill('#comment', 'hello');
  assert.equal(ok.calls.length, 1);
});

test('a heal is discarded when the origin changes while the model is consulted (any action kind)', async () => {
  const state = { url: HOME };
  const { rp, calls, log } = setup({ '#new': { text: 'Details' } }, () => { state.url = OTHER; return good('#new'); }, {}, state);
  await assert.rejects(rp.click('#old'), /Timeout/);
  assert.deepEqual(calls, []);
  assert.ok(log.named('selector_heal_rejected').some((e) => /origin changed while the model/.test(e.data.reason)));
});

test('cached healed selectors are re-validated: ambiguous, invisible, deny-listed, unsafe, malformed and foreign-origin entries are dropped', async () => {
  const entries = [
    ['ambiguous', { selector: '#cached', origin: 'https://portal.example.com' }, { '#cached': { text: 'Details', count: 2 } }, /matches 2/],
    ['invisible', { selector: '#cached', origin: 'https://portal.example.com' }, { '#cached': { text: 'Details', visible: false } }, /not visible/],
    ['deny-listed', { selector: '#cached', origin: 'https://portal.example.com' }, { '#cached': { text: 'Delete all', desc: 'Delete all' } }, /deny-listed/],
    ['deny-listed (send)', { selector: '#cached', origin: 'https://portal.example.com' }, { '#cached': { text: 'Send', desc: 'Send' } }, /deny-listed/],
    ['unsafe syntax', { selector: 'javascript:alert(1)', origin: 'https://portal.example.com' }, {}, /unsafe selector/],
    ['too long', { selector: 'a'.repeat(400), origin: 'https://portal.example.com' }, {}, /unsafe selector/],
    ['malformed', { selector: 42 }, {}, /malformed/],
    ['foreign origin', { selector: '#cached', origin: 'https://evil.example.net' }, { '#cached': { text: 'Details' } }, /different origin/],
    ['gone', { selector: '#cached', origin: 'https://portal.example.com' }, {}, /matches 0/],
  ];
  for (const [name, entry, dom, reason] of entries) {
    const cache = memCache();
    cache.store.set(cacheKey('#old'), entry);
    const { rp, log, calls } = setup(dom, null, { cache });
    await assert.rejects(rp.click('#old'), /Timeout/, name);
    assert.deepEqual(calls, [], name);
    assert.equal(cache.store.has(cacheKey('#old')), false, `${name}: entry dropped`);
    const dropped = log.named('selector_cache_dropped');
    assert.equal(dropped.length, 1, name);
    assert.match(dropped[0].data.reason, reason, name);
  }
});

test('a valid cached healing is still used without consulting the model, and new healings record their origin', async () => {
  const cache = memCache();
  cache.store.set(cacheKey('#old'), { selector: '#cached', origin: 'https://portal.example.com' });
  const { rp, llmCalls, calls, log } = setup({ '#cached': { text: 'Details' } }, good('#x'), { cache });
  await rp.click('#old');
  assert.deepEqual(calls, [['click', '#cached']]);
  assert.equal(llmCalls.length, 0);
  assert.equal(log.named('selector_cache_dropped').length, 0);

  const fresh = setup({ '#new': { text: 'Details' } }, good('#new'));
  await fresh.rp.click('#old');
  assert.equal(fresh.cache.store.get(cacheKey('#old')).origin, 'https://portal.example.com');
});

test('a cached healing is not used for fill/select with a sensitive value', async () => {
  const cache = memCache();
  cache.store.set(cacheKey('input[name=password]'), { selector: '#cached', origin: 'https://portal.example.com' });
  const { rp, calls, log } = setup({ '#cached': { text: 'Field' } }, null, { cache });
  await assert.rejects(rp.fill('input[name=password]', 'hunter2'), /Timeout/);
  assert.deepEqual(calls, []);
  assert.match(log.named('selector_cache_dropped')[0].data.reason, /sensitive/);
});

test('canHeal gate: healing (fresh and cached) is only allowed where the caller says so', async () => {
  const denied = setup({ '#new': { text: 'Details' } }, good('#new'), { canHeal: () => false });
  await assert.rejects(denied.rp.click('#old'), /Timeout/);
  assert.equal(denied.llmCalls.length, 0);
  assert.deepEqual(denied.calls, []);
  assert.ok(denied.log.named('selector_heal_rejected').some((e) => /canHeal/.test(e.data.reason)));

  const throwing = setup({ '#new': { text: 'Details' } }, good('#new'), { canHeal: () => { throw new Error('boom'); } });
  await assert.rejects(throwing.rp.click('#old'), /Timeout/);
  assert.equal(throwing.llmCalls.length, 0);

  const cache = memCache();
  cache.store.set(cacheKey('#old'), { selector: '#cached', origin: 'https://portal.example.com' });
  const noCache = setup({ '#cached': { text: 'Details' } }, null, { cache, canHeal: async () => false });
  await assert.rejects(noCache.rp.click('#old'), /Timeout/);
  assert.deepEqual(noCache.calls, [], 'cached healing is not used where healing is not permitted');
  assert.equal(cache.store.has(cacheKey('#old')), true, 'and it is not discarded either');

  const allowed = setup({ '#new': { text: 'Details' } }, good('#new'), { canHeal: async (pg) => pg.url().startsWith('https://portal.example.com') });
  await allowed.rp.click('#old');
  assert.deepEqual(allowed.calls, [['click', '#new']]);
});
