'use strict';
/**
 * Native (no DevTools protocol) mode. Managed Edge often has the policy RemoteDebuggingAllowed=0, which makes Playwright
 * unable to drive it ("DevTools remote debugging is disallowed by the system admin"). We respect that policy: the page is
 * opened in the user's normal Edge and the account tile is pressed through Windows UI Automation (scripts/native-click-account.ps1).
 */
const { execFileSync, spawnSync } = require('node:child_process');
const path = require('node:path');

const POLICY_KEYS = ['HKLM\\SOFTWARE\\Policies\\Microsoft\\Edge', 'HKCU\\SOFTWARE\\Policies\\Microsoft\\Edge'];

/** true when an Edge policy disables remote debugging (value 0). Never throws; non-Windows => false. */
function devtoolsBlockedByPolicy(run = execFileSync) {
  if (process.platform !== 'win32') return false;
  for (const key of POLICY_KEYS) {
    try {
      const out = String(run('reg', ['query', key, '/v', 'RemoteDebuggingAllowed'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }));
      if (/RemoteDebuggingAllowed\s+REG_DWORD\s+0x0\b/i.test(out)) return true;
    } catch (_) { /* value not set */ }
  }
  return false;
}

function isDevtoolsPolicyError(err) {
  return /disallowed by the system admin|remote debugging is disabled/i.test(String((err && err.message) || err));
}

/** Parse the single JSON line the script prints. */
function parseResult(stdout) {
  const line = String(stdout).trim().split(/\r?\n/).filter(Boolean).pop() || '';
  try { return JSON.parse(line); } catch (_) { return { state: 'error', clicked: false, detail: line.slice(0, 200) }; }
}

function runNative({ url, account, timeoutS, dryRun }, spawn = spawnSync) {
  const script = path.resolve(__dirname, '..', 'scripts', 'native-click-account.ps1');
  const args = ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script, '-Url', url, '-Account', account, '-TimeoutS', String(timeoutS)];
  if (dryRun) args.push('-DryRun');
  const r = spawn('powershell.exe', args, { encoding: 'utf8', timeout: (timeoutS + 30) * 1000, windowsHide: true });
  if (r.error) throw new Error(`native mode failed to start: ${r.error.message}`);
  return parseResult(r.stdout);
}

module.exports = { devtoolsBlockedByPolicy, isDevtoolsPolicyError, parseResult, runNative };
