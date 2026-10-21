'use strict';
/**
 * llmCall - client for the wrapper-core loopback broker (SPEC 8). The framework NEVER holds an LLM key:
 * the runner owns auth, safeguards (anti-injection system prompt, untrusted-context delimiting, schema retry,
 * classification gate) and call logging. This client only POSTs {prompt, context, schema, ...}.
 *
 * Contract: returns the parsed result, or null on ANY failure. Never throws. Callers must fail safe on null.
 */
const events = require('./events');
const schemaLib = require('./schema');

async function llmCall(opts = {}, deps = {}) {
  const env = deps.env || process.env;
  const fetchFn = deps.fetch || globalThis.fetch;
  const log = deps.logEvent || ((event, data, o) => events.current(env).append(event, data, o && o.sensitiveKeys));
  const {
    prompt = '', context = '', schema = null, caller = 'unknown', template = null, classification = null, timeoutS = 30,
    decisionLog = false,
  } = opts;
  const url = env.AUTOMAGIX_BROKER_URL;
  try {
    if (!url) {
      log('llm_call', { caller, status: 'disabled', detail: 'no AUTOMAGIX_BROKER_URL (not started by the wrapper-core runner)' });
      return null;
    }
    if (typeof fetchFn !== 'function') {
      log('llm_call', { caller, status: 'error', detail: 'global fetch unavailable (Node >= 18 required)' });
      return null;
    }
    const body = {
      prompt: String(prompt),
      context: String(context || ''),
      schema: schema ? schemaLib.toJsonSchema(schema) : null,
      caller,
      template,
      timeout_s: timeoutS,
      classification,
      inject_context: null,
      decision_log: Boolean(decisionLog),
    };
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), (timeoutS + 15) * 1000);
    let out;
    try {
      const resp = await fetchFn(`${url.replace(/\/+$/, '')}/llm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${env.AUTOMAGIX_BROKER_TOKEN || ''}` },
        body: JSON.stringify(body),
        signal: ctl.signal,
      });
      out = await resp.json();
    } finally {
      clearTimeout(timer);
    }
    if (!out || out.status !== 'success' || out.result === undefined || out.result === null) return null;
    if (schema) {
      const v = schemaLib.validate(out.result, schema);
      if (!v.ok) {
        log('llm_schema_failure', { caller, error: v.error.slice(0, 300) });
        return null;
      }
    }
    return out.result;
  } catch (exc) {
    try {
      log('llm_call', { caller, status: 'error', detail: `broker unreachable: ${exc && exc.message}`.slice(0, 200) });
    } catch (_) { /* logging must never throw */ }
    return null;
  }
}

module.exports = { llmCall };
