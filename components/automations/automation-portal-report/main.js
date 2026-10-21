'use strict';
/**
 * automation-portal-report: open the Deloitte "AI Season of Certifications" resources page, and when Microsoft
 * authentication appears, click the account tile for the configured e-mail address - then STOP.
 *
 * It never types a password or a code. Authentication is the signed-in user's: Edge persistent profile (SSO / cert),
 * the only action is choosing the existing account tile. If Microsoft asks for a password / MFA instead of showing the
 * picker, the automation stops and reports `manual_step_required` rather than attempting anything.
 *
 * Parameters (schedule slot params): portal_url, account (default devpatel@deloitte.com), headless (default true),
 * channel (default msedge), timeout_s (default 60), picker_host (local demo only). Honours --dry-run: navigates and classifies, never clicks.
 */
const path = require('node:path');
const fw = require(process.env.AUTOMAGIX_FRAMEWORK_DIR || path.resolve(__dirname, '..', '..', 'execution-framework'));
const { DEFAULT_URL, DEFAULT_ACCOUNT, assertEmail, accountTileSelectors, classifyPage } = require('./lib/login');

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
  const portalUrl = w.param('portal_url', DEFAULT_URL);
  const account = assertEmail(w.param('account', DEFAULT_ACCOUNT));
  const timeoutMs = Number(w.param('timeout_s', '60')) * 1000;
  const selectors = accountTileSelectors(account);
  const pickerHost = w.param('picker_host', undefined); // local demo only: treat this host as the Microsoft account picker
  w.logEvent('start', { target: new URL(portalUrl).origin, dry_run: w.isDryRun() }, { sensitiveKeys: [] });

  const session = await framework.browser.launch({
    headless: String(w.param('headless', 'true')) !== 'false',
    channel: w.param('channel', 'msedge'),
  });
  try {
    const { page } = session;
    await w.runWithRetry(() => page.goto(portalUrl, { waitUntil: 'domcontentloaded', timeout: 30000 }));

    const deadline = Date.now() + timeoutMs;
    let kind = 'other';
    let clicked = false;
    while (Date.now() < deadline) {
      const state = await pageState(page, selectors);
      kind = classifyPage(state, portalUrl, { pickerHost });
      w.logEvent('page_state', { kind, host: new URL(state.url).host });
      if (kind === 'target' || kind === 'credential') break;
      if (kind === 'account-picker') {
        if (w.isDryRun()) break;
        // pick the first selector that exists; the resilient page heals the choice via LLM Pattern 3 if Microsoft changed the markup
        let chosen = selectors[0];
        for (const sel of selectors) {
          try { if ((await page.locator(sel).count()) > 0) { chosen = sel; break; } } catch (_) { /* try next */ }
        }
        const rp = framework.resilience.resilientPage(page, { caller: 'automation-portal-report' });
        await rp.click(chosen);
        clicked = true;
        w.logEvent('account_tile_clicked', { account_domain: account.split('@')[1] });
        await page.waitForLoadState('domcontentloaded', { timeout: 10000 }).catch(() => {});
        break; // STOP: nothing after the click is this automation's business
      }
      await page.waitForTimeout(1000);
    }

    const finalHost = new URL(page.url()).host;
    const manual = kind === 'credential';
    if (manual) w.logEvent('manual_step_required', { reason: 'Microsoft is asking for credentials/MFA; not handled by design', host: finalHost });
    w.setResult({ state: clicked ? 'account_selected' : (manual ? 'manual_step_required' : kind), clicked, final_host: finalHost });
  } finally {
    await session.close();
  }
});
