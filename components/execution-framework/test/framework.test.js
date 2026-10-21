'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const cdp = require('../lib/cdp');
const browser = require('../lib/browser');
const { buildCodegenArgs } = require('../bin/amx-codegen');
const { planBuild } = require('../scripts/build-plan.cjs');
const { withRunDir } = require('./_helpers');

const root = path.join(__dirname, '..');

// ---- public API surface (what automations code against) ---------------------------------------------
test('index exposes the documented API; heavy modules are lazy', async (t) => {
  withRunDir(t);
  const fw = require('../index');
  assert.equal(fw.version, fs.readFileSync(path.join(root, 'VERSION'), 'utf8').trim());
  assert.equal(fw.version, require('../package.json').version);
  for (const fn of ['logEvent', 'logItem', 'trackItem', 'runItem', 'runWithRetry', 'llmCall', 'setResult', 'params', 'param', 'isDryRun', 'assets', 'entrypoint', 'emitTrigger']) {
    assert.equal(typeof fw.wrapper[fn], 'function', `wrapper.${fn}`);
  }
  for (const name of ['Category', 'AutomationError', 'NetworkError', 'AuthExpired', 'SystemUnavailable', 'DataError', 'LogicError', 'Escalation']) {
    assert.ok(fw.wrapper.errors[name], `errors.${name}`);
  }
  assert.equal(typeof fw.http.client, 'function');
  assert.equal(typeof fw.browser.launch, 'function');
  assert.equal(typeof fw.browser.attach, 'function');
  assert.equal(typeof fw.cdp.domSnapshot, 'function');
  assert.equal(typeof fw.resilience.resilientPage, 'function');
  assert.equal(typeof fw.agent.runAgent, 'function');
  assert.equal(typeof fw.mcp.createServer, 'function');
  assert.equal(fw.adapters.list().length, 9);
  assert.equal(typeof fw.wrapper.addEvidence, 'function');
  assert.equal(typeof fw.evidence.captureFailure, 'function');
  assert.equal(typeof fw.debug.buildDebugOptions, 'function');
  assert.equal(typeof fw.recorder.convert, 'function');

  // entrypoint hands the framework object to main
  let seen;
  const io = { stderr: { write() {} }, setExitCode() {} };
  assert.equal(await fw.wrapper.entrypoint(async (f) => { seen = f; }, io), 0);
  assert.equal(seen, fw);
});

test('package.json contract: name, engines, single runtime dependency, bins exist', () => {
  const pkg = require('../package.json');
  assert.equal(pkg.name, 'execution-framework');
  assert.equal(pkg.engines.node, '>=18');
  assert.deepEqual(Object.keys(pkg.dependencies), ['playwright-core']);
  for (const bin of Object.values(pkg.bin)) assert.ok(fs.existsSync(path.join(root, bin)), bin);
  assert.ok(fs.readFileSync(path.join(root, '.npmrc'), 'utf8').includes('playwright_skip_browser_download=1'));
});

// ---- cdp snapshot ----------------------------------------------------------------------------------
const node = (o) => ({ i: 1, tag: 'button', role: '', type: '', text: '', name: '', selector: '#x', href: '', hasValue: false, checked: null, disabled: false, ...o });

test('snapshot formatting: selectors, redacted values, masked URL', () => {
  const out = cdp.formatSnapshot({
    url: 'https://portal.example.com/orders?token=SECRET123&id=9#frag',
    title: 'Orders',
    nodes: [
      node({ i: 1, tag: 'input', type: 'password', selector: 'input[name="pw"]', hasValue: true, name: 'password' }),
      node({ i: 2, tag: 'a', text: 'Reports', href: '/reports', selector: '#reports' }),
      node({ i: 3, tag: 'input', type: 'checkbox', checked: true, selector: '#c' }),
      node({ i: 4, text: 'Submit', disabled: true, selector: '#s' }),
    ],
  });
  assert.ok(!out.includes('SECRET123'));
  assert.ok(out.startsWith('URL: https://portal.example.com/orders?token=…&id=…\n'));
  assert.match(out, /\[1\] input type=password name="password" value=\[REDACTED\] sel=input\[name="pw"\]/);
  assert.match(out, /\[2\] a "Reports" href=\/reports sel=#reports/);
  assert.match(out, /\[3\] input type=checkbox checked sel=#c/);
  assert.match(out, /\[4\] button "Submit" disabled sel=#s/);
});

test('snapshot is truncated to maxChars with a marker', () => {
  const nodes = Array.from({ length: 200 }, (_, i) => node({ i: i + 1, text: `Button number ${i}`, selector: `#b${i}` }));
  const out = cdp.formatSnapshot({ url: 'https://x.example.com/', title: 't', nodes }, { maxChars: 400 });
  assert.ok(out.length <= 400);
  assert.match(out, /\.\.\. \(\d+ more elements truncated\)/);
});

test('domSnapshot uses page.evaluate with the in-page collector and a node cap', async () => {
  let seenFn;
  let seenArg;
  const page = { evaluate: async (fn, arg) => { seenFn = fn; seenArg = arg; return { url: 'https://a.example.com/', title: 'T', nodes: [node({ text: 'Go', selector: '#go' })] }; } };
  const out = await cdp.domSnapshot(page, { maxNodes: 50 });
  assert.equal(seenFn, cdp.collectNodesInPage);
  assert.equal(seenArg, 50);
  assert.match(out, /\[1\] button "Go" sel=#go/);
  // the collector runs in the browser: it must not close over any Node-side variable
  assert.ok(!/\brequire\b|\bprocess\b|\bBuffer\b/.test(String(cdp.collectNodesInPage)));
});

test('safeUrl masks query values and fragments; tolerates garbage', () => {
  assert.equal(cdp.safeUrl('https://a.example.com/p?x=1&y=2#f'), 'https://a.example.com/p?x=…&y=…');
  assert.equal(cdp.safeUrl('https://a.example.com/p'), 'https://a.example.com/p');
  assert.equal(cdp.safeUrl('not a url?secret=1'), 'not a url');
});

// ---- browser launch options ------------------------------------------------------------------------
test('buildLaunchOptions: Edge channel by default (no browser download), persistent per-automation profile', () => {
  const env = { AUTOMAGIX_HOME: path.join('h', 'amx'), AUTOMAGIX_AUTOMATION: 'auto-a' };
  const { userDataDir, options } = browser.buildLaunchOptions({}, env);
  assert.equal(userDataDir, path.join('h', 'amx', 'browser-profile', 'auto-a'));
  assert.equal(options.channel, 'msedge');
  assert.equal(options.headless, true);
  assert.equal(options.executablePath, undefined);
  const custom = browser.buildLaunchOptions({ headless: false, cdpPort: 9333, args: ['--lang=en'], userDataDir: 'p', executablePath: 'C:\\edge.exe' }, env);
  assert.equal(custom.userDataDir, 'p');
  assert.equal(custom.options.headless, false);
  assert.equal(custom.options.executablePath, 'C:\\edge.exe');
  assert.equal(custom.options.channel, undefined, 'explicit executablePath replaces the channel');
  assert.deepEqual(custom.options.args, ['--lang=en', '--remote-debugging-port=9333']);
});

function fakePlaywright() {
  const calls = [];
  const page = { name: 'page' };
  const sessionLog = [];
  const ctx = {
    pages: () => [page],
    newPage: async () => page,
    browser: () => null,
    close: async () => { calls.push('close'); },
    newCDPSession: async () => ({ send: async (m, p) => { sessionLog.push([m, p]); return { nodes: [] }; }, detach: async () => { calls.push('detach'); } }),
  };
  page.context = () => ctx;
  const browserObj = { contexts: () => [ctx], newContext: async () => ctx, close: async () => { calls.push('browser.close'); } };
  const pw = {
    chromium: {
      launchPersistentContext: async (dir, opts) => { calls.push(['launch', dir, opts]); return ctx; },
      connectOverCDP: async (url) => { calls.push(['connect', url]); return browserObj; },
    },
  };
  return { pw, calls, page, sessionLog };
}

test('launch/attach return {browser, context, page, cdp} and close cleans up', async () => {
  const f = fakePlaywright();
  const s = await browser.launch({ userDataDir: 'prof' }, { playwright: f.pw, env: {} });
  assert.equal(s.page, f.page);
  assert.equal(s.browser, null);
  assert.deepEqual(f.calls[0].slice(0, 2), ['launch', 'prof']);
  assert.equal(f.calls[0][2].channel, 'msedge');
  await s.cdp.clearCookies();
  assert.deepEqual(f.sessionLog[0][0], 'Network.clearBrowserCookies');
  await s.close();
  assert.deepEqual(f.calls.slice(1), ['detach', 'close']);

  const g = fakePlaywright();
  const a = await browser.attach('http://127.0.0.1:9222', { playwright: g.pw });
  assert.deepEqual(g.calls[0], ['connect', 'http://127.0.0.1:9222']);
  assert.equal(a.page, g.page);
  await a.close();
  assert.ok(g.calls.includes('browser.close'));
});

// ---- codegen ---------------------------------------------------------------------------------------
test('buildCodegenArgs: Edge + dedicated profile, target and output options', () => {
  const env = { AUTOMAGIX_HOME: path.join('h', 'amx') };
  assert.deepEqual(buildCodegenArgs(['https://portal.example.com'], env), [
    'codegen', '--channel=msedge', `--user-data-dir=${path.join('h', 'amx', 'browser-profile', 'codegen')}`, '--target=javascript', 'https://portal.example.com']);
  const py = buildCodegenArgs(['--target', 'python', '--out', 'rec.py', '--profile', 'pf', 'https://x'], env);
  assert.deepEqual(py, ['codegen', '--channel=msedge', '--user-data-dir=pf', '--target=python', '--output=rec.py', 'https://x']);
  assert.throws(() => buildCodegenArgs(['--bogus'], env), /unknown option/);
  assert.throws(() => buildCodegenArgs(['--target', 'cobol'], env), /unsupported --target/);
});

// ---- build plan ------------------------------------------------------------------------------------
test('planBuild: ordered, lock-file-only install, pinned node, optional python-embed + tzdata, assertions', () => {
  const plan = planBuild({ srcDir: '/s', outDir: '/o', nodeDir: '/n', pythonEmbedZip: '/p.zip', tzdataWheel: '/t.whl', version: '0.1.0', builtAt: 'now' });
  const ops = plan.steps.map((s) => s.op);
  assert.equal(ops[0], 'rmrf');
  const idx = (pred) => plan.steps.findIndex(pred);
  const npm = idx((s) => s.op === 'run' && s.cmd === 'npm');
  assert.deepEqual(plan.steps[npm].args.slice(0, 2), ['ci', '--omit=dev']);
  assert.ok(plan.steps[npm].args.includes('--ignore-scripts'));
  assert.ok(idx((s) => s.op === 'copy' && s.to.endsWith('package-lock.json')) < npm, 'lock file is copied before npm ci');
  assert.ok(!plan.steps.find((s) => s.to && s.to.endsWith('package-lock.json')).optional, 'lock file is mandatory');
  assert.ok(idx((s) => s.op === 'copy' && s.from === '/n') > npm);
  const unzipPy = idx((s) => s.op === 'unzip' && s.from === '/p.zip');
  const unzipTz = idx((s) => s.op === 'unzip' && s.from === '/t.whl');
  assert.ok(unzipPy >= 0 && unzipTz > unzipPy);
  assert.ok(plan.steps.some((s) => s.op === 'assert-exists' && /node[\\/]node\.exe$/.test(s.path)));
  assert.ok(plan.steps.some((s) => s.op === 'assert-version'));
  assert.equal(plan.steps[plan.steps.length - 1].op, 'write-json');
  assert.deepEqual(plan.steps[plan.steps.length - 1].value, { version: '0.1.0', builtAt: 'now', pythonEmbed: true, tzdata: true });
  assert.throws(() => planBuild({ srcDir: '/s', outDir: '/o', nodeDir: '/n', tzdataWheel: '/t.whl' }), /requires pythonEmbedZip/);
  assert.throws(() => planBuild({ srcDir: '/s', outDir: '/o' }), /nodeDir is required/);
});

test('every shipped file named by the build plan exists in the source tree (except the generated lock file)', () => {
  const { SHIP_FILES } = require('../scripts/build-plan.cjs');
  for (const f of SHIP_FILES) {
    if (f === 'package-lock.json') continue; // generated against the internal mirror: see README "Dependency policy"
    assert.ok(fs.existsSync(path.join(root, f)), f);
  }
});
