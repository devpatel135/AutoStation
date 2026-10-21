'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { parseRow, summarize, narrativeFallback, RowError } = require('../lib/report');
const errors = require('../../../execution-framework/lib/errors');

const good = { id: 'C-1', control: 'Access review', owner: 'it@x', status: 'Overdue', due_date: '2026-09-30', findings: '2' };

test('parseRow normalises a valid row', () => {
  assert.deepEqual(parseRow(good), { id: 'C-1', control: 'Access review', owner: 'it@x', status: 'overdue', due_date: '2026-09-30', findings: 2 });
});

test('parseRow rejects bad data with a DATA_ERROR-classified error', () => {
  for (const bad of [{ ...good, id: '' }, { ...good, status: 'weird' }, { ...good, findings: '-1' }, { ...good, due_date: '30/09/2026' }]) {
    assert.throws(() => parseRow(bad), RowError);
    try { parseRow(bad); } catch (e) { assert.equal(errors.classifyException(e), errors.Category.DATA_ERROR); }
  }
});

test('summarize + fallback narrative', () => {
  const rows = [parseRow(good), parseRow({ ...good, id: 'C-2', status: 'compliant', findings: '0' })];
  const s = summarize(rows);
  assert.equal(s.total, 2);
  assert.deepEqual(s.overdue, ['C-1']);
  assert.equal(s.findings, 2);
  assert.match(narrativeFallback(s), /2 control\(s\) reviewed.*overdue: C-1.*2 open finding/);
});
