'use strict';
/**
 * Intelligent retry (SPEC 10). Port of wrapper_core/retry.py: deterministic classifier first, LLM only for UNKNOWN errors,
 * fail-safe ESCALATE. Exactly one retry_event per failed attempt, logged once its outcome is known.
 */
const crypto = require('node:crypto');
const path = require('node:path');
const childProcess = require('node:child_process');
const E = require('./errors');
const events = require('./events');
const redact = require('./redact');
const { llmCall } = require('./llm');

const { Category, Escalation } = E;
const SKIPPED = Symbol('SKIPPED');

class RetryPolicy {
  constructor(o = {}) {
    this.maxRetries = o.maxRetries ?? 3;
    this.networkRetryWaitS = o.networkRetryWaitS ?? 60;
    this.backoffMultiplier = o.backoffMultiplier ?? 2.0;
    this.vpnTool = o.vpnTool ?? null;
    this.skipOnDataError = o.skipOnDataError ?? true;
    this.escalateAfterRetries = o.escalateAfterRetries ?? true;
    this.cwd = o.cwd ?? null;
  }

  /** From the manifest's snake_case error_handling block. */
  static fromManifest(block, cwd = null) {
    const b = block || {};
    const pick = (k, camel) => (k in b ? { [camel]: b[k] } : {});
    return new RetryPolicy({
      ...pick('max_retries', 'maxRetries'), ...pick('network_retry_wait_s', 'networkRetryWaitS'),
      ...pick('backoff_multiplier', 'backoffMultiplier'), ...pick('vpn_tool', 'vpnTool'),
      ...pick('skip_on_data_error', 'skipOnDataError'), ...pick('escalate_after_retries', 'escalateAfterRetries'), cwd,
    });
  }

  /** Policy exported by the runner as AUTOMAGIX_ERROR_POLICY. */
  static fromEnv(env = process.env, cwd = process.cwd()) {
    let block = {};
    try {
      block = JSON.parse(env.AUTOMAGIX_ERROR_POLICY || '{}') || {};
    } catch (_) {
      block = {};
    }
    return RetryPolicy.fromManifest(block, cwd);
  }
}

const shortHash = (text) => crypto.createHash('sha256').update(text).digest('hex').slice(0, 12);

function defaultRunVpn(policy) {
  if (!policy.vpnTool) return false;
  const p = path.isAbsolute(policy.vpnTool) ? policy.vpnTool : path.join(policy.cwd || '.', policy.vpnTool);
  const isPs = p.toLowerCase().endsWith('.ps1');
  const cmd = isPs ? 'powershell.exe' : p;
  const args = isPs ? ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', p] : [];
  try {
    return childProcess.spawnSync(cmd, args, { timeout: 300000, windowsHide: true }).status === 0;
  } catch (_) {
    return false;
  }
}

function tracebackTail(err, n = 3) {
  return String((err && err.stack) || err).split('\n').map((s) => s.trim()).filter(Boolean).slice(-n);
}

function defaultSleep(seconds) {
  return new Promise((resolve) => setTimeout(resolve, seconds * 1000));
}

/** LLM fallback for UNKNOWN errors. Returns {category, retry, reasoning} or null (=> ESCALATE). */
async function llmClassify(err, deps) {
  const ctx = JSON.stringify({
    error_type: (err && err.name) || 'Error',
    error_message: String((err && err.message) || err).slice(0, 500),
    traceback_tail: tracebackTail(err),
    run_context: redact.redactedJson(deps.readEvents().slice(-40), 3000).slice(0, 3000),
  });
  const verdict = await deps.llmCall({
    prompt: 'Classify this automation error. Decide whether it is transient (worth retrying) or permanent.\n'
      + 'Return JSON: {"category": "transient|permanent|unknown", "retry": true|false, "reasoning": "one sentence"}',
    context: ctx,
    schema: { category: 'transient|permanent|unknown', retry: 'boolean', reasoning: 'string' },
    caller: 'wrapper-core/retry-classifier',
    template: 'retry-classifier@1',
  });
  return verdict && typeof verdict === 'object' && !Array.isArray(verdict) ? verdict : null;
}

function resolveDeps(opts) {
  const d = opts.deps || {};
  return {
    logEvent: d.logEvent || ((event, data, o) => events.current().append(event, data, o && o.sensitiveKeys)),
    readEvents: d.readEvents || (() => events.current().readEvents()),
    llmCall: d.llmCall || llmCall,
    sleep: d.sleep || defaultSleep,
    runVpn: d.runVpn || defaultRunVpn,
  };
}

/**
 * Run fn applying the policy. Resolves to fn's value or SKIPPED; rejects with Escalation when exhausted.
 * opts: {itemId, authRefresh, useLlm=true, deps:{logEvent, readEvents, llmCall, sleep, runVpn}}
 */
async function runWithRetry(fn, policy, opts = {}) {
  policy = policy || RetryPolicy.fromEnv();
  const deps = resolveDeps(opts);
  const useLlm = opts.useLlm !== false;
  let attempt = 0;
  let authRetried = false;
  let pending = null;

  const flush = (outcome) => {
    if (pending) {
      deps.logEvent('retry_event', { ...pending, outcome });
      pending = null;
    }
  };

  for (;;) {
    attempt += 1;
    try {
      const result = await fn();
      flush('recovered');
      return result;
    } catch (exc) {
      if (exc instanceof Escalation) {
        flush('escalated');
        throw exc;
      }
      flush('failed_again');
      const cat = E.classifyException(exc);
      let source = 'deterministic';
      let reasoning = '';
      let waitS = 0;
      let decision = 'escalate';

      if (cat === Category.LOGIC_ERROR) {
        reasoning = 'same code will fail again - not retrying';
      } else if (cat === Category.DATA_ERROR) {
        if (policy.skipOnDataError) {
          decision = 'skip';
          reasoning = 'bad data - skipping item and continuing';
        } else {
          reasoning = 'data error and skip_on_data_error is false';
        }
      } else if (cat === Category.NETWORK_ERROR) {
        if (attempt <= policy.maxRetries) {
          decision = 'retry';
          waitS = policy.networkRetryWaitS;
          reasoning = 'network error - reconnect, wait, retry';
        } else {
          reasoning = 'network error persisted after retries';
        }
      } else if (cat === Category.AUTH_EXPIRED) {
        if (!authRetried) {
          decision = 'retry';
          reasoning = 'auth expired - refresh once and retry';
        } else {
          reasoning = 'auth still failing after refresh';
        }
      } else if (cat === Category.SYSTEM_UNAVAILABLE) {
        const backoffs = Math.min(3, policy.maxRetries);
        if (attempt <= backoffs) {
          decision = 'retry';
          waitS = policy.networkRetryWaitS * (policy.backoffMultiplier ** (attempt - 1));
          reasoning = 'target system unavailable - exponential backoff';
        } else {
          reasoning = 'system unavailable after backoff attempts (SLOW signal)';
        }
      } else {
        // UNKNOWN: LLM may only ever *allow* a retry; absent/invalid verdict => ESCALATE (fail safe)
        let verdict = null;
        if (useLlm) {
          try {
            verdict = await llmClassify(exc, deps);
          } catch (_) {
            verdict = null;
          }
        }
        const inHash = shortHash(`${exc && exc.name}:${exc && exc.message}`);
        const audit = (dec, why) => deps.logEvent('llm_decision', {
          caller: 'wrapper-core/retry-classifier', template: 'retry-classifier@1', input_hash: inHash,
          decision: dec, reasoning: why, error_type: (exc && exc.name) || 'Error',
        });
        if (verdict && verdict.category === 'transient' && verdict.retry === true && attempt <= policy.maxRetries) {
          decision = 'retry';
          source = 'llm';
          waitS = policy.networkRetryWaitS;
          reasoning = String(verdict.reasoning || '').slice(0, 300);
          audit('retry', reasoning);
        } else if (verdict) {
          source = 'llm';
          reasoning = String(verdict.reasoning || '').slice(0, 300) || 'LLM judged error permanent';
          audit('escalate', reasoning);
        } else {
          source = 'fallback';
          reasoning = 'unknown error and no LLM verdict - escalating (fail safe)';
        }
      }

      pending = {
        attempt, item_id: opts.itemId ?? null, error_type: (exc && exc.name) || 'Error',
        error_message: String((exc && exc.message) || exc).slice(0, 300), category: cat, decision, source, reasoning, wait_s: waitS,
      };
      if (decision === 'skip') {
        flush('skipped');
        return SKIPPED;
      }
      if (decision === 'escalate') {
        flush('escalated');
        throw new Escalation(`${cat}: ${exc && exc.message}`, cat, attempt, exc);
      }

      // remediation before the retry
      if (cat === Category.NETWORK_ERROR) {
        deps.runVpn(policy);
      } else if (cat === Category.AUTH_EXPIRED) {
        authRetried = true;
        if (opts.authRefresh) {
          try {
            await opts.authRefresh();
          } catch (refreshExc) {
            // keep the pending retry_event so the audit trail shows why we stopped
            flush('escalated');
            throw new Escalation(`AUTH_EXPIRED: refresh failed: ${refreshExc && refreshExc.message}`, cat, attempt, exc);
          }
        }
      }
      if (waitS) await deps.sleep(waitS);
    }
  }
}

/** Process one item with retry + telemetry. DATA_ERROR -> skipped (resolves null). Else Escalation after logging failed.
 *  opts.logItem(id, outcome, fields) receives fields.durationMs; the default writes an item_processed event. */
async function runItem(itemId, fn, policy, opts = {}) {
  const deps = resolveDeps(opts);
  const t0 = Date.now();
  const logItem = opts.logItem || ((id, outcome, fields) => {
    const { durationMs, ...rest } = fields;
    deps.logEvent('item_processed', { item_id: String(id), outcome, duration_ms: durationMs, ...rest });
  });
  let out;
  try {
    out = await runWithRetry(fn, policy, { ...opts, itemId: String(itemId) });
  } catch (esc) {
    if (esc instanceof Escalation) {
      logItem(itemId, 'failed', { durationMs: Date.now() - t0, error_category: esc.category, detail: String(esc.message).slice(0, 300) });
    }
    throw esc;
  }
  if (out === SKIPPED) {
    logItem(itemId, 'skipped', { durationMs: Date.now() - t0, reason: 'data_error' });
    return null;
  }
  logItem(itemId, 'success', { durationMs: Date.now() - t0 });
  return out;
}

module.exports = { RetryPolicy, runWithRetry, runItem, SKIPPED, llmClassify, defaultSleep };
