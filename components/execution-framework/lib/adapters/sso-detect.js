'use strict';
/**
 * sso-detect: notice that the user's SSO session/cert auth has lapsed (redirected to an IdP, login form, "session expired")
 * and surface it as AuthExpired so the retry engine can refresh once and then escalate.
 */
const { AuthExpired } = require('../errors');

const DEFAULT_CFG = {
  loginUrlPatterns: [/\/login\b/i, /\/signin\b/i, /\/sso\b/i, /login\.microsoftonline\.com/i, /\/adfs\//i, /\/oauth2?\/authorize/i, /\/saml/i],
  expiredTextPatterns: [/session (has )?(expired|timed out)/i, /please (sign|log) ?in/i, /your session is no longer valid/i, /re-?authenticate/i],
  loginSelectors: ['input[type=password]', 'form[action*=login i]'],
};

/** Pure: {url, title, text, hasPasswordField} -> {loggedOut, reason}. */
function isLoginState(state, cfg = {}) {
  const c = { ...DEFAULT_CFG, ...cfg };
  if (c.expectedOrigin) {
    try {
      if (new URL(state.url).origin !== c.expectedOrigin) return { loggedOut: true, reason: `redirected away from ${c.expectedOrigin}` };
    } catch (_) { /* fall through to pattern checks */ }
  }
  if (c.loginUrlPatterns.some((rx) => rx.test(state.url || ''))) return { loggedOut: true, reason: 'URL looks like a login/SSO page' };
  if (state.hasPasswordField) return { loggedOut: true, reason: 'password field present' };
  const hay = `${state.title || ''}\n${state.text || ''}`;
  if (c.expiredTextPatterns.some((rx) => rx.test(hay))) return { loggedOut: true, reason: 'page says the session expired' };
  return { loggedOut: false, reason: '' };
}

async function readState(page, cfg = {}) {
  const raw = page.raw || page;
  const selectors = (cfg.loginSelectors || DEFAULT_CFG.loginSelectors);
  let hasPasswordField = false;
  for (const s of selectors) {
    try {
      if ((await raw.locator(s).count()) > 0) { hasPasswordField = true; break; }
    } catch (_) { /* invalid selector for this engine: ignore */ }
  }
  let text = '';
  try { text = (await raw.locator('body').innerText({ timeout: 2000 })).slice(0, 2000); } catch (_) { text = ''; }
  return { url: raw.url(), title: await raw.title().catch(() => ''), text, hasPasswordField };
}

/** Throws AuthExpired if the page is a login/expired-session state. */
async function assertSession(page, cfg = {}) {
  const state = await readState(page, cfg);
  const r = isLoginState(state, cfg);
  if (r.loggedOut) {
    const err = new AuthExpired(`session not valid: ${r.reason}`);
    // failure evidence from the live page (cfg.captureEvidence is injectable; never throws)
    await (cfg.captureEvidence || require('../evidence').captureSafe)(err, { reason: 'session lapse', page: page.raw || page });
    throw err;
  }
  return true;
}

module.exports = { name: 'sso-detect', description: 'Detect expired SSO/login state and throw AuthExpired', isLoginState, assertSession, readState, DEFAULT_CFG };
