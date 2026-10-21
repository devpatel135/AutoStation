'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { resilientPage, describeSelector, METHODS } = require('../lib/resilience');
const { eventSink } = require('./_helpers');

function pwTimeout(what) {
  return Object.assign(new Error(`locator.click: Timeout 10000ms exceeded.\nCall log:\n  - waiting for ${what}`), { name: 'TimeoutError' });
}

/** Fake page: locator(css) from a table; getByRole(role, {name}) from another; chain helpers return the same locator. */
function fakePage(dom) {
  const calls = [];
  const mkLoc = (key, el) => {
    const need = () => { if (!el) throw pwTimeout(key); };
    const loc = {
      async click() { need(); calls.push(['click', key]); },
      async dblclick() { need(); calls.push(['dblclick', key]); },
      async fill(v) { need(); calls.push(['fill', key, v]); },
      async count() { return el ? 1 : 0; },
      first() { return loc; },
      async isVisible() { return Boolean(el); },
      async evaluate() { return el ? el.text : ''; },
    };
    return loc;
  };
  const page = {
    url: () => 'https://portal.example.com/page',
    locator: (sel) => mkLoc(sel, dom[sel]),
    getByRole: (role, o) => mkLoc(`role:${role}:${o && o.name}`, dom[`role:${role}:${o && o.name}`]),
  };
  return { page, calls };
}

const memCache = () => { const m = new Map(); return { m, get: (k) => m.get(k) || null, set: (k, s) => m.set(k, { selector: s }), delete: (k) => m.delete(k) }; };

test('describeSelector: strings as-is; functions by normalised source', () => {
  assert.equal(describeSelector('#a'), '#a');
  assert.equal(describeSelector((p) => p.getByRole('button',   { name: 'Go' })), "fn:(p) => p.getByRole('button', { name: 'Go' })");
});

test('function selectors (recorder output) keep the exact Playwright chain and work like strings', async () => {
  const fp = fakePage({ 'role:button:Sign in': { text: 'Sign in' } });
  const rp = resilientPage(fp.page, { cache: memCache(), logEvent: eventSink(), llmCall: async () => { throw new Error('no LLM needed'); } });
  await rp.click((p) => p.getByRole('button', { name: 'Sign in' }));
  assert.deepEqual(fp.calls, [['click', 'role:button:Sign in']]);
});

test('a broken function selector is healed by the LLM; the healed CSS is cached under the function description', async () => {
  const fp = fakePage({ '#sign-in-v2': { text: 'Sign in' } });
  const log = eventSink();
  const cache = memCache();
  const seen = [];
  const rp = resilientPage(fp.page, {
    cache, logEvent: log, snapshot: async () => 'SNAP', captureEvidence: async () => {},
    llmCall: async (a) => { seen.push(a.prompt); return { selector: '#sign-in-v2', action: 'click', reasoning: 'same button' }; },
  });
  await rp.click((p) => p.getByRole('button', { name: 'Sign in' }));
  assert.deepEqual(fp.calls, [['click', '#sign-in-v2']]);
  assert.match(seen[0], /fn:\(p\) => p\.getByRole/);
  const key = [...cache.m.keys()][0];
  assert.ok(key.includes('fn:(p) => p.getByRole'));
  assert.equal(log.named('selector_healed')[0].data.healed, '#sign-in-v2');
});

test('heal failure captures failure evidence once and rethrows the original error', async () => {
  const fp = fakePage({});
  const ev = [];
  const rp = resilientPage(fp.page, {
    cache: memCache(), logEvent: eventSink(), snapshot: async () => 'S',
    llmCall: async () => null, captureEvidence: async (err, ctx) => ev.push([err.name, ctx.reason, ctx.page === fp.page]),
  });
  await assert.rejects(() => rp.click('#gone'), /Timeout/);
  assert.equal(ev.length, 1);
  assert.equal(ev[0][0], 'TimeoutError');
  assert.match(ev[0][1], /#gone.*healing failed/);
  assert.equal(ev[0][2], true);
});

test('a healed selector that fails on retry also captures evidence', async () => {
  const dom = { '#new': { text: 'x' } };
  const fp = fakePage(dom);
  // exists for validation (count) but click fails
  const origLocator = fp.page.locator;
  fp.page.locator = (sel) => {
    const l = origLocator(sel);
    if (sel === '#new') l.click = async () => { throw pwTimeout('#new'); };
    return l;
  };
  const ev = [];
  const rp = resilientPage(fp.page, {
    cache: memCache(), logEvent: eventSink(), snapshot: async () => 'S',
    llmCall: async () => ({ selector: '#new', action: 'click', reasoning: 'r' }),
    captureEvidence: async (err, ctx) => ev.push(ctx.reason),
  });
  await assert.rejects(() => rp.click('#old'), /Timeout/);
  assert.equal(ev.length, 1);
  assert.match(ev[0], /failed on retry/);
});

test('dblclick is a resilient method (kind click)', async () => {
  assert.equal(METHODS.dblclick.kind, 'click');
  const fp = fakePage({ '#row': { text: 'r' } });
  const rp = resilientPage(fp.page, { cache: memCache(), logEvent: eventSink() });
  await rp.dblclick('#row');
  assert.deepEqual(fp.calls, [['dblclick', '#row']]);
});

test('debugSelectors logs every locator with its match count', async () => {
  const fp = fakePage({ '#a': { text: 'a' } });
  const log = eventSink();
  const rp = resilientPage(fp.page, { cache: memCache(), logEvent: log, debugSelectors: true });
  await rp.click('#a');
  const d = log.named('selector_debug')[0].data;
  assert.deepEqual([d.selector, d.action, d.matches], ['#a', 'click', 1]);
  const off = eventSink();
  await resilientPage(fp.page, { cache: memCache(), logEvent: off, debugSelectors: false }).click('#a');
  assert.equal(off.named('selector_debug').length, 0);
});
