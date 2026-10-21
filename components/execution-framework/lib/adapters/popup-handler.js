'use strict';
/**
 * popup-handler: dismiss known cookie/consent banners via a configurable selector list. Privacy-first: the defaults prefer
 * "reject / necessary only" over "accept all". NEVER clicks anything whose text/selector matches the deny-list
 * (delete / remove / pay / submit / confirm ...), whatever the config says - unless allowDestructive is set explicitly.
 */

const DEFAULT_BANNERS = [
  '#onetrust-reject-all-handler',
  '#onetrust-accept-btn-handler',
  '#CybotCookiebotDialogBodyButtonDecline',
  '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowallSelection',
  'button[data-testid="cookie-policy-manage-dialog-btn-reject"]',
  '.cc-deny',
  '.cc-dismiss',
  '[aria-label="Reject all"]',
  '[aria-label="Close cookie banner"]',
  '[id*="cookie" i] button[class*="reject" i]',
  '[id*="consent" i] button[class*="reject" i]',
];

const DEFAULT_DENY = /delete|remove|destroy|erase|purge|submit|confirm|pay\b|payment|transfer|approve|send|publish|checkout|buy|order/i;

/** Pure decision: may we click an element described by (selector, text)? */
function isSafeToClick(selector, text, { deny = DEFAULT_DENY, allowDestructive = false } = {}) {
  if (allowDestructive) return true;
  return !deny.test(String(selector || '')) && !deny.test(String(text || ''));
}

function describeInPage(el) {
  return [el.innerText, el.value, el.getAttribute('aria-label'), el.title].filter(Boolean).join(' ').slice(0, 200);
}

/**
 * dismissBanners(page, {selectors, deny, allowDestructive, timeoutMs, logEvent}) -> {dismissed:[selector], skipped:[{selector,reason}]}
 * Clicks at most ONE banner button per selector, only when it matches exactly one visible element. Never throws.
 */
async function dismissBanners(page, opts = {}) {
  const raw = page.raw || page;
  const selectors = opts.selectors || DEFAULT_BANNERS;
  const logEvent = opts.logEvent || (() => {});
  const out = { dismissed: [], skipped: [] };
  for (const sel of selectors) {
    try {
      const loc = raw.locator(sel);
      const n = await loc.count();
      if (n === 0) continue;
      if (n > 1) { out.skipped.push({ selector: sel, reason: `matches ${n} elements` }); continue; }
      const first = loc.first();
      if (!(await first.isVisible())) continue;
      let text = '';
      try { text = await first.evaluate(describeInPage); } catch (_) { text = ''; }
      if (!isSafeToClick(sel, text, opts)) { out.skipped.push({ selector: sel, reason: 'deny-listed target' }); continue; }
      await first.click({ timeout: opts.timeoutMs || 3000 });
      out.dismissed.push(sel);
      logEvent('popup_dismissed', { selector: sel });
    } catch (err) {
      out.skipped.push({ selector: sel, reason: String((err && err.message) || err).slice(0, 80) });
    }
  }
  return out;
}

/** Register a Playwright locator handler so banners that appear later are dismissed automatically (Playwright >= 1.42). */
async function installAutoDismiss(page, opts = {}) {
  const raw = page.raw || page;
  if (typeof raw.addLocatorHandler !== 'function') return { installed: 0, reason: 'addLocatorHandler unavailable' };
  let installed = 0;
  for (const sel of opts.selectors || DEFAULT_BANNERS) {
    if (!isSafeToClick(sel, '', opts)) continue;
    await raw.addLocatorHandler(raw.locator(sel), async (loc) => {
      let text = '';
      try { text = await loc.first().evaluate(describeInPage); } catch (_) { text = ''; }
      if (isSafeToClick(sel, text, opts)) await loc.first().click({ timeout: opts.timeoutMs || 3000 });
    });
    installed += 1;
  }
  return { installed };
}

module.exports = {
  name: 'popup-handler',
  description: 'Dismiss known cookie/consent banners (reject-first); never clicks deny-listed targets',
  DEFAULT_BANNERS, DEFAULT_DENY, isSafeToClick, dismissBanners, installAutoDismiss,
};
