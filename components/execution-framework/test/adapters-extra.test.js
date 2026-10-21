'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const E = require('../lib/errors');
const adapters = require('../lib/adapters');
const upload = require('../lib/adapters/file-upload');
const frames = require('../lib/adapters/iframe-helper');
const waits = require('../lib/adapters/wait-helpers');
const popups = require('../lib/adapters/popup-handler');
const csv = require('../lib/adapters/table-export-csv');
const sso = require('../lib/adapters/sso-detect');
const { withRunDir } = require('./_helpers');

// ---- file-upload ---------------------------------------------------------------------------------------
function memFs(files) {
  return {
    statSync: (p) => {
      if (!(p in files)) throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
      return { size: files[p], isDirectory: () => files[p] === 'dir' };
    },
  };
}

test('validateFiles: missing / empty / too large / too many are reported with the path', () => {
  const fs2 = memFs({ 'a.pdf': 10, 'empty.txt': 0, 'huge.bin': 500, 'd': 'dir' });
  const limits = { maxFiles: 3, maxFileBytes: 100, maxTotalBytes: 1000 };
  assert.deepEqual(upload.validateFiles('a.pdf', { fs: fs2, limits }).files, [{ path: 'a.pdf', name: 'a.pdf', size: 10 }]);
  const bad = upload.validateFiles(['a.pdf', 'nope.pdf', 'empty.txt', 'huge.bin', 'd'], { fs: fs2, limits });
  assert.equal(bad.ok, false);
  const msg = bad.errors.join(' | ');
  assert.match(msg, /too many files/);
  assert.match(msg, /file not found: nope\.pdf/);
  assert.match(msg, /file is empty: empty\.txt/);
  assert.match(msg, /file too large .*huge\.bin/);
  assert.match(msg, /not a file: d/);
  assert.equal(upload.validateFiles([], { fs: fs2 }).ok, false);
});

test('uploadFiles: validates first (DataError), then setInputFiles; selector string or locator', async () => {
  const fs2 = memFs({ 'a.pdf': 10 });
  const set = [];
  const loc = { setInputFiles: async (f) => set.push(f) };
  const page = { locator: (sel) => { set.push(['locator', sel]); return loc; } };
  assert.deepEqual(await upload.uploadFiles(page, '#file', ['a.pdf'], { fs: fs2 }), { uploaded: ['a.pdf'], bytes: 10 });
  assert.deepEqual(set, [['locator', '#file'], ['a.pdf']]);
  await upload.uploadFiles(page, loc, 'a.pdf', { fs: fs2 });
  await assert.rejects(() => upload.uploadFiles(page, '#file', ['missing.pdf'], { fs: fs2 }), (e) => e instanceof E.DataError && /file not found/.test(e.message));
  assert.equal(E.classifyException(new E.DataError('x')), E.Category.DATA_ERROR);
});

test('uploadViaChooser clicks, waits for the chooser, sets files', async () => {
  const fs2 = memFs({ 'a.pdf': 3 });
  const done = [];
  const chooser = { setFiles: async (f) => done.push(f) };
  const page = { waitForEvent: async (n) => { assert.equal(n, 'filechooser'); return chooser; } };
  const r = await upload.uploadViaChooser(page, async () => done.push('clicked'), ['a.pdf'], { fs: fs2 });
  assert.deepEqual(r.uploaded, ['a.pdf']);
  assert.ok(done.includes('clicked') && done.some((x) => Array.isArray(x)));
});

// ---- iframe-helper -------------------------------------------------------------------------------------
const fr = (url, name = '') => ({ url: () => url, name: () => name, waitForLoadState: async () => {} });

test('frameMatches / findFrame: url regexp or substring, name, main frame excluded', () => {
  const main = fr('https://portal/');
  const pay = fr('https://pay.example.com/widget?x=1', 'payframe');
  const page = { mainFrame: () => main, frames: () => [main, fr('https://ads.example.com/'), pay] };
  assert.equal(frames.findFrame(page, { urlPattern: /pay\.example\.com/ }), pay);
  assert.equal(frames.findFrame(page, { urlPattern: 'pay.example.com/widget' }), pay);
  assert.equal(frames.findFrame(page, { name: 'payframe' }), pay);
  assert.equal(frames.findFrame(page, { urlPattern: 'portal' }), null, 'the main frame never matches');
  assert.equal(frames.frameMatches(pay, { urlPattern: 'pay', name: 'other' }), false);
  assert.equal(frames.frameMatches(pay, {}), false);
});

test('waitForFrame polls until the frame attaches; times out with a TimeoutError naming the pattern', async () => {
  let now = 0;
  const list = [];
  const main = fr('m');
  const page = { mainFrame: () => main, frames: () => [main, ...list] };
  const sleep = async (ms) => { now += ms; if (now >= 600) list.push(fr('https://late.example.com/')); };
  const f = await frames.waitForFrame(page, { urlPattern: /late/ }, { sleep, now: () => now, pollMs: 200, timeoutMs: 5000 });
  assert.equal(f.url(), 'https://late.example.com/');
  let t = 0;
  await assert.rejects(() => frames.waitForFrame({ mainFrame: () => main, frames: () => [main] }, { urlPattern: /never/ }, { sleep: async (ms) => { t += ms; }, now: () => t, pollMs: 500, timeoutMs: 1000 }),
    (e) => e.name === 'TimeoutError' && /never/.test(e.message));
  const out = await frames.withFrame({ mainFrame: () => main, frames: () => [main, fr('https://x/f')] }, { urlPattern: 'x/f' }, async (frame) => frame.url());
  assert.equal(out, 'https://x/f');
});

// ---- wait-helpers --------------------------------------------------------------------------------------
test('retryUntil: backoff intervals, truthy result, last error rethrown at the deadline, TimeoutError otherwise', async () => {
  let now = 0;
  const waits_ = [];
  const sleep = async (ms) => { waits_.push(ms); now += ms; };
  let n = 0;
  const v = await waits.retryUntil(() => { n += 1; return n >= 4 ? 'ready' : null; }, { sleep, now: () => now, intervalMs: 100, backoff: 2, maxIntervalMs: 350, timeoutMs: 10000 });
  assert.equal(v, 'ready');
  assert.deepEqual(waits_, [100, 200, 350]);

  now = 0;
  await assert.rejects(() => waits.retryUntil(() => { throw new Error('still failing'); }, { sleep, now: () => now, intervalMs: 100, timeoutMs: 350 }), /still failing/);
  now = 0;
  await assert.rejects(() => waits.retryUntil(() => false, { sleep, now: () => now, intervalMs: 100, timeoutMs: 250 }), (e) => e.name === 'TimeoutError' && /not met within 250 ms/.test(e.message));
  now = 0;
  assert.equal(await waits.retryUntil(() => 7, { until: (x) => x === 7, sleep, now: () => now }), 7);
});

test('waitForStable: resolves once the signature is unchanged for stableMs; times out if it keeps changing or never appears', async () => {
  let now = 0;
  const sleep = async (ms) => { now += ms; };
  const sigs = ['a', 'a', 'b', 'b', 'b', 'b', 'b', 'b', 'b'];
  let i = 0;
  const r = await waits.waitForStable(null, '#x', { getSignature: () => sigs[Math.min(i++, sigs.length - 1)], stableMs: 300, pollMs: 100, sleep, now: () => now, timeoutMs: 5000 });
  assert.equal(r, 'b');
  now = 0;
  let k = 0;
  await assert.rejects(() => waits.waitForStable(null, '#flicker', { getSignature: () => String(k++), stableMs: 300, pollMs: 100, sleep, now: () => now, timeoutMs: 1000 }), (e) => e.name === 'TimeoutError' && /#flicker/.test(e.message));
  now = 0;
  await assert.rejects(() => waits.waitForStable(null, '#absent', { getSignature: () => null, stableMs: 100, pollMs: 100, sleep, now: () => now, timeoutMs: 500 }), /did not settle/);
});

test('waitForNetworkIdle never throws', async () => {
  assert.equal(await waits.waitForNetworkIdle({ waitForLoadState: async () => {} }), true);
  assert.equal(await waits.waitForNetworkIdle({ waitForLoadState: async () => { throw new Error('timeout'); } }), false);
});

// ---- popup-handler -------------------------------------------------------------------------------------
function bannerPage(table) {
  const clicks = [];
  const page = {
    locator: (sel) => {
      const el = table[sel];
      const loc = {
        count: async () => (el ? (el.count === undefined ? 1 : el.count) : 0),
        first: () => loc,
        isVisible: async () => Boolean(el) && el.visible !== false,
        evaluate: async () => (el ? el.text : ''),
        click: async () => { clicks.push(sel); },
      };
      return loc;
    },
  };
  return { page, clicks };
}

test('isSafeToClick: deny-list always wins unless allowDestructive', () => {
  assert.equal(popups.isSafeToClick('#cookie-reject', 'Reject all'), true);
  assert.equal(popups.isSafeToClick('#x', 'Delete account'), false);
  assert.equal(popups.isSafeToClick('#confirm-payment', 'OK'), false);
  assert.equal(popups.isSafeToClick('#x', 'Delete', { allowDestructive: true }), true);
});

test('dismissBanners clicks known banners (reject first), skips ambiguous / hidden / deny-listed ones, never throws', async () => {
  const { page, clicks } = bannerPage({
    '#onetrust-reject-all-handler': { text: 'Reject All' },
    '#onetrust-accept-btn-handler': { text: 'Accept all', visible: false },
    '.cc-dismiss': { text: 'Got it', count: 2 },
    '#custom': { text: 'Delete my data' },
  });
  const log = eventSink2();
  const res = await popups.dismissBanners(page, { selectors: ['#onetrust-reject-all-handler', '#onetrust-accept-btn-handler', '.cc-dismiss', '#custom', '#absent'], logEvent: log });
  assert.deepEqual(res.dismissed, ['#onetrust-reject-all-handler']);
  assert.deepEqual(clicks, ['#onetrust-reject-all-handler']);
  assert.deepEqual(res.skipped.map((s) => s.selector).sort(), ['#custom', '.cc-dismiss']);
  assert.ok(res.skipped.find((s) => s.selector === '#custom').reason.includes('deny-listed'));
  assert.equal(log.list.length, 1);
  assert.equal(popups.DEFAULT_BANNERS.indexOf('#onetrust-reject-all-handler') < popups.DEFAULT_BANNERS.indexOf('#onetrust-accept-btn-handler'), true, 'reject before accept');
  // errors are contained
  const bad = { locator: () => { throw new Error('boom'); } };
  const r2 = await popups.dismissBanners(bad, { selectors: ['#a'] });
  assert.equal(r2.skipped[0].reason, 'boom');
});

function eventSink2() {
  const list = [];
  const f = (name, data) => list.push({ name, data });
  f.list = list;
  return f;
}

test('installAutoDismiss registers locator handlers only for safe selectors', async () => {
  const handlers = [];
  const page = { locator: (s) => ({ sel: s }), addLocatorHandler: async (loc, fn) => handlers.push(loc.sel) };
  const r = await popups.installAutoDismiss(page, { selectors: ['#ok', '#delete-all'] });
  assert.deepEqual(handlers, ['#ok']);
  assert.equal(r.installed, 1);
  assert.equal((await popups.installAutoDismiss({}, {})).installed, 0);
});

// ---- table-export-csv ----------------------------------------------------------------------------------
test('toCsv: quoting, delimiter, BOM, CRLF, formula-injection guard that leaves plain numbers alone', () => {
  const rows = [{ name: 'Smith, J', note: 'say "hi"', amt: '-12.50', f: '=HYPERLINK("x")' }, { name: 'B', note: 'line1\nline2', amt: 5, f: '@cmd' }];
  const out = csv.toCsv(rows, { columns: ['name', 'note', 'amt', 'f'] });
  assert.equal(out, 'name,note,amt,f\r\n"Smith, J","say ""hi""",-12.50,"\'=HYPERLINK(""x"")"\r\nB,"line1\nline2",5,\'@cmd\r\n');
  assert.equal(csv.toCsv([{ a: 1 }], { bom: true, delimiter: ';' }).charCodeAt(0), 0xfeff);
  assert.equal(csv.toCsv([{ a: '=1+1' }], { formulaGuard: false }), 'a\r\n=1+1\r\n');
  assert.equal(csv.neutralizeFormula('+1 555 123'), '+1 555 123');
  assert.equal(csv.neutralizeFormula('-cmd'), "'-cmd");
  assert.deepEqual(csv.toCsv([{ a: 1 }, { b: 2 }]).split('\r\n').slice(0, 3), ['a,b', '1,', ',2']);
});

test('exportTable: scrape -> CSV file with row count and sha256', async (t) => {
  const r = withRunDir(t);
  const dest = path.join(r.dir, 'out', 'table.csv');
  const res = await csv.exportTable({ raw: {} }, { selector: '#t' }, dest, { scrape: async (page, o) => ({ rows: [{ id: 'A', v: '1' }, { id: 'B', v: '2' }], pages: 2, o }) });
  assert.deepEqual([res.rows, res.pages, res.path], [2, 2, dest]);
  assert.equal(fs.readFileSync(dest, 'utf8'), 'id,v\r\nA,1\r\nB,2\r\n');
  assert.match(res.sha256, /^[0-9a-f]{64}$/);
});

// ---- registry + sso evidence hook ---------------------------------------------------------------------
test('new adapters are registered under their names', () => {
  for (const n of ['file-upload', 'iframe-helper', 'wait-helpers', 'popup-handler', 'table-export-csv']) assert.ok(adapters.has(n), n);
  assert.equal(adapters.get('wait-helpers').retryUntil, waits.retryUntil);
});

test('sso-detect asks for failure evidence (page-scoped) before throwing AuthExpired', async () => {
  const got = [];
  const page = {
    url: () => 'https://login.microsoftonline.com/x', title: async () => 'Sign in',
    locator: () => ({ count: async () => 1, innerText: async () => 'Sign in' }),
  };
  await assert.rejects(() => sso.assertSession(page, { captureEvidence: async (err, ctx) => got.push([err.name, ctx.reason, ctx.page === page]) }), E.AuthExpired);
  assert.deepEqual(got, [['AuthExpired', 'session lapse', true]]);
});
