'use strict';
/**
 * Pure logic for the "open the portal, pick the Microsoft account tile, stop" flow (no browser, no I/O).
 * Credentials are NEVER typed by this automation: the user's Edge profile (SSO) supplies authentication; the only
 * interaction is choosing the existing account tile on Microsoft's "Pick an account" page.
 */

const DEFAULT_URL = 'https://develop.deloitte.com/ai-season-of-certifications#subpage/res';
const DEFAULT_ACCOUNT = 'devpatel@deloitte.com';
const MICROSOFT_LOGIN_HOSTS = /(^|\.)login\.microsoftonline\.com$|(^|\.)login\.live\.com$|(^|\.)login\.microsoft\.com$/i;

/** Validate the account parameter: it is interpolated into selectors, so it must look like a plain e-mail address. */
function assertEmail(email) {
  if (!/^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(String(email || ''))) {
    throw new Error(`invalid account parameter ${JSON.stringify(email)}: expected an e-mail address`);
  }
  return String(email);
}

/** Ordered selector candidates for the account tile on Microsoft's account picker (most specific first). */
function accountTileSelectors(email) {
  const e = assertEmail(email);
  return [
    `[data-test-id="${e}"]`,
    `#tilesHolder [role="button"]:has-text("${e}")`,
    `#tilesHolder .tile:has-text("${e}")`,
    `[role="button"]:has-text("${e}")`,
  ];
}

function hostOf(url) {
  try { return new URL(url).host; } catch (_) { return ''; }
}

/**
 * Classify what the browser is showing.
 *   target          the portal itself (signed in / no auth challenge)
 *   account-picker  Microsoft "Pick an account" (tiles listed)
 *   credential      Microsoft is asking for a password / code / MFA - a human or SSO step this automation never performs
 *   other           anything else (still loading, error page, unrelated site)
 * state: {url, title, text, hasTile}
 */
function classifyPage(state, targetUrl = DEFAULT_URL, opts = {}) {
  const url = state.url || '';
  const host = hostOf(url);
  const isLoginHost = (h) => MICROSOFT_LOGIN_HOSTS.test(h) || (opts.pickerHost && h === opts.pickerHost); // pickerHost: local demo/testing only
  const text = `${state.title || ''}\n${state.text || ''}`;
  if (host && host === hostOf(targetUrl) && !isLoginHost(host)) return 'target';
  if (isLoginHost(host)) {
    if (state.hasTile || /pick an account/i.test(text)) return 'account-picker';
    if (/enter password|enter code|approve sign.?in|verify your identity|sign in with a security key|stay signed in/i.test(text)) return 'credential';
    return 'other';
  }
  return 'other';
}

module.exports = { DEFAULT_URL, DEFAULT_ACCOUNT, MICROSOFT_LOGIN_HOSTS, assertEmail, accountTileSelectors, classifyPage, hostOf };
