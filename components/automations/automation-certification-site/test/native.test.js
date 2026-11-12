'use strict';
const test = require('node:test');
const assert = require('node:assert');
const native = require('../lib/native');

test('policy detection reads RemoteDebuggingAllowed=0', { skip: process.platform !== 'win32' }, () => {
  assert.strictEqual(native.devtoolsBlockedByPolicy(() => '\r\n    RemoteDebuggingAllowed    REG_DWORD    0x0\r\n'), true);
  assert.strictEqual(native.devtoolsBlockedByPolicy(() => '    RemoteDebuggingAllowed    REG_DWORD    0x1'), false);
  assert.strictEqual(native.devtoolsBlockedByPolicy(() => { throw new Error('not found'); }), false);
});

test('recognises the managed-Edge launch failure', () => {
  assert.ok(native.isDevtoolsPolicyError(new Error('x\nDevTools remote debugging is disallowed by the system admin.')));
  assert.ok(!native.isDevtoolsPolicyError(new Error('net::ERR_CONNECTION_RESET')));
});

test('parses the last JSON line and fails closed on garbage', () => {
  assert.deepStrictEqual(native.parseResult('noise\n{"state":"account_selected","clicked":true}\n'), { state: 'account_selected', clicked: true });
  assert.strictEqual(native.parseResult('boom').state, 'error');
});

test('runNative passes validated args to powershell', () => {
  let seen;
  const r = native.runNative({ url: 'https://example.test/', account: 'a@b.co', timeoutS: 5, dryRun: true },
    (cmd, args) => { seen = { cmd, args }; return { stdout: '{"state":"target","clicked":false}' }; });
  assert.strictEqual(r.state, 'target');
  assert.strictEqual(seen.cmd, 'powershell.exe');
  assert.ok(seen.args.includes('-DryRun') && seen.args.includes('a@b.co'));
});

test('judge: only a pressed tile or an already-signed-in target is a success', () => {
  assert.deepStrictEqual(native.judge({ state: 'account_selected', clicked: true }, false), { ok: true });
  assert.deepStrictEqual(native.judge({ state: 'target', clicked: false }, false), { ok: true });
  assert.deepStrictEqual(native.judge({ state: 'account-picker', clicked: false }, true), { ok: true });   // dry run finds the picker
  for (const r of [{ state: 'account_selected', clicked: false }, { state: 'account-picker', clicked: false }, { state: 'other', clicked: false }, { state: 'error', detail: 'boom' }, null, {}]) {
    assert.strictEqual(native.judge(r, false).ok, false, JSON.stringify(r));
  }
  assert.strictEqual(native.judge({ state: 'manual_step_required', clicked: false }, false).kind, 'manual');
  assert.strictEqual(native.judge({ state: 'signin' }, false).kind, 'manual');
  assert.strictEqual(native.judge({ state: 'other' }, false).kind, 'other');
});

test('runNative passes the portal host and only a sane one', () => {
  let args = [];
  native.runNative({ url: 'https://example.test/', account: 'a@b.co', timeoutS: 5, dryRun: false, portalHost: 'portal.example.test' }, (_c, a) => { args = a; return { stdout: '{"state":"target"}' }; });
  assert.ok(args.includes('-PortalHost') && args.includes('portal.example.test'));
  native.runNative({ url: 'https://example.test/', account: 'a@b.co', timeoutS: 5, dryRun: false, portalHost: 'x; evil' }, (_c, a) => { args = a; return { stdout: '{"state":"target"}' }; });
  assert.ok(!args.includes('-PortalHost'));
});

test('the native script matches no translated captions and closes the windows it opened', () => {
  const ps = require('node:fs').readFileSync(require('node:path').join(__dirname, '..', 'scripts', 'native-click-account.ps1'), 'utf8');
  assert.ok(!/Sign in to your account|Pick an account/.test(ps.replace(/<#[\s\S]*?#>/, '')), 'no English caption matching in code');
  assert.ok(/Close-Ours/.test(ps) && /WindowPattern/.test(ps));
});
