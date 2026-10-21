#!/usr/bin/env node
'use strict';
/**
 * amx-record: UiPath-Recorder equivalent. Records a browser session with `playwright codegen` against the user's Edge
 * profile (so Windows SSO / cert auth works while recording), then converts the recording into an AUTOSTATION payload.
 *
 *   node bin/amx-record.js https://portal.internal.example.com --name automation-foo [--out ./automation-foo]
 *        [--profile edge|chrome|<profile dir>] [--force]
 *   node bin/amx-record.js --from-file recording.js --name automation-foo     # convert an existing codegen file (no browser)
 *
 * Output: main.js, automation.yaml, README.md (recorded steps table + review checklist), params.suggested.yaml,
 * tests/test_lifecycle.py. Credentials are never recorded - see docs/RECORDER.md.
 */
const nodeFs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { buildCodegenArgs } = require('./amx-codegen');
const recorder = require('../lib/recorder');

const USAGE = 'usage: amx-record <url> --name automation-foo [--out dir] [--profile edge|chrome|<dir>] [--force]\n'
  + '       amx-record --from-file <codegen.js> --name automation-foo [--out dir] [--url <recorded url>] [--force]\n';

/** Pure argv parser. Throws Error with a user-facing message on bad input. */
function parseArgs(argv) {
  const o = { url: null, name: null, out: null, profile: null, channel: 'msedge', force: false, fromFile: null, help: false };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    const val = () => {
      if (i + 1 >= argv.length) throw new Error(`${a} needs a value`);
      i += 1;
      return argv[i];
    };
    if (a === '--name') o.name = val();
    else if (a === '--out') o.out = val();
    else if (a === '--profile') o.profile = val();
    else if (a === '--channel') o.channel = val();
    else if (a === '--from-file') o.fromFile = val();
    else if (a === '--url') o.url = val();
    else if (a === '--force') o.force = true;
    else if (a === '--help' || a === '-h') o.help = true;
    else if (!a.startsWith('--') && !o.url) o.url = a;
    else throw new Error(`unknown option ${a}`);
  }
  if (o.help) return o;
  if (!o.name) throw new Error('--name automation-<kebab-name> is required');
  if (!/^automation-[a-z0-9]+(?:-[a-z0-9]+)*$/.test(o.name)) throw new Error(`--name must look like automation-some-name (got ${o.name})`);
  if (!o.url && !o.fromFile) throw new Error('give the start URL to record, or --from-file <codegen.js>');
  if (o.url && !/^https?:\/\//i.test(o.url)) throw new Error('the URL must start with http:// or https://');
  if (o.profile) {
    const p = o.profile.toLowerCase();
    if (p === 'edge') o.channel = 'msedge';
    else if (p === 'chrome') o.channel = 'chrome';
    else if (!/[\\/]/.test(o.profile)) throw new Error('--profile must be edge, chrome, or a profile directory path');
    else o.profileDir = o.profile;
  }
  if (!o.out) o.out = path.join('.', o.name);
  return o;
}

/** Pure: the playwright codegen argv for this recording. */
function buildRecordArgs(opts, codegenOut, env = process.env) {
  const argv = [opts.url, '--target', 'javascript', '--out', codegenOut, '--channel', opts.channel];
  if (opts.profileDir) argv.push('--profile', opts.profileDir);
  return buildCodegenArgs(argv, env);
}

function defaultSpawnCodegen(args) {
  return new Promise((resolve, reject) => {
    let cli;
    try {
      cli = path.join(path.dirname(require.resolve('playwright-core/package.json')), 'cli.js');
    } catch (err) {
      reject(new Error('playwright-core is not installed (npm ci --omit=dev in execution-framework)'));
      return;
    }
    const child = spawn(process.execPath, [cli, ...args], { stdio: 'inherit' });
    child.on('error', reject);
    child.on('exit', (code) => resolve(code === null ? 1 : code));
  });
}

/** Run the whole flow. deps are injectable: {fs, spawnCodegen, stdout, stderr, env, now, tmpdir}. Resolves to an exit code. */
async function run(argv, deps = {}) {
  const fs = deps.fs || nodeFs;
  const stdout = deps.stdout || process.stdout;
  const stderr = deps.stderr || process.stderr;
  const env = deps.env || process.env;
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (err) {
    stderr.write(`${err.message}\n${USAGE}`);
    return 2;
  }
  if (opts.help) { stdout.write(USAGE); return 0; }

  let text;
  let tmp = null;
  try {
    if (opts.fromFile) {
      text = fs.readFileSync(opts.fromFile, 'utf8');
    } else {
      tmp = path.join(deps.tmpdir || os.tmpdir(), `amx-record-${process.pid}-${Date.now()}.js`);
      const args = buildRecordArgs(opts, tmp, env);
      stdout.write(`Recording ${opts.url} - interact with the page, then close the browser window to finish.\n`);
      const code = await (deps.spawnCodegen || defaultSpawnCodegen)(args);
      if (code !== 0) { stderr.write(`codegen exited with code ${code}\n`); return 1; }
      if (!fs.existsSync(tmp)) { stderr.write('nothing was recorded (no codegen output file)\n'); return 1; }
      text = fs.readFileSync(tmp, 'utf8');
    }
    const result = recorder.convert(text, { name: opts.name, url: opts.url || undefined, now: deps.now });
    const written = recorder.writeProject(result.files, opts.out, { fs, force: opts.force });
    stdout.write(`\nConverted ${result.total} step(s) -> ${opts.out}\n`);
    for (const f of written) stdout.write(`  ${f}\n`);
    if (result.params.length) stdout.write(`Suggested params: ${result.params.map((p) => (p.sensitive ? `${p.name} (secret, not recorded)` : p.name)).join(', ')}\n`);
    for (const w of result.warnings) stdout.write(`warning: ${w}\n`);
    stdout.write(`\nNext: review README.md, then  python -m wrapper_core dev ${opts.out}\n`);
    return 0;
  } catch (err) {
    stderr.write(`amx-record failed: ${err.message}\n`);
    return 1;
  } finally {
    if (tmp) { try { fs.unlinkSync(tmp); } catch (_) { /* already gone */ } }
  }
}

if (require.main === module) {
  run(process.argv.slice(2)).then((code) => process.exit(code));
}

module.exports = { parseArgs, buildRecordArgs, run, USAGE };
