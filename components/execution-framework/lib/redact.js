'use strict';
/** Redaction before any LLM context is assembled (SPEC 6.1, 9). Mirrors wrapper_core/redact.py. */

const REDACTED = '[REDACTED]';

function redactValue(obj, keys) {
  if (Array.isArray(obj)) return obj.map((v) => redactValue(v, keys));
  if (obj !== null && typeof obj === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(obj)) out[k] = keys.has(k) ? REDACTED : redactValue(v, keys);
    return out;
  }
  return obj;
}

/** Copy of an event with every key in `sensitive_keys` replaced (nested dicts included); drops `sensitive_keys`. */
function redactEvent(event) {
  const out = JSON.parse(JSON.stringify(event));
  const keys = new Set(out.sensitive_keys || []);
  if (keys.size && out.data && typeof out.data === 'object') out.data = redactValue(out.data, keys);
  delete out.sensitive_keys;
  return out;
}

function redactEvents(events) {
  return events.map(redactEvent);
}

function redactedJson(events, maxChars) {
  let text = JSON.stringify(redactEvents(events));
  if (maxChars && text.length > maxChars) {
    const head = Math.floor((maxChars * 2) / 3);
    const tail = maxChars - head - 40;
    text = `${text.slice(0, head)}..."<truncated ${text.length - maxChars} chars>"...${tail > 0 ? text.slice(-tail) : ''}`;
  }
  return text;
}

module.exports = { REDACTED, redactEvent, redactEvents, redactedJson, redactValue };
