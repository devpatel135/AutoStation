'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { parseArgs, buildRecordArgs, run } = require('../bin/amx-record');
const { TEST_ROWS, LIB_LOGIN } = require('./fixtures/codegen');
const { withRunDir } = require('./_helpers');

function sink() {
  const chunks = [];
  return { write: (m) => { chunks.push(String(m)); return true; }, text: () => chunks.join('') };
}

test('parseArgs: url + name required, name shape, url scheme, profile forms', () => {
  const ok = parseArgs(['https://portal.example.com', '--name', 'automation-foo']);
  assert.deepEqual([ok.url, ok.name, ok.channel, ok.out, ok.force], ['https://portal.example.com', 'automation-foo', 'msedge', path.join('.', 'automation-foo'), false]);
  assert.equal(parseArgs(['https://x.test', '--name', 'automation-a', '--profile', 'chrome']).channel, 'chrome');
  assert.equal(parseArgs(['https://x.test', '--name', 'automation-a', '--profile', 'edge']).channel, 'msedge');
  assert.equal(parseArgs(['https://x.test', '--name', 'automation-a', '--profile', 'C:\\profiles\\amx']).profileDir, 'C:\\profiles\\amx');
  assert.equal(parseArgs(['--from-file', 'rec.js', '--name', 'automation-a']).fromFile, 'rec.js');
  assert.equal(parseArgs(['--help']).help, true);
  assert.throws(() => parseArgs(['https://x.test']), /--name .* is required/);
  assert.throws(() => parseArgs(['https://x.test', '--name', 'Foo']), /must look like automation-/);
  assert.throws(() => parseArgs(['--name', 'automation-a']), /give the start URL/);
  assert.throws(() => parseArgs(['ftp://x', '--name', 'automation-a']), /http:\/\/ or https:\/\//);
  assert.throws(() => parseArgs(['https://x.test', '--name', 'automation-a', '--profile', 'firefox']), /--profile must be/);
  assert.throws(() => parseArgs(['https://x.test', '--name']), /needs a value/);
  assert.throws(() => parseArgs(['https://x.test', '--name', 'automation-a', '--bogus']), /unknown option/);
});

test('buildRecordArgs: codegen against the user\'s Edge profile, javascript target, output file', () => {
  const o = parseArgs(['https://portal.example.com', '--name', 'automation-foo']);
  const args = buildRecordArgs(o, '/tmp/rec.js', { AUTOMAGIX_HOME: 'h' });
  assert.deepEqual(args, ['codegen', '--channel=msedge', `--user-data-dir=${path.join('h', 'browser-profile', 'codegen')}`, '--target=javascript', '--output=/tmp/rec.js', 'https://portal.example.com']);
  const o2 = parseArgs(['https://p.test', '--name', 'automation-foo', '--profile', 'D:\\prof']);
  assert.ok(buildRecordArgs(o2, 'r.js', {}).includes('--user-data-dir=D:\\prof'));
});

test('run: records via codegen (injected), converts, writes the project, prints warnings and next steps', async (t) => {
  const r = withRunDir(t);
  const out = path.join(r.dir, 'automation-approve');
  const so = sink();
  const se = sink();
  let seen;
  const code = await run(['https://erp.example.com/invoices', '--name', 'automation-approve', '--out', out], {
    fs, stdout: so, stderr: se, tmpdir: r.dir, env: { AUTOMAGIX_HOME: r.home },
    spawnCodegen: async (args) => { seen = args; fs.writeFileSync(args.find((a) => a.startsWith('--output=')).slice('--output='.length), TEST_ROWS); return 0; },
  });
  assert.equal(code, 0, se.text());
  assert.ok(seen.includes('--target=javascript') && seen.includes('--channel=msedge'));
  assert.ok(fs.readFileSync(path.join(out, 'main.js'), 'utf8').includes('ROWS_1'));
  assert.ok(fs.existsSync(path.join(out, 'README.md')) && fs.existsSync(path.join(out, 'params.suggested.yaml')) && fs.existsSync(path.join(out, 'tests', 'test_lifecycle.py')));
  assert.match(so.text(), /Converted 4 step\(s\)/);
  assert.match(so.text(), /Next: review README\.md, then\s+python -m wrapper_core dev/);
  assert.equal(fs.readdirSync(r.dir).some((n) => n.startsWith('amx-record-')), false, 'temporary codegen output removed');
});

test('run: credentials in the recording are reported, never written', async (t) => {
  const r = withRunDir(t);
  const out = path.join(r.dir, 'automation-login');
  const so = sink();
  const code = await run(['https://portal.example.com', '--name', 'automation-login', '--out', out], {
    fs, stdout: so, stderr: sink(), tmpdir: r.dir, env: { AUTOMAGIX_HOME: r.home },
    spawnCodegen: async (args) => { fs.writeFileSync(args.find((a) => a.startsWith('--output=')).slice(9), LIB_LOGIN); return 0; },
  });
  assert.equal(code, 0);
  assert.match(so.text(), /warning: line \d+: ".*Password.*" looks like a credential\/secret - NOT recorded/);
  assert.match(so.text(), /password \(secret, not recorded\)/);
  const all = ['main.js', 'README.md', 'params.suggested.yaml'].map((f) => fs.readFileSync(path.join(out, f), 'utf8')).join('\n');
  assert.ok(!all.includes('S3cret!pass'));
});

test('run --from-file converts an existing recording without launching a browser', async (t) => {
  const r = withRunDir(t);
  const rec = path.join(r.dir, 'rec.js');
  fs.writeFileSync(rec, TEST_ROWS);
  const out = path.join(r.dir, 'p');
  let launched = false;
  const code = await run(['--from-file', rec, '--name', 'automation-from-file', '--out', out], { fs, stdout: sink(), stderr: sink(), spawnCodegen: async () => { launched = true; return 0; } });
  assert.equal(code, 0);
  assert.equal(launched, false);
  assert.ok(fs.readFileSync(path.join(out, 'main.js'), 'utf8').includes("https://erp.example.com/invoices"));
});

test('run: failure modes return non-zero with a message (bad args, codegen failure, nothing recorded, non-empty dir)', async (t) => {
  const r = withRunDir(t);
  const base = { fs, tmpdir: r.dir, env: { AUTOMAGIX_HOME: r.home } };
  let se = sink();
  assert.equal(await run(['https://x.test'], { ...base, stdout: sink(), stderr: se }), 2);
  assert.match(se.text(), /usage: amx-record/);

  se = sink();
  assert.equal(await run(['https://x.test', '--name', 'automation-a', '--out', path.join(r.dir, 'o1')], { ...base, stdout: sink(), stderr: se, spawnCodegen: async () => 3 }), 1);
  assert.match(se.text(), /codegen exited with code 3/);

  se = sink();
  assert.equal(await run(['https://x.test', '--name', 'automation-a', '--out', path.join(r.dir, 'o2')], { ...base, stdout: sink(), stderr: se, spawnCodegen: async () => 0 }), 1);
  assert.match(se.text(), /nothing was recorded/);

  se = sink();
  assert.equal(await run(['https://x.test', '--name', 'automation-a', '--out', path.join(r.dir, 'o3')], { ...base, stdout: sink(), stderr: se, spawnCodegen: async (a) => { fs.writeFileSync(a.find((x) => x.startsWith('--output=')).slice(9), '// empty\n'); return 0; } }), 1);
  assert.match(se.text(), /no recorded actions/);

  const full = path.join(r.dir, 'full');
  fs.mkdirSync(full, { recursive: true });
  fs.writeFileSync(path.join(full, 'keep.txt'), 'x');
  const rec = path.join(r.dir, 'rec2.js');
  fs.writeFileSync(rec, TEST_ROWS);
  se = sink();
  assert.equal(await run(['--from-file', rec, '--name', 'automation-a', '--out', full], { ...base, stdout: sink(), stderr: se }), 1);
  assert.match(se.text(), /not empty/);
  assert.equal(await run(['--from-file', rec, '--name', 'automation-a', '--out', full, '--force'], { ...base, stdout: sink(), stderr: sink() }), 0);
});
