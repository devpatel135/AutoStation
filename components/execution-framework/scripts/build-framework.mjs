#!/usr/bin/env node
// Assemble the shippable execution-framework tree (see build-plan.cjs for the layout and rationale).
//
//   node scripts/build-framework.mjs --out dist --node-dir /artifacts/node-v20-win-x64 \
//        [--python-embed /artifacts/python-3.12-embed-amd64.zip] [--tzdata-wheel /artifacts/tzdata-2025.1-py2.py3-none-any.whl] \
//        [--dry-run]
//
// Then package with automation-builder:
//   python package.py component execution-framework dist/tree --version "$(cat VERSION)" --out dist
//
// Dependency policy: package-lock.json is mandatory and committed; `npm ci` (never `npm install`) is the only way CI
// installs; registry comes from .npmrc (internal mirror). Updating a dependency = a reviewed MR that regenerates the lock
// against the mirror.
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { planBuild } = require('./build-plan.cjs');
const here = path.dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const o = { srcDir: path.join(here, '..'), outDir: 'dist', dryRun: false };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--out') o.outDir = argv[++i];
    else if (a === '--src') o.srcDir = argv[++i];
    else if (a === '--node-dir') o.nodeDir = argv[++i];
    else if (a === '--python-embed') o.pythonEmbedZip = argv[++i];
    else if (a === '--tzdata-wheel') o.tzdataWheel = argv[++i];
    else if (a === '--dry-run') o.dryRun = true;
    else throw new Error(`unknown option ${a}`);
  }
  return o;
}

function run(cmd, args, cwd) {
  const r = spawnSync(cmd, args, { cwd, stdio: 'inherit', shell: process.platform === 'win32' });
  if (r.status !== 0) throw new Error(`${cmd} ${args.join(' ')} failed with status ${r.status}`);
}

function execute(step) {
  switch (step.op) {
    case 'rmrf': fs.rmSync(step.path, { recursive: true, force: true }); break;
    case 'mkdir': fs.mkdirSync(step.path, { recursive: true }); break;
    case 'copy':
      if (!fs.existsSync(step.from)) {
        if (step.optional) break;
        throw new Error(`missing ${step.from}`);
      }
      fs.cpSync(step.from, step.to, { recursive: true });
      break;
    case 'run': run(step.cmd, step.args, step.cwd); break;
    case 'unzip': run('python', ['-m', 'zipfile', '-e', step.from, step.to]); break;
    case 'assert-exists':
      if (!fs.existsSync(step.path)) throw new Error(`expected ${step.path} to exist`);
      break;
    case 'assert-version': {
      const v = fs.readFileSync(step.versionFile, 'utf8').trim();
      const p = JSON.parse(fs.readFileSync(step.packageJson, 'utf8')).version;
      if (v !== p) throw new Error(`VERSION (${v}) != package.json version (${p})`);
      break;
    }
    case 'write-json': fs.writeFileSync(step.path, JSON.stringify(step.value, null, 2)); break;
    default: throw new Error(`unknown step ${step.op}`);
  }
}

const opts = parseArgs(process.argv.slice(2));
opts.version = fs.readFileSync(path.join(opts.srcDir, 'VERSION'), 'utf8').trim();
opts.builtAt = new Date().toISOString();
const plan = planBuild(opts);
for (const [i, s] of plan.steps.entries()) {
  console.log(`[${i + 1}/${plan.steps.length}] ${s.op} ${s.path || s.from || s.cmd || ''}${s.note ? `  # ${s.note}` : ''}`);
  if (!opts.dryRun) execute(s);
}
console.log(opts.dryRun ? 'dry run complete' : `built ${plan.tree}`);
