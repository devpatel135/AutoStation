'use strict';
/** table-export-csv: scrape a table (generic-table) and write it as CSV - with formula-injection protection for Excel. */
const nodeFs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const generic = require('./generic-table');

/** A cell starting with = + - @ (or tab/CR) is executed as a formula by Excel; prefix an apostrophe so it stays text. */
function neutralizeFormula(v) {
  const s = String(v === undefined || v === null ? '' : v);
  if (/^[+-]?(\d[\d,. ]*|\.\d+)(e[+-]?\d+)?%?$/i.test(s)) return s; // plain (signed) numbers are not formulas
  return /^[=+\-@\t\r]/.test(s) ? `'${s}` : s;
}

function csvCell(v, delimiter, formulaGuard) {
  let s = formulaGuard ? neutralizeFormula(v) : String(v === undefined || v === null ? '' : v);
  if (s.includes('"') || s.includes(delimiter) || s.includes('\n') || s.includes('\r')) s = `"${s.replace(/"/g, '""')}"`;
  return s;
}

/**
 * toCsv(rows, {columns, delimiter=',', bom=false, formulaGuard=true, eol='\r\n'}) -> string. Columns default to the union of
 * keys in first-seen order. Numbers are written as-is (the guard only touches strings that LOOK like formulas, so "-5" stays a
 * number only when passed as a number).
 */
function toCsv(rows, opts = {}) {
  const { delimiter = ',', bom = false, formulaGuard = true, eol = '\r\n' } = opts;
  const columns = opts.columns || [...rows.reduce((set, r) => { Object.keys(r).forEach((k) => set.add(k)); return set; }, new Set())];
  const cell = (v) => (typeof v === 'number' && Number.isFinite(v) ? String(v) : csvCell(v, delimiter, formulaGuard));
  const lines = [columns.map((c) => csvCell(c, delimiter, false)).join(delimiter)];
  for (const r of rows) lines.push(columns.map((c) => cell(r[c])).join(delimiter));
  return (bom ? '﻿' : '') + lines.join(eol) + eol;
}

/** exportTable(page, scrapeOpts, destPath, {fs, ...csvOpts}) -> {path, rows, pages, bytes, sha256}. */
async function exportTable(page, scrapeOpts, destPath, opts = {}) {
  const fs = opts.fs || nodeFs;
  const scrape = opts.scrape || generic.scrapeTable;
  const { rows, pages } = await scrape(page.raw || page, scrapeOpts || {});
  const csv = toCsv(rows, opts);
  fs.mkdirSync(path.dirname(destPath), { recursive: true });
  fs.writeFileSync(destPath, csv, 'utf8');
  return { path: destPath, rows: rows.length, pages, bytes: Buffer.byteLength(csv, 'utf8'), sha256: crypto.createHash('sha256').update(csv).digest('hex') };
}

module.exports = {
  name: 'table-export-csv',
  description: 'Scrape a table and write CSV (Excel formula-injection safe)',
  toCsv, exportTable, neutralizeFormula,
};
