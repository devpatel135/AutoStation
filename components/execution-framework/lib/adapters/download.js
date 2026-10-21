'use strict';
/** download: trigger a download, wait for it, verify size/hash, move it to the target dir. */
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { DataError, SystemUnavailable } = require('../errors');

/** Pure: strip path separators / reserved chars so a server-suggested name cannot escape the target dir. */
function safeFilename(name) {
  const base = String(name || 'download.bin').split(/[\\/]/).pop().replace(/[<>:"|?*\u0000-\u001f]/g, '_').replace(/^\.+/, '');
  return (base || 'download.bin').slice(0, 180);
}

function sha256File(file) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash('sha256');
    fs.createReadStream(file).on('data', (d) => h.update(d)).on('end', () => resolve(h.digest('hex'))).on('error', reject);
  });
}

/**
 * trigger: async () => click the thing that starts the download.
 * opts: {dir (required), filename, minBytes=1, sha256, timeoutMs=60000}
 * Resolves {path, size, sha256, suggested}.
 */
async function waitForDownload(page, trigger, opts = {}) {
  if (!opts.dir) throw new DataError('download: opts.dir is required');
  const timeout = opts.timeoutMs || 60000;
  const [dl] = await Promise.all([page.waitForEvent('download', { timeout }), trigger()]);
  const failure = await dl.failure();
  if (failure) throw new SystemUnavailable(`download failed: ${failure}`);
  const suggested = dl.suggestedFilename();
  const name = safeFilename(opts.filename || suggested);
  fs.mkdirSync(opts.dir, { recursive: true });
  const dest = path.join(opts.dir, name);
  const tmp = `${dest}.part`;
  await dl.saveAs(tmp);
  const size = fs.statSync(tmp).size;
  if (size < (opts.minBytes || 1)) {
    fs.rmSync(tmp, { force: true });
    throw new DataError(`download ${name} is ${size} bytes (expected at least ${opts.minBytes || 1})`);
  }
  const digest = await sha256File(tmp);
  if (opts.sha256 && opts.sha256.toLowerCase() !== digest) {
    fs.rmSync(tmp, { force: true });
    throw new DataError(`download ${name} sha256 mismatch`);
  }
  fs.renameSync(tmp, dest);
  return { path: dest, size, sha256: digest, suggested };
}

module.exports = { name: 'download', description: 'Wait for a download, verify size/hash, move to a target dir', waitForDownload, safeFilename, sha256File };
