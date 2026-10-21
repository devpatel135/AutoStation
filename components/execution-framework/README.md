# execution-framework

Shared Node.js runtime for AUTOSTATION automations. It is installed **once per machine** (`%LOCALAPPDATA%\AUTOMAGIX\execution-framework`) and shared by every
automation on that VDI. wrapper-core (Python) owns scheduling, self-heal, auth and logging transport; this package gives automation authors everything they
need to *do the work*: a Node SDK that mirrors `wrapper_core`, a browser stack, an API client, and the LLM-assisted patterns from the architecture assessment.

```js
const fw = require(process.env.AUTOMAGIX_FRAMEWORK_DIR);       // or require('execution-framework') via NODE_PATH

fw.wrapper.entrypoint(async (fw) => {
  const { wrapper, browser, resilience, adapters } = fw;
  const { page, close } = await browser.launch({ headless: true });
  try {
    const rp = resilience.resilientPage(page, { caller: 'invoice-router' });   // Pattern 3: self-healing selectors
    await page.goto('https://portal.internal.example.com/invoices');
    await adapters.get('sso-detect').assertSession(page);                      // AuthExpired if the SSO session lapsed
    const { rows } = await adapters.get('generic-table').scrapeTable(page, { selector: '#invoices', nextSelector: 'a.next' });
    for (const row of rows) await wrapper.runItem(row.invoice_no, () => process(row));   // retry + per-item telemetry
    wrapper.setResult({ processed: rows.length });
  } finally { await close(); }
});
```

## Architecture

```
 Task Scheduler ─▶ wrapper-core (Python)  ──spawns──▶  node automation/main.js
        │            lock · self-heal · kill-switch        │  require(AUTOMAGIX_FRAMEWORK_DIR)
        │            retry (process level) · synopsis      ▼
        │            LLM BROKER 127.0.0.1:<port> ◀───── fw.wrapper.llmCall   (Bearer per-run token, NO key in this process)
        │            log transport (SSH, write-only)       │
        │                                                  ├─ fw.wrapper   events.jsonl / result.json / error.json  (same files wrapper-core reads)
        │                                                  ├─ fw.http      API-first path (client-cert PFX, classified errors)
        │                                                  ├─ fw.browser   playwright-core + system Edge/Chrome, persistent profile
        │                                                  ├─ fw.cdp       DOM snapshot (redaction-aware), CDP session helpers
        │                                                  ├─ fw.resilience  Pattern 3  (LLM selector healing)
        │                                                  ├─ fw.agent       Pattern 4  (goal-driven loop)
        │                                                  ├─ fw.mcp / bin/mcp-browser.js   MCP server for Claude-class clients
        │                                                  └─ fw.adapters    generic-table · generic-form · download · sso-detect
```

Design rules that shape every module:

* **Pure logic + thin I/O shell, dependency-injected.** Decision code (classifier, retry, healing, agent policy, MCP protocol) takes its `llmCall`, `logEvent`, `sleep`, page, and
  transport as arguments, so the tests run against fakes with no browser, network, or Windows.
* **Same wire formats as Python.** `events.jsonl`, `result.json`, `error.json` are byte-compatible with `wrapper_core` (SPEC 6.1) — the runner and the CI analytics do not care which
  language produced them. The classifier mirrors `wrapper_core/retry.py` rule-for-rule and is covered by a parity table in `test/classifier.test.js`.
* **Fail safe.** `llmCall` returns `null` instead of throwing; every caller treats `null` as "stop / escalate", never "proceed".

## The three execution modes

| Mode | Use when | Modules | LLM involvement |
|---|---|---|---|
| **1. Deterministic / headless** (default) | The target has an API or accepts cert auth | `fw.http`, `fw.wrapper` | none (optionally Pattern 1/2 via `llmCall`) |
| **2. Playwright (resilient)** | Browser-only target, stable page structure | `fw.browser`, `fw.resilience`, `fw.adapters` | only when a selector breaks (Pattern 3) |
| **3. MCP / agentic** | Ambiguous, highly variable workflow | `fw.agent`, `bin/mcp-browser.js` | every step (Pattern 4) — last resort |

Prefer 1 over 2 over 3. Determinism is cheap, fast and auditable; the LLM earns its cost only where fixed logic breaks.

## Airgap and the Edge-channel decision

VDIs are airgapped, so nothing is downloaded at runtime or at install time:

* **No bundled browsers.** `playwright-core` (not `playwright`) is the only runtime dependency and `.npmrc` sets `playwright_skip_browser_download=1`.
  `fw.browser.launch` defaults to `channel: 'msedge'`, the Edge that ships with Windows 10/11 and is patched by the fleet's normal update path.
  Pass `executablePath` to pin a different Chromium. The framework size stays small and browser security patches never require an automation release.
* **Persistent per-automation profile** (`%LOCALAPPDATA%\AUTOMAGIX\browser-profile\<automation>`): the automation runs *as the user*, so Windows SSO, the cert store and cookies work
  exactly as they do for the person. No separate credential vault (assessment: user-profile credential model).
* **npm packages only from the internal mirror** (`.npmrc`), installed only via `npm ci` from a committed `package-lock.json`.
* Node itself (`node/node.exe`) and the optional embeddable Python (+ `tzdata`) are copied from the internal artifact store by `scripts/build-framework.mjs`.

### Dependency policy

* `package-lock.json` is **mandatory and committed**; CI runs `npm ci --ignore-scripts` (fails if `package.json` and the lock disagree). Never `npm install` in CI.
* Updating a dependency is a reviewed MR that regenerates the lock against the mirror: `npm install --package-lock-only --ignore-scripts` then commit.
* **The lock file is not in this repository yet** — it must be generated once on a machine with npm and mirror access (Node was not available where this component was authored).
* Pin exact versions (`save-exact=true`). Validate any new package on the internal mirror before adding it; the runtime dependency list is deliberately a single package.

## Security model

| Concern | Control |
|---|---|
| LLM key exposure | The framework never reads a key. `llmCall` POSTs to the wrapper-core loopback broker with a per-run bearer token; the broker holds auth. |
| Prompt injection | Page text, logs and scraped data go only in `llmCall({ context })` (delimited as untrusted by the broker). Instructions (`prompt`) are author-written. Agent goal is the only instruction; tool calls are schema-validated against a closed tool set. |
| Selector-healing abuse | LLM never supplies values; proposed action must match; selector must match exactly one visible element; destructive/payment targets deny-listed (`allowDestructive` opt-in); per-run budget; DOM never logged (template name + input hash only). |
| Agent blast radius | Origin allow-list (default: starting origin only, re-checked after every step), irreversible-action gate (`params.allow_irreversible=true` required for submit/send/delete/pay), step budget, stall detection, fail-safe on LLM unavailability. |
| Sensitive data | `sensitive`/`sensitiveKeys` on `logEvent`/`logItem`: kept in the raw run log, replaced by `[REDACTED]` before any LLM context. DOM snapshots never include form values; URL query values are masked. |
| MCP server | stdio only, no network listener. `navigate` is http(s) only + optional `AMX_MCP_ALLOWED_ORIGINS`. `evaluate` is not even listed unless `AMX_MCP_ALLOW_EVALUATE=1`. |
| HTTP client | Cross-origin request targets rejected (no SSRF-by-parameter). Response bodies never appear in error messages. PFX + passphrase come from `scripts/export-cert.ps1`, which writes an owner-only temp file. |
| Supply chain | One runtime dependency, internal mirror only, lock-file installs, `--ignore-scripts`. |

## Node SDK reference (and Python equivalents)

All of `fw.wrapper` writes into `AUTOMAGIX_RUN_DIR` (falls back to `./.amx-dev-run/` when the runner did not start you).

| Node (`fw.wrapper.*`) | Python (`wrapper_core`) | Notes |
|---|---|---|
| `logEvent(event, data, { sensitive, sensitiveKeys })` | `log_event(event, data, sensitive=, sensitive_keys=)` | `sensitive: true` marks every key |
| `logItem(id, outcome, { durationMs, ...fields })` | `log_item(id, outcome, duration_ms=, **fields)` | outcome `success\|skipped\|failed` |
| `trackItem(id, async (it) => {...}, fields)` | `track_item(id, **fields)` (context manager) | set `it.outcome = 'skipped'` to override |
| `runItem(id, fn, policy?)` | `run_item(id, fn, ...)` | retry + telemetry; DATA_ERROR -> skipped (returns `null`), else `Escalation` |
| `runWithRetry(fn, policy?, opts)` | `run_with_retry(fn, policy, ...)` | one `retry_event` per attempt incl. `outcome`; UNKNOWN -> LLM classifier, fail-safe ESCALATE |
| `RetryPolicy.fromEnv()` | `RetryPolicy.from_env()` | reads `AUTOMAGIX_ERROR_POLICY` (manifest `error_handling`) |
| `llmCall({ prompt, context, schema, caller, template, classification, timeoutS })` | `llm_call(prompt, context, schema, caller=, template=, classification=)` | `schema` accepts a shape (`{vendor:'string'}`), a JSON-Schema subset, or `{type:'json_object'}`; returns result or `null` |
| `setResult(obj)` | `set_result(**kw)` | stored as `result` in the run log |
| `params()` / `param(name, dflt)` | `params()` / `param(name, dflt)` | slot/trigger params (`AUTOMATION_PARAM_*`), keys lower-cased |
| `isDryRun()` | `is_dry_run()` | |
| `assets()` | `assets()` | dynamic config assets for the ring |
| `emitTrigger(target, params, reason)` | `emit_trigger(...)` | event-driven orchestration |
| `entrypoint(async (fw) => {...})` | `@entrypoint` | writes `error.json` {category,type,message,traceback_tail,escalated,attempts}; sets `process.exitCode = 1` on failure |
| `errors.{Category, NetworkError, AuthExpired, SystemUnavailable, DataError, LogicError, Escalation}` | same names in `wrapper_core` | throw these to steer the classifier |

### `fw.http` — headless / API-first

```js
const api = fw.http.client({ baseUrl: 'https://api.internal.example.com', retry: { retries: 3, waitMs: 500 }, pfxPath, passphrase });
const invoices = await api.getJson('/v1/invoices', { query: { status: 'open' } });
```
Non-2xx responses throw `HttpError` already classified (401/403 AUTH_EXPIRED, 429/5xx SYSTEM_UNAVAILABLE, 400/404/409/422 DATA_ERROR); transport failures throw `NetworkError`.
Built-in retry applies to idempotent methods only. For the corporate trust store run Node with `--use-system-ca` (22.15+/23.8+) or set `NODE_USE_SYSTEM_CA=1`, or pass `caPath`.
Client certificates: `powershell -File scripts/export-cert.ps1 -SubjectLike "*CN=svc-automation*"` prints `{pfxPath, passphrase}` (owner-only temp PFX); remove it afterwards with `-Cleanup <pfxPath>`.

### `fw.browser`, `fw.cdp`

`browser.launch({ headless = true, channel = 'msedge', userDataDir, cdpPort, args, executablePath })` -> `{ browser, context, page, cdp, close() }`; `browser.attach(cdpUrl)` connects to a running browser.
`cdp.domSnapshot(page, { maxChars })` returns the compact LLM-facing snapshot (interactive elements + stable selectors; values redacted; URL queries masked).

### `fw.resilience` — Pattern 3

`resilience.resilientPage(page, { caller, maxHealsPerRun = 5, denyPatterns, allowDestructive, cachePath })` wraps `click / fill / selectOption / check / uncheck / textContent / innerText / getAttribute / waitFor / press / hover`
(other members pass through to the real page; `rp.raw` is the page). On a selector failure it heals once, verifies the proposal in the live DOM, retries, logs `llm_decision` + `selector_healed`, and caches the result per
(URL pattern, original selector) in `%LOCALAPPDATA%\AUTOMAGIX\state\selector-cache\<automation>.json`.

### `fw.agent` — Pattern 4

`agent.runAgent({ goal, page, tools?, maxSteps = 15, allowedOrigins = [], confirmBeforeIrreversible = true, caller, params, data })` ->
`{ status: 'done'|'failed'|'stalled'|'max_steps'|'blocked_origin'|'blocked_irreversible', result?, reason?, extracts, history }`.
Add domain tools with `tools: { lookup_vendor: { description, args: { vendor: 'string' }, run: async (args, io) => {...} } }`.

### `fw.mcp`

`node bin/mcp-browser.js` is a stdio MCP server (JSON-RPC 2.0, protocol `2024-11-05`) with tools `navigate, snapshot, click, fill, press, screenshot, extract_table` (+ `evaluate` only when explicitly enabled).
Env: `AMX_MCP_HEADLESS=0`, `AMX_MCP_ALLOWED_ORIGINS`, `AMX_MCP_ALLOW_EVALUATE=1`, `AMX_MCP_CDP_URL`, `AMX_MCP_PROFILE`.
`lib/mcp-server.js` exports `createToolRegistry`, `createServer`, `serveStdio` for embedding/testing.

### `fw.adapters`

An adapter is just an object with a `name` and the async methods your automations need; it receives a Playwright page or a `resilientPage`.

```js
fw.adapters.register('contoso-portal', {
  description: 'Contoso AP portal helpers',
  async openInvoice(page, id) { await page.goto(`https://ap.contoso.example/invoices/${id}`); },
});
fw.adapters.get('contoso-portal').openInvoice(page, 'INV-1');
```
Shipped: `generic-table` (header normalisation + pagination), `generic-form` (fill + verify, never submits), `download` (size/hash check, atomic move, sanitised file names), `sso-detect` (throws `AuthExpired`).
Adapters shared across automations belong in `lib/adapters/` and in `registerBuiltins`.

### Developer onboarding: Playwright CodeGen

`node bin/amx-codegen.js https://portal.internal.example.com --target javascript` records selectors against your Edge profile (SSO works while recording) and prints working code to refine.

## Build and release

```
npm ci --ignore-scripts
node scripts/check-syntax.js          # lint
node --test                           # unit tests (no browser needed)
node scripts/build-framework.mjs --out dist --node-dir <node> [--python-embed <zip> --tzdata-wheel <whl>] [--dry-run]
python <automation-builder>/scripts/package.py component execution-framework dist/tree --version "$(cat VERSION)" --out dist/pkg
```
`.gitlab-ci.yml` runs exactly this. The package is zipped with a `VERSION` file at its root (self-heal compares it with the registry manifest).

## Testing status — read this

This component was authored on a machine **without Node.js**, so **`node --test` has never been run against it.** What was verified instead:

* **Syntax:** all 37 `.js/.cjs/.mjs` files parse cleanly with a full modern-JS grammar (tree-sitter), with a negative control confirming the checker flags broken code.
* **Logic, in a real JS engine (V8) via a Node-compat shim:** `tools/v8-harness/run_in_v8.py` runs the real library code and all `test/*.test.js` suites against in-memory shims (fs, path, sha256, assert, test, readline, stream,
  in-memory HTTP servers, URL/timer polyfills). **173 tests pass.** A mutation check (disabling the deny-list and the LOGIC_ERROR no-retry rule) made the suite fail as expected. To re-run on a machine without Node:
  `py -3 -m pip install py-mini-racer` then `py -3 tools/v8-harness/run_in_v8.py [--only test/retry.test.js]`.
* **Cross-language format:** the golden `events.jsonl` lines in `test/events.test.js` were confirmed byte-identical (apart from the timestamp) to what `wrapper_core.runctx` writes, and the classifier parity table was
  confirmed against the Python classifier (29/29 rows).
* **PowerShell:** `scripts/export-cert.ps1` was run for real against a throw-away self-signed certificate (export, open with the passphrase, cleanup, no-match failure); the test cert and files were removed.

What is **not** verified — the first real `node --test` / smoke run still has to cover it:

* The shim `assert`/`test` are re-implementations; real Node semantics could differ in corner cases (e.g. `assert.rejects` with a RegExp matches `String(err)`).
* Anything needing a real browser: `browser.launch/attach` against real Playwright, the in-page DOM collector `collectNodesInPage` (only its output formatting is tested), `bin/mcp-browser.js` wiring, `bin/amx-codegen.js` actually spawning the Playwright CLI.
* Real sockets/TLS (`fw.http` against HTTPS with a PFX), `scripts/build-framework.mjs` execution (only its pure planner is tested), and `package-lock.json` (generate it once with npm and mirror access).
* Unicode property regexes (`\p{L}`) in `generic-table` need an ICU-enabled Node (all official builds); the harness substitutes ASCII when its V8 lacks ICU.

Suggested first smoke run on a Node 18+ box: `npm ci --ignore-scripts && node --test`, then `node bin/amx-codegen.js about:blank` and `AMX_MCP_HEADLESS=0 node bin/mcp-browser.js` (send an `initialize` line on stdin).
