'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const rec = require('../lib/recorder');

const { LIB_LOGIN, TEST_ROWS, LEGACY, SENSITIVE, FRAMES } = require('./fixtures/codegen');

// ---- harness: execute generated main.js against a recording fake framework -----------------------------
function chain(parts = []) {
  return new Proxy({}, {
    get(_, name) {
      if (name === '__chain') return parts.join('.');
      return (...args) => chain([...parts, `${String(name)}(${args.map((a) => JSON.stringify(a)).join(',')})`]);
    },
  });
}
const srcOf = (loc) => (typeof loc === 'function' ? loc(chain()).__chain : loc);

async function runGenerated(code, { dryRun = false, params = {} } = {}) {
  const calls = [];
  const rp = (label) => new Proxy({}, { get: (_, method) => async (loc, ...args) => { calls.push([label, method, srcOf(loc), ...args]); } });
  const popup = { goto: async (u) => calls.push(['goto', 'popup', u]), url: () => 'https://popup.example.com/' };
  const page = {
    url: () => 'https://x.example.com/final',
    goto: async (u) => calls.push(['goto', 'page', u]),
    waitForURL: async (a) => calls.push(['waitForURL', a]),
    waitForEvent: async () => popup,
    keyboard: { press: async (k) => calls.push(['keyboard.press', k]) },
  };
  const w = {
    param: (n, d) => (params[n] !== undefined ? params[n] : d),
    logEvent: () => {},
    isDryRun: () => dryRun,
    runItem: async (id, fn) => { calls.push(['runItem', id]); return fn(); },
    setResult: (r) => calls.push(['setResult', r]),
  };
  const session = { page, guard: async (fn) => fn(session), close: async () => calls.push(['close']) };
  const framework = {
    wrapper: w,
    adapters: { get: (n) => (n === 'sso-detect' ? { assertSession: async (pg, cfg) => calls.push(['sso', pg === page ? 'page' : 'popup', cfg]) } : { waitForNetworkIdle: async () => calls.push(['idle']) }) },
    browser: { launch: async (o) => { calls.push(['launch', o]); return session; } },
    resilience: { resilientPage: (pg) => rp(pg === page ? 'rp' : 'rp_page1') },
  };
  let pending;
  const fakeFw = { wrapper: { entrypoint: (fn) => { pending = fn(framework); } } };
  const fakeRequire = (id) => (id === 'node:path' ? path : fakeFw);
  new Function('require', 'process', '__dirname', code)(fakeRequire, { env: {} }, '/x');
  let error = null;
  try { await pending; } catch (e) { error = e; }
  return { calls, error };
}

// ---- lexical layer -------------------------------------------------------------------------------------
test('scanLiterals / splitStatements: semicolons and parens inside strings, regexes and comments do not split', () => {
  const body = `
    await page.getByText('a;b').click(); // trailing; comment
    /* block; comment */
    await page.getByText(/foo;bar\\)/i).first().click();
    await page.locator("x[title='a;b']").fill('c;d')
  `;
  const stmts = rec.splitStatements(body).map((s) => s.text);
  assert.equal(stmts.length, 3);
  assert.equal(stmts[0], "await page.getByText('a;b').click()");
  assert.ok(stmts[1].startsWith('await page.getByText(/foo;bar'));
  assert.equal(stmts[2], `await page.locator("x[title='a;b']").fill('c;d')`);
  const lits = rec.scanLiterals("a('x'); // c\n/re/g");
  assert.deepEqual(lits.map((l) => l.type), ['str', 'comment', 'regex']);
});

test('extractBody handles the library script, @playwright/test and bare statements', () => {
  assert.equal(rec.extractBody(LIB_LOGIN).variant, 'playwright-library');
  assert.ok(!rec.extractBody(LIB_LOGIN).body.includes('context.close'));
  const t = rec.extractBody(TEST_ROWS);
  assert.equal(t.variant, 'playwright-test');
  assert.ok(t.body.includes("page.goto('https://erp.example.com/invoices')") && !t.body.includes('});'));
  assert.equal(rec.extractBody("await page.goto('https://a.b');").variant, 'statements');
  assert.equal(rec.extractBody("test('x', async ({ page, context }) => { await page.goto('u'); });").variant, 'playwright-test');
});

test('parseChain / parseStatement: modern, legacy, popup, keyboard, assertions, unknown', () => {
  const c = rec.parseChain("page.getByRole('row', { name: 'a.b' }).getByRole('button').first().click()");
  assert.equal(c.base, 'page');
  assert.deepEqual(c.segs.map((s) => s.name), ['getByRole', 'getByRole', 'first', 'click']);
  const a = rec.parseStatement("await page.getByLabel('U').fill('x')", 1);
  assert.deepEqual([a.kind, a.method, a.locSrc, a.desc], ['action', 'fill', "getByLabel('U')", 'label "U"']);
  const l = rec.parseStatement("await page.click('text=Login')", 2);
  assert.deepEqual([l.kind, l.method, l.locSrc], ['action', 'click', "locator('text=Login')"]);
  assert.equal(rec.parseStatement("const page1Promise = page.waitForEvent('popup')", 3).kind, 'popup-wait');
  assert.equal(rec.parseStatement('const page1 = await page1Promise', 4).kind, 'popup-bind');
  assert.equal(rec.parseStatement("await page.keyboard.press('Enter')", 5).kind, 'keyboard');
  assert.equal(rec.parseStatement("await expect(page.locator('h1')).toBeVisible()", 6).kind, 'assert');
  assert.equal(rec.parseStatement('page.mystery.thing()', 7).kind, 'unknown');
  assert.equal(rec.parseStatement("await page.waitForTimeout(500)", 8).kind, 'sleep');
});

test('looksSensitiveValue: SSN, Luhn cards, JWT, bearer, long hex, opaque tokens - but not ordinary text', () => {
  for (const v of ['123-45-6789', '4111 1111 1111 1111', 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2', 'Bearer abcdefgh12345', 'a'.repeat(32), 'abcdEFGH1234abcdEFGH1234']) assert.equal(rec.looksSensitiveValue(v), true, v);
  for (const v of ['alice@example.com', 'invoice 42', 'Leeds', '4111 1111 1111 1112', '2026-10-21']) assert.equal(rec.looksSensitiveValue(v), false, v);
});

// ---- conversion: login flow ----------------------------------------------------------------------------
test('convert (library script): resilient calls, params, sso assertions, popup, credential throw, comments + warnings', () => {
  const r = rec.convert(LIB_LOGIN, { name: 'automation-portal-flow', now: '2026-10-21T00:00:00Z' });
  assert.equal(r.variant, 'playwright-library');
  const main = r.files['main.js'];
  assert.ok(main.includes("await page.goto(P.start_url, { waitUntil: 'domcontentloaded' });"));
  assert.ok(main.includes("await sso.assertSession(page, { expectedOrigin: 'https://portal.example.com' });"));
  assert.ok(main.includes("await rp.fill((p) => p.getByLabel('Username'), P.username);"));
  assert.ok(main.includes("await rp.click((p) => p.getByRole('button', { name: 'Sign in' }));"));
  assert.ok(main.includes("await rp.press((p) => p.getByPlaceholder('Search'), 'Enter');"));
  assert.ok(main.includes("await rp.check((p) => p.getByRole('checkbox', { name: 'Include archived' }));"));
  assert.ok(main.includes("await rp.selectOption((p) => p.locator('#region'), P.region);"));
  assert.ok(main.includes("await page.waitForURL('https://portal.example.com/dashboard');"));
  assert.ok(main.includes("framework.adapters.get('wait-helpers').waitForNetworkIdle(page)"), 'fixed sleep replaced');
  assert.ok(main.includes('const page1 = await page1Promise;') && main.includes('rp_page1.click((p) => p.getByRole(\'button\', { name: \'Download\' }))'));
  assert.ok(main.includes('// recorded assertion (not converted): expect(page.locator'));
  // credentials
  assert.ok(main.includes(rec.CREDENTIAL_THROW));
  assert.ok(!main.includes('S3cret!pass'));
  assert.ok(!r.files['params.suggested.yaml'].includes('S3cret!pass') && !r.files['README.md'].includes('S3cret!pass'));
  const byName = Object.fromEntries(r.params.map((p) => [p.name, p]));
  assert.equal(byName.start_url.default, 'https://portal.example.com/');
  assert.equal(byName.username.default, 'alice@example.com');
  assert.equal(byName.password.sensitive, true);
  assert.equal(byName.password.default, null);
  assert.ok(r.files['params.suggested.yaml'].includes('# password: <not recorded'));
  assert.ok(r.warnings.some((x) => /credential\/secret - NOT recorded/.test(x)));
  assert.ok(r.warnings.some((x) => /assertion not converted/.test(x)));
  assert.ok(r.warnings.some((x) => /fixed sleep/.test(x)));
  // the whole project
  assert.deepEqual(Object.keys(r.files).sort(), ['README.md', 'automation.yaml', 'main.js', 'params.suggested.yaml', 'tests/test_lifecycle.py']);
  assert.match(r.files['automation.yaml'], /^name: automation-portal-flow\nversion: 0.1.0\nruntime: node\nentrypoint: main.js/);
  assert.match(r.files['README.md'], /\| 1 \| goto \|/);
  assert.match(r.files['README.md'], /NOT production-ready until reviewed/);
  assert.match(r.files['tests/test_lifecycle.py'], /run_payload\(ROOT, \{\}, dry_run=True\)/);
  assert.match(main, /2026-10-21T00:00:00\.000Z/);
});

test('generated main.js parses, and a payload with a credential step fails loudly at that step (never types a password)', async () => {
  const r = rec.convert(LIB_LOGIN, { name: 'automation-portal-flow' });
  const out = await runGenerated(r.files['main.js']);
  assert.ok(out.error && out.error.message === 'credential required: use the user profile / Credential Manager', String(out.error));
  const done = out.calls.map((c) => c.slice(0, 2).join(':'));
  assert.deepEqual(done, ['launch:[object Object]', 'goto:page', 'sso:page', 'rp:click', 'rp:fill', 'close'], 'stops at the credential step: nothing after it ran');
  assert.ok(!out.calls.some((c) => JSON.stringify(c).includes('S3cret')), 'the secret value never reaches a call');
  assert.ok(out.calls.some((c) => c[0] === 'close'), 'the browser is always closed');
});

test('a recording without secrets executes end to end against the fake framework (params flow, popup page, dry-run)', async () => {
  const noPwd = LIB_LOGIN.replace("  await page.getByLabel('Password').fill('S3cret!pass');\n", '');
  const r = rec.convert(noPwd, { name: 'automation-portal-flow' });
  const out = await runGenerated(r.files['main.js'], { params: { username: 'bob@example.com' } });
  assert.equal(out.error, null, String(out.error && out.error.stack));
  const fill = out.calls.find((c) => c[0] === 'rp' && c[1] === 'fill');
  assert.deepEqual(fill.slice(2), ["getByLabel(\"Username\")", 'bob@example.com'], 'schedule param overrides the recorded value');
  assert.ok(out.calls.some((c) => c[0] === 'goto' && c[1] === 'page' && c[2] === 'https://portal.example.com/'));
  assert.ok(out.calls.some((c) => c[0] === 'rp_page1' && c[1] === 'click'), 'actions on the popup use its own resilient page');
  assert.ok(out.calls.some((c) => c[0] === 'idle'));
  const res = out.calls.find((c) => c[0] === 'setResult')[1];
  assert.equal(res.final_host, 'x.example.com');
  assert.deepEqual(out.calls[out.calls.length - 1], ['close']);
});

// ---- repeats -------------------------------------------------------------------------------------------
test('repeated row pattern becomes a runItem loop over ROWS; irreversible clicks are skipped in dry-run', async () => {
  const r = rec.convert(TEST_ROWS, { name: 'automation-invoice-approve' });
  const main = r.files['main.js'];
  assert.ok(main.includes('const ROWS_1 = [{"v0":"INV-1001"},{"v0":"INV-1002"},{"v0":"INV-1003"}];'), main);
  assert.ok(main.includes('for (const row of ROWS_1) {') && main.includes('await w.runItem(String(row.v0), async () => {'));
  assert.ok(main.includes("p.getByRole('row', { name: row.v0 }).getByRole('button', { name: 'Approve' })"));
  assert.equal((main.match(/name: 'Approve'/g) || []).length, 1, 'recorded once, not three times');
  assert.ok(main.includes("await rp.fill((p) => p.getByRole('textbox', { name: 'Comment' }), P.comment);"), 'a constant value inside the loop is still a param');
  assert.ok(main.includes('if (!w.isDryRun()) await rp.click'), 'Approve/Submit are irreversible-looking');
  assert.ok(!/comment_2|comment_3/.test(r.files['params.suggested.yaml']), 'loop values are not params');
  assert.ok(r.files['README.md'].includes('loop 1'));

  const live = await runGenerated(main);
  assert.equal(live.error, null, String(live.error && live.error.stack));
  assert.deepEqual(live.calls.filter((c) => c[0] === 'runItem').map((c) => c[1]), ['INV-1001', 'INV-1002', 'INV-1003']);
  assert.equal(live.calls.filter((c) => c[0] === 'rp' && c[1] === 'click').length, 4, '3 approvals + submit');
  const dry = await runGenerated(main, { dryRun: true });
  assert.equal(dry.calls.filter((c) => c[0] === 'rp' && c[1] === 'click').length, 0, 'nothing irreversible in dry-run');
  assert.equal(dry.calls.filter((c) => c[0] === 'rp' && c[1] === 'fill').length, 3, 'harmless steps still run');
});

test('no loop is invented when nothing varies or the pattern appears once', () => {
  const same = `test('x', async ({ page }) => {
    await page.getByRole('button', { name: 'Next' }).click();
    await page.getByRole('button', { name: 'Next' }).click();
  });`;
  assert.ok(!rec.convert(same, { name: 'automation-next' }).files['main.js'].includes('ROWS_'));
  const once = `test('x', async ({ page }) => { await page.getByRole('row', { name: 'A' }).click(); await page.getByRole('cell', { name: 'B' }).click(); });`;
  assert.ok(!rec.convert(once, { name: 'automation-once' }).files['main.js'].includes('ROWS_'));
  const nth = `test('x', async ({ page }) => {
    await page.getByRole('row').nth(1).getByRole('button', { name: 'Open' }).click();
    await page.getByRole('row').nth(2).getByRole('button', { name: 'Open' }).click();
  });`;
  const m = rec.convert(nth, { name: 'automation-nth' }).files['main.js'];
  assert.ok(m.includes('const ROWS_1 = [{"v0":1},{"v0":2}];') && m.includes('.nth(row.v0)'), m);
});

// ---- legacy API, sensitive variants, frames/unknown ----------------------------------------------------
test('legacy API (page.click(selector) etc.) converts; non-healable calls are plain with a warning', async () => {
  const r = rec.convert(LEGACY, { name: 'automation-legacy' });
  const main = r.files['main.js'];
  assert.ok(main.includes("await rp.click((p) => p.locator('text=Login'));"));
  assert.ok(main.includes("await rp.fill((p) => p.locator('input[name=\"q\"]'), P.q);"));
  assert.ok(main.includes("await rp.selectOption((p) => p.locator('select#size'), P.size);") || main.includes("await rp.selectOption((p) => p.locator('select#size'), P.select_size);") || /await rp\.selectOption\(\(p\) => p\.locator\('select#size'\), P\.\w+\);/.test(main));
  assert.ok(main.includes("await rp.dblclick((p) => p.locator('#row-2'));"));
  assert.ok(main.includes("const L = (fn, pg = page) => fn(pg);") && /await L\(\(p\) => p\.locator\('input\[type=file\]'\)\)\.setInputFiles\(/.test(main));
  assert.ok(r.warnings.some((x) => /setInputFiles is not self-healing.*file-upload/.test(x)));
  assert.ok(r.params.some((p) => p.name === 'q' && p.default === 'hello'));
  const out = await runGenerated(main.replace(/await L\(.*setInputFiles.*\n/, ''));
  assert.equal(out.error, null, String(out.error && out.error.stack));
});

test('secrets in any form are never written: SSN, password input, card, API key, typed JWT - but "Passport office" is fine', async () => {
  const r = rec.convert(SENSITIVE, { name: 'automation-hr' });
  const everything = Object.values(r.files).join('\n');
  for (const secret of ['123-45-6789', '4111111111111111', 'abcdEFGH1234abcdEFGH1234', 'eyJhbGciOiJIUzI1NiJ9', "fill('x')"]) assert.ok(!everything.includes(secret), secret);
  assert.equal((r.files['main.js'].match(/credential required/g) || []).length, 5, 'ssn, password, card, api key, typed jwt');
  assert.ok(r.files['main.js'].includes("P.passport_office") && r.params.find((p) => p.name === 'passport_office').default === 'Leeds');
  assert.equal(r.params.filter((p) => p.sensitive).length, 4);
  assert.ok(r.warnings.some((x) => /typed value looks like a secret/.test(x)));
});

test('frames, dialogs and unknown statements: kept in the chain / TODO comments, never silently dropped or executed', () => {
  const r = rec.convert(FRAMES, { name: 'automation-shop' });
  const main = r.files['main.js'];
  assert.ok(main.includes("(p) => p.frameLocator('#checkout').getByText('Accept terms')"));
  assert.ok(main.includes("(p) => p.locator('iframe').contentFrame().getByRole('button', { name: 'Pay now' })"));
  assert.ok(main.includes('if (!w.isDryRun()) await rp.click((p) => p.locator(\'iframe\')'), 'Pay now is irreversible-looking');
  assert.ok(main.includes('// TODO(unconverted): page.once(\'dialog\''));
  assert.ok(main.includes('// TODO(unconverted): page.mystery.thing()'));
  assert.ok(!/^\s*page\.mystery/m.test(main) && !/^\s*page\.once/m.test(main), 'unconverted statements are comments only');
  assert.ok(r.warnings.filter((x) => /not converted/.test(x)).length >= 2);
  assert.ok(main.includes('getByText(/total: \\d+;/)'), 'regex locators survive intact');
});

// ---- errors, naming, writing ---------------------------------------------------------------------------
test('convert validates the name and refuses an empty recording', () => {
  assert.throws(() => rec.convert(LIB_LOGIN, { name: 'My Automation' }), /name must look like automation-/);
  assert.throws(() => rec.convert('// nothing here\n', { name: 'automation-empty' }), /no recorded actions/);
});

test('writeProject: writes all files, refuses a non-empty dir without force', (t) => {
  const { withRunDir } = require('./_helpers');
  const r0 = withRunDir(t);
  const out = path.join(r0.dir, 'proj');
  const files = rec.convert(TEST_ROWS, { name: 'automation-w' }).files;
  const written = rec.writeProject(files, out);
  assert.equal(written.length, 5);
  assert.ok(fs.readFileSync(path.join(out, 'main.js'), 'utf8').includes("'use strict';"));
  assert.ok(fs.existsSync(path.join(out, 'tests', 'test_lifecycle.py')));
  assert.throws(() => rec.writeProject(files, out), /not empty/);
  assert.equal(rec.writeProject(files, out, { force: true }).length, 5);
});

test('jsStr / unquote round-trip awkward strings', () => {
  for (const s of ["it's", 'a\\b', 'line\nbreak', 'quote"d', '']) assert.equal(rec.unquote(rec.jsStr(s)), s, JSON.stringify(s));
});
