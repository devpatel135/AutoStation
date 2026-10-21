'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const E = require('../lib/errors');
const { RetryPolicy, runWithRetry, runItem, SKIPPED } = require('../lib/retry');
const { eventSink } = require('./_helpers');

const net = (msg = 'net down') => Object.assign(new Error(msg), { code: 'ECONNRESET' });

function harness(over = {}) {
  const log = eventSink();
  const waits = [];
  const vpn = [];
  const deps = {
    logEvent: log,
    readEvents: () => [],
    llmCall: async () => null,
    sleep: async (s) => { waits.push(s); },
    runVpn: (p) => { vpn.push(p.vpnTool); return true; },
    ...over,
  };
  return { log, waits, vpn, deps };
}

function flaky(fails, makeErr) {
  const f = async () => {
    f.n += 1;
    if (f.n <= fails) throw makeErr();
    return 'ok';
  };
  f.n = 0;
  return f;
}

const pol = (o = {}) => new RetryPolicy({ networkRetryWaitS: 0, ...o });
const retryEvents = (h) => h.log.named('retry_event').map((e) => e.data);

test('network error: retries, runs VPN tool, recovers; one retry_event per failed attempt with outcomes', async () => {
  const h = harness();
  const f = flaky(2, net);
  const out = await runWithRetry(f, pol({ maxRetries: 3, vpnTool: 'tools/vpn.ps1' }), { deps: h.deps });
  assert.equal(out, 'ok');
  const evs = retryEvents(h);
  assert.deepEqual(evs.map((e) => e.decision), ['retry', 'retry']);
  assert.deepEqual(evs.map((e) => e.outcome), ['failed_again', 'recovered']);
  assert.ok(evs.every((e) => e.source === 'deterministic' && e.category === 'NETWORK_ERROR'));
  assert.deepEqual(h.vpn, ['tools/vpn.ps1', 'tools/vpn.ps1']);
});

test('network exhaustion escalates with attempts and category', async () => {
  const h = harness();
  const f = flaky(99, net);
  await assert.rejects(runWithRetry(f, pol({ maxRetries: 2 }), { deps: h.deps }), (e) => {
    assert.ok(e instanceof E.Escalation);
    assert.equal(e.category, 'NETWORK_ERROR');
    assert.equal(e.attempts, 3);
    return true;
  });
  assert.equal(f.n, 3);
  assert.equal(retryEvents(h).pop().outcome, 'escalated');
});

test('LOGIC_ERROR is never retried', async () => {
  const h = harness();
  const f = flaky(99, () => new TypeError('x is not a function'));
  await assert.rejects(runWithRetry(f, pol({ maxRetries: 5 }), { deps: h.deps }), E.Escalation);
  assert.equal(f.n, 1);
});

test('AUTH_EXPIRED: refresh once then retry once; second failure escalates', async () => {
  const h = harness();
  let refreshed = 0;
  const ok = await runWithRetry(flaky(1, () => new E.AuthExpired('expired')), pol(), { deps: h.deps, authRefresh: async () => { refreshed += 1; } });
  assert.equal(ok, 'ok');
  assert.equal(refreshed, 1);
  const g = flaky(5, () => new E.AuthExpired('expired'));
  await assert.rejects(runWithRetry(g, pol(), { deps: harness().deps, authRefresh: async () => {} }), E.Escalation);
  assert.equal(g.n, 2);
});

test('AUTH refresh failure escalates and is audited', async () => {
  const h = harness();
  await assert.rejects(runWithRetry(flaky(5, () => new E.AuthExpired('x')), pol(), { deps: h.deps, authRefresh: async () => { throw new Error('cert store locked'); } }),
    /refresh failed: cert store locked/);
  assert.equal(retryEvents(h).pop().outcome, 'escalated');
});

test('SYSTEM_UNAVAILABLE: exponential backoff 10,20,40 then escalate', async () => {
  const h = harness();
  const f = flaky(99, () => new E.SystemUnavailable('503'));
  await assert.rejects(runWithRetry(f, new RetryPolicy({ networkRetryWaitS: 10, backoffMultiplier: 2, maxRetries: 5 }), { deps: h.deps }), E.Escalation);
  assert.deepEqual(h.waits, [10, 20, 40]);
  assert.equal(f.n, 4);
});

test('DATA_ERROR: skip when skipOnDataError, escalate otherwise', async () => {
  const h = harness();
  assert.equal(await runWithRetry(flaky(1, () => new E.DataError('bad')), pol(), { deps: h.deps }), SKIPPED);
  assert.equal(retryEvents(h)[0].decision, 'skip');
  assert.equal(retryEvents(h)[0].outcome, 'skipped');
  await assert.rejects(runWithRetry(flaky(1, () => new E.DataError('bad')), pol({ skipOnDataError: false }), { deps: harness().deps }), E.Escalation);
});

test('runItem: success / skipped / failed telemetry', async () => {
  const h = harness();
  const items = [];
  const logItem = (id, outcome, fields) => items.push({ id, outcome, ...fields });
  assert.equal(await runItem('A', async () => 42, pol(), { deps: h.deps, logItem }), 42);
  assert.equal(await runItem('B', async () => { throw new E.DataError('missing vendor_id'); }, pol(), { deps: h.deps, logItem }), null);
  await assert.rejects(runItem('C', async () => { throw new TypeError('bug'); }, pol(), { deps: h.deps, logItem }), E.Escalation);
  assert.deepEqual(items.map((i) => [i.id, i.outcome]), [['A', 'success'], ['B', 'skipped'], ['C', 'failed']]);
  assert.equal(items[1].reason, 'data_error');
  assert.equal(items[2].error_category, 'LOGIC_ERROR');
  assert.equal(retryEvents(h)[0].item_id, 'B');
});

test('runItem default logging writes item_processed events with duration', async () => {
  const h = harness();
  await runItem('Z', async () => 1, pol(), { deps: h.deps });
  const ev = h.log.named('item_processed')[0].data;
  assert.equal(ev.item_id, 'Z');
  assert.equal(ev.outcome, 'success');
  assert.equal(typeof ev.duration_ms, 'number');
});

test('UNKNOWN: LLM transient verdict allows a retry, audited with hash + reasoning only', async () => {
  const calls = [];
  const h = harness({ llmCall: async (a) => { calls.push(a); return { category: 'transient', retry: true, reasoning: 'flaky upstream' }; } });
  const f = flaky(1, () => new Error('quantum fluctuation'));
  assert.equal(await runWithRetry(f, pol({ maxRetries: 2 }), { deps: h.deps }), 'ok');
  const ev = retryEvents(h)[0];
  assert.equal(ev.source, 'llm');
  assert.equal(ev.reasoning, 'flaky upstream');
  const dec = h.log.named('llm_decision')[0].data;
  assert.equal(dec.template, 'retry-classifier@1');
  assert.match(dec.input_hash, /^[0-9a-f]{12}$/);
  assert.ok(!JSON.stringify(dec).includes('quantum fluctuation'), 'audit record must carry a hash, not the error text');
  assert.equal(calls[0].caller, 'wrapper-core/retry-classifier');
  assert.deepEqual(calls[0].schema, { category: 'transient|permanent|unknown', retry: 'boolean', reasoning: 'string' });
});

test('UNKNOWN: no LLM verdict => ESCALATE (fail safe); permanent verdict => escalate; LLM can never retry a LOGIC error', async () => {
  const none = harness({ llmCall: async () => null });
  const f = flaky(1, () => new Error('odd'));
  await assert.rejects(runWithRetry(f, pol(), { deps: none.deps }), E.Escalation);
  assert.equal(f.n, 1);
  assert.equal(retryEvents(none)[0].source, 'fallback');

  const perm = harness({ llmCall: async () => ({ category: 'permanent', retry: false, reasoning: 'bad credentials' }) });
  await assert.rejects(runWithRetry(flaky(1, () => new Error('odd')), pol(), { deps: perm.deps }), E.Escalation);
  assert.equal(retryEvents(perm)[0].source, 'llm');

  let asked = false;
  const eager = harness({ llmCall: async () => { asked = true; return { category: 'transient', retry: true, reasoning: 'x' }; } });
  const g = flaky(1, () => new TypeError('bug'));
  await assert.rejects(runWithRetry(g, pol(), { deps: eager.deps }), E.Escalation);
  assert.equal(asked, false);
  assert.equal(g.n, 1);
});

test('UNKNOWN: a throwing LLM client is treated as no verdict', async () => {
  const h = harness({ llmCall: async () => { throw new Error('broker down'); } });
  await assert.rejects(runWithRetry(flaky(1, () => new Error('odd')), pol(), { deps: h.deps }), E.Escalation);
});

test('UNKNOWN: LLM context contains redacted events only', async () => {
  let ctx = '';
  const h = harness({
    readEvents: () => [{ ts: 't', event: 'customer', data: { name: 'Jane', ssn: '123-45-6789' }, sensitive_keys: ['ssn'] }],
    llmCall: async (a) => { ctx = a.context; return { category: 'transient', retry: true, reasoning: 'r' }; },
  });
  await runWithRetry(flaky(1, () => new Error('odd')), pol(), { deps: h.deps });
  assert.ok(ctx.includes('[REDACTED]'));
  assert.ok(!ctx.includes('123-45-6789'));
});

test('useLlm:false skips the classifier entirely', async () => {
  let asked = false;
  const h = harness({ llmCall: async () => { asked = true; return null; } });
  await assert.rejects(runWithRetry(flaky(1, () => new Error('odd')), pol(), { deps: h.deps, useLlm: false }), E.Escalation);
  assert.equal(asked, false);
});

test('RetryPolicy.fromEnv / fromManifest read the snake_case manifest block', () => {
  const p = RetryPolicy.fromEnv({ AUTOMAGIX_ERROR_POLICY: JSON.stringify({ max_retries: 5, network_retry_wait_s: 7, backoff_multiplier: 3, vpn_tool: 'a.ps1', skip_on_data_error: false }) }, '/work');
  assert.equal(p.maxRetries, 5);
  assert.equal(p.networkRetryWaitS, 7);
  assert.equal(p.backoffMultiplier, 3);
  assert.equal(p.vpnTool, 'a.ps1');
  assert.equal(p.skipOnDataError, false);
  assert.equal(p.cwd, '/work');
  const d = RetryPolicy.fromEnv({}, '.');
  assert.equal(d.maxRetries, 3);
  assert.equal(d.networkRetryWaitS, 60);
  assert.equal(RetryPolicy.fromEnv({ AUTOMAGIX_ERROR_POLICY: 'not json' }, '.').maxRetries, 3);
});
