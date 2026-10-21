'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const E = require('../lib/errors');

const { Category: C } = E;

// Parity table with wrapper_core/retry.py classify_text (rules, order, and the "final exception type wins" rule).
// Each row is the expectation the PYTHON classifier produces; JS must agree.
const TEXT_PARITY = [
  ['HTTP 503 Service Unavailable', C.SYSTEM_UNAVAILABLE],
  ['502 Bad Gateway', C.SYSTEM_UNAVAILABLE],
  ['Gateway Time-out', C.SYSTEM_UNAVAILABLE],
  ['the site is under maintenance', C.SYSTEM_UNAVAILABLE],
  ['401 Unauthorized', C.AUTH_EXPIRED],
  ['Your session has expired', C.AUTH_EXPIRED],
  ['certificate expired', C.AUTH_EXPIRED],
  ['getaddrinfo failed', C.NETWORK_ERROR],
  ['Connection reset by peer', C.NETWORK_ERROR],
  ['VPN disconnected', C.NETWORK_ERROR],
  ['ENOTFOUND intranet.example.com', C.NETWORK_ERROR],
  ['ECONNRESET', C.NETWORK_ERROR],
  ['read timed out', C.NETWORK_ERROR],
  ['KeyError: vendor', C.LOGIC_ERROR],
  ['AttributeError: nope', C.LOGIC_ERROR],
  ['missing required field: vendor_id', C.DATA_ERROR],
  ['could not parse date', C.DATA_ERROR],
  ['validation error for Invoice', C.DATA_ERROR],
  ['quantum fluctuation', C.UNKNOWN],
  ['', C.UNKNOWN],
  // order: AUTH before SYSTEM_UNAVAILABLE before NETWORK
  ['503 while the connection timed out', C.SYSTEM_UNAVAILABLE],
  ['403 forbidden after a timeout', C.AUTH_EXPIRED],
  // final exception type name wins over keywords (the Python bug fixed in 0.1.0: "ModuleNotFoundError" contains "eNotFound")
  ["Traceback (most recent call last):\n  File \"main.py\", line 2, in <module>\n    import wrapper_core\nModuleNotFoundError: No module named 'wrapper_core'", C.LOGIC_ERROR],
  ['ConnectionResetError: [WinError 10054] reset', C.NETWORK_ERROR],
  ['ValueError: HTTP 503 in a field', C.DATA_ERROR],
  ['Something timed out\nKeyError: x', C.LOGIC_ERROR],
  ['KeyError: x\nthen later: connection refused (no exception type)', C.LOGIC_ERROR],
  ['requests.exceptions.ConnectTimeout: host unreachable', C.NETWORK_ERROR],
  ['json.decoder.JSONDecodeError: Expecting value', C.DATA_ERROR],
];

for (const [text, expected] of TEXT_PARITY) {
  test(`classifyText parity: ${JSON.stringify(text.slice(0, 50))} -> ${expected}`, () => {
    assert.equal(E.classifyText(text), expected);
  });
}

test('keyword rules are case-insensitive but ENOTFOUND-style codes are case-sensitive', () => {
  assert.equal(E.classifyText('SERVICE UNAVAILABLE'), C.SYSTEM_UNAVAILABLE);
  assert.equal(E.classifyText('ModuleNotFoundError'), C.LOGIC_ERROR);
  assert.equal(E.classifyText('the eNotFound thing'), C.UNKNOWN);
});

test('explicit AutomationError subclasses steer the classifier', () => {
  assert.equal(E.classifyException(new E.NetworkError('x')), C.NETWORK_ERROR);
  assert.equal(E.classifyException(new E.AuthExpired('x')), C.AUTH_EXPIRED);
  assert.equal(E.classifyException(new E.SystemUnavailable('x')), C.SYSTEM_UNAVAILABLE);
  assert.equal(E.classifyException(new E.DataError('x')), C.DATA_ERROR);
  assert.equal(E.classifyException(new E.LogicError('x')), C.LOGIC_ERROR);
  assert.equal(E.classifyException(new E.AutomationError('x')), C.UNKNOWN);
  assert.equal(new E.DataError('x').name, 'DataError');
});

test('classifyException: HTTP statuses (parity with the HTTPError branch)', () => {
  const http = (status) => Object.assign(new Error(`HTTP ${status}`), { status, isHttp: true });
  assert.equal(E.classifyException(http(401)), C.AUTH_EXPIRED);
  assert.equal(E.classifyException(http(403)), C.AUTH_EXPIRED);
  assert.equal(E.classifyException(http(503)), C.SYSTEM_UNAVAILABLE);
  assert.equal(E.classifyException(http(429)), C.SYSTEM_UNAVAILABLE);
  assert.equal(E.classifyException(http(404)), C.DATA_ERROR);
  assert.equal(E.classifyException(http(422)), C.DATA_ERROR);
  assert.equal(E.classifyHttpStatus(500), C.SYSTEM_UNAVAILABLE);
  assert.equal(E.classifyHttpStatus(302), C.UNKNOWN);
});

test('classifyException: Node/JS specifics', () => {
  assert.equal(E.classifyException(Object.assign(new Error('read ECONNRESET'), { code: 'ECONNRESET' })), C.NETWORK_ERROR);
  assert.equal(E.classifyException(Object.assign(new Error('connect ETIMEDOUT'), { code: 'ETIMEDOUT' })), C.NETWORK_ERROR);
  const fetchFailed = Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED' } });
  assert.equal(E.classifyException(fetchFailed), C.NETWORK_ERROR); // must not be mistaken for a TypeError bug
  assert.equal(E.classifyException(new TypeError('x is not a function')), C.LOGIC_ERROR);
  assert.equal(E.classifyException(new ReferenceError('y is not defined')), C.LOGIC_ERROR);
  assert.equal(E.classifyException(new SyntaxError('Unexpected token < in JSON at position 0')), C.DATA_ERROR);
  assert.equal(E.classifyException(new SyntaxError('Unexpected identifier')), C.LOGIC_ERROR);
  assert.equal(E.classifyException(Object.assign(new Error('net::ERR_INTERNET_DISCONNECTED at https://x'), {})), C.NETWORK_ERROR);
  assert.equal(E.classifyException(Object.assign(new Error('page.goto: net::ERR_CONNECTION_REFUSED'), {})), C.NETWORK_ERROR);
  const pwSelector = Object.assign(new Error('locator.click: Timeout 10000ms exceeded.\nCall log:\n  - waiting for locator(\'#x\')'), { name: 'TimeoutError' });
  assert.equal(E.classifyException(pwSelector), C.LOGIC_ERROR); // selector problem, not a transient fault
  const pwNav = Object.assign(new Error('page.goto: Timeout 30000ms exceeded.'), { name: 'TimeoutError' });
  assert.equal(E.classifyException(pwNav), C.NETWORK_ERROR);
  assert.equal(E.classifyException(new Error('weird')), C.UNKNOWN);
});

test('Escalation carries category, attempts and cause', () => {
  const cause = new Error('x');
  const e = new E.Escalation('NETWORK_ERROR: x', C.NETWORK_ERROR, 3, cause);
  assert.equal(e.name, 'Escalation');
  assert.equal(e.category, C.NETWORK_ERROR);
  assert.equal(e.attempts, 3);
  assert.equal(e.cause, cause);
  assert.ok(e instanceof Error);
});
