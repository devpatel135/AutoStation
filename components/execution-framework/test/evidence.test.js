'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const evidence = require('../lib/evidence');
const events = require('../lib/events');
const wrapper = require('../lib/wrapper');
const { eventSink, withRunDir } = require('./_helpers');

// ---- naming / types ------------------------------------------------------------------------------------
test('sanitizeName: safe basenames, allowed types only, extension from content type', () => {
  assert.deepEqual(evidence.sanitizeName('shot.png'), { name: 'shot.png', ext: 'png', contentType: 'image/png' });
  assert.equal(evidence.sanitizeName('../../etc/hosts.txt').name, 'hosts.txt');
  assert.equal(evidence.sanitizeName('C:\\temp\\a b!.json').name, 'a_b.json');
  assert.equal(evidence.sanitizeName('photo.jpeg').name, 'photo.jpg');
  assert.equal(evidence.sanitizeName('noext', 'text/html').name, 'noext.html');
  assert.equal(evidence.sanitizeName('x.txt', 'image/png').name, 'x.png', 'content type wins over a misleading extension');
  assert.match(evidence.sanitizeName('x.exe').error, /not allowed/);
  assert.match(evidence.sanitizeName('x.bin', 'application/octet-stream').error, /content type .* not allowed/);
  assert.match(evidence.sanitizeName('x').error, /not allowed/);
  assert.equal(evidence.sanitizeName('...hidden.txt').name, 'hidden.txt');
});

test('cookies / storage / credentials are refused by name', () => {
  for (const bad of ['cookies.json', 'storage-state.json', 'localStorage.txt', 'session_storage.json', 'password.txt', 'my-token.txt', 'credentials.json']) {
    assert.match(evidence.sanitizeName(bad).error, /refused/, bad);
  }
});

test('scrubText masks bearer tokens, JWTs, password/token assignments, SSNs and Luhn-valid card numbers', () => {
  const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2';
  const out = evidence.scrubText(`Authorization: Bearer abcdef123456 jwt=${jwt} password=hunter22 {"token":"zzz999"} ssn 123-45-6789 card 4111 1111 1111 1111 order 4111 1111 1111 1112`);
  assert.ok(!/abcdef123456|hunter22|zzz999|123-45-6789|4111 1111 1111 1111/.test(out), out);
  assert.ok(out.includes('[REDACTED-JWT]') && out.includes('[REDACTED-SSN]') && out.includes('[REDACTED-CARD]'));
  assert.ok(out.includes('4111 1111 1111 1112'), 'a non-Luhn digit run is left alone');
  assert.equal(evidence.luhnOk('4111111111111111'), true);
});

// ---- store ---------------------------------------------------------------------------------------------
function store(t, limits) {
  const r = withRunDir(t);
  const log = eventSink();
  return { s: evidence.createStore({ runDir: r.runDir, logEvent: log, limits }), log, runDir: r.runDir };
}

test('store.add writes into <run>/evidence, scrubs text, de-duplicates names, logs evidence_added', (t) => {
  const { s, log, runDir } = store(t);
  const a = s.add('note.txt', 'password=hunter22 hello', 'text/plain');
  assert.equal(a.ok, true);
  assert.equal(fs.readFileSync(path.join(runDir, 'evidence', 'note.txt'), 'utf8'), 'password=[REDACTED] hello');
  const b = s.add('note.txt', 'again', 'text/plain');
  assert.equal(b.name, 'note-2.txt');
  assert.deepEqual(s.list().map((f) => f.name).sort(), ['note-2.txt', 'note.txt']);
  assert.equal(log.named('evidence_added').length, 2);
});

test('store caps: at most 8 files and 1.5 MB total; excess is dropped with a logged reason (never truncated)', (t) => {
  const { s, log } = store(t, { maxFiles: 3, maxTotalBytes: 100, maxCaptures: 2 });
  assert.equal(s.add('a.txt', 'x'.repeat(40)).ok, true);
  assert.equal(s.add('b.txt', 'x'.repeat(40)).ok, true);
  const big = s.add('c.txt', 'x'.repeat(40));
  assert.equal(big.ok, false);
  assert.match(big.reason, /size limit/);
  assert.equal(s.add('d.txt', 'x').ok, true);
  const full = s.add('e.txt', 'x');
  assert.match(full.reason, /file limit/);
  assert.equal(log.named('evidence_dropped').length, 2);
  assert.equal(s.list().length, 3);
  assert.equal(evidence.LIMITS.maxFiles, 8);
  assert.equal(evidence.LIMITS.maxTotalBytes, 1.5 * 1024 * 1024);
});

test('store.add never throws (bad type, no data, unwritable dir)', (t) => {
  const { s } = store(t);
  assert.equal(s.add('x.exe', 'a').ok, false);
  assert.equal(s.add('x.txt', undefined).ok, false);
  const broken = evidence.createStore({ runDir: '/definitely/not/writable', fsMod: { readdirSync() { throw new Error('nope'); }, statSync() { return { size: 0 }; }, mkdirSync() { throw new Error('EACCES'); }, writeFileSync() {} } });
  const r = broken.add('x.txt', 'a');
  assert.equal(r.ok, false);
  assert.match(r.reason, /write failed/);
});

test('wrapper.addEvidence writes to the active run dir', (t) => {
  const r = withRunDir(t);
  const res = wrapper.addEvidence('hello.txt', 'hi there', 'text/plain');
  assert.equal(res.ok, true);
  assert.equal(fs.readFileSync(path.join(r.runDir, 'evidence', 'hello.txt'), 'utf8'), 'hi there');
  assert.ok(events.current().readEvents().some((e) => e.event === 'evidence_added'));
});

// ---- captureFailure ------------------------------------------------------------------------------------
function fakeSession(over = {}) {
  const page = {
    url: () => 'https://portal.example.com/orders?token=SECRET123&id=9#frag',
    screenshot: async () => Buffer.from('PNGBYTES'),
    ...over.page,
  };
  return { page, context: over.context || null, trace: Boolean(over.trace), name: 'fake', traceStopped: false };
}

test('captureFailure: screenshot + redacted DOM snapshot + context.txt (masked URL, last 20 events, no secrets)', async (t) => {
  const r = withRunDir(t);
  const ctx = events.current();
  for (let i = 0; i < 30; i += 1) ctx.append('step', { n: i });
  ctx.append('customer', { ssn: '123-45-6789', note: 'x' }, ['ssn']);
  const err = new Error('boom password=hunter22');
  const sess = fakeSession();
  const res = await evidence.captureFailure(err, { sessions: [sess], snapshot: async () => 'URL: https://portal.example.com/orders\n[1] button "Pay" sel=#pay\n', reason: 'unit test' });
  assert.deepEqual(res.captured, ['fail1-screenshot.png', 'fail1-dom-snapshot.txt', 'fail1-context.txt']);
  const dir = path.join(r.runDir, 'evidence');
  assert.equal(fs.readFileSync(path.join(dir, 'fail1-screenshot.png'), 'utf8'), 'PNGBYTES');
  assert.ok(fs.readFileSync(path.join(dir, 'fail1-dom-snapshot.txt'), 'utf8').includes('sel=#pay'));
  const c = fs.readFileSync(path.join(dir, 'fail1-context.txt'), 'utf8');
  assert.match(c, /reason: unit test/);
  assert.ok(c.includes('url: https://portal.example.com/orders?token=…&id=…'), c);
  assert.ok(!/SECRET123|hunter22|123-45-6789/.test(c), 'no secrets in context.txt');
  assert.ok(c.includes('[REDACTED]'), 'sensitive_keys redaction applied to the event tail');
  assert.ok(!c.includes('"n":5,') && c.includes('"n":29'), 'only the last 20 events');
  assert.ok(events.current().readEvents().some((e) => e.event === 'evidence_captured'));
});

test('captureFailure: every step is independently guarded - a failing screenshot never blocks the rest or throws', async (t) => {
  withRunDir(t);
  const sess = fakeSession({ page: { screenshot: async () => { throw new Error('page closed'); } } });
  const res = await evidence.captureFailure(new Error('x'), { sessions: [sess], snapshot: async () => { throw new Error('dom gone'); } });
  assert.deepEqual(res.captured, ['fail1-context.txt']);
  assert.equal(res.dropped.length, 2);
  assert.ok(res.dropped.some((d) => /screenshot failed/.test(d)) && res.dropped.some((d) => /dom snapshot failed/.test(d)));
});

test('captureFailure: same error object once; later failures get fail2-; then the capture limit', async (t) => {
  const r = withRunDir(t);
  const sess = fakeSession();
  const snap = async () => 'snap';
  const e1 = new Error('one');
  assert.equal((await evidence.captureFailure(e1, { sessions: [sess], snapshot: snap })).captured.length, 3);
  assert.equal((await evidence.captureFailure(e1, { sessions: [sess], snapshot: snap })).skipped, 'already captured');
  const second = await evidence.captureFailure(new Error('two'), { sessions: [sess], snapshot: snap });
  assert.ok(second.captured.includes('fail2-context.txt'));
  const third = await evidence.captureFailure(new Error('three'), { sessions: [sess], snapshot: snap });
  assert.equal(third.skipped, 'capture limit reached');
  assert.ok(fs.readdirSync(path.join(r.runDir, 'evidence')).length <= 8);
});

test('captureFailure: no browser session -> skipped (non-browser automations get no evidence noise)', async (t) => {
  const r = withRunDir(t);
  const res = await evidence.captureFailure(new Error('x'), { sessions: [] });
  assert.equal(res.skipped, 'no browser session');
  assert.equal(fs.existsSync(path.join(r.runDir, 'evidence')), false);
  // a page that was not launched through the framework has no session either
  const res2 = await evidence.captureFailure(new Error('y'), { sessions: [fakeSession()], page: { url: () => 'x' } });
  assert.equal(res2.skipped, 'no browser session');
});

test('captureFailure: trace.zip is kept only on failure (and dropped by the size cap if too big)', async (t) => {
  const r = withRunDir(t);
  let stoppedWith = null;
  const context = { tracing: { stop: async (o) => { stoppedWith = o; if (o && o.path) fs.writeFileSync(o.path, 'ZIPDATA'); } } };
  const sess = fakeSession({ trace: true, context });
  sess.context = context;
  const res = await evidence.captureFailure(new Error('x'), { sessions: [sess], snapshot: async () => 's' });
  assert.ok(res.captured.includes('trace.zip'));
  assert.ok(stoppedWith && stoppedWith.path);
  assert.equal(fs.existsSync(stoppedWith.path), false, 'temporary trace file removed');
  assert.equal(fs.readFileSync(path.join(r.runDir, 'evidence', 'trace.zip'), 'utf8'), 'ZIPDATA');
  // success path: stopTrace without a path discards
  const ok = { tracing: { stop: async (o) => { stoppedWith = o; } }, };
  const s2 = { context: ok, trace: true, traceStopped: false };
  assert.equal(await evidence.stopTrace(s2), true);
  assert.equal(stoppedWith, undefined);
  assert.equal(await evidence.stopTrace(s2), false, 'only stops once');
});

test('captureFailure: interactive pause only when the debug options ask for it', async (t) => {
  withRunDir(t);
  const calls = [];
  const sess = fakeSession();
  await evidence.captureFailure(new Error('a'), { sessions: [sess], snapshot: async () => 's', debug: { pauseOnError: false }, pause: async () => calls.push('x') });
  assert.equal(calls.length, 0);
  await evidence.captureFailure(new Error('b'), { sessions: [sess], snapshot: async () => 's', debug: { pauseOnError: true, pauseMinutes: 3 }, pause: async (o) => calls.push(o.minutes) });
  assert.deepEqual(calls, [3]);
});

test('entrypoint captures evidence from the live session before writing error.json, and escalations are captured too', async (t) => {
  const r = withRunDir(t);
  const sess = fakeSession({ page: { screenshot: async () => Buffer.from('PNG') } });
  const off = evidence.registerSession(sess);
  t.after(off);
  const errs = [];
  const io = { stderr: { write: (m) => errs.push(m) }, setExitCode() {} };
  const code = await wrapper.entrypoint(async () => { throw new TypeError('x is not a function'); }, {}, io);
  assert.equal(code, 1);
  const dir = path.join(r.runDir, 'evidence');
  assert.ok(fs.readdirSync(dir).some((f) => /^fail\d+-screenshot\.png$/.test(f)), 'evidence written');
  assert.equal(JSON.parse(fs.readFileSync(path.join(r.runDir, 'error.json'), 'utf8')).category, 'LOGIC_ERROR', 'error.json still written');
});
