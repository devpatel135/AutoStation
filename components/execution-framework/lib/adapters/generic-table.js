'use strict';
/** generic-table: scrape an HTML table (with pagination) into an array of objects keyed by normalised headers. */
const crypto = require('node:crypto');

/** "Invoice No." -> "invoice_no"; blanks become col_<n>; duplicates get _2, _3. */
function normalizeHeaders(headers) {
  const seen = new Map();
  return headers.map((h, i) => {
    let key = String(h || '').toLowerCase().normalize('NFKD').replace(/[^\p{L}\p{N}]+/gu, '_').replace(/^_+|_+$/g, '');
    if (!key) key = `col_${i + 1}`;
    const n = (seen.get(key) || 0) + 1;
    seen.set(key, n);
    return n === 1 ? key : `${key}_${n}`;
  });
}

function rowsToObjects(headers, rows) {
  const keys = normalizeHeaders(headers);
  return rows
    .filter((r) => r.some((c) => String(c).trim() !== ''))
    .map((r) => {
      const o = {};
      keys.forEach((k, i) => { o[k] = r[i] === undefined ? '' : String(r[i]).trim(); });
      return o;
    });
}

/** Runs INSIDE the browser: {headers, rows} of the first matching table. */
function extractTableInPage(sel) {
  var t = document.querySelector(sel);
  if (!t) return null;
  var headRow = t.querySelector('thead tr') || t.querySelector('tr');
  var headers = Array.prototype.map.call(headRow ? headRow.children : [], function (c) { return (c.innerText || c.textContent || '').trim(); });
  var bodyRows = t.querySelectorAll('tbody tr');
  if (!bodyRows.length) {
    bodyRows = Array.prototype.slice.call(t.querySelectorAll('tr'), 1);
  }
  var rows = Array.prototype.map.call(bodyRows, function (tr) {
    return Array.prototype.map.call(tr.children, function (c) { return (c.innerText || c.textContent || '').replace(/\s+/g, ' ').trim(); });
  });
  return { headers: headers, rows: rows };
}

const hash = (v) => crypto.createHash('sha256').update(JSON.stringify(v)).digest('hex');

/**
 * opts: {selector='table', nextSelector, maxPages=50, settle(page)}
 * Stops when there is no (enabled, visible) next control, the page repeats, or maxPages is reached.
 */
async function scrapeTable(page, opts = {}) {
  const selector = opts.selector || 'table';
  const maxPages = opts.maxPages || 50;
  const all = [];
  let lastHash = null;
  let pages = 0;
  for (;;) {
    const raw = await page.evaluate(extractTableInPage, selector);
    if (!raw) {
      if (pages === 0) throw new Error(`generic-table: no element matches ${selector}`);
      break;
    }
    const h = hash(raw.rows);
    if (h === lastHash) break; // pagination did not change the data
    lastHash = h;
    pages += 1;
    all.push(...rowsToObjects(raw.headers, raw.rows));
    if (!opts.nextSelector || pages >= maxPages) break;
    const next = page.locator(opts.nextSelector);
    if ((await next.count()) === 0) break;
    const first = next.first();
    if (!(await first.isVisible()) || !(await first.isEnabled())) break;
    await first.click();
    if (opts.settle) await opts.settle(page);
    else if (page.waitForLoadState) await page.waitForLoadState('networkidle').catch(() => {});
  }
  return { rows: all, pages };
}

module.exports = {
  name: 'generic-table',
  description: 'Scrape an HTML table (header normalisation + pagination) into row objects',
  scrapeTable, normalizeHeaders, rowsToObjects, extractTableInPage,
};
