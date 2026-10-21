'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const schema = require('../lib/schema');
const redact = require('../lib/redact');

// Mirrors tests in wrapper-core/tests/test_core.py::test_schema_validation (same rows, same expectations).
const ROWS = [
  [{ vendor: 'string', amount: 'number' }, { vendor: 'x', amount: 1.5 }, true],
  [{ vendor: 'string', amount: 'number' }, { vendor: 'x', amount: '1' }, false],
  [{ kind: 'a|b' }, { kind: 'c' }, false],
  [{ kind: 'a|b' }, { kind: 'a' }, true],
  [{ tags: ['string'] }, { tags: ['x', 2] }, false],
  [{ tags: ['string'] }, { tags: ['x', 'y'] }, true],
  [{ note: 'string?' }, { note: null }, true],
  [{ note: 'string?' }, {}, true],
  [{ type: 'string', vendor: 'string' }, { type: 'invoice', vendor: 'v' }, true], // shape with a field literally named "type"
  [{ type: 'object', properties: { n: { type: 'integer', minimum: 1 } }, required: ['n'] }, { n: 0 }, false],
  [{ type: 'object', properties: { n: { type: 'integer', minimum: 1 } }, required: ['n'] }, { n: 2 }, true],
  [{ type: 'json_object' }, { anything: 1 }, true],
  [{ type: 'json_object' }, [1], false],
  [{ vendor: 'string' }, {}, false],
  [{ o: { p: 'boolean' } }, { o: { p: 'yes' } }, false],
  [{ type: 'object', properties: { a: { type: 'string' } }, additionalProperties: false }, { a: 'x', b: 1 }, false],
  [{ anyOf: [{ type: 'string' }, { type: 'null' }] }, null, true],
  [{ type: 'array', items: { type: 'integer' }, minItems: 2 }, [1], false],
];

ROWS.forEach(([sch, value, ok], i) => {
  test(`schema.validate row ${i}: ${JSON.stringify(value)} ${ok ? 'valid' : 'invalid'}`, () => {
    assert.equal(schema.validate(value, sch).ok, ok);
  });
});

test('booleans are not numbers/integers; NaN is not a number; integers are numbers', () => {
  assert.equal(schema.validate(true, { type: 'number' }).ok, false);
  assert.equal(schema.validate(1.5, { type: 'integer' }).ok, false);
  assert.equal(schema.validate(2, { type: 'number' }).ok, true);
  assert.equal(schema.validate(NaN, { type: 'number' }).ok, false);
});

test('shape -> JSON schema conversion matches the Python converter', () => {
  const js = schema.toJsonSchema({ vendor: 'string', n: 'integer', k: 'x|y', t: ['string'], note: 'string?' });
  assert.deepEqual(js.required, ['vendor', 'n', 'k', 't']);
  assert.deepEqual(js.properties.k, { enum: ['x', 'y'] });
  assert.deepEqual(js.properties.t, { type: 'array', items: { type: 'string' } });
  assert.deepEqual(js.properties.note, { anyOf: [{ type: 'string' }, { type: 'null' }] });
  assert.throws(() => schema.toJsonSchema({ a: 'strng' }), /unknown shape type/);
});

test('example() always produces a valid instance', () => {
  const sh = { vendor: 'string', n: 'integer', k: 'x|y', o: { p: 'boolean' }, f: 'number' };
  assert.equal(schema.validate(schema.example(sh), sh).ok, true);
});

test('validation error text is bounded and names the path', () => {
  const r = schema.validate({ vendor: 5 }, { vendor: 'string' });
  assert.equal(r.ok, false);
  assert.match(r.error, /\$\.vendor: expected string, got number/);
});

test('redactEvent replaces sensitive keys at any depth and drops sensitive_keys', () => {
  const ev = { event: 'x', data: { customer: { ssn: '123', name: 'n' }, ssn: '9', list: [{ ssn: '1' }] }, sensitive_keys: ['ssn'] };
  const out = redact.redactEvent(ev);
  assert.equal(out.data.ssn, redact.REDACTED);
  assert.equal(out.data.customer.ssn, redact.REDACTED);
  assert.equal(out.data.list[0].ssn, redact.REDACTED);
  assert.equal(out.data.customer.name, 'n');
  assert.equal('sensitive_keys' in out, false);
  assert.equal(ev.data.ssn, '9', 'input must not be mutated');
});

test('events without sensitive_keys pass through; redactedJson truncates with a marker', () => {
  const evs = [{ event: 'a', data: { x: 1 } }];
  assert.deepEqual(redact.redactEvents(evs), evs);
  const big = Array.from({ length: 200 }, (_, i) => ({ event: 'e', data: { i, pad: 'x'.repeat(50) } }));
  const j = redact.redactedJson(big, 500);
  assert.ok(j.includes('<truncated'));
  assert.ok(j.length < 700);
});
