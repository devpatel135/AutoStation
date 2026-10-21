'use strict';
/**
 * Error taxonomy + classifier. Mirrors wrapper_core/retry.py (SPEC section 10):
 *   - explicit AutomationError subclasses steer the handler
 *   - the FINAL exception type name in a stack/stderr tail wins over keyword rules
 *   - keyword rules are ordered AUTH -> SYSTEM_UNAVAILABLE -> NETWORK -> LOGIC -> DATA
 * JS-specific additions (kept out of the parity table): Node error codes, Playwright selector timeouts,
 * net::ERR_* browser errors.
 */

const Category = Object.freeze({
  NETWORK_ERROR: 'NETWORK_ERROR',
  AUTH_EXPIRED: 'AUTH_EXPIRED',
  SYSTEM_UNAVAILABLE: 'SYSTEM_UNAVAILABLE',
  DATA_ERROR: 'DATA_ERROR',
  LOGIC_ERROR: 'LOGIC_ERROR',
  UNKNOWN: 'UNKNOWN',
});

class AutomationError extends Error {
  constructor(message) {
    super(message);
    this.name = this.constructor.name;
  }
}
AutomationError.prototype.category = Category.UNKNOWN;

class NetworkError extends AutomationError {}
NetworkError.prototype.category = Category.NETWORK_ERROR;
class AuthExpired extends AutomationError {}
AuthExpired.prototype.category = Category.AUTH_EXPIRED;
class SystemUnavailable extends AutomationError {}
SystemUnavailable.prototype.category = Category.SYSTEM_UNAVAILABLE;
class DataError extends AutomationError {}
DataError.prototype.category = Category.DATA_ERROR;
class LogicError extends AutomationError {}
LogicError.prototype.category = Category.LOGIC_ERROR;

/** Raised when the handler gave up. Never retried again. */
class Escalation extends Error {
  constructor(message, category = Category.UNKNOWN, attempts = 0, cause = null) {
    super(message);
    this.name = 'Escalation';
    this.category = category;
    this.attempts = attempts;
    this.cause = cause;
  }
}

// Ordered exactly like _TEXT_RULES in retry.py. Each rule: [regex (case-insensitive), category, optional case-sensitive regex].
const TEXT_RULES = [
  [/\b(401|403)\b|unauthori[sz]ed|forbidden|token (has )?expired|session (has )?expired|certificate (has )?expired|login required|authentication failed/i, Category.AUTH_EXPIRED],
  [/\b(502|503|504)\b|service unavailable|bad gateway|gateway time-?out|under maintenance|maintenance page|temporarily unavailable|server is busy/i, Category.SYSTEM_UNAVAILABLE],
  [
    /connection (reset|refused|aborted|error)|timed? ?out|name or service not known|getaddrinfo failed|network is unreachable|no route to host|vpn|proxy error|ERR_INTERNET_DISCONNECTED|ERR_NAME_NOT_RESOLVED/i,
    Category.NETWORK_ERROR,
    /\bE(?:CONNRESET|TIMEDOUT|NOTFOUND)\b/,
  ],
  [/KeyError|AttributeError|TypeError|NameError|ImportError|ModuleNotFoundError|IndexError|AssertionError|SyntaxError|UnboundLocalError|ZeroDivisionError|NotImplementedError/i, Category.LOGIC_ERROR],
  [/missing required field|invalid (format|value|date)|could not parse|JSONDecodeError|UnicodeDecodeError|ValueError|validation error|malformed|unexpected format/i, Category.DATA_ERROR],
];

const LAST_EXC = /^\s*([A-Za-z_][\w.]*(?:Error|Exception|Exit|Warning)):/gm;
const NET_NAMES = new Set(['ConnectionError', 'ConnectionResetError', 'ConnectionRefusedError', 'ConnectionAbortedError', 'TimeoutError',
  'timeout', 'gaierror', 'URLError', 'ReadTimeout', 'ConnectTimeout', 'ProxyError', 'ConnectError']);
const LOGIC_NAMES = new Set(['KeyError', 'AttributeError', 'TypeError', 'NameError', 'ImportError', 'ModuleNotFoundError', 'IndexError',
  'AssertionError', 'SyntaxError', 'UnboundLocalError', 'ZeroDivisionError', 'NotImplementedError']);
const DATA_NAMES = new Set(['ValueError', 'UnicodeDecodeError', 'UnicodeEncodeError', 'JSONDecodeError', 'DataError']);
// JS-only extras for the final-name rule
const JS_LOGIC_NAMES = new Set(['ReferenceError', 'RangeError', 'EvalError']);

/** Classify free text (stderr tail, message). The final exception type name wins over keywords. */
function classifyText(text) {
  const t = text || '';
  const names = [];
  let m;
  LAST_EXC.lastIndex = 0;
  while ((m = LAST_EXC.exec(t)) !== null) names.push(m[1]);
  if (names.length) {
    const parts = names[names.length - 1].split('.');
    const last = parts[parts.length - 1];
    if (LOGIC_NAMES.has(last) || JS_LOGIC_NAMES.has(last)) return Category.LOGIC_ERROR;
    if (NET_NAMES.has(last)) return Category.NETWORK_ERROR;
    if (DATA_NAMES.has(last)) return Category.DATA_ERROR;
  }
  for (const [rx, cat, rxCs] of TEXT_RULES) {
    if (rx.test(t) || (rxCs && rxCs.test(t))) return cat;
  }
  if (/net::ERR_[A-Z_]+/.test(t)) return Category.NETWORK_ERROR; // JS-only: Chromium network errors
  return Category.UNKNOWN;
}

const NODE_NET_CODES = new Set(['ECONNRESET', 'ECONNREFUSED', 'ECONNABORTED', 'ETIMEDOUT', 'ENOTFOUND', 'EAI_AGAIN', 'EPIPE',
  'ENETUNREACH', 'EHOSTUNREACH', 'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_SOCKET', 'UND_ERR_HEADERS_TIMEOUT']);

/** Map an HTTP status to a category (parity with the HTTPError branch in retry.py, plus 429/5xx). */
function classifyHttpStatus(status) {
  if (status === 401 || status === 403) return Category.AUTH_EXPIRED;
  if (status === 502 || status === 503 || status === 504 || status === 429) return Category.SYSTEM_UNAVAILABLE;
  if (status === 400 || status === 404 || status === 409 || status === 422) return Category.DATA_ERROR;
  if (status >= 500) return Category.SYSTEM_UNAVAILABLE;
  return Category.UNKNOWN;
}

function classifyException(err) {
  if (err && err.category && Object.values(Category).includes(err.category)) return err.category;
  const name = (err && err.name) || '';
  const msg = String((err && err.message) || err || '');
  if (err && typeof err.status === 'number' && err.isHttp) {
    const c = classifyHttpStatus(err.status);
    if (c !== Category.UNKNOWN) return c;
  }
  if (err && err.code && NODE_NET_CODES.has(err.code)) return Category.NETWORK_ERROR;
  // undici/fetch wraps socket failures as TypeError('fetch failed') with the real code on .cause
  if (err && err.cause && err.cause.code && NODE_NET_CODES.has(err.cause.code)) return Category.NETWORK_ERROR;
  // Playwright: a selector that never appears is a script/page-contract problem, not a transient fault.
  if (name === 'TimeoutError' && /waiting for (locator|selector|getBy)|locator\.|page\.waitForSelector/i.test(msg)) return Category.LOGIC_ERROR;
  if (name === 'SyntaxError' && /JSON/i.test(msg)) return Category.DATA_ERROR;
  if (['TypeError', 'ReferenceError', 'RangeError', 'SyntaxError', 'EvalError'].includes(name)) return Category.LOGIC_ERROR;
  if (name === 'TimeoutError' || name === 'AbortError') return Category.NETWORK_ERROR;
  return classifyText(`${name}: ${msg}`);
}

module.exports = {
  Category, AutomationError, NetworkError, AuthExpired, SystemUnavailable, DataError, LogicError, Escalation,
  classifyText, classifyException, classifyHttpStatus, TEXT_RULES,
};
