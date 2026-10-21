'use strict';
/**
 * Pure planner for assembling the shippable execution-framework tree. build-framework.mjs executes the plan;
 * tests assert it. Keeping this pure means the CI assembly logic is reviewable and testable without Node-on-Windows.
 *
 * Shipped tree layout (this becomes execution-framework-<version>.zip, installed at %LOCALAPPDATA%\AUTOMAGIX\execution-framework):
 *   index.js lib/ bin/ scripts/ package.json package-lock.json VERSION README.md .npmrc
 *   node_modules/            (npm ci --omit=dev from the INTERNAL mirror)
 *   node/node.exe            (pinned Node LTS, from the internal artifact store - never downloaded from the internet)
 *   python-embed/ (optional) (embeddable CPython for wrapper-core + tzdata, so manifest timezones work on stock Windows)
 */
const path = require('node:path');

const SHIP_FILES = ['index.js', 'lib', 'bin', 'scripts', 'package.json', 'package-lock.json', 'VERSION', 'README.md', '.npmrc'];

function planBuild(o) {
  const required = ['srcDir', 'outDir', 'nodeDir'];
  for (const k of required) if (!o[k]) throw new Error(`planBuild: ${k} is required`);
  const tree = path.join(o.outDir, 'tree');
  const steps = [];
  steps.push({ op: 'rmrf', path: tree });
  steps.push({ op: 'mkdir', path: tree });
  for (const f of SHIP_FILES) steps.push({ op: 'copy', from: path.join(o.srcDir, f), to: path.join(tree, f), optional: f === '.npmrc' });
  steps.push({ op: 'run', cwd: tree, cmd: 'npm', args: ['ci', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'], note: 'installs ONLY from package-lock.json via the internal mirror in .npmrc' });
  steps.push({ op: 'copy', from: o.nodeDir, to: path.join(tree, 'node'), note: 'pinned Node LTS (must contain node.exe)' });
  if (o.pythonEmbedZip) {
    steps.push({ op: 'unzip', from: o.pythonEmbedZip, to: path.join(tree, 'python-embed') });
    if (o.tzdataWheel) steps.push({ op: 'unzip', from: o.tzdataWheel, to: path.join(tree, 'python-embed', 'Lib', 'site-packages'), note: 'tzdata wheel is a zip' });
  } else if (o.tzdataWheel) {
    throw new Error('planBuild: tzdataWheel requires pythonEmbedZip');
  }
  steps.push({ op: 'assert-exists', path: path.join(tree, 'node', 'node.exe') });
  steps.push({ op: 'assert-exists', path: path.join(tree, 'node_modules', 'playwright-core', 'package.json') });
  steps.push({ op: 'assert-version', versionFile: path.join(tree, 'VERSION'), packageJson: path.join(tree, 'package.json') });
  steps.push({ op: 'write-json', path: path.join(tree, 'BUILD-INFO.json'), value: { version: o.version || null, builtAt: o.builtAt || null, pythonEmbed: Boolean(o.pythonEmbedZip), tzdata: Boolean(o.tzdataWheel) } });
  return { tree, steps };
}

module.exports = { planBuild, SHIP_FILES };
