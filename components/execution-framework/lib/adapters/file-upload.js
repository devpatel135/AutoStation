'use strict';
/** file-upload: setInputFiles with existence/size checks up front, so a missing file is a DATA_ERROR, not a Playwright timeout. */
const nodeFs = require('node:fs');
const path = require('node:path');
const { DataError } = require('../errors');

const DEFAULT_LIMITS = { maxFiles: 20, maxFileBytes: 25 * 1024 * 1024, maxTotalBytes: 100 * 1024 * 1024 };

/** Pure-ish: {ok, errors[], total, files[{path,name,size}]}. */
function validateFiles(files, { fs = nodeFs, limits = DEFAULT_LIMITS } = {}) {
  const list = Array.isArray(files) ? files : [files];
  const errors = [];
  const out = [];
  let total = 0;
  if (list.length === 0 || list.some((f) => !f)) errors.push('no file given');
  if (list.length > limits.maxFiles) errors.push(`too many files (${list.length} > ${limits.maxFiles})`);
  for (const f of list) {
    if (!f) continue;
    let st;
    try {
      st = fs.statSync(f);
    } catch (_) {
      errors.push(`file not found: ${f}`);
      continue;
    }
    if (st.isDirectory && st.isDirectory()) { errors.push(`not a file: ${f}`); continue; }
    if (st.size === 0) errors.push(`file is empty: ${f}`);
    if (st.size > limits.maxFileBytes) errors.push(`file too large (${st.size} > ${limits.maxFileBytes} bytes): ${f}`);
    total += st.size;
    out.push({ path: f, name: path.basename(f), size: st.size });
  }
  if (total > limits.maxTotalBytes) errors.push(`total size too large (${total} > ${limits.maxTotalBytes} bytes)`);
  return { ok: errors.length === 0, errors, total, files: out };
}

/** Upload to an <input type=file>. `target` is a selector string or a Locator. */
async function uploadFiles(page, target, files, opts = {}) {
  const v = validateFiles(files, opts);
  if (!v.ok) throw new DataError(`file-upload: ${v.errors.join('; ')}`);
  const loc = typeof target === 'string' ? (page.raw || page).locator(target) : target;
  await loc.setInputFiles(v.files.map((f) => f.path));
  return { uploaded: v.files.map((f) => f.name), bytes: v.total };
}

/** For pages that only open a native chooser on click: `clickFn` triggers it. */
async function uploadViaChooser(page, clickFn, files, opts = {}) {
  const v = validateFiles(files, opts);
  if (!v.ok) throw new DataError(`file-upload: ${v.errors.join('; ')}`);
  const raw = page.raw || page;
  const [chooser] = await Promise.all([raw.waitForEvent('filechooser', { timeout: opts.timeoutMs || 10000 }), clickFn()]);
  await chooser.setFiles(v.files.map((f) => f.path));
  return { uploaded: v.files.map((f) => f.name), bytes: v.total };
}

module.exports = {
  name: 'file-upload',
  description: 'Upload files with up-front existence/size validation (DataError on bad input)',
  validateFiles, uploadFiles, uploadViaChooser, DEFAULT_LIMITS,
};
