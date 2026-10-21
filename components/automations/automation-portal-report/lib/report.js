'use strict';
/** Pure report logic (no browser, no I/O) so it is unit-testable. */

class RowError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RowError';
    this.category = 'DATA_ERROR'; // the framework honours err.category: the item is skipped, the run continues
  }
}

const STATUSES = new Set(['compliant', 'overdue', 'pending', 'exempt']);

/** Validate + normalise one scraped row ({id, control, owner, status, due_date, findings}). Throws RowError on bad data. */
function parseRow(raw) {
  const id = String(raw.id || '').trim();
  if (!id) throw new RowError('missing required field: id');
  const status = String(raw.status || '').trim().toLowerCase();
  if (!STATUSES.has(status)) throw new RowError(`invalid status ${JSON.stringify(raw.status)} for ${id}`);
  const findings = raw.findings === undefined || raw.findings === '' ? 0 : Number(raw.findings);
  if (!Number.isFinite(findings) || findings < 0) throw new RowError(`invalid findings count for ${id}`);
  const due = String(raw.due_date || '').trim();
  if (due && !/^\d{4}-\d{2}-\d{2}$/.test(due)) throw new RowError(`invalid due_date ${JSON.stringify(due)} for ${id}`);
  return { id, control: String(raw.control || '').trim(), owner: String(raw.owner || '').trim(), status, due_date: due || null, findings };
}

function summarize(rows) {
  const byStatus = {};
  let findings = 0;
  for (const r of rows) {
    byStatus[r.status] = (byStatus[r.status] || 0) + 1;
    findings += r.findings;
  }
  const overdue = rows.filter((r) => r.status === 'overdue').map((r) => r.id);
  return { total: rows.length, byStatus, findings, overdue };
}

/** Deterministic narrative used when the LLM is disabled/unavailable (Pattern 2 fallback). */
function narrativeFallback(summary) {
  const parts = [`${summary.total} control(s) reviewed`];
  const bs = Object.entries(summary.byStatus).map(([k, v]) => `${v} ${k}`).join(', ');
  if (bs) parts.push(bs);
  if (summary.overdue.length) parts.push(`overdue: ${summary.overdue.join(', ')}`);
  parts.push(`${summary.findings} open finding(s)`);
  return parts.join('; ') + '.';
}

const NARRATIVE_SCHEMA = { type: 'object', properties: { narrative: { type: 'string', maxLength: 600 }, escalate: { type: 'array', items: { type: 'string' } } }, required: ['narrative', 'escalate'] };

module.exports = { parseRow, summarize, narrativeFallback, RowError, STATUSES, NARRATIVE_SCHEMA };
