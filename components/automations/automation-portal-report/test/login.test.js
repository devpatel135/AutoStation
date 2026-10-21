'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { DEFAULT_URL, DEFAULT_ACCOUNT, assertEmail, accountTileSelectors, classifyPage } = require('../lib/login');

test('defaults are the requested portal and account', () => {
  assert.equal(DEFAULT_URL, 'https://develop.deloitte.com/ai-season-of-certifications#subpage/res');
  assert.equal(DEFAULT_ACCOUNT, 'devpatel@deloitte.com');
});

test('account is validated before it is interpolated into selectors', () => {
  assert.equal(assertEmail('devpatel@deloitte.com'), 'devpatel@deloitte.com');
  for (const bad of ['', 'nope', 'a"b@x.com', 'a@b', "x'y@z.com", 'a b@c.com', '<script>@x.com']) {
    assert.throws(() => assertEmail(bad), /invalid account parameter/);
  }
});

test('tile selectors are ordered most-specific first and contain the account', () => {
  const sel = accountTileSelectors('devpatel@deloitte.com');
  assert.equal(sel[0], '[data-test-id="devpatel@deloitte.com"]');
  assert.ok(sel.every((s) => s.includes('devpatel@deloitte.com')));
  assert.ok(sel.length >= 3);
});

test('page classification', () => {
  const target = 'https://develop.deloitte.com/ai-season-of-certifications#subpage/res';
  assert.equal(classifyPage({ url: 'https://develop.deloitte.com/ai-season-of-certifications#subpage/res' }, target), 'target');
  assert.equal(classifyPage({ url: 'https://login.microsoftonline.com/common/oauth2/authorize', title: 'Sign in to your account', text: 'Pick an account' }, target), 'account-picker');
  assert.equal(classifyPage({ url: 'https://login.microsoftonline.com/x', hasTile: true, text: '' }, target), 'account-picker');
  assert.equal(classifyPage({ url: 'https://login.microsoftonline.com/x', text: 'Enter password' }, target), 'credential');
  assert.equal(classifyPage({ url: 'https://login.microsoftonline.com/x', text: 'Approve sign in request' }, target), 'credential');
  assert.equal(classifyPage({ url: 'https://login.microsoftonline.com/x', text: 'loading' }, target), 'other');
  assert.equal(classifyPage({ url: 'https://evil.example.com/' }, target), 'other');
  assert.equal(classifyPage({ url: 'about:blank' }, target), 'other');
});

test('look-alike hosts are not treated as the Microsoft login host', () => {
  const target = 'https://develop.deloitte.com/x';
  assert.equal(classifyPage({ url: 'https://login.microsoftonline.com.evil.example/', text: 'Pick an account' }, target), 'other');
  assert.equal(classifyPage({ url: 'https://evil-login.microsoftonline.com.example.org/', hasTile: true }, target), 'other');
});

test('demo picker host override is opt-in', () => {
  const target = 'http://127.0.0.1:8099/portal.html';
  assert.equal(classifyPage({ url: 'http://localhost:8099/login.html', hasTile: true }, target), 'other');
  assert.equal(classifyPage({ url: 'http://localhost:8099/login.html', hasTile: true }, target, { pickerHost: 'localhost:8099' }), 'account-picker');
});
