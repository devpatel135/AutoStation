'use strict';
/**
 * Run context: the per-run directory and the append-only events.jsonl (SPEC 6.1).
 * Byte-compatible with wrapper_core/runctx.py: {"ts","event","data"[, "sensitive_keys" (sorted, unique)]}, compact JSON, '\n' endings.
 */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function amxHome(env = process.env) {
  if (env.AUTOMAGIX_HOME) return env.AUTOMAGIX_HOME;
  if (env.LOCALAPPDATA) return path.join(env.LOCALAPPDATA, 'AUTOMAGIX');
  return path.join(os.homedir(), '.automagix');
}

class RunContext {
  constructor(runDir) {
    this.runDir = runDir;
    fs.mkdirSync(runDir, { recursive: true });
    this.eventsPath = path.join(runDir, 'events.jsonl');
  }

  append(event, data, sensitiveKeys) {
    const rec = { ts: new Date().toISOString(), event, data: data || {} };
    if (sensitiveKeys && sensitiveKeys.length) rec.sensitive_keys = [...new Set(sensitiveKeys)].sort();
    fs.appendFileSync(this.eventsPath, `${JSON.stringify(rec)}\n`, 'utf8');
    return rec;
  }

  readEvents() {
    let text;
    try {
      text = fs.readFileSync(this.eventsPath, 'utf8');
    } catch (_) {
      return [];
    }
    const out = [];
    for (const line of text.split('\n')) {
      if (!line.trim()) continue;
      try {
        out.push(JSON.parse(line));
      } catch (_) {
        out.push({ ts: new Date().toISOString(), event: 'corrupt_event_line', data: { line: line.slice(0, 200) } });
      }
    }
    return out;
  }

  writeResult(obj) {
    fs.writeFileSync(path.join(this.runDir, 'result.json'), JSON.stringify(obj), 'utf8');
  }

  writeError(obj) {
    fs.writeFileSync(path.join(this.runDir, 'error.json'), JSON.stringify(obj), 'utf8');
  }

  readError() {
    try {
      return JSON.parse(fs.readFileSync(path.join(this.runDir, 'error.json'), 'utf8'));
    } catch (_) {
      return null;
    }
  }
}

const cache = new Map();

/** The active run context: AUTOMAGIX_RUN_DIR, else ./.amx-dev-run/ (dev/tests). */
function current(env = process.env) {
  const dir = env.AUTOMAGIX_RUN_DIR || path.join(process.cwd(), '.amx-dev-run');
  if (!cache.has(dir)) cache.set(dir, new RunContext(dir));
  return cache.get(dir);
}

function resetCache() {
  cache.clear();
}

module.exports = { RunContext, current, resetCache, amxHome };
