# Architecture and roadmap coverage

Read [`assessment.html`](assessment.html) for the reasoning; read [`SPEC.md`](SPEC.md) for the contracts. This page answers one
question: *which assessment item is implemented where?*

## Repo map (assessment → monorepo)

| Assessment repo | Directory | Notes |
|---|---|---|
| automation-builder | `components/automation-builder` | Docker (containeryard base), NSIS template + generator, signing, packaging, prompt library, CI templates |
| wrapper-core | `components/wrapper-core` | stdlib-only Python |
| execution-framework | `components/execution-framework` | Node + Playwright(Edge) + CDP + MCP |
| automation-logging | `components/automation-logging` | ingest, analytics, signals, diagnosis, alerting, dashboard |
| automation-registry | `components/automation-registry` | manifest, operator CLI, Pages portal |
| automation-{name} ×N | `components/automations/*` | 4 shipped examples + `tools/new_automation.py` |

## Capability roadmap (assessment "Next" tab, items 1–30)

| # | Item | Where | State |
|---|---|---|---|
| — | Kill-switch / pause | manifest `status`/`fleet.paused` → `selfheal.run` → `runner` (halts before payload, status file, toast, `paused_seen`); `registry pause/resume/fleet-pause`; admin panel | built + tested |
| 1 | Alerting channel | `automation-logging/alerting.py` (Jira, webhook/Teams, email, GitLab issue, file outbox) **and** the dog-fooded `automation-notifier`; routing, dedupe, re-alert, resolve | built + tested; **Q10 (which channel) remains your decision** |
| 2 | `llm_call()` + run synopsis | `wrapper_core/llm.py`, `broker.py`, `synopsis.py` | built + tested (offline mock; real endpoint untested — Q6) |
| 3 | `log_item()` item telemetry | `wrapper_core/logging.py`; analytics throughput/P50-P95-P99/skip-rate/item-failure-rate | built + tested |
| 4 | Intelligent retry | `wrapper_core/retry.py` (+ process-level retry in `runner`); manifest `error_handling` | built + tested |
| 5 | Hung detection / in-progress | `runner._Execution` watchdog, `expected_finish_at` in heartbeat, `run_hung` lifecycle event, HUNG signal | built + tested |
| 6 | Schedule compliance + maintenance windows | manifest `schedule`; `manifest.in_maintenance`; analytics MISSED/SILENT/SLOW/VERSION_DRIFT/UNEXPECTED/HUNG | built + tested |
| 7 | Credential hardening | `secrets.py` (DPAPI, Credential Manager), installer `protect-secret.ps1`/`set-credential.ps1`, signing in builder | built; DPAPI/credman tested on Windows; signing logic tested, real signing needs your PKI cert |
| 8 | Dev ring | `rings: [dev, pilot, broad]`, ring-order validation, ring in transport path/heartbeat, `wrapper_core dev` offline loop | built + tested; dev-specific logging remote is configuration (runbook §7) |
| 9 | Dynamic config assets | manifest `assets` → `selfheal` → `config/assets.json` → `wrapper.assets()` | built + tested |
| 10 | Fleet admin panel | `automation-registry` portal `admin.html` (generates MR deep links / commands / `ops-action` pipeline variables — no server) | built + tested (generation + XSS); not exercised against live GitLab |
| 11 | Scaffolding | `tools/new_automation.py` + templates; CI templates in builder | built + tested |
| 12 | LLM Pattern 1 | `automation-invoice-router` (grounded extraction), `automation-hello-world` | built + tested |
| 13 | Automation catalog | manifest `catalog:` block → portal storefront/detail pages | built + tested |
| 14 | NSIS uninstall + testing conventions + compat matrix | installer uninstall section → `wrapper_core uninstall` (ref-counted); lifecycle tests; `tools/compat_smoke.py`; `registry validate` compat rules | built; uninstall logic tested in Python, NSIS section needs a VM run |
| 15 | Onboarding kit | `docs/GETTING_STARTED.md`, `WRAPPER_CORE_API.md`, examples, `amx-codegen` (Playwright CodeGen preset) | built |
| 16 | Parameterized schedules | `params` per slot → `AUTOMATION_PARAM_*` / `wrapper.param` | built + tested |
| 17 | LLM Pattern 3 | `execution-framework/lib/resilience.js` (+ healed-selector cache, deny-list, validation) | written; Node tests not executed here |
| 18 | Cross-automation orchestration | `emit_trigger` → analytics `triggers.json` → poll slots → `selfheal.pending_triggers` | built + tested |
| 19 | Analytics / BI | analytics KPIs, trends, business-impact (`catalog.minutes_per_item`) | built + tested |
| 20 | LLM Pattern 4 | `execution-framework/lib/agent.js` (origin allow-list, irreversible-action gate, stall detection) + `mcp-browser.js` | written; Node tests not executed here |
| 21 | Attended / on-demand trigger | `registry trigger-add` / admin panel / `ops-action` pipeline → `triggers.json` | built (last-resort path, by design) |
| 22 | Context Assembler | `wrapper_core/context.py` (introspected API, manifest format, entry point, Automation Standard) | built + tested |
| 23 | Schema + parse-retry | `wrapper_core/schema.py`, `llm_call` (one corrective retry, `None` on failure) | built + tested |
| 24 | Prompt library | `automation-builder/prompts/*.j2` (+ `loader.py`) | built + tested |
| 25 | Developer assist CLI | `wrapper_core/assist.py` — `scaffold`, `review` (deterministic static pass + LLM), `test`, `design` (4-step chain) | built + tested offline |
| 26 | Rotation runbook | `docs/RUNBOOKS.md` §4 | documented (rotation = overlap + silent reinstall; see SECURITY known gaps) |
| 27 | LLM decision audit | `llm_decision` + `retry_event` events (template name + input hash, never prompt text) | built + tested |
| 28 | Prompt-injection safeguards | `llm.py` (fixed system prompt, delimiting, neutralisation, schema), lint AS-09, grounding in examples | built + tested |
| 29 | Sensitive-field redaction | `sensitive_keys` → `redact.py` before synopsis/retry (endpoint) and diagnosis (CI) | built + tested |
| 30 | Endpoint feedback | `notify.py`: status file, version-bump / paused / run-complete toasts (`notify_on_complete`) | built; toast path needs an interactive session to see |

## Beyond the assessment

* **Loopback LLM broker** so payloads in any language never hold the key (SECURITY §deviations).
* **Offline everything**: `mock://` LLM, `dir` transports, `wrapper_core.testing.run_payload`, `tools/e2e_demo.py`.
* **Grounding checks** on LLM extraction (invoice router) so a wrong model answer cannot route money.
* **Deterministic static reviewer** (`assist review`) usable in CI with no LLM.

## Run lifecycle (sequence)

```
Task Scheduler ─▶ run-<automation>.vbs ─▶ run-<automation>.cmd (apply staged wrapper-core) ─▶ python -m wrapper_core run
   lock → spool flush → self-heal ──▶ manifest (token) ──▶ kill-switch? ─▶ HALT (status file, toast, log)
                                  └▶ install/update components + payload (sha256, atomic swap), assets, triggers
   → maintenance window? ─▶ SKIP (logged) → compat check → heartbeat(run_start, expected_finish_at)
   → broker up → payload process (watchdog; process-level retry on classified NETWORK/AUTH/UNAVAILABLE)
   → status → synopsis (LLM→deterministic) → run log + heartbeat(run_end) via transport (spool on failure) → notify
```
