'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { withRunDir } = require('./_helpers');
const wrapper = require('../lib/wrapper');

// Golden lines copied from wrapper-core's Python writer (wrapper_core/runctx.py, SPEC 6.1).
const GOLDEN = [
  '{"ts":"2026-10-21T00:21:44.719Z","event":"phase","data":{"name":"execute","status":"start","slot":"daily","params":["mode"]}}',
  '{"ts":"2026-10-21T00:21:45.299Z","event":"customer","data":{"name":"Jane","ssn":"123-45-6789"},"sensitive_keys":["ssn"]}',
  '{"ts":"2026-10-21T00:21:45.400Z","event":"item_processed","data":{"item_id":"ITEM-1","outcome":"success","duration_ms":11}}',
];

test('events.jsonl lines are byte-compatible with the Python writer (shape, key order, compact JSON)', (t) => {
  const { ctx } = withRunDir(t);
  const rec = wrapper.logEvent('customer', { name: 'Jane', ssn: '123-45-6789' }, { sensitiveKeys: ['ssn', 'ssn'] });
  assert.match(rec.ts, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  const line = fs.readFileSync(path.join(ctx().runDir, 'events.jsonl'), 'utf8').trimEnd();
  assert.deepEqual(Object.keys(JSON.parse(line)), ['ts', 'event', 'data', 'sensitive_keys']);
  assert.equal(line.replace(/"ts":"[^"]+"/, '"ts":"X"'), GOLDEN[1].replace(/"ts":"[^"]+"/, '"ts":"X"'));
  assert.ok(!line.includes(': ') && !line.includes(', '), 'must be compact JSON like Python separators=(",", ":")');
});

test('lines written by Python parse in readEvents; corrupt lines become corrupt_event_line', (t) => {
  const { ctx } = withRunDir(t);
  fs.writeFileSync(ctx().eventsPath, `${GOLDEN.join('\n')}\nnot json\n`);
  const evs = ctx().readEvents();
  assert.equal(evs.length, 4);
  assert.equal(evs[1].sensitive_keys[0], 'ssn');
  assert.equal(evs[3].event, 'corrupt_event_line');
});

test('logEvent: sensitive=true marks every key; no sensitive_keys field when none', (t) => {
  const { ctx } = withRunDir(t);
  wrapper.logEvent('a', { x: 1, y: 2 }, { sensitive: true });
  wrapper.logEvent('b', { x: 1 });
  const [a, b] = ctx().readEvents();
  assert.deepEqual(a.sensitive_keys, ['x', 'y']);
  assert.equal('sensitive_keys' in b, false);
});

test('logItem matches the item_processed format; validates outcome; accepts durationMs', (t) => {
  const { ctx } = withRunDir(t);
  wrapper.logItem('INV-1', 'success', { durationMs: 11.9, routed_to: 'AP' });
  wrapper.logItem('INV-2', 'skipped', { duration_ms: 5, reason: 'duplicate' });
  const [a, b] = ctx().readEvents();
  assert.deepEqual(a, { ts: a.ts, event: 'item_processed', data: { item_id: 'INV-1', outcome: 'success', duration_ms: 11, routed_to: 'AP' } });
  assert.deepEqual(Object.keys(a.data), ['item_id', 'outcome', 'duration_ms', 'routed_to']);
  assert.equal(b.data.reason, 'duplicate');
  assert.throws(() => wrapper.logItem('x', 'maybe'), /outcome must be/);
});

test('trackItem logs success, override, and failure (and rethrows)', async (t) => {
  const { ctx } = withRunDir(t);
  await wrapper.trackItem('A', async () => 'ok');
  await wrapper.trackItem('B', async (it) => { it.outcome = 'skipped'; it.reason = 'dup'; });
  await assert.rejects(() => wrapper.trackItem('C', async () => { throw new TypeError('boom'); }), TypeError);
  const items = ctx().readEvents().map((e) => e.data);
  assert.deepEqual(items.map((d) => d.outcome), ['success', 'skipped', 'failed']);
  assert.equal(items[1].reason, 'dup');
  assert.equal(items[2].error_category, 'TypeError');
  assert.ok(items.every((d) => typeof d.duration_ms === 'number'));
});

test('params/param/isDryRun/assets/setResult/emitTrigger', (t) => {
  const { ctx, home } = withRunDir(t);
  const env = { AUTOMATION_PARAM_DATE_RANGE: 'yesterday', AUTOMATION_PARAM_Mode: 'x', AUTOMAGIX_DRY_RUN: '1' };
  assert.deepEqual(wrapper.params(env), { date_range: 'yesterday', mode: 'x' });
  assert.equal(wrapper.param('DATE_RANGE', 'dflt', env), 'yesterday');
  assert.equal(wrapper.param('missing', 'dflt', env), 'dflt');
  assert.equal(wrapper.isDryRun(env), true);
  assert.equal(wrapper.isDryRun({}), false);

  fs.mkdirSync(path.join(home, 'config'), { recursive: true });
  fs.writeFileSync(path.join(home, 'config', 'assets.json'), JSON.stringify({ internal_api_url: 'https://x' }));
  assert.deepEqual(wrapper.assets(), { internal_api_url: 'https://x' });

  wrapper.setResult({ processed: 3 });
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(ctx().runDir, 'result.json'), 'utf8')), { processed: 3 });
  wrapper.emitTrigger('automation-b', { k: 'v' }, 'done');
  assert.deepEqual(ctx().readEvents().pop().data, { target: 'automation-b', params: { k: 'v' }, reason: 'done' });
});

test('entrypoint writes error.json like Python (category/type/message/traceback_tail/escalated/attempts) and exits 1', async (t) => {
  const { ctx } = withRunDir(t);
  let code = null;
  let stderr = '';
  const io = { stderr: { write: (s) => { stderr += s; } }, setExitCode: (c) => { code = c; } };

  const rc = await wrapper.entrypoint(async () => { throw new TypeError('x is not a function'); }, { fake: 'fw' }, io);
  assert.equal(rc, 1);
  assert.equal(code, 1);
  const err = ctx().readError();
  assert.deepEqual(Object.keys(err).sort(), ['attempts', 'category', 'escalated', 'message', 'traceback_tail', 'type']);
  assert.equal(err.category, 'LOGIC_ERROR');
  assert.equal(err.type, 'TypeError');
  assert.equal(err.escalated, false);
  assert.match(stderr, /TypeError/);

  const E = wrapper.errors;
  await wrapper.entrypoint(async () => { throw new E.Escalation('NETWORK_ERROR: down', E.Category.NETWORK_ERROR, 4, Object.assign(new Error('down'), { name: 'ConnectionError' })); }, {}, io);
  const e2 = ctx().readError();
  assert.equal(e2.escalated, true);
  assert.equal(e2.attempts, 4);
  assert.equal(e2.category, 'NETWORK_ERROR');
  assert.equal(e2.type, 'ConnectionError');

  let seen;
  assert.equal(await wrapper.entrypoint(async (fw) => { seen = fw; }, { fake: 'fw' }, io), 0);
  assert.deepEqual(seen, { fake: 'fw' });
});

test('runItem through the SDK writes item_processed events WITH durations (success, skipped, failed)', async (t) => {
  const { ctx } = withRunDir(t);
  const policy = new wrapper.RetryPolicy({ networkRetryWaitS: 0 });
  await wrapper.runItem('A', async () => 'ok', policy);
  await wrapper.runItem('B', async () => { throw new wrapper.errors.DataError('missing vendor_id'); }, policy);
  await assert.rejects(wrapper.runItem('C', async () => { throw new TypeError('bug'); }, policy), wrapper.errors.Escalation);
  const items = ctx().readEvents().filter((e) => e.event === 'item_processed').map((e) => e.data);
  assert.deepEqual(items.map((d) => [d.item_id, d.outcome]), [['A', 'success'], ['B', 'skipped'], ['C', 'failed']]);
  assert.ok(items.every((d) => Number.isInteger(d.duration_ms) && d.duration_ms >= 0), 'duration_ms must be an integer on every item');
  assert.equal(items[1].reason, 'data_error');
  assert.equal(items[2].error_category, 'LOGIC_ERROR');
  assert.ok(!items.some((d) => 'durationMs' in d), 'camelCase key must not leak into the wire format');
});
