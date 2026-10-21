'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const E = require('../lib/errors');
const adapters = require('../lib/adapters');
const table = require('../lib/adapters/generic-table');
const form = require('../lib/adapters/generic-form');
const download = require('../lib/adapters/download');
const sso = require('../lib/adapters/sso-detect');

// ---- registry ------------------------------------------------------------------------------------
test('built-in adapters are registered and discoverable', () => {
  const names = adapters.list().map((a) => a.name).sort();
  assert.deepEqual(names, ['download', 'generic-form', 'generic-table', 'sso-detect']);
  assert.equal(adapters.get('generic-table').scrapeTable, table.scrapeTable);
  assert.equal(adapters.has('download'), true);
});

test('register/get/unregister; duplicates and unknown names are errors', () => {
  const a = adapters.register('my-portal', { description: 'x', login: async () => 'ok' });
  assert.equal(a.name, 'my-portal');
  assert.equal(adapters.get('my-portal').description, 'x');
  assert.throws(() => adapters.register('my-portal', {}), /already registered/);
  assert.throws(() => adapters.register('', {}), /non-empty/);
  assert.throws(() => adapters.register('bad', null), /must be an object/);
  assert.equal(adapters.unregister('my-portal'), true);
  assert.throws(() => adapters.get('my-portal'), /unknown adapter my-portal; registered: /);
});

// ---- generic-table -------------------------------------------------------------------------------
test('normalizeHeaders: snake_case, blanks, duplicates', () => {
  assert.deepEqual(table.normalizeHeaders(['Invoice No.', 'Amount', '', 'Amount', 'Total ($)']), ['invoice_no', 'amount', 'col_3', 'amount_2', 'total']);
});

test('rowsToObjects trims cells, pads short rows, drops blank rows', () => {
  const rows = table.rowsToObjects(['A', 'B'], [[' 1 ', '2'], ['3'], ['', ' ']]);
  assert.deepEqual(rows, [{ a: '1', b: '2' }, { a: '3', b: '' }]);
});

function tablePage(pages, { nextEnabled = true } = {}) {
  let i = 0;
  const page = {
    evaluate: async () => pages[Math.min(i, pages.length - 1)],
    locator: () => ({
      count: async () => 1,
      first: () => ({ isVisible: async () => true, isEnabled: async () => nextEnabled, click: async () => { i += 1; } }),
    }),
  };
  return page;
}

test('scrapeTable follows pagination and stops when the page repeats', async () => {
  const p1 = { headers: ['Invoice No.', 'Amount'], rows: [['1', '10'], ['2', '20']] };
  const p2 = { headers: ['Invoice No.', 'Amount'], rows: [['3', '30']] };
  const out = await table.scrapeTable(tablePage([p1, p2, p2]), { nextSelector: '.next', settle: async () => {} });
  assert.equal(out.pages, 2);
  assert.deepEqual(out.rows, [{ invoice_no: '1', amount: '10' }, { invoice_no: '2', amount: '20' }, { invoice_no: '3', amount: '30' }]);
});

test('scrapeTable honours maxPages, disabled next controls, and a missing table', async () => {
  const mk = (n) => ({ headers: ['A'], rows: [[String(n)]] });
  const capped = await table.scrapeTable(tablePage([mk(1), mk(2), mk(3), mk(4)]), { nextSelector: '.n', maxPages: 2, settle: async () => {} });
  assert.equal(capped.pages, 2);
  const disabled = await table.scrapeTable(tablePage([mk(1), mk(2)], { nextEnabled: false }), { nextSelector: '.n', settle: async () => {} });
  assert.equal(disabled.pages, 1);
  await assert.rejects(table.scrapeTable({ evaluate: async () => null }, { selector: '#nope' }), /no element matches #nope/);
});

// ---- generic-form --------------------------------------------------------------------------------
function formPage({ dropFor = [] } = {}) {
  const vals = {};
  const checks = {};
  const page = {
    fill: async (s, v) => { if (!dropFor.includes(s)) vals[s] = v; },
    selectOption: async (s, v) => { if (!dropFor.includes(s)) vals[s] = v; },
    check: async (s) => { checks[s] = true; },
    uncheck: async (s) => { checks[s] = false; },
    raw: { locator: (s) => ({ inputValue: async () => vals[s] || '', isChecked: async () => Boolean(checks[s]) }) },
  };
  return page;
}

test('fillForm fills every field type and verifies', async () => {
  const out = await form.fillForm(formPage(), [
    { selector: '#vendor', value: 'ACME' }, { selector: '#cur', value: 'USD', type: 'select' },
    { selector: '#paid', type: 'check' }, { selector: '#draft', type: 'uncheck' },
  ]);
  assert.deepEqual(out, { filled: 4, mismatches: [] });
});

test('fillForm: a field that did not take its value raises DataError naming the field (never the sensitive value)', async () => {
  const mapping = [{ selector: '#a', value: 'one', label: 'Vendor' }, { selector: '#ssn', value: '123-45-6789', sensitive: true }];
  await assert.rejects(form.fillForm(formPage({ dropFor: ['#a', '#ssn'] }), mapping), (e) => {
    assert.ok(e instanceof E.DataError);
    assert.match(e.message, /Vendor/);
    assert.ok(!e.message.includes('123-45-6789'));
    return true;
  });
  const soft = await form.fillForm(formPage({ dropFor: ['#ssn'] }), mapping, { strict: false });
  assert.equal(soft.mismatches.length, 1);
  assert.equal(soft.mismatches[0].reason, 'value mismatch (hidden)');
});

test('validateMapping rejects malformed mappings before touching the page', () => {
  assert.throws(() => form.validateMapping([]), E.DataError);
  assert.throws(() => form.validateMapping([{ value: 'x' }]), /needs a selector/);
  assert.throws(() => form.validateMapping([{ selector: '#a', type: 'radio', value: 'x' }]), /unknown type/);
  assert.throws(() => form.validateMapping([{ selector: '#a' }]), /no value/);
  assert.doesNotThrow(() => form.validateMapping([{ selector: '#a', type: 'check' }]));
});

// ---- download ------------------------------------------------------------------------------------
test('safeFilename strips paths and reserved characters', () => {
  assert.equal(download.safeFilename('../../evil.exe'), 'evil.exe');
  assert.equal(download.safeFilename('a\\b\\c.pdf'), 'c.pdf');
  assert.equal(download.safeFilename('rep:ort?.pdf'), 'rep_ort_.pdf');
  assert.equal(download.safeFilename(''), 'download.bin');
  assert.equal(download.safeFilename('.hidden'), 'hidden');
});

function downloadPage(content, { failure = null } = {}) {
  return {
    waitForEvent: async () => ({
      failure: async () => failure,
      suggestedFilename: () => '../report.csv',
      saveAs: async (p) => { fs.writeFileSync(p, content); },
    }),
  };
}

test('waitForDownload verifies size + sha256 and moves the file into place', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'amx-dl-'));
  try {
    const content = 'a,b\n1,2\n';
    const sha = crypto.createHash('sha256').update(content).digest('hex');
    let triggered = false;
    const r = await download.waitForDownload(downloadPage(content), async () => { triggered = true; }, { dir: path.join(dir, 'out'), minBytes: 4, sha256: sha });
    assert.equal(triggered, true);
    assert.equal(r.size, 8);
    assert.equal(r.sha256, sha);
    assert.equal(path.basename(r.path), 'report.csv', 'server-suggested name is sanitised');
    assert.equal(fs.readFileSync(r.path, 'utf8'), content);
    assert.deepEqual(fs.readdirSync(path.join(dir, 'out')), ['report.csv'], 'no .part leftovers');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('waitForDownload rejects too-small files, bad hashes, failed downloads, and a missing dir option', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'amx-dl-'));
  try {
    await assert.rejects(download.waitForDownload(downloadPage('x'), async () => {}, { dir, minBytes: 100 }), E.DataError);
    await assert.rejects(download.waitForDownload(downloadPage('abc'), async () => {}, { dir, sha256: '0'.repeat(64) }), /sha256 mismatch/);
    await assert.rejects(download.waitForDownload(downloadPage('abc', { failure: 'canceled' }), async () => {}, { dir }), E.SystemUnavailable);
    await assert.rejects(download.waitForDownload(downloadPage('abc'), async () => {}, {}), /opts\.dir is required/);
    assert.deepEqual(fs.readdirSync(dir), [], 'failed downloads leave nothing behind');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ---- sso-detect ----------------------------------------------------------------------------------
test('isLoginState table', () => {
  const st = (o) => ({ url: 'https://app.example.com/home', title: 'Home', text: 'Welcome', hasPasswordField: false, ...o });
  assert.equal(sso.isLoginState(st({})).loggedOut, false);
  assert.equal(sso.isLoginState(st({ url: 'https://login.microsoftonline.com/tenant/oauth2/authorize' })).loggedOut, true);
  assert.equal(sso.isLoginState(st({ url: 'https://app.example.com/login?next=/' })).loggedOut, true);
  assert.equal(sso.isLoginState(st({ hasPasswordField: true })).loggedOut, true);
  assert.equal(sso.isLoginState(st({ title: 'Session expired' })).loggedOut, true);
  assert.equal(sso.isLoginState(st({ text: 'Please sign in to continue' })).loggedOut, true);
  assert.equal(sso.isLoginState(st({ url: 'https://idp.example.net/x' }), { expectedOrigin: 'https://app.example.com' }).loggedOut, true);
  assert.equal(sso.isLoginState(st({}), { expectedOrigin: 'https://app.example.com' }).loggedOut, false);
});

function ssoPage({ url, password = false, text = 'Dashboard' }) {
  return {
    url: () => url,
    title: async () => 'Title',
    locator: (s) => ({ count: async () => (password && s.includes('password') ? 1 : 0), innerText: async () => text }),
  };
}

test('assertSession throws AuthExpired on a login state and passes on a live session', async () => {
  await assert.rejects(sso.assertSession(ssoPage({ url: 'https://app.example.com/home', password: true })), (e) => e instanceof E.AuthExpired && E.classifyException(e) === 'AUTH_EXPIRED');
  await assert.rejects(sso.assertSession(ssoPage({ url: 'https://app.example.com/home', text: 'Your session has expired' })), E.AuthExpired);
  assert.equal(await sso.assertSession(ssoPage({ url: 'https://app.example.com/home' })), true);
  // works through the resilientPage proxy too (page.raw)
  assert.equal(await sso.assertSession({ raw: ssoPage({ url: 'https://app.example.com/home' }) }), true);
});
