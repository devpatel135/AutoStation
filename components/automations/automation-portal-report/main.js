'use strict';
/**
 * automation-portal-report: scrape the quarterly compliance table from an internal portal and export the report.
 *
 * Browser flow on execution-framework: the user's Edge profile (SSO/cert auth inherited - no credentials in the
 * automation), generic-table adapter, sso-detect (session lapse -> AuthExpired -> platform refreshes/escalates),
 * LLM Pattern 3 selector self-healing (fw.resilience) for the export control, LLM Pattern 2 narrative with a
 * deterministic fallback, per-row item telemetry.
 *
 * Parameters (schedule slot params): portal_url, headless (default true), channel (default msedge), export (default true).
 * Honours --dry-run: scrapes and validates but never clicks the export control.
 */
const path = require('node:path');
const fw = require(process.env.AUTOMAGIX_FRAMEWORK_DIR || path.resolve(__dirname, '..', '..', 'execution-framework'));
const { parseRow, summarize, narrativeFallback, NARRATIVE_SCHEMA } = require('./lib/report');

fw.wrapper.entrypoint(async (framework) => {
  const w = framework.wrapper;
  const portalUrl = w.param('portal_url', 'http://127.0.0.1:8099/v1.html');
  const doExport = String(w.param('export', 'true')) !== 'false';
  w.logEvent('start', { portal: new URL(portalUrl).origin, dry_run: w.isDryRun() });

  const session = await framework.browser.launch({
    headless: String(w.param('headless', 'true')) !== 'false',
    channel: w.param('channel', 'msedge'),
  });
  try {
    const { page } = session;
    await w.runWithRetry(() => page.goto(portalUrl, { waitUntil: 'domcontentloaded', timeout: 30000 }));
    await framework.adapters.get('sso-detect').assertSession(page, { expectedOrigin: new URL(portalUrl).origin });

    const { rows: rawRows } = await framework.adapters.get('generic-table').scrapeTable(page, { selector: '#report', nextSelector: '#next' });
    w.logEvent('scraped', { rows: rawRows.length });

    const rows = [];
    for (const raw of rawRows) {
      // DataError rows are skipped and flagged; everything else retries/escalates through the platform
      const parsed = await w.runItem(raw.id || '(blank id)', async () => parseRow(raw));
      if (parsed) rows.push(parsed);
    }

    const summary = summarize(rows);
    // Pattern 2: LLM narrative over the (non-sensitive) summary, deterministic fallback
    const out = await w.llmCall({
      prompt: 'Write a two-sentence compliance status narrative and list control ids that need escalation.',
      context: JSON.stringify(summary), schema: NARRATIVE_SCHEMA, caller: 'automation-portal-report/narrative', template: 'report-narrative@1',
    });
    const narrative = out ? out.narrative : narrativeFallback(summary);
    w.logEvent('summary', { ...summary, narrative_source: out ? 'llm' : 'deterministic' });

    let exported = false;
    if (doExport && !w.isDryRun()) {
      // Pattern 3: if the portal renames the button, the resilient page heals the selector once (and caches it)
      const rp = framework.resilience.resilientPage(page, { caller: 'automation-portal-report' });
      await rp.click('#export-btn');
      const status = await rp.textContent('#status');
      exported = /exported/i.test(status || '');
      if (!exported) throw new framework.wrapper.errors.SystemUnavailable(`export did not confirm (status: ${JSON.stringify(status)})`);
      w.logEvent('exported', { status });
    }
    w.setResult({ rows: rows.length, skipped: rawRows.length - rows.length, overdue: summary.overdue, narrative, exported });
  } finally {
    await session.close();
  }
});
