# UiPath parity: what a replacement must cover, and where AutoStation stands

Reviewed against what UiPath (Studio, Robot, Orchestrator, Action Center, Document Understanding, Insights, Test Suite) and
the other tools teams actually move to (Power Automate, Robocorp/Playwright scripts, n8n/Airflow-style schedulers) give people
day to day. "Built" means it exists in this repo and is covered by tests or CI; "partial" says what is missing; "gap" is honest.

## For the people whose machines run automations

| Need | AutoStation | State |
|---|---|---|
| One click to get an automation | **Install center** (Pages) → real signed-or-unsigned `.exe` from the rolling GitHub Release `installers-latest`; per-user, no admin, bundled Python + payload, works offline on first install | built (CI builds, smoke-tests install → task → first run → uninstall, publishes) |
| Know what it does to my PC | Per-automation page: data/systems touched, schedule in plain English, exact list of what the installer changes, SHA-256 + verify steps | built |
| Trust the download | SHA-256 beside every installer; `bootstrap.ps1` verifies before installing; code signing supported (`--require-signing`) | built; signing needs your certificate |
| "Is it working?" | `python -m wrapper_core doctor` (PASS/WARN/FAIL with fixes), status files and toasts when paused/updated | built |
| Pause / uninstall | Operators' kill-switch; Add/Remove Programs entry; clean uninstall (ref-counted shared parts) | built |
| Report a problem | Prefilled GitHub issue forms per automation | built |
| Enterprise rollout | Intune/SCCM settings generated per automation | built |

## For developers (Studio replacement)

| UiPath Studio | AutoStation | State |
|---|---|---|
| Recorder / Web recorder | `amx-record.js` (Playwright codegen on the user's Edge profile → payload with resilient selectors, params, no hard-coded credentials) | built (Node tests run in CI; not exercised against a live codegen session) |
| Activities pane (Excel, Mail, PDF, HTTP, DB, files, Queues, Credentials, UI) | `wrapper_core.activities` (excel/csv/xlsx, SMTP/IMAP/Outlook, files+watchers, REST client, sqlite/ODBC, durable queue, credentials, Windows UI Automation, PDF text, notify) + [mapping table](ACTIVITIES.md) | built; Outlook/ODBC/real-UI paths verified only locally |
| REFramework | `automation-transactional` template (dispatcher/performer, business vs system exceptions, retries, `needs_review`) | built |
| Selector fragility | LLM Pattern 3 selector healing (validated, deny-listed, cached) + stable-selector adapters (iframes, popups, waits, uploads) | built (fakes in CI; real browser unverified) |
| Debugger / Highlight / Slow-step | `AMX_HEADED`, `AMX_SLOWMO`, `AMX_DEBUG_SELECTORS`, pause-on-error (interactive only), `wrapper_core dev` offline lifecycle | built |
| Screenshot on error | Failure evidence: screenshot + redacted DOM + context (+ trace) shipped with the run, shown on the dashboard | built |
| Publish to Orchestrator | `git push` → CI → packages, dev-ring PR, installers, site | built |
| Test Suite / Test Manager | pytest + `wrapper_core.testing.run_payload` lifecycle tests, compat smoke, CI matrix | built |
| Source control / code review | Git-native; PRs with promotion checklist, CODEOWNERS | built |
| Document Understanding | LLM Pattern 1/2 with grounding checks, schema-validated output, redaction, classification gate | built (LLM endpoint not yet exercised) |
| Computer Vision / Citrix / image automation | optional `pyautogui`/OCR hooks only | **gap** (documented) |
| SAP GUI / mainframe terminal / Java UIs | not provided (Windows UIA covers standard Win32/WPF/WinForms) | **gap** |
| Process/Task Mining | not provided | **gap** |

## For operators (Orchestrator replacement)

| Orchestrator | AutoStation | State |
|---|---|---|
| Folders / tenants / environments | rings dev → pilot → broad, namespaces via manifest | built |
| Robots / machines | heartbeats, fleet versions, SILENT/VERSION_DRIFT signals | built |
| Processes + releases + rollback | manifest versions per ring; promotion/rollback = reviewed PR (`ops-action`, admin panel) | built |
| Jobs + logs | run logs per run with item telemetry, synopsis, evidence, retry audit | built |
| Schedules | per-slot Task Scheduler tasks (daily, weekdays, weekly, monthly, interval, cron, blackout calendar) + login re-assertion; params per slot | built |
| Triggers (queue/file/mail/API) | `poll` slots + `activities.files.new_files` + queue + `triggers.json` / on-demand `trigger-add` | built (minute latency by design) |
| Queues | durable local queue: priority, deadline/SLA, defer, dedupe, retries + dead-letter, stats, export; multi-machine distribution is a gap by design | built (single machine) |
| Assets / credentials | manifest `assets` (non-secret) with machine/user/slot overrides + Windows Credential Manager API | built |
| Alerts | Jira / Teams / email / GitHub issue / dog-fooded notifier, dedupe, resolve | built |
| Roles / audit | GitHub permissions, branch protection, PR history = audit trail | built (needs your repo settings) |
| Insights / dashboards | Fleet dashboard, **Program/ROI dashboard** (hours saved, licences avoided, failure causes), migration dashboard | built |
| Action Center | `activities.approvals` (human-in-the-loop tasks, expiry, audit) + local approval page/CLI + dashboard Queues & approvals page | built (local approver; not an identity system) |
| Attended automation (Assistant) | not offered by design (redesign or `trigger-add`) | gap by design |

## For the migration programme

| Need | AutoStation | State |
|---|---|---|
| Know what you have | `tools/uipath/inventory.py` — static scan of projects/Orchestrator export: activities, selectors, REFramework, secrets (redacted), complexity, target pattern, effort | built |
| Plan waves | migration dashboard (funnel, waves, burn-down, blockers, licence savings estimator) | built |
| Start fast | `scaffold.py` generates a payload + manifest entry + checklist per project | built (a head start, not a transpiler) |
| Prove equivalence | `parity.py` go/no-go on key-matched outputs | built |
| Cut over safely | playbook in [UIPATH_MIGRATION.md](UIPATH_MIGRATION.md) (parallel run → gate → cutover → decommission → licence true-down) | documented |

## The honest remainder

1. **Image/Citrix/SAP-GUI automations cannot be migrated mechanically** — expect redesign (API, file hand-off, or vendor scripting).
2. **Everything needs one real-environment pass**: your signing certificate, your LLM endpoint, a real browser run of the recorder output,
   a standard-user VM install, and your GitHub settings ([PIPELINES.md](PIPELINES.md)).
3. **Multi-robot queue fan-out and attended runs** are intentionally out of scope; the doc explains the alternatives.
