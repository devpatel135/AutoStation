'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { resilientPage, urlPattern, createFileCache, isHealable } = require('../lib/resilience');
const { eventSink } = require('./_helpers');

const SNAP = 'SNAPSHOT-WITH-SECRET-DOM';

function pwTimeout(sel) {
  return Object.assign(new Error(`locator.click: Timeout 10000ms exceeded.\nCall log:\n  - waiting for locator('${sel}')`), { name: 'TimeoutError' });
}

/** Fake Playwright page driven by a selector->element table. */
function fakePage(dom, url = 'https://portal.example.com/orders/123?token=abc') {
  const calls = [];
  const page = {
    url: () => url,
    extra: () => 'passthrough',
    locator(sel) {
      const el = dom[sel];
      const need = () => { if (!el) throw pwTimeout(sel); };
      const loc = {
        async click() { need(); calls.push(['click', sel]); },
        async fill(v) { need(); calls.push(['fill', sel, v]); },
        async selectOption(v) { need(); calls.push(['select', sel, v]); },
        async check() { need(); calls.push(['check', sel]); },
        async uncheck() { need(); calls.push(['uncheck', sel]); },
        async textContent() { need(); calls.push(['read', sel]); return el.text; },
        async innerText() { need(); return el.text; },
        async getAttribute(n) { need(); return (el.attrs || {})[n] || null; },
        async waitFor() { need(); calls.push(['wait', sel]); },
        async press(k) { need(); calls.push(['press', sel, k]); },
        async hover() { need(); calls.push(['hover', sel]); },
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
  return { store, get: (k) => store.get(k) || null, set: (k, s) => store.set(k, { selector: s }), delete: (k) => store.delete(k) };
}

function setup(dom, verdict, over = {}) {
  const log = eventSink();
  const llmCalls = [];
  const fp = fakePage(dom);
  const cache = over.cache || memCache();
  const rp = resilientPage(fp.page, {
    caller: 'invoice-router', cache, logEvent: log, snapshot: async () => SNAP,
    llmCall: async (args) => { llmCalls.push(args); return typeof verdict === 'function' ? verdict(args) : verdict; },
    ...over,
  });
  return { rp, log, llmCalls, calls: fp.calls, cache };
}

const good = (sel, action = 'click') => ({ selector: sel, action, reasoning: 'same purpose' });

test('healthy selector never calls the LLM', async () => {
  const { rp, llmCalls, calls } = setup({ '#submit': { text: 'Submit' } }, good('#x'));
  await rp.click('#submit');
  assert.deepEqual(calls, [['click', '#submit']]);
  assert.equal(llmCalls.length, 0);
});

test('heals a broken selector: LLM proposal verified, retried, cached, audited without leaking the DOM', async () => {
  const { rp, log, llmCalls, calls, cache } = setup({ '#new-submit': { text: 'Place order' } }, good('#new-submit'));
  await rp.click('#old-submit');
  assert.deepEqual(calls, [['click', '#new-submit']]);
  assert.equal(llmCalls.length, 1);
  assert.equal(llmCalls[0].context, SNAP, 'DOM goes in the untrusted context argument');
  assert.ok(!llmCalls[0].prompt.includes(SNAP), 'never interpolated into the instructions');
  assert.equal(llmCalls[0].template, 'selector-heal@1');
  assert.equal(llmCalls[0].caller, 'invoice-router');

  const healed = log.named('selector_healed')[0].data;
  assert.deepEqual([healed.original, healed.healed, healed.action, healed.source], ['#old-submit', '#new-submit', 'click', 'llm']);
  assert.equal(healed.url_pattern, 'https://portal.example.com/orders/:id');
  const dec = log.named('llm_decision')[0].data;
  assert.equal(dec.template, 'selector-heal@1');
  assert.match(dec.input_hash, /^[0-9a-f]{12}$/);
  assert.equal(dec.decision, '#new-submit');
  assert.ok(!JSON.stringify(log.list).includes(SNAP), 'events carry a hash, never the DOM');
  assert.deepEqual([...cache.store.values()], [{ selector: '#new-submit' }]);
});

test('second run uses the cached healed selector and does not call the LLM', async () => {
  const cache = memCache();
  const dom = { '#new-submit': { text: 'Place order' } };
  const first = setup(dom, good('#new-submit'), { cache });
  await first.rp.click('#old-submit');
  const second = setup(dom, () => { throw new Error('LLM must not be called'); }, { cache });
  await second.rp.click('#old-submit');
  assert.equal(second.llmCalls.length, 0);
  assert.deepEqual(second.calls, [['click', '#new-submit']]);
  assert.equal(second.log.named('selector_healed')[0].data.source, 'cache');
});

test('a stale cache entry is dropped, then the original selector is tried again', async () => {
  const cache = memCache();
  cache.set('https://portal.example.com/orders/:id|#a', '#gone');
  const { rp, calls } = setup({ '#a': { text: 'ok' } }, good('#zzz'), { cache });
  await rp.click('#a');
  assert.deepEqual(calls, [['click', '#a']]);
  assert.equal(cache.store.size, 0);
});

test('action mismatch is rejected and the ORIGINAL error is thrown', async () => {
  const { rp, log, calls } = setup({ '#x': { text: 'Go' } }, good('#x', 'fill'));
  await assert.rejects(rp.click('#old'), (e) => e.name === 'TimeoutError' && /waiting for locator\('#old'\)/.test(e.message));
  assert.deepEqual(calls, []);
  assert.match(log.named('selector_heal_rejected')[0].data.reason, /action mismatch/);
});

test('deny-listed proposals (delete/remove/payment) are never auto-healed unless allowDestructive', async () => {
  const dom = { '#del': { text: 'Delete account' } };
  const denied = setup(dom, good('#del'));
  await assert.rejects(denied.rp.click('#old'), /waiting for locator/);
  assert.match(denied.log.named('selector_heal_rejected')[0].data.reason, /deny-listed/);
  assert.deepEqual(denied.calls, []);

  const allowed = setup(dom, good('#del'), { allowDestructive: true });
  await allowed.rp.click('#old');
  assert.deepEqual(allowed.calls, [['click', '#del']]);
});

test('an original selector that is itself deny-listed is not healed at all', async () => {
  const { rp, llmCalls } = setup({}, good('#whatever'));
  await assert.rejects(rp.click('#delete-everything'), /waiting for locator/);
  assert.equal(llmCalls.length, 0);
});

test('custom denyPatterns are honoured', async () => {
  const { rp, log } = setup({ '#wire': { text: 'Release funds' } }, good('#wire'), { denyPatterns: [/release funds/i] });
  await assert.rejects(rp.click('#old'), /waiting for locator/);
  assert.match(log.named('selector_heal_rejected')[0].data.reason, /deny-listed/);
});

test('proposals must match exactly one VISIBLE element', async () => {
  const two = setup({ '.btn': { text: 'Go', count: 2 } }, good('.btn'));
  await assert.rejects(two.rp.click('#old'), /waiting for locator/);
  assert.match(two.log.named('selector_heal_rejected')[0].data.reason, /matches 2 elements/);

  const hidden = setup({ '#h': { text: 'Go', visible: false } }, good('#h'));
  await assert.rejects(hidden.rp.click('#old'), /waiting for locator/);
  assert.match(hidden.log.named('selector_heal_rejected')[0].data.reason, /not visible/);

  const missing = setup({}, good('#nope'));
  await assert.rejects(missing.rp.click('#old'), /waiting for locator/);
  assert.match(missing.log.named('selector_heal_rejected')[0].data.reason, /matches 0 elements/);
});

test('unsafe selector syntax is rejected', async () => {
  const { rp, log } = setup({ 'javascript:alert(1)': { text: 'x' } }, good('javascript:alert(1)'));
  await assert.rejects(rp.click('#old'), /waiting for locator/);
  assert.match(log.named('selector_heal_rejected')[0].data.reason, /unsafe/);
});

test('LLM unavailable (null) fails safe: original error, no retry', async () => {
  const { rp, log } = setup({}, null);
  await assert.rejects(rp.click('#old'), /waiting for locator/);
  assert.match(log.named('selector_heal_rejected')[0].data.reason, /LLM unavailable/);
});

test('model output that violates the strict schema is rejected (extra property)', async () => {
  const { rp, log } = setup({ '#x': { text: 'Go' } }, { selector: '#x', action: 'click', reasoning: 'r', extra: 1 });
  await assert.rejects(rp.click('#old'), /waiting for locator/);
  assert.match(log.named('selector_heal_rejected')[0].data.reason, /schema/);
});

test('heal budget per run is enforced', async () => {
  const { rp, llmCalls, log } = setup({ '#n1': { text: 'a' }, '#n2': { text: 'b' } }, (a) => good(a.prompt.includes('#o1') ? '#n1' : '#n2'), { maxHealsPerRun: 1 });
  await rp.click('#o1');
  await assert.rejects(rp.click('#o2'), /waiting for locator/);
  assert.equal(llmCalls.length, 1);
  assert.match(log.named('selector_heal_rejected').pop().data.reason, /budget/);
  assert.equal(rp.stats().heals, 1);
});

test('the LLM never supplies values: fill uses the caller value even if the model returns one', async () => {
  const { rp, calls } = setup({ '#email': { text: '' } }, { selector: '#email', action: 'fill', value: 'attacker@evil.test', reasoning: 'r' });
  await rp.fill('#old-email', 'real@corp.test');
  assert.deepEqual(calls, [['fill', '#email', 'real@corp.test']]);
});

test('read/wait/select/check/press/hover are supported with the right action kinds', async () => {
  const dom = { '#t': { text: 'hello', attrs: { href: '/x' } } };
  for (const [method, args, kind] of [['textContent', [], 'read'], ['innerText', [], 'read'], ['getAttribute', ['href'], 'read'], ['waitFor', [], 'wait'],
    ['selectOption', ['A'], 'select'], ['check', [], 'check'], ['uncheck', [], 'uncheck'], ['press', ['Enter'], 'press'], ['hover', [], 'hover']]) {
    const { rp, llmCalls } = setup(dom, good('#t', kind));
    await rp[method]('#missing', ...args);
    assert.equal(llmCalls.length, 1, method);
  }
});

test('non-healable errors are rethrown without asking the LLM', async () => {
  const dom = { '#a': { text: 'x' } };
  const { rp, llmCalls } = setup(dom, good('#a'));
  rp.raw.locator = () => ({ click: async () => { throw new Error('Execution context was destroyed'); } });
  await assert.rejects(rp.click('#a'), /Execution context/);
  assert.equal(llmCalls.length, 0);
});

test('unknown properties fall through to the underlying page (Proxy)', () => {
  const { rp } = setup({}, null);
  assert.equal(rp.extra(), 'passthrough');
  assert.equal(typeof rp.raw.url, 'function');
});

test('urlPattern collapses ids/uuids/hex and drops query/hash', () => {
  assert.equal(urlPattern('https://p.example.com/orders/123/items/9?x=1#f'), 'https://p.example.com/orders/:id/items/:id');
  assert.equal(urlPattern('https://p.example.com/u/3f2a9c1e-1111-2222-3333-444455556666/edit'), 'https://p.example.com/u/:id/edit');
  assert.equal(urlPattern('https://p.example.com/files/deadbeef01'), 'https://p.example.com/files/:id');
  assert.equal(urlPattern('https://p.example.com/reports/summary'), 'https://p.example.com/reports/summary');
});

test('isHealable: selector-ish errors yes, others no', () => {
  assert.equal(isHealable(pwTimeout('#x')), true);
  assert.equal(isHealable(new Error('strict mode violation: locator resolved to 2 elements')), true);
  assert.equal(isHealable(new Error('Execution context was destroyed')), false);
  assert.equal(isHealable(new Error('net::ERR_CONNECTION_RESET')), false);
});

test('file cache persists across instances and survives a corrupt file', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'amx-cache-'));
  try {
    const file = path.join(dir, 'sub', 'auto.json');
    const a = createFileCache(file);
    assert.equal(a.get('k'), null);
    a.set('k', '#new');
    a.set('k', '#new');
    const b = createFileCache(file);
    assert.equal(b.get('k').selector, '#new');
    assert.equal(b.get('k').uses, 2);
    b.delete('k');
    assert.equal(createFileCache(file).get('k'), null);
    fs.writeFileSync(file, '{corrupt');
    assert.equal(createFileCache(file).get('k'), null);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
