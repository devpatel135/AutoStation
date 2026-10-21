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
 *   signin          Microsoft asks for an e-mail address (this browser profile is not signed in yet: sign in once by hand)
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
    if (/email, phone, or skype|enter your email|no account\?/i.test(text)) return 'signin';
    return 'other';
  }
  return 'other';
}

// ---- host trust (pure) -------------------------------------------------------------------------------------
// The automation only ever opens a portal on an allow-listed https host, only ever clicks the account tile when the Microsoft
// sign-in was initiated FOR that portal (redirect_uri), and stops if the browser ends up anywhere else.
const DEFAULT_ALLOWED_HOSTS = Object.freeze(['develop.deloitte.com']);
const HOST_ENTRY = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::\d{1,5})?$/;
const IP_LITERAL = /^(?:\d+(?:\.\d+){0,3}|\[.*\]|.*:.*:.*)$/; // dotted/decimal IPv4 or any IPv6 literal (checked on the bare hostname)
const BAD_URL_CHARS = /[\x00-\x20\x7f-\x9f\\]/;

function bareHost(host) {
  return String(host || '').replace(/:\d+$/, '');
}

/**
 * Default allow-list plus the operator's `allowed_hosts` param (comma/space separated, exact hosts, optional :port).
 * Wildcards, URLs, trailing dots and (outside test mode) IP literals / single-label / localhost names are refused.
 */
function parseAllowedHosts(extra, { allowTestHosts = false } = {}) {
  const items = Array.isArray(extra) ? extra : String(extra === undefined || extra === null ? '' : extra).split(/[\s,;]+/);
  const out = new Set(DEFAULT_ALLOWED_HOSTS);
  for (const raw of items) {
    const h = String(raw).trim().toLowerCase();
    if (!h) continue;
    if (!HOST_ENTRY.test(h)) throw new Error(`invalid allowed_hosts entry ${JSON.stringify(String(raw).slice(0, 80))}: expected a plain host name (optionally :port)`);
    const bare = bareHost(h);
    if (!allowTestHosts && (IP_LITERAL.test(bare) || !bare.includes('.') || /(?:^|\.)localhost$/.test(bare))) {
      throw new Error(`invalid allowed_hosts entry ${JSON.stringify(h)}: IP addresses and local names are not allowed`);
    }
    out.add(h);
  }
  return [...out];
}

/** Exact, case-insensitive membership. The URL host includes a non-default port, so `host:8443` never matches `host`. */
function hostAllowed(host, allowedHosts = DEFAULT_ALLOWED_HOSTS) {
  const h = String(host || '').toLowerCase();
  if (!h || h.endsWith('.') || bareHost(h).endsWith('.')) return false;
  return (allowedHosts || []).some((a) => String(a).toLowerCase() === h);
}

function isMicrosoftLoginHost(host, { pickerHost } = {}) {
  return MICROSOFT_LOGIN_HOSTS.test(String(host || '')) || Boolean(pickerHost && String(host) === pickerHost); // pickerHost: demo/testing only
}

/** Is `host` somewhere the browser may legitimately be during this flow (portal / allow-listed host / Microsoft sign-in)? */
function isTrustedHost(host, { allowedHosts = DEFAULT_ALLOWED_HOSTS, pickerHost } = {}) {
  return hostAllowed(host, allowedHosts) || isMicrosoftLoginHost(host, { pickerHost });
}

/** Validate the portal URL before anything is opened. -> {ok, host, reason}. https only (http only with allowTestHosts). */
function isAllowedPortal(url, { allowedHosts = DEFAULT_ALLOWED_HOSTS, allowTestHosts = false } = {}) {
  const raw = String(url === undefined || url === null ? '' : url);
  const no = (reason) => ({ ok: false, host: '', reason });
  if (!raw || raw.length > 2048) return no('empty or oversized URL');
  if (BAD_URL_CHARS.test(raw)) return no('URL contains whitespace, control characters or a backslash');
  if (!/^[a-z][a-z0-9+.-]*:\/\/[^\/]/i.test(raw)) return no('URL must be scheme://host with no extra slashes');
  let u;
  try { u = new URL(raw); } catch (_) { return no('not a valid URL'); }
  if (u.protocol !== 'https:' && !(allowTestHosts && u.protocol === 'http:')) return no(`scheme ${u.protocol} is not allowed (https only)`);
  if (u.username || u.password) return no('URLs with embedded credentials are not allowed');
  if (u.hostname.endsWith('.')) return no('host has a trailing dot');
  if (!allowTestHosts && IP_LITERAL.test(u.hostname)) return no('IP-address hosts are not allowed');
  if (!hostAllowed(u.host, allowedHosts)) return no(`host ${u.host} is not in the allow-list`);
  return { ok: true, host: u.host, reason: '' };
}

/**
 * Before clicking the account tile: was this Microsoft sign-in started for OUR portal? Looks at redirect_uri / wreply (every
 * occurrence, query or fragment) on the Microsoft login URL. A redirect_uri whose host is neither the portal's nor allow-listed,
 * unparseable, credentialed or non-https (outside test mode) is refused; a client_id without any redirect_uri cannot be verified
 * and is refused too. A login URL carrying neither (a later page of the same sign-in) is fine.
 */
function redirectAllowed(loginUrl, { portalHost, allowedHosts = DEFAULT_ALLOWED_HOSTS, allowTestHosts = false } = {}) {
  let u;
  try { u = new URL(String(loginUrl)); } catch (_) { return { ok: false, reason: 'sign-in URL is not parseable', redirectHost: '' }; }
  const entries = [...u.searchParams.entries()];
  if (u.hash && u.hash.includes('=')) entries.push(...new URLSearchParams(u.hash.replace(/^#[/?]?/, '')).entries());
  const redirects = entries.filter(([k]) => /^(?:redirect_uri|wreply)$/i.test(k)).map(([, v]) => v);
  const hasClient = entries.some(([k]) => /^client_id$/i.test(k));
  if (!redirects.length) return hasClient ? { ok: false, reason: 'client_id without a redirect_uri cannot be verified', redirectHost: '' } : { ok: true, reason: '', redirectHost: '' };
  const trusted = [...(allowedHosts || [])];
  if (portalHost) trusted.push(String(portalHost).toLowerCase());
  for (const r of redirects) {
    let ru;
    try { ru = new URL(r); } catch (_) { return { ok: false, reason: 'redirect_uri is not a valid absolute URL', redirectHost: '' }; }
    if (ru.protocol !== 'https:' && !(allowTestHosts && ru.protocol === 'http:')) return { ok: false, reason: `redirect_uri scheme ${ru.protocol} is not allowed`, redirectHost: ru.host };
    if (ru.username || ru.password) return { ok: false, reason: 'redirect_uri carries credentials', redirectHost: ru.host };
    if (!hostAllowed(ru.host, trusted)) return { ok: false, reason: `redirect_uri host ${ru.host} is neither the portal nor allow-listed`, redirectHost: ru.host };
  }
  return { ok: true, reason: '', redirectHost: '' };
}

/** After the click: the browser may only be on Microsoft sign-in or an allow-listed host (about:blank is neutral). */
function postClickOk(url, ctx = {}) {
  const u = String(url || '');
  if (u === 'about:blank') return true;
  try { return isTrustedHost(new URL(u).host, ctx); } catch (_) { return false; }
}

module.exports = {
  DEFAULT_ALLOWED_HOSTS, parseAllowedHosts, hostAllowed, isMicrosoftLoginHost, isTrustedHost, isAllowedPortal, redirectAllowed, postClickOk,
  DEFAULT_URL, DEFAULT_ACCOUNT, MICROSOFT_LOGIN_HOSTS, assertEmail, accountTileSelectors, classifyPage, hostOf };
