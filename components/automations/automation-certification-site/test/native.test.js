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
