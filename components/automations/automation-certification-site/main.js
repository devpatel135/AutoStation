'use strict';
/**
 * automation-certification-site: open the Deloitte "AI Season of Certifications" resources page, and when Microsoft
 * authentication appears, click the account tile for the configured e-mail address - then STOP.
 *
 * It never types a password or a code. Authentication is the signed-in user's: Edge persistent profile (SSO / cert),
 * the only action is choosing the existing account tile. If Microsoft asks for a password / MFA instead of showing the
 * picker, the automation stops and reports `manual_step_required` rather than attempting anything.
 *
 * Parameters (schedule slot params): portal_url (https, host must be allow-listed), allowed_hosts (extra exact hosts, optional),
 * account (default devpatel@deloitte.com), channel (default msedge), timeout_s (default 60).
 * Test-only overrides - ignored unless the environment variable AMX_ALLOW_TEST_HOSTS=1 is set: picker_host, headless, http:// and
 * loopback hosts in portal_url / allowed_hosts. Honours --dry-run: navigates and classifies, never clicks.
 *
 * Trust rules (lib/login.js, all pure + tested): the portal must be https on an allow-listed host (default develop.deloitte.com);
 * the tile is clicked only when the Microsoft sign-in's redirect_uri points back at the portal / an allow-listed host (otherwise the
 * run stops with state `untrusted_redirect` and never clicks); after the click the browser must still be on Microsoft sign-in or an
 * allow-listed host (otherwise `untrusted_redirect` is reported); selector healing is only allowed while on a Microsoft sign-in host.
 */
const path = require('node:path');
const fw = require(process.env.AUTOMAGIX_FRAMEWORK_DIR || path.resolve(__dirname, '..', '..', 'execution-framework'));
const native = require('./lib/native');
const {
  DEFAULT_URL, DEFAULT_ACCOUNT, assertEmail, accountTileSelectors, classifyPage, hostOf,
  parseAllowedHosts, isAllowedPortal, isMicrosoftLoginHost, redirectAllowed, postClickOk,
} = require('./lib/login');

async function pageState(page, selectors) {
  let hasTile = false;
  for (const sel of selectors) {
    try {
      if ((await page.locator(sel).count()) > 0) { hasTile = true; break; }
    } catch (_) { /* selector unsupported on this engine: ignore */ }
  }
  let text = '';
  try { text = (await page.locator('body').innerText({ timeout: 2000 })).slice(0, 1500); } catch (_) { text = ''; }
  return { url: page.url(), title: await page.title().catch(() => ''), text, hasTile };
}

fw.wrapper.entrypoint(async (framework) => {
  const w = framework.wrapper;
  const allowTestHosts = process.env.AMX_ALLOW_TEST_HOSTS === '1'; // CI / local demo only; never set on a user's machine
  const allowedHosts = parseAllowedHosts(w.param('allowed_hosts', ''), { allowTestHosts });
  const portalUrl = String(w.param('portal_url', DEFAULT_URL));
  const portalCheck = isAllowedPortal(portalUrl, { allowedHosts, allowTestHosts });
  if (!portalCheck.ok) throw new Error(`portal_url refused: ${portalCheck.reason} (allowed hosts: ${allowedHosts.join(', ')})`);
  const portalHost = portalCheck.host;
  const account = assertEmail(w.param('account', DEFAULT_ACCOUNT));
  const timeoutParam = Number(w.param('timeout_s', '60'));
  const timeoutMs = (Number.isFinite(timeoutParam) ? Math.min(Math.max(timeoutParam, 5), 600) : 60) * 1000;
  const selectors = accountTileSelectors(account);
  // test-only overrides are honoured only in test mode; in production they are ignored (and the attempt is logged)
  const pickerParam = w.param('picker_host', undefined);
  const headlessParam = w.param('headless', undefined);
  if ((pickerParam !== undefined || headlessParam !== undefined) && !allowTestHosts) {
    w.logEvent('test_override_ignored', { picker_host: pickerParam !== undefined, headless: headlessParam !== undefined });
  }
  const pickerHost = allowTestHosts && /^[a-z0-9.-]+(?::\d{1,5})?$/i.test(String(pickerParam || '')) ? String(pickerParam).toLowerCase() : undefined; // treat this host as the Microsoft account picker
  const trustCtx = { allowedHosts, pickerHost };
  w.logEvent('start', { target: new URL(portalUrl).origin, dry_run: w.isDryRun() }, { sensitiveKeys: [] });

  // mode: auto (default) = Playwright, or native UI Automation when managed Edge forbids DevTools (policy RemoteDebuggingAllowed=0)
  const mode = String(w.param('mode', 'auto'));
  const runNativeMode = async (why) => {
    w.logEvent('native_mode', { reason: why });
    const r = native.runNative({ url: portalUrl, account, timeoutS: Math.round(timeoutMs / 1000), dryRun: w.isDryRun() });
    if (r.state === 'error') throw new Error(`native mode: ${r.detail || 'unknown error'}`);
    if (r.clicked) w.logEvent('account_tile_clicked', { account_domain: account.split('@')[1] });
    if (r.state === 'manual_step_required') w.logEvent('manual_step_required', { reason: 'Microsoft sign-in needs a person (password/MFA); not handled by design', host: 'login.microsoftonline.com' });
    w.setResult({ state: r.state, clicked: !!r.clicked, final_host: '', mode: 'native-uia' });
  };
  if (mode === 'native' || (mode === 'auto' && native.devtoolsBlockedByPolicy())) {
    await runNativeMode(mode === 'native' ? 'requested' : 'Edge policy RemoteDebuggingAllowed=0');
    return;
  }
  let session;
  try {
    session = await framework.browser.launch({
      headless: allowTestHosts && String(headlessParam) === 'true', // headed always in production: the first sign-in is interactive
      channel: w.param('channel', 'msedge'),
    });
  } catch (err) {
    if (mode === 'auto' && native.isDevtoolsPolicyError(err)) { await runNativeMode('DevTools disallowed by the system admin'); return; }
    throw err;
  }
  try {
    // session.guard captures failure evidence (screenshot + redacted DOM snapshot) on any error, then rethrows it unchanged
    await session.guard(async () => {
    const { page } = session;
    await w.runWithRetry(() => page.goto(portalUrl, { waitUntil: 'domcontentloaded', timeout: 30000 }));

    const deadline = Date.now() + timeoutMs;
    let kind = 'other';
    let clicked = false;
    let untrusted = '';
    while (Date.now() < deadline) {
      const state = await pageState(page, selectors);
      kind = classifyPage(state, portalUrl, { pickerHost });
      w.logEvent('page_state', { kind, host: new URL(state.url).host });
      if (kind === 'target' || kind === 'credential' || kind === 'signin') break;
      if (kind === 'account-picker') {
        // was this sign-in started for OUR portal? (redirect_uri host must be the portal or allow-listed) - otherwise never click
        const rd = redirectAllowed(state.url, { portalHost, allowedHosts, allowTestHosts });
        if (!rd.ok) {
          kind = 'untrusted_redirect';
          untrusted = rd.reason;
          w.logEvent('untrusted_redirect', { reason: rd.reason, redirect_host: rd.redirectHost, portal_host: portalHost, stage: 'before_click' });
          break;
        }
        if (w.isDryRun()) break;
        // pick the first selector that exists; the resilient page heals the choice via LLM Pattern 3 if Microsoft changed the markup
        let chosen = selectors[0];
        for (const sel of selectors) {
          try { if ((await page.locator(sel).count()) > 0) { chosen = sel; break; } } catch (_) { /* try next */ }
        }
        // healing (LLM Pattern 3) is only allowed while the browser is on a Microsoft sign-in host
        const rp = framework.resilience.resilientPage(page, {
          caller: 'automation-certification-site',
          canHeal: (pg) => isMicrosoftLoginHost(hostOf(pg.url()), { pickerHost }),
        });
        await rp.click(chosen);
        clicked = true;
        w.logEvent('account_tile_clicked', { account_domain: account.split('@')[1] });
        await page.waitForLoadState('domcontentloaded', { timeout: 10000 }).catch(() => {});
        if (!postClickOk(page.url(), trustCtx)) {
          kind = 'untrusted_redirect';
          untrusted = `after the click the browser left the trusted hosts (now on ${hostOf(page.url()) || 'an unknown host'})`;
          w.logEvent('untrusted_redirect', { reason: untrusted, final_host: hostOf(page.url()), stage: 'after_click' });
        }
        break; // STOP: nothing after the click is this automation's business
      }
      await page.waitForTimeout(1000);
    }

    const finalHost = new URL(page.url()).host;
    const manual = kind === 'credential' || kind === 'signin' || kind === 'untrusted_redirect';
    if (manual) {
      const reason = kind === 'untrusted_redirect' ? untrusted
        : (kind === 'signin' ? 'this browser profile is not signed in yet: run once and sign in by hand; later runs reuse the profile' : 'Microsoft is asking for credentials/MFA; not handled by design');
      w.logEvent('manual_step_required', { reason, host: finalHost });
    }
    const state = kind === 'untrusted_redirect' ? 'untrusted_redirect' : (clicked ? 'account_selected' : (manual ? 'manual_step_required' : kind));
    w.setResult({ state, clicked, final_host: finalHost, ...(manual ? { manual_step_required: true } : {}) });
    });
  } finally {
    await session.close();
  }
});
