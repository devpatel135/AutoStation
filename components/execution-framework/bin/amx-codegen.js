#!/usr/bin/env node
'use strict';
/**
 * Onboarding kit helper: `playwright codegen` preset that records selectors against the machine's Edge using a
 * dedicated profile (so SSO / cert auth works while recording).
 *
 *   node bin/amx-codegen.js https://portal.internal.example.com [--target python|javascript] [--profile DIR] [--out file]
 */
const path = require('node:path');
const os = require('node:os');
const { spawn } = require('node:child_process');

/** Pure: argv for playwright-core's cli.js. */
function buildCodegenArgs(argv, env = process.env) {
  const opts = { target: 'javascript', channel: 'msedge', profile: null, out: null, url: null };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--target') opts.target = argv[++i];
    else if (a === '--profile') opts.profile = argv[++i];
    else if (a === '--out') opts.out = argv[++i];
    else if (a === '--channel') opts.channel = argv[++i];
    else if (!a.startsWith('--')) opts.url = a;
    else throw new Error(`unknown option ${a}`);
  }
  if (!['javascript', 'python', 'python-async', 'python-pytest', 'csharp'].includes(opts.target)) throw new Error(`unsupported --target ${opts.target}`);
  const home = env.AUTOMAGIX_HOME || path.join(env.LOCALAPPDATA || os.homedir(), 'AUTOMAGIX');
  const profile = opts.profile || path.join(home, 'browser-profile', 'codegen');
  const args = ['codegen', `--channel=${opts.channel}`, `--user-data-dir=${profile}`, `--target=${opts.target}`];
  if (opts.out) args.push(`--output=${opts.out}`);
  if (opts.url) args.push(opts.url);
  return args;
}

function main() {
  let args;
  try {
    args = buildCodegenArgs(process.argv.slice(2));
  } catch (err) {
    process.stderr.write(`${err.message}\nusage: amx-codegen <url> [--target python|javascript] [--profile DIR] [--out file]\n`);
    process.exit(2);
  }
  const cli = path.join(path.dirname(require.resolve('playwright-core/package.json')), 'cli.js');
  const child = spawn(process.execPath, [cli, ...args], { stdio: 'inherit' });
  child.on('exit', (code) => process.exit(code === null ? 1 : code));
}

if (require.main === module) main();
module.exports = { buildCodegenArgs };
