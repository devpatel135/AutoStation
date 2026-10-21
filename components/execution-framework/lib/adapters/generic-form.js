'use strict';
/**
 * generic-form: fill a form from a mapping, then verify what actually landed in each field.
 * Pass a resilientPage proxy as `page` to get selector healing for free. Never submits: submitting is a separate,
 * explicit step in the automation (irreversible actions stay visible in code).
 *
 * mapping: [{selector, value, type: 'text'|'select'|'check'|'uncheck', verify=true, sensitive=false, label?}]
 */
const { DataError } = require('../errors');

/** Pure: validate the mapping shape before touching the page. */
function validateMapping(mapping) {
  if (!Array.isArray(mapping) || !mapping.length) throw new DataError('generic-form: mapping must be a non-empty array');
  mapping.forEach((m, i) => {
    if (!m || typeof m.selector !== 'string' || !m.selector) throw new DataError(`generic-form: entry ${i} needs a selector`);
    const t = m.type || 'text';
    if (!['text', 'select', 'check', 'uncheck'].includes(t)) throw new DataError(`generic-form: entry ${i} has unknown type ${t}`);
    if ((t === 'text' || t === 'select') && (m.value === undefined || m.value === null)) throw new DataError(`generic-form: entry ${i} (${m.selector}) has no value`);
  });
}

function labelOf(m) {
  return m.label || m.selector;
}

/** Returns {filled, mismatches:[{field, reason}]}. Throws DataError when verification fails and opts.strict !== false. */
async function fillForm(page, mapping, opts = {}) {
  validateMapping(mapping);
  const mismatches = [];
  let filled = 0;
  for (const m of mapping) {
    const type = m.type || 'text';
    if (type === 'text') await page.fill(m.selector, String(m.value));
    else if (type === 'select') await page.selectOption(m.selector, String(m.value));
    else if (type === 'check') await page.check(m.selector);
    else await page.uncheck(m.selector);
    filled += 1;
    if (m.verify === false) continue;
    const verify = opts.verify || defaultVerify;
    const got = await verify(page, m);
    if (!got.ok) mismatches.push({ field: labelOf(m), reason: m.sensitive ? 'value mismatch (hidden)' : got.reason });
  }
  if (mismatches.length && opts.strict !== false) {
    throw new DataError(`generic-form: ${mismatches.length} field(s) did not take the value: ${mismatches.map((x) => x.field).join(', ')}`);
  }
  return { filled, mismatches };
}

async function defaultVerify(page, m) {
  const raw = page.raw || page;
  const loc = raw.locator(m.selector);
  const type = m.type || 'text';
  if (type === 'text') {
    const v = await loc.inputValue();
    return v === String(m.value) ? { ok: true } : { ok: false, reason: `expected "${m.value}", field holds "${v}"` };
  }
  if (type === 'select') {
    const v = await loc.inputValue();
    return v === String(m.value) ? { ok: true } : { ok: false, reason: `expected option "${m.value}", selected "${v}"` };
  }
  const checked = await loc.isChecked();
  return checked === (type === 'check') ? { ok: true } : { ok: false, reason: `expected ${type}ed` };
}

module.exports = { name: 'generic-form', description: 'Fill a form from a mapping and verify each field (never submits)', fillForm, validateMapping };
