# wrapper-core API reference

`import wrapper_core as wrapper` — stdlib only, Python 3.9+. Node equivalents live in `execution-framework` (`fw.wrapper.*`,
same `events.jsonl` format). Everything here is also introspected live by `wrapper_core.context` for the dev-assist LLM
prompts, so this page cannot drift from what models see.

## Telemetry

| Call | Purpose |
|---|---|
| `log_event(event, data=None, *, sensitive=False, sensitive_keys=None, **fields)` | Structured event in the run log. `sensitive_keys=["ssn"]` keeps the value in the raw log but replaces it with `[REDACTED]` before **any** LLM sees it. |
| `log_item(item_id, outcome, *, duration_ms=None, **fields)` | One item's outcome: `success` \| `skipped` \| `failed`. Feeds throughput, P50/P95/P99, skip-rate and item-failure-rate analytics. |
| `track_item(item_id, **fields)` | Context manager: times the block, logs success, or failed (+ re-raises). Set `it["outcome"]="skipped"` inside to override. |
| `set_result(**values)` | Small structured result returned to the runner (appears as `result` in the run log). |
| `emit_trigger(target, params, reason)` | Ask the platform to start another automation (event-driven orchestration; CI turns it into a `triggers.json` entry). |

## Resilience

| Call | Purpose |
|---|---|
| `run_item(item_id, fn, *args, policy=None, **kw)` | Run `fn` for one item with retry + telemetry. DATA_ERROR → item skipped (run continues, reason logged). Unresolvable → logs `failed`, raises `Escalation`. |
| `run_with_retry(fn, policy=None, *, item_id=None, auth_refresh=None, use_llm=True)` | The retry engine without item logging. Returns the value or a `Skipped`; raises `Escalation`. |
| `@entrypoint` | Wrap `main`: uncaught errors are classified, `error.json` written (category, type, message, traceback tail, `escalated`), non-zero exit. Always use it. |
| `RetryPolicy` | `max_retries, network_retry_wait_s, backoff_multiplier, vpn_tool, skip_on_data_error, escalate_after_retries`. Default comes from the manifest's `error_handling` via `AUTOMAGIX_ERROR_POLICY`. |
| Exceptions | `NetworkError`, `AuthExpired`, `SystemUnavailable`, `DataError`, `LogicError`, `Escalation`, base `AutomationError`. Raise them to steer the classifier; ordinary exceptions are classified automatically (`ConnectionError` → NETWORK, `KeyError` → LOGIC, `ValueError` → DATA, HTTP 401/403/5xx …). |

Retry behaviour (every attempt logs exactly one `retry_event` with the outcome):

| Category | Action |
|---|---|
| NETWORK_ERROR | run `vpn_tool` → wait `network_retry_wait_s` → retry up to `max_retries` → escalate |
| AUTH_EXPIRED | `auth_refresh()` once → retry once → escalate |
| SYSTEM_UNAVAILABLE | exponential backoff, ≤3 → escalate (+ SLOW signal) |
| DATA_ERROR | skip item (if `skip_on_data_error`), continue |
| LOGIC_ERROR | never retry; escalate with traceback |
| UNKNOWN | LLM classifies `{transient\|permanent\|unknown}`; no LLM/invalid ⇒ **ESCALATE** (fail safe) |

## LLM seam

```python
out = wrapper.llm_call(
    "Classify the document and extract key fields.",    # trusted instructions, written by you
    context=document_text,                              # UNTRUSTED data: scraped text, logs, documents
    schema={"type": "invoice|tax_form|other", "vendor": "string?", "amount": "number?"},
    caller="my-automation/extract",                     # audit trail
    template="extract@1",                               # optional: logged with an input hash (never the prompt)
    classification="confidential",                      # optional: refused if above the automation's approved LLM level
    decision_log=True,                                  # emit an `llm_decision` audit event
)
if out is None:   # disabled / failed / invalid after one corrective retry  ->  you MUST have a fallback
    ...
```

* `schema` may be a **shape dict** (above; `"x?"` nullable, `"a|b"` enum, `["string"]` array), a **JSON-Schema subset**, or a **pydantic model** if installed.
* Returns `str` (no schema) / validated `dict`|`list` (schema) / `None`. Never raises for LLM problems.
* Payloads never hold the key: in a run the call is routed through the runner's loopback broker.
* Safeguards you do not have to write: anti-injection system prompt, `<untrusted_data>` delimiting (delimiter text inside context is neutralised), schema validation + one retry, classification gate, call logging.
* `llm_call_ex(...)` returns `LLMResult(status, value)` when you need to distinguish `disabled | error | schema_failure`.
* `log_llm_decision(caller, template, input_hash, decision, reasoning)` for custom audit events.

## Parameters, assets, environment

| Call | Purpose |
|---|---|
| `params()` / `param(name, default)` | The slot's (or trigger's) `params` — `AUTOMATION_PARAM_*`. |
| `is_dry_run()` | `--dry-run`: skip every side effect. |
| `assets()` | Dynamic non-secret config for this ring (manifest `assets`), refreshed every run. |
| `run_dir()` | Scratch directory for this run. |

Secrets: never in the manifest or payload. Use the user profile / Windows Credential Manager (`wrapper_core.secrets.credman_read("AUTOMAGIX/<name>")`).

## Operational commands

```bash
python -m wrapper_core run <automation> [--slot S] [--dry-run]    # what Task Scheduler launches
python -m wrapper_core install|uninstall <automation>            # what the installer calls
python -m wrapper_core maintain                                   # login: re-assert tasks + heartbeat
python -m wrapper_core status                                     # installed versions, last runs, spool depth
python -m wrapper_core dev <payload_dir> [--param k=v]            # offline lifecycle
python -m wrapper_core assist scaffold|review|test|design …       # LLM-assisted development
python -m wrapper_core secrets protect|credman-set …              # DPAPI / Credential Manager helpers
```

## Run lifecycle

```
lock → flush log spool → self-heal (manifest, kill-switch, install/update, assets, triggers)
     → maintenance window → compatibility check → [broker up] execute payload (watchdog, process-level retry)
     → status (success|partial|failed|hung) → synopsis (LLM, or deterministic) → run log + heartbeat → notify
```

## Asset overrides, approvals and fixture tests

**Assets (non-secret configuration).** `wrapper.assets()` returns the merged assets, lowest to highest precedence:

1. ring default: `config/assets.json` (written by self-heal from the manifest `assets:` block)
2. machine override: `%ProgramData%\AUTOMAGIX\assets.json` (admin-managed) or `$AUTOMAGIX_MACHINE_ASSETS`
3. user override: `config/assets.user.json`
4. slot parameters named `asset_<name>` (typed like the ring default: `asset_batch_size: "40"` becomes the integer 40)
5. `AMX_ASSET_<NAME>` environment variable (development only; read by `credentials.get_asset`)

Overrides are validated: names `[A-Za-z][A-Za-z0-9_.-]{0,63}`; values are scalars or small lists/mappings of scalars; files <= 256 KB; **secrets are refused**
(names like password/token/secret/api key/credential, and values that look like keys, tokens or URLs with passwords); an override must have the same type as
the ring default. Rejected entries are dropped and logged to `logs/wrapper.log`; they never stop a run. Inspect with `python -m wrapper_core assets show`
(prints each value and the layer it came from) and `python -m wrapper_core assets validate <file>`.

**Approvals** (`wrapper_core.activities.approvals`): `create`, `get`, `list_tasks` (also `approvals.list`), `decide`, `wait`, `expire_due`, `prune`, plus
`python -m wrapper_core approvals list|show|approve|reject|ui|prune`. Telemetry events: `approval_requested` and `approval_decided` (ids, decision and a user hash only).

**Fixture tests.** `python -m wrapper_core test <payload_dir> [--fixtures cases.json|cases.yaml] [--case NAME] [--json]` runs each case in a throw-away sandbox
through the real lifecycle (offline, mock LLM) and checks its `expect` block. Case keys: `name`, `params`, `env` (only `AMX_*`, `AUTOMATION_*`, `TEST_*`),
`dry_run`, `llm`, `ring`, `error_handling`, `expect`. `expect` keys: `status` (string or list), `exit_code`, `result` (subset match), `result_keys`,
`items` (`success`/`skipped`/`failed`/`total` counts), `events` (`{event, min, max, data}`), `error_contains`, `max_duration_ms`. In pytest use
`testing.run_payload(...)` with `testing.assert_run(run, status="success", items={"failed": 0})`. Exit code 1 if any case fails.


## Tray indicator ("a train is running")

While any automation runs, a train icon sits in the Windows notification area with the tooltip `AUTOSTATION - <automation> running - mm:ss`; it disappears when the run ends. Every automation gets it, each run gets its own icon, and a manual `python -m wrapper_core run <name>` shows it too.

* **How it works:** `runner.run_automation` calls `tray.start_for_run` right after taking the run lock and `tray.stop` in its `finally`. The icon lives in a separate hidden helper process (`python -m wrapper_core tray --name N --pid P ...`, started with `pythonw` when present, so no console window), so a missing or crashed icon can never fail or delay a run. `stop` writes a stop file so the helper removes its own icon (killing it would leave a ghost icon until the mouse passes over it); the helper also exits when the runner PID is gone, after a lifetime cap (4 h 15 min), on logoff/shutdown, and quietly when there is no desktop (service session).
* **Menu:** right-click for the automation name, *Open run log folder* and *Open AUTOSTATION dashboard* (https only, derived from `registry_url`); double-click opens the run folder.
* **Switches (any one turns it off):** `AUTOMAGIX_NO_TRAY=1`; `AUTOMAGIX_NO_TOAST=1` (CI/tests); `"tray": false` in `config/platform.json` (whole machine); `tray: false` in an automation's `automation.yaml` (one automation); non-Windows; dry runs; pytest.
* **Preview:** `python -m wrapper_core tray --demo 8` shows the icon for 8 seconds (`--verify` prints whether Windows registered it). `python -m wrapper_core doctor` has a line for it.
* **Icon:** drawn from pixel art in `wrapper_core/_trayicon.py` (16/32/48 px); `wrapper_core/assets/train.ico` is generated from it (`python -m wrapper_core._trayicon --write wrapper_core/assets/train.ico`) and a test keeps the two identical.
* **Public helpers (`wrapper_core.tray`):** `sanitize_name`, `tooltip`, `format_elapsed`, `pid_alive`, `site_url`, `disabled_reason`, `start_for_run(paths, name, cfg, dry_run=False) -> TrayHandle | None`, `stop(handle)`.


## Remediation (`wrapper_core.remediation`, `playbooks`, `debugbundle`)
Operator view: [RUNBOOKS.md, Automatic remediation](RUNBOOKS.md). Public surface:

* `diagnose(error: dict, env: dict | None) -> Diagnosis(category, confidence, evidence, signature, automation)` - deterministic; `error` keys: `message, type, category, traceback_tail, stderr_tail, exit_code, status, result_state`; `env` keys: `root, automation, runtime`. Never raises.
* `Remediator(paths, automation, cfg, dry_run=False, force=False, lister=, killer=, scheduler_runner=, http_head=, now=).handle(diagnosis, run_id=None, allow_retry=True) -> Report(category, attempts, retry, fixed, escalated, recommendations, debug_bundle)`.
* `handle_failure(...)`, `preflight(...)`, `proactive_pass(...)`, `fix_from_doctor(...)`, `diagnose_environment(...)`, `load_overrides(paths, name)` / `write_override` / `clear_override` (whitelist: `mode=native`), `needs_person(...)`, `scrub_text(text)`.
* Add a playbook by subclassing `remediation.Playbook` (`name, categories, risk, max_attempts, cooldown_s, retry_after_fix; check, fix, verify, rollback`) and listing it in `playbooks.all_playbooks()`. Only `risk = "safe"` playbooks run unattended.
* `debugbundle.create_bundle(paths, run_id=None, reason="", out=None, max_total=5 MB) -> Path`.
* Runner contract: the payload sees `AUTOMATION_PARAM_MODE=native` when the override is active (`record.runtime_overrides`).

## Hardening notes (review follow-ups)

* **Remediation retries:** after a fix the payload is re-run only when nothing had been processed yet and the failure is a startup problem (DevTools blocked, stale profile lock, missing/corrupt component, Node missing, version mismatch). Any other category - and any run that already processed items - needs the automation to opt in with `remediation_retry: true` in `automation.yaml` (a promise that a re-run is idempotent). The fix itself stays applied; the run record carries `remediation.retry_skipped` with the reason.
* **Repairs are transactional:** a component repair writes `state/repair-pending/<name>.json` before it touches anything and reports `fixed` only with positive evidence (something was reinstalled, the VERSION marker and install record are back). A repair that dies half way is undone at the next run start.
* **Live runs are never disturbed:** `release_browser_profile_lock` does nothing while another live process holds the automation's run lock (the runner's own lock does not count), and re-lists processes immediately before every kill.
* **Time zones:** a schedule whose `timezone` cannot be resolved on this machine (stock Windows Python has no tz database; `pip install tzdata` into the embedded Python fixes it) is NOT registered as a fixed-time task, `doctor` reports it as FAIL, and window/blackout checks log a warning once. Task Scheduler needs local time, so the generator converts with today's UTC offset: when the schedule zone and the machine zone switch DST on different dates, the task drifts by an hour for those weeks - use the machine's zone, or UTC, for time-critical slots.
* **Cron:** `0 0 31 2 *` (a day that never exists) is refused; a stepped `*/n` day field next to a restricted other day field is refused as ambiguous (classic cron and other schedulers disagree on AND/OR).
* **Machine-wide assets** (`%ProgramData%\AUTOMAGIX\assets.json`) are honoured only when an administrator owns the file; a file created by a standard user in that shared folder is ignored (reported in `wrapper.log`). NaN/Infinity values are rejected.
* **Approvals:** the local page is threaded with a 5 s per-connection timeout, refuses a shared port (`SO_EXCLUSIVEADDRUSE` on Windows), strips control/format/bidi characters from everything it displays, and only authenticated requests keep it alive. It is not an identity system: the audit trail records the OS user that ran the page or CLI, and any local process holding the one-time token can decide, so requester and approver are separated by process and token, not by account. Decided tasks older than 30 days are pruned automatically once a day.
* **Debug bundles** carry the run of the automation that failed (never another automation's newest run); automations classified `confidential` or `restricted` contribute metadata only (no run output files, no synopsis). Bundles are written to a temp file and renamed, so a failure never leaves a partial zip.
* **Queue:** completing or failing an item is fenced on the claim (`started_at` + `retry_no`): a worker whose claim was re-queued to someone else gets a LogicError instead of finishing the other worker's claim.
* **Test harness:** `testing.run_payload` sets `AUTOMAGIX_NO_REMEDIATION=1` so a failing fixture stays a failing fixture (pass `env={"AUTOMAGIX_NO_REMEDIATION": "0"}` to test remediation itself).
