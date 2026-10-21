'use strict';
/**
 * Schema validation with no dependencies. Mirrors wrapper_core/schema.py:
 *  - JSON-Schema subset: type, properties, required, items, enum, anyOf, minimum, maximum, minLength, maxLength,
 *    additionalProperties, minItems
 *  - "shape" dicts: {vendor: 'string', amount: 'number', tags: ['string'], kind: 'a|b|c', note: 'string?'}
 *  - {type: 'json_object'} = any object
 */

const JSON_TYPES = new Set(['object', 'array', 'string', 'number', 'integer', 'boolean', 'null']);
const JS_KEYWORDS = new Set(['type', 'properties', 'required', 'items', 'enum', 'anyOf', 'minimum', 'maximum', 'minLength',
  'maxLength', 'additionalProperties', 'description', 'title', '$schema', 'default', 'minItems']);

const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

function isJsonSchema(schema) {
  if (!isPlainObject(schema)) return false;
  if ('properties' in schema || '$schema' in schema || 'anyOf' in schema) return true;
  return JSON_TYPES.has(schema.type) && Object.keys(schema).every((k) => JS_KEYWORDS.has(k));
}

function shapeToJs(v) {
  if (isPlainObject(v)) return toJsonSchema(v);
  if (Array.isArray(v)) return { type: 'array', items: v.length ? shapeToJs(v[0]) : {} };
  if (typeof v === 'string') {
    const nullable = v.endsWith('?');
    const t = nullable ? v.slice(0, -1) : v;
    let js;
    if (t.includes('|')) js = { enum: t.split('|') };
    else if (t === 'any') js = {};
    else if (JSON_TYPES.has(t)) js = { type: t };
    else throw new Error(`unknown shape type ${JSON.stringify(v)}`);
    return nullable ? { anyOf: [js, { type: 'null' }] } : js;
  }
  throw new Error(`unsupported shape element ${JSON.stringify(v)}`);
}

function toJsonSchema(schema) {
  if (isJsonSchema(schema)) return schema;
  if (isPlainObject(schema) && Object.keys(schema).length === 1 && schema.type === 'json_object') return { type: 'object' };
  const props = {};
  const required = [];
  for (const [k, v] of Object.entries(schema)) {
    props[k] = shapeToJs(v);
    if (!(typeof v === 'string' && v.endsWith('?'))) required.push(k);
  }
  return { type: 'object', properties: props, required };
}

function describe(schema) {
  return JSON.stringify(toJsonSchema(schema));
}

function typeOk(v, t) {
  switch (t) {
    case 'object': return isPlainObject(v);
    case 'array': return Array.isArray(v);
    case 'string': return typeof v === 'string';
    case 'boolean': return typeof v === 'boolean';
    case 'integer': return Number.isInteger(v);
    case 'number': return typeof v === 'number' && Number.isFinite(v);
    case 'null': return v === null;
    default: return true;
  }
}

function jsonEq(a, b) {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

function check(v, s, path, errs) {
  if (s.anyOf) {
    for (const alt of s.anyOf) {
      const sub = [];
      check(v, alt, path, sub);
      if (!sub.length) return;
    }
    errs.push(`${path}: matches none of the allowed alternatives`);
    return;
  }
  if (s.type !== undefined) {
    const types = Array.isArray(s.type) ? s.type : [s.type];
    if (!types.some((t) => typeOk(v, t))) {
      errs.push(`${path}: expected ${types.join('|')}, got ${v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v}`);
      return;
    }
  }
  if (s.enum && !s.enum.some((e) => jsonEq(e, v))) errs.push(`${path}: ${JSON.stringify(v)} not in ${JSON.stringify(s.enum)}`);
  if (typeof v === 'number') {
    if (s.minimum !== undefined && v < s.minimum) errs.push(`${path}: ${v} < minimum ${s.minimum}`);
    if (s.maximum !== undefined && v > s.maximum) errs.push(`${path}: ${v} > maximum ${s.maximum}`);
  }
  if (typeof v === 'string') {
    if (s.minLength !== undefined && v.length < s.minLength) errs.push(`${path}: shorter than ${s.minLength}`);
    if (s.maxLength !== undefined && v.length > s.maxLength) errs.push(`${path}: longer than ${s.maxLength}`);
  }
  if (isPlainObject(v)) {
    const props = s.properties || {};
    for (const req of s.required || []) if (!(req in v)) errs.push(`${path}: missing required '${req}'`);
    for (const [k, sub] of Object.entries(props)) if (k in v) check(v[k], sub, `${path}.${k}`, errs);
    if (s.additionalProperties === false) {
      for (const k of Object.keys(v)) if (!(k in props)) errs.push(`${path}: unexpected property '${k}'`);
    }
  }
  if (Array.isArray(v)) {
    if (s.minItems !== undefined && v.length < s.minItems) errs.push(`${path}: fewer than ${s.minItems} items`);
    if (s.items) v.forEach((item, i) => check(item, s.items, `${path}[${i}]`, errs));
  }
}

/** Returns {ok, error}. Never throws for bad data. */
function validate(value, schema) {
  const errs = [];
  check(value, toJsonSchema(schema), '$', errs);
  return { ok: errs.length === 0, error: errs.slice(0, 5).join('; ') };
}

function example(schema) {
  return exampleOf(toJsonSchema(schema));
}

function exampleOf(s) {
  if (s.anyOf) return exampleOf(s.anyOf[0]);
  if (s.enum) return s.enum[0];
  const t = Array.isArray(s.type) ? s.type[0] : s.type;
  if (t === 'object' || s.properties) {
    const props = s.properties || {};
    const required = s.required || Object.keys(props);
    const out = {};
    for (const [k, v] of Object.entries(props)) if (required.includes(k)) out[k] = exampleOf(v);
    return out;
  }
  if (t === 'array') return [];
  if (t === 'string') return 'mock';
  if (t === 'integer') return Math.trunc(s.minimum || 0);
  if (t === 'number') return Number(s.minimum || 0);
  if (t === 'boolean') return false;
  return null;
}

module.exports = { isJsonSchema, toJsonSchema, describe, validate, example };
