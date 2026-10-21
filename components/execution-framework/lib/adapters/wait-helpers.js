'use strict';
/**
 * wait-helpers: the waits UiPath developers reach for ("Wait element vanish", "Check app state", retry scope) as small, pure,
 * injectable functions - no fixed sleeps.
 */

const realSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function timeoutError(message) {
  const e = new Error(message);
  e.name = 'TimeoutError';
  return e;
}

/**
 * retryUntil(fn, {timeoutMs, intervalMs, backoff, maxIntervalMs, until, sleep, now}) - call fn until it returns a truthy value
 * (or until `until(value)` is true), sleeping `intervalMs` (growing by `backoff`, capped at `maxIntervalMs`) between attempts.
 * Errors thrown by fn count as "not yet" until the deadline, then the LAST error is rethrown.
 */
async function retryUntil(fn, opts = {}) {
  const {
    timeoutMs = 10000, intervalMs = 250, backoff = 1.5, maxIntervalMs = 2000, until = (v) => Boolean(v), sleep = realSleep, now = Date.now,
  } = opts;
  const deadline = now() + timeoutMs;
  let wait = intervalMs;
  let lastErr = null;
  for (let attempt = 1; ; attempt += 1) {
    try {
      const v = await fn(attempt);
      if (until(v)) return v;
      lastErr = null;
    } catch (err) {
      lastErr = err;
    }
    if (now() + wait > deadline) break;
    await sleep(wait);
    wait = Math.min(maxIntervalMs, Math.round(wait * backoff));
  }
  if (lastErr) throw lastErr;
  throw timeoutError(`retryUntil: condition not met within ${timeoutMs} ms`);
}

/** Wait for network idle without throwing: resolves true/false. `idleMs` is Playwright's own idle window (500 ms). */
async function waitForNetworkIdle(page, { timeoutMs = 10000 } = {}) {
  try {
    await page.waitForLoadState('networkidle', { timeout: timeoutMs });
    return true;
  } catch (_) {
    return false;
  }
}

/** Default signature of an element: text + bounding box (changes while it animates / re-renders). */
function signatureInPage(sel) {
  var el = document.querySelector(sel);
  if (!el) return null;
  var r = el.getBoundingClientRect();
  return [String(el.innerText || el.value || '').slice(0, 500), Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)].join('|');
}

/**
 * waitForStable(page|frame, selector, {stableMs, timeoutMs, pollMs, getSignature, sleep, now}) - resolves once the element exists and
 * its signature has not changed for `stableMs`; rejects with TimeoutError otherwise.
 */
async function waitForStable(target, selector, opts = {}) {
  const { stableMs = 500, timeoutMs = 10000, pollMs = 100, sleep = realSleep, now = Date.now } = opts;
  const getSignature = opts.getSignature || (() => target.evaluate(signatureInPage, selector));
  const deadline = now() + timeoutMs;
  let last;
  let since = null;
  for (;;) {
    let sig = null;
    try { sig = await getSignature(); } catch (_) { sig = null; }
    const t = now();
    if (sig !== null && sig === last) {
      if (since !== null && t - since >= stableMs) return sig;
    } else {
      last = sig;
      since = sig === null ? null : t;
    }
    if (t >= deadline) throw timeoutError(`waitForStable: ${selector} did not settle within ${timeoutMs} ms`);
    await sleep(pollMs);
  }
}

module.exports = {
  name: 'wait-helpers',
  description: 'retryUntil (backoff), waitForNetworkIdle, waitForStable - no fixed sleeps',
  retryUntil, waitForNetworkIdle, waitForStable, signatureInPage,
};
