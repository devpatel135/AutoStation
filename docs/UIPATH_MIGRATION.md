# Replacing UiPath with AutoStation: the migration guide

This is the practical guide for moving every UiPath automation onto AutoStation, safely and measurably. It is backed by tools in
[`tools/uipath/`](../tools/uipath/) (inventory, scaffold, parity, dashboard) and a tracking folder, [`migration/`](../migration/README.md).

**Honest scope.** AutoStation replaces *unattended* UiPath work well: API/file/data automations, browser automations (Playwright in the
user's Edge profile), desktop automations through Windows UI Automation, queue-based transactional processing, and LLM-assisted
document handling. It does **not** replace everything: image/Citrix/terminal automation, Excel macros, UiPath-hosted AI skills, attended
tray workflows and multi-machine queue distribution have no like-for-like equivalent (details below). The inventory tool tells you
which of your automations fall into which bucket *before* you commit to dates.

Related docs: [Getting started](GETTING_STARTED.md) - [Activities (UiPath activity map)](ACTIVITIES.md) - [Automation Standard](AUTOMATION_STANDARD.md)
- [wrapper-core API](WRAPPER_CORE_API.md) - [Governance](GOVERNANCE.md) - [Runbooks](RUNBOOKS.md) - [Security](SECURITY.md)

---

## 1. Concept map

### Studio and the robot

| UiPath | AutoStation | Notes |
|---|---|---|
| Studio project (`project.json` + XAML) | an automation repo (`automation.yaml` + Python/Node code), scaffolded by `tools/new_automation.py` or `tools/uipath/scaffold.py` | code-first; any editor; reviewed in a PR |
| Activities | `wrapper_core.activities.*` (Python) and `execution-framework` (`fw.browser`, `fw.http`, `fw.adapters`) | the inventory prints the exact equivalent per activity |
| Recorder / UI Explorer | `npx playwright codegen` (`bin/amx-codegen.js`) for browsers; UIA inspection tools for desktop | selectors are code you own and review |
| Variables / arguments | ordinary variables / function arguments / slot `params` | `wrapper.param("date_range")` |
| `InvokeWorkflowFile` | a function or module call | |
| Exception handling (TryCatch, RetryScope) | `try/except`, `wrapper.run_with_retry`, manifest `error_handling` policy | classification: NETWORK/AUTH/SYSTEM retried, DATA skipped, LOGIC escalated |
| `LogMessage` / robot logs | `wrapper.log_event`, `wrapper.log_item` -> run log -> dashboard + synopsis | item-level telemetry is richer than UiPath's per-job log |
| Attended robot / Assistant | not offered (unattended by default); trigger-based runs via `registry trigger-add` | redesign first, see section 6 |
| Debugging | `python -m wrapper_core dev .` (real lifecycle, offline), Playwright headed mode, `pytest` | |

### Orchestrator

| Orchestrator | AutoStation | Notes |
|---|---|---|
| Tenant / folders | registry manifest + **rings** (dev/pilot/broad) + `owner_team` | no folder hierarchy: ownership and rings replace it |
| Machines / robots / unattended slots | VDI sessions with wrapper-core installed; **heartbeats**; fleet table on the dashboard | one robot = one user session; no licence slot to allocate |
| Processes / releases / package versions | `versions` + `rings` in `manifest.yaml`; promotion by merge request | rollback = a manifest edit |
| Jobs | **runs** (`run` artefacts), with status success/partial/failed/hung/halted | |
| Time triggers (cron) | schedule **slots** (daily/weekly/hourly) -> Windows Task Scheduler | `inventory.py`/`scaffold.py` convert Quartz cron; monthly/sub-hourly need a decision |
| Queue triggers | a `poll` slot (every N minutes) that calls the performer; exits quietly when the queue is empty | |
| File triggers (`FileChangeTrigger`) | a `poll` slot + `activities.files.watch_new(folder, pattern)` | |
| Mail triggers | a `poll` slot + `activities.mail.read_imap/read_outlook` | |
| API / on-demand triggers | `registry trigger-add` / admin panel / `ops-action` pipeline -> `triggers.json` -> poll slot | minutes of latency, by design |
| **Queues** | `wrapper_core.activities.queue` (durable local SQLite queue: priority, deferral, deadline, dedupe, business vs system failure, retries, review) | **single-machine** queue: dispatcher and performer share a VDI session (or a shared scratch path). Multi-robot work distribution from one queue is not provided; shard the work by slot/param or use a real broker |
| **Assets** (text/int/bool) | manifest `assets` (non-secret) -> `wrapper.assets()`, refreshed on every run | changed by MR, not by a UI |
| **Credential assets** | the user's Windows session/SSO (preferred), else **Windows Credential Manager** `AUTOMAGIX/<name>` via `activities.credentials.get_credential` | never in the repo/manifest; DPAPI-protected per user |
| Storage buckets | a shared path (UNC/SharePoint sync) used through `activities.files` | |
| Roles / permissions | GitHub/GitLab permissions, CODEOWNERS on the manifest, protected branches | |
| Audit logs | git history (every promotion/pause is a reviewed commit), `llm_decision` + `retry_event` run events | |
| Alerts / monitoring | alerting pipeline (Jira/Teams/email/issue), `automation-notifier`, compliance signals (MISSED/SILENT/SLOW/HUNG...) | |
| Insights / Process Mining | dashboards: fleet health, item-level analytics, business impact (`catalog.minutes_per_item`), this migration dashboard | |
| Test Suite / Test Manager | `pytest` + `wrapper_core.testing.run_payload` lifecycle tests in CI before ring promotion | |
| Action Center | `q.needs_review(item, reason)` / `raise NeedsReview(...)`; pending reviews via `q.review_items()` | a review queue, not a task UI |
| Document Understanding | LLM patterns 1 and 2 (`wrapper.llm_call` with schema + **grounding checks**), classification-gated | section 4 |
| Computer Vision, Citrix, OCR | **gap** - section 5 | |
| AI Center / ML skills | `wrapper.llm_call` or your own model behind an API | |

---

## 2. REFramework -> the transactional template

`python tools/new_automation.py automation-my-process --template transactional ...` (or `scaffold.py`, which picks it automatically for
queue/REFramework projects) gives you the REFramework's behaviour in ~150 lines you can read:

| REFramework | Transactional template |
|---|---|
| Init All Settings (`Config.xlsx` Settings/Constants/Assets) | `init()` - manifest `assets` + schedule-slot `params` (Config.xlsx values become these) |
| Dispatcher process (Add Queue Item) | `mode=dispatch`: `dispatch(q, cfg)` -> `q.add(reference, payload)`; duplicates ignored so it is safe to re-run |
| Performer: Get Transaction Data | `q.next()` |
| Process Transaction | `process_transaction(item)` - your logic |
| Business rule exception (no retry) | `raise wrapper.DataError(...)` -> `q.fail_business(...)`; shown as *skipped* with the reason |
| System exception (retry, `MaxRetryNumber`) | transient faults retried in place by `wrapper.run_with_retry`; then `q.fail_system(item, reason, max_retries)` re-queues; `LogicError` is never retried |
| `MaxConsecutiveSystemExceptions` | `max_consecutive_system_exceptions` param; the run stops with an `Escalation` |
| Set Transaction Status | `q.succeed / fail_business / fail_system / needs_review` |
| Init/Close/Kill All Applications | open the application inside `process_transaction` (or a context manager) and close it in `finally`; Playwright sessions are per run |
| End Process (summary email) | `wrapper.set_result(...)` + the automatic run synopsis + alerting |
| One robot, one queue, many items | one run, one queue, many items (`mode=both`, or separate `dispatch`/`perform` slots) |

Mapping the Config.xlsx: *Settings* -> slot `params` (per schedule) or manifest `assets`; *Constants* -> module constants in `main.py`;
*Assets* sheet -> names looked up with `wrapper.assets()` / `get_credential`. `inventory.py` lists every Config entry (secrets redacted) in
`inventory.json` under `config`.

---

## 3. Action Center (human in the loop)

UiPath tasks become **review items**: when a transaction needs a decision, raise `NeedsReview("reason")` in `process_transaction`. The item
moves to `needs_review`, the run continues, and the item is listed by `q.review_items()` (and can be exported with `q.export_csv(...)` for a dashboard). A person decides, then
`q.resolve_review(item, approve=True/False, note=...)` re-queues or closes it (a small script, or `registry trigger-add` for an operator-run
step). If you need forms, attachments and SLA clocks, keep that part in your ticketing tool (Jira/ServiceNow) and have the automation open the ticket
and poll its status.

## 4. Document Understanding -> LLM patterns, with grounding

* **Classification and field extraction** = LLM *pattern 1*: `wrapper.llm_call(prompt, context=document_text, schema={...}, caller=...)`.
  The document text goes in `context` only (never in the prompt), the answer is schema-validated, and **it is never trusted blindly**:
  re-check that every extracted value actually appears in the source text (see `components/examples/automation-invoice-router/router/extract.py`),
  and route money/records with deterministic rules.
* **Summaries/anomaly flags** = *pattern 2* with a deterministic fallback when the LLM is unavailable (`llm_call` returns `None`).
* **Data classification gate:** set `data_classification` and `llm_allowed_classification` honestly; above the approved level the platform withholds the
  content from the model. Keep a rule-based or manual path for restricted documents.
* **Validation station** = `NeedsReview`. **Confidence thresholds** = have the model return `confidence` in the schema and route low confidence to review.
* Training/ground-truth: keep a labelled sample set in the repo (synthetic or approved) and run it in CI as a regression test of extraction quality.
* Digitisation of scanned images/PDFs needs OCR, which is an optional add-on (section 5); text-based PDFs work with `activities.pdf`.

## 5. Computer Vision, Citrix, OCR, terminals: the honest gap

UiPath can click pixels. AutoStation does not provide a production image-recognition engine. In order of preference:

1. **Find a non-visual interface.** An API, a database/file/MQ interface, a supported export, or the application's scripting object model (SAP RFC/BAPI/OData,
   Excel file formats, mainframe MQ/file transfer). This is the right fix for most "Citrix + image" automations - the image approach was a workaround.
2. **Drive the real application with UI Automation** (`activities.windows_ui`) when the app runs on the same VDI session - robust against moving pixels.
3. **For Citrix/remote desktops where the remote UI is only pixels**: run the automation *inside* the remote session (install wrapper-core there), or use the
   optional image/OCR helpers described in [ACTIVITIES.md](ACTIVITIES.md), accepting that they are the most fragile option (resolution, scaling, theme).
4. **Terminal/mainframe screen scraping**: move to the host's batch/API interface; if impossible, treat as *needs redesign* in the inventory.

The inventory marks these projects `needs-redesign` and adds `R-IMAGE` / `R-TERMINAL` high risks. Plan them as a separate wave with the application owner.

---

## 6. The playbook

```
inventory -> classify -> waves -> scaffold -> build+test (dev) -> parallel run (parity gate) -> pilot -> broad -> cutover -> retire -> licence true-down
```

1. **Inventory (days).** Export from Orchestrator (processes, schedules, queues, assets, robots as CSV/JSON; **never export asset values**) and collect the
   project folders or `.nupkg` files. Run:
   `python tools/uipath/inventory.py <projects> --out migration/inv --orchestrator-export <export>` and open `inventory.html`.
2. **Classify.** Review the recommended pattern, risks, data-classification hint and effort per project; agree owner + classification. Fix the hard-coded secrets the
   report found (rotate them - they are in your UiPath projects today).
3. **Waves.** Wave 1 = 2-3 *representative, low-risk, high-value* automations (one headless, one browser, one transactional) to calibrate the effort model and the team.
   Later waves by business owner or system. Push `needs-redesign` projects to their own wave. Record in `migration/status.yaml`.
4. **Scaffold.** `python tools/uipath/scaffold.py migration/inv/inventory.json --out scaffolds --owner <dl> --project <name>` -> a runnable payload, a registry stanza,
   a per-project `MIGRATION.md`. Commit it as the automation's own repo.
5. **Build + test in dev.** Implement the stubs (the comments list each source activity and its equivalent). `python -m wrapper_core dev .`,
   `python -m wrapper_core assist review main.py`, `pytest`. Open the registry MR (dev ring).
6. **Parallel run + parity gate.** Run UiPath and AutoStation on the *same inputs* for N consecutive scheduled runs (suggest 5 for daily jobs, at least 2 month-ends for monthly
   ones). Side-effect-free comparison only: have AutoStation write to a *shadow* destination (or `--dry-run` + export) while UiPath stays the system of record. Compare the outputs:
   `python tools/uipath/parity.py --uipath <ui.xlsx> --autostation <as.csv> --key <id> --out migration/parity/<project>`.
7. **Pilot, then broad** by promotion MRs (`registry promote`). Watch the dashboard signals; keep the UiPath trigger enabled only if the AutoStation runs are *shadow*.
8. **Cutover.** Disable the UiPath trigger (do not delete), watch two live AutoStation runs, keep UiPath available for the rollback window, then archive.
9. **Retire + true-down.** Set `status: retired` in `migration/status.yaml` with `robots_retired`; release the robot/licence at the next renewal boundary.

### The parity gate (go / no-go)

`parity.py` exits 0 only if **all** hold (defaults; tune per process with the flags and record the decision in the MR):

| Check | Default |
|---|---|
| UiPath rows missing from AutoStation | 0 % |
| Extra rows in AutoStation | 0 % |
| Matched rows with any differing field | <= 0.5 % (numeric tolerance 0.005; whitespace/date/currency-format differences are normalised) |
| Every UiPath output column present | yes |
| Minimum compared rows | 1 (raise it: a GO on 3 rows proves little) |

Cutover needs **GO on N consecutive runs**, an owner sign-off in the promotion MR, and (for restricted data) the security reviewer's. A NO-GO is not a failure of the tool: read
`parity.json` - it lists every mismatching field. Use `--no-values` when the data is sensitive.

---

## 7. Per-automation checklist

- [ ] Inventory row reviewed: pattern, risks, effort, data-classification hint agreed with the owner
- [ ] Owner (distribution list), systems, classification, schedule, `expected_duration_ms` and `catalog` completed in the manifest stanza
- [ ] Every UiPath credential/asset re-created (Credential Manager / manifest `assets`); old hard-coded secrets rotated; **no secrets in the repo**
- [ ] Attended activities (`InputDialog`, `MessageBox`, `SelectFile`...) replaced by params, files or rules
- [ ] Business exceptions -> `DataError`; system exceptions -> retry policy; escalation reaches the owner
- [ ] Idempotent: a re-run after a partial failure does not duplicate side effects (dedupe by item id/reference)
- [ ] Dry-run path has no side effects; irreversible actions are gated by an explicit parameter
- [ ] Selectors verified on the real target; Playwright healing events reviewed; waits are conditions, not sleeps
- [ ] `assist review` has no HIGH findings; unit + lifecycle tests pass in CI
- [ ] Parity GO for N consecutive runs; report linked from `migration/status.yaml`
- [ ] Dev -> pilot -> broad MRs approved; alert routing tested with an induced failure
- [ ] UiPath trigger disabled, rollback window agreed, project archived, robot/licence released

## 8. Common pitfalls

* **Selectors.** UiPath selectors are not portable. Re-derive them (Playwright locators by role/label/test-id beat positional CSS). Keep `fw.resilience` on for the
  steps that matter, but treat every heal event as a ticket to fix the selector properly.
* **Timing and waits.** `Delay` activities hide races. Wait for a condition (element visible, file present, row count stable) with a timeout.
* **Excel interop vs files.** UiPath's Excel scope drives the Excel application (formulas recalculate, macros run, formatting). AutoStation reads/writes xlsx *files* without Excel:
  formulas are not recalculated, macros do not run, and complex formatting is not preserved. Compute values in code; write results; if a workbook *must* be recalculated by Excel, keep
  that step on a machine with Excel via `windows_ui` or move the logic out of the workbook.
* **Credentials.** Prefer SSO/cert (the automation runs as the user). Orchestrator credential assets do not migrate by copying: re-create them per user in Credential Manager, or
  redesign to use a service endpoint.
* **Attended assumptions.** Many "unattended" robots still assume a logged-in desktop, a specific resolution, or a user who will click a dialog. Run the first dev runs on a clean
  VDI session as a standard user.
* **Time zones.** Orchestrator triggers carry a time zone; the manifest `schedule.timezone` does too. Stock Windows Python has no tz database: bundle `tzdata` with the embedded runtime (see SECURITY.md);
  otherwise times fall back to machine-local (logged). Month-end/"last business day" logic must be in code.
* **Queue semantics.** A UiPath queue is shared by many robots; the AutoStation queue is local to a VDI session. If the process fanned work out to several robots, decide how
  (shard by param/slot, or keep a real broker).
* **Retry double-dipping.** UiPath `RetryScope` + REFramework retries + job retries stack. Define *one* policy in the manifest and let the platform apply it.
* **Logging volume.** Per-item `log_item` is cheap; never log secrets or full documents. Mark sensitive fields with `sensitive_keys`.
* **Big-bang risk.** Do not retire UiPath for a process until the parity gate and a month-end (or the process's longest cycle) have passed.

## 9. Decision tree: choosing the target pattern

```
Does the process need a person to click/decide mid-run?  -- yes --> redesign: params/files/rules; human decisions -> NeedsReview
 | no
Does it process many independent items (queue / REFramework)?  -- yes --> TRANSACTIONAL template (dispatch/perform)
 |                                                                          (inside it: the choices below)
 | no
Does the target expose an API / file / DB / MQ interface?  -- yes --> HEADLESS (Python): http/db/files/excel/mail/pdf activities
 | no
Is it a web application?  -- yes --> PLAYWRIGHT browser (Node): user's Edge profile, resilient selectors
 | no
Is it a Windows desktop / SAP GUI app on the same VDI?  -- yes --> prefer the app's API (SAP RFC/OData); else WINDOWS UIA (activities.windows_ui)
 | no
Is it pixels only (Citrix, remote desktop, image/OCR)?  -- yes --> NEEDS REDESIGN (section 5)
```

## 10. Effort model

`effort_days = (base + per_point * complexity) * pattern_multiplier * (1 + test_overhead) + 1.0 per high risk + 0.5 per medium risk`, rounded to half days.
Defaults: `base=1.0`, `per_point=0.06`, `test_overhead=0.25`; `complexity = 2 * workflows + sum(activities * category weight)`; pattern multipliers: headless 0.8, browser 1.0,
transactional 1.2, desktop 1.5, needs-redesign 2.5. All the weights live in `tools/uipath/catalog.py`. **Calibrate:** after wave 1, compare estimates with actuals and
rerun the inventory with `--effort-per-point` adjusted. Treat the numbers as a planning aid, not a quote.

## 11. Tools reference

| Tool | Purpose | Key options |
|---|---|---|
| `tools/uipath/inventory.py SRC --out DIR` | static scan of projects (folder, `project.json` dir, `.nupkg`/`.zip`) -> `inventory.json/.csv/.html` | `--orchestrator-export DIR`, `--effort-base/--effort-per-point/--test-overhead`, `--no-html` |
| `tools/uipath/scaffold.py INVENTORY --out DIR` | per-project payload + `manifest-entry.yaml` + `MIGRATION.md` | `--project NAME`, `--owner`, `--owner-team`, `--force` |
| `tools/uipath/parity.py --uipath F --autostation F --key K --out DIR` | go/no-go output comparison (`.xlsx/.csv/.tsv/.json`) | `--ignore`, `--column-map`, `--numeric-tol`, `--date-format`, `--max-*-pct`, `--min-rows`, `--no-values` |
| `tools/uipath/dashboard.py --inventory F --status F --out DIR` | migration dashboard (funnel, waves, burn-down, parity, licence estimate) | `--program program.yaml`, `--now` |
| `tools/new_automation.py NAME --template transactional` | the REFramework-style starting point | `--template python|transactional|node` |

What the scanner cannot see (known limits): it parses XAML *statically* - dynamic workflow invocations (`WorkflowFileName` from a variable), selectors assembled in expressions,
values held in Orchestrator or in external config files, and custom/third-party activity packages are counted but not resolved. Compiled `.nupkg` libraries (DLLs) are not decompiled.
Treat the inventory as an accurate *first pass* and read the flagged workflows.
