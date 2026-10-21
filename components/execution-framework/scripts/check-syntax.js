'use strict';
/** `node --check` every .js/.cjs/.mjs file in the package (the CI lint step). Exits non-zero on the first syntax error. */
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.join(__dirname, '..');
const skip = new Set(['node_modules', '.git', 'dist', 'build']);

function walk(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (skip.has(e.name)) continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (/\.(c|m)?js$/.test(e.name)) out.push(p);
  }
  return out;
}

let failed = 0;
for (const f of walk(root)) {
  const r = spawnSync(process.execPath, ['--check', f], { encoding: 'utf8' });
  if (r.status !== 0) {
    failed += 1;
    process.stderr.write(`SYNTAX ERROR in ${path.relative(root, f)}\n${r.stderr}\n`);
  }
}
if (failed) process.exit(1);
process.stdout.write('syntax ok\n');
