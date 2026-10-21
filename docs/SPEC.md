# AUTOSTATION Platform Specification (v1)

This is the contract between components. Every component in this monorepo implements its side of it.
If code and this document disagree, fix one of them in the same change.

Source of truth for *why*: `docs/assessment.html` (the architecture assessment). This file is the *what*.

## 1. Components

| Directory | Role | Language | Runs where |
|---|---|---|---|
| `components/wrapper-core` | Bootstrapper: scheduling, self-heal, run lifecycle, logging client, LLM seam, retry | Python 3.9+, **stdlib only** (pydantic optional) | Endpoint (VDI) + CI |
| `components/execution-framework` | Shared runtime: Node.js, Playwright, MCP, CDP, adapters, resilience, agent loop | Node 18+ | Endpoint |
| `components/automation-builder` | Docker build image, NSIS installer template, packaging, signing, prompt library | Python / PowerShell / NSIS | CI |
| `components/automation-logging` | Log ingestion, analytics, compliance signals, LLM diagnosis, alerting, dashboard | Python (uses wrapper-core) | CI |
| `components/automation-registry` | Manifest, schema, validation, portal + admin panel + catalog generator | Python + static HTML | CI → Pages |
| `components/automations/*` | One payload per automation | Python / Node | Endpoint |
| `tools/` | Scaffolder, repo-wide helpers | Python | Dev machine |

In production each directory is its own GitLab project (as in the assessment). The monorepo layout makes
the platform buildable and testable as one unit; each component carries its own `.gitlab-ci.yml` so it can be
split out with `git subtree split` without changes.

## 2. Endpoint layout

Root: `%LOCALAPPDATA%\AUTOMAGIX` (no admin required). Override with env `AUTOMAGIX_HOME` (used by tests and the dev harness).

```
AUTOMAGIX\
  config\
    platform.json          # written by installer: ring, registry_url, log_transport, log_remote, llm_endpoint, llm_model
    assets.json            # dynamic config assets (cache of manifest `assets` for this ring)
  secrets\
    logging_key.dpapi      # DPAPI (CurrentUser) encrypted SSH private key
    registry_token.dpapi   # optional; preferred location is Windows Credential Manager target AUTOMAGIX/registry_token
  wrapper-core\            # python package dir (contains wrapper_core\), plus VERSION file
  execution-framework\     # node runtime + node_modules + framework js, plus VERSION file
  python\                  # embedded CPython (+ tzdata) laid down by the installer; NOT under execution-framework (self-heal swaps that dir while Python runs)
  automations\<name>\      # payload (automation.yaml + code), plus VERSION file
  state\
    installed.json         # {"components": {"wrapper-core": "0.1.0", ...}, "automations": {"name": "1.0.0"}}
    runs\<name>.json       # last run summary (status, started_at, ended_at)
    consumed_triggers.json
    locks\<name>.lock
    toast\<name>.status.txt   # human-readable status when paused/halted
  spool\                   # unsent log entries (flushed next run). `<kind>-<id>.json`
  runs\<run_id>\           # per-run scratch: events.jsonl, result.json, stdout.log, stderr.log (pruned after 14 days)
  logs\wrapper.log         # wrapper-core's own diagnostic log (rotating)
  bin\                     # generated launch scripts (run-<name>.cmd, maintenance.ps1)
```

Startup-folder maintenance script: `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AUTOMAGIX-maintain.cmd`
→ runs `python -m wrapper_core maintain` at every login: re-asserts all Task Scheduler entries, sends a heartbeat.

## 3. Environment variables

Set by wrapper-core for the automation child process:

| Var | Meaning |
|---|---|
| `AUTOMAGIX_HOME` | root dir |
| `AUTOMAGIX_RUN_ID` | run id (`YYYYMMDDTHHMMSSZ-<8 hex>`) |
| `AUTOMAGIX_RUN_DIR` | per-run dir; SDKs append events to `events.jsonl` here |
| `AUTOMAGIX_AUTOMATION` | automation name |
| `AUTOMAGIX_AUTOMATION_VERSION` | payload version |
| `AUTOMAGIX_SLOT` | schedule slot id (e.g. `daily`, `monday-weekly`) |
| `AUTOMAGIX_RING` | `dev` \| `pilot` \| `broad` |
| `AUTOMAGIX_BROKER_URL` | `http://127.0.0.1:<port>` — loopback broker for LLM calls (§8) |
| `AUTOMAGIX_BROKER_TOKEN` | per-run bearer token for the broker |
| `AUTOMAGIX_FRAMEWORK_DIR` | execution-framework dir (Node automations `require` from here) |
| `AUTOMATION_PARAM_<NAME>` | one per entry of the schedule slot's `params` (upper-cased name) |
| `AUTOMAGIX_DRY_RUN` | `1` when run via `--dry-run` |

Read by wrapper-core / CI only (never exported to payload children):

| Var | Meaning |
|---|---|
| `AUTOSTATION_LLM_ENDPOINT` | OpenAI-compatible chat-completions URL. `mock://` = deterministic offline model |
| `AUTOSTATION_LLM_KEY` | API key (falls back to Credential Manager target `AUTOMAGIX/llm_key`) |
| `AUTOSTATION_LLM_MODEL` | model name (default `default`) |
| `AUTOMAGIX_LOG_TRANSPORT` | `git-ssh` \| `dir` (overrides `platform.json`) |
| `AUTOMAGIX_LOG_REMOTE` | git remote URL (git-ssh) or directory (dir) |
| `AUTOMAGIX_REGISTRY_URL` | base URL or **local directory** of the registry (`manifest.yaml` is read from it) |
| `AUTOMAGIX_REGISTRY_TOKEN` | read-only token (dev/CI; endpoints use Credential Manager) |

## 4. Registry manifest (`manifest.yaml`)

Hand-edited YAML in `automation-registry`, validated by `registry validate` (JSON-Schema in `automation-registry/schema/manifest.schema.json`
and the same rules in `wrapper_core.manifest`). Edits arrive via merge requests: **the MR approval is the governance gate.**

```yaml
schema_version: 1
fleet:
  paused: false                 # global kill-switch: wrapper-core halts every automation
  paused_reason: ""
rings: [dev, pilot, broad]
compatibility:                  # wrapper-core ↔ execution-framework support matrix
  - wrapper_core: ">=0.1.0,<1.0.0"
    execution_framework: ">=0.1.0,<1.0.0"
components:
  wrapper-core:
    rings: {dev: "0.1.0", pilot: "0.1.0", broad: "0.1.0"}     # which version each ring should run
    versions:
      "0.1.0": {url: "artifacts/components/wrapper-core-0.1.0.zip", sha256: "<hex>"}
  execution-framework:
    rings: {dev: "0.1.0", pilot: "0.1.0", broad: "0.1.0"}
    versions:
      "0.1.0": {url: "artifacts/components/execution-framework-0.1.0.zip", sha256: "<hex>"}
assets:                         # dynamic config (non-secret). per ring, with `default` fallback
  default:
    internal_api_url: "https://api.example.internal"
    feature_flags: {run_synopsis: true}
  dev: {}
automations:
  automation-example-hello:
    description: "..."
    owner: "team-automation@example.com"
    owner_team: "Automation Platform"
    systems: ["none"]
    data_classification: internal       # public | internal | confidential | restricted
    status: active                      # active | paused  (per-automation kill-switch)
    paused_reason: ""
    llm_allowed_classification: internal  # max classification the LLM endpoint is approved for; automations above it never send content to the LLM
    rings: {dev: "1.1.0", pilot: "1.0.0", broad: "1.0.0"}
    versions:
      "1.0.0": {url: "artifacts/automations/automation-example-hello-1.0.0.zip", sha256: "<hex>", requires: {wrapper_core: ">=0.1.0,<1.0.0", execution_framework: ">=0.1.0,<1.0.0"}}
    schedule:
      timezone: America/New_York
      expected_duration_ms: {baseline: 120000, warn_threshold: 2.0}
      maintenance_windows:
        - {days: [Saturday, Sunday]}
        - {daily: "23:00-02:00"}
      slots:                            # one Task Scheduler task per slot
        - {id: daily,  type: daily,  time: "09:00", params: {date_range: yesterday}}
        - {id: weekly, type: weekly, days: [Monday], time: "08:00", params: {date_range: last_week}}
        # type: hourly (every: N hours) | poll (every_minutes: N — event/trigger driven)
    error_handling:
      max_retries: 3
      network_retry_wait_s: 60
      backoff_multiplier: 2.0
      vpn_tool: "tools/vpn_reconnect.ps1"      # relative to the automation payload; optional
      skip_on_data_error: true
      escalate_after_retries: true
    notify_on_complete: false
    triggers:                           # event-driven: start THIS automation when another finishes
      - {on_automation: automation-other, on_status: success, slot: poll, params: {source: other}}
```


### Schedule slot types and calendars (extension)

Besides `daily`, `weekly`, `hourly` and `poll`, a slot may be:

```yaml
      slots:
        - {id: month-end, type: monthly,  time: "18:00", day: last}                       # day: 1..31 | last | [1, 15, last]; months: [1, 4, 7, 10] optional
        - {id: bizdays,   type: weekdays, time: "07:30"}                                  # Monday-Friday
        - {id: office,    type: interval, every_minutes: 15, window: "08:00-18:00", days: [Monday, Tuesday]}
        - {id: quarter,   type: cron,     expr: "0 6 1 1,4,7,10 *"}                       # minute hour day-of-month month day-of-week
      blackouts:                                                                          # schedule-level: dates the automation must not run
        - "2026-12-25"
        - {date: "2027-01-01", reason: "New Year"}
        - {from: "2026-12-28", to: "2026-12-31", reason: "year-end freeze"}
```

Each slot becomes native Task Scheduler triggers (`ScheduleByMonth`, `ScheduleByWeek`, repetition inside a window, one trigger per distinct cron time).
Cron supports `*`, numbers, ranges, lists, steps, month and weekday names; if both day-of-month and day-of-week are restricted a day matches when either does.
A cron slot that fires at more than 48 distinct times per day is refused (use `interval`). Fixed days of the month cannot be shifted across a calendar day
when the schedule's time zone differs from the machine's by enough to change the date: such a slot is refused instead of mis-scheduled. Blackout dates are
enforced at run time: the run is recorded as `skipped_maintenance` with the blackout as its window, whatever fired it. `wrapper_core.schedule.next_runs(slot, after, n, tz, blackouts)`
is the pure function behind "when does this run next". `registry validate` checks all of it.

Rules enforced by `registry validate`: every ring version exists in `versions`; sha256 is 64 hex; `requires` is satisfiable by the
`compatibility` matrix for the ring versions; `paused` automations carry a `paused_reason`; maintenance windows parse;
`dev`-ring-only versions never appear in `broad`.

**Resolution (wrapper-core):** version for a component/automation = `rings[ring]`. Kill-switch halts if `fleet.paused` or
`automations[name].status == paused`. Dynamic `assets` = `assets.default` ⊕ `assets[ring]`.

**Signed manifests.** `manifest.yaml` is published with a detached Ed25519 signature, `manifest.yaml.sig` (JSON: `v`, `key_id` = first 8 hex
of the public key, `signed_at` ISO-8601 UTC, `manifest_sha256` of the exact manifest bytes, `sig` = base64 over the UTF-8 message
`autostation-manifest-v1\n<manifest_sha256>\n<signed_at>`). Endpoints pin the operator's public key(s) in `wrapper_core/trust.py`
(`PINNED_REGISTRY_KEYS`), `registry_pubkeys` in `platform.json`, or env `AUTOMAGIX_REGISTRY_PUBKEYS`. Once any key is configured, self-heal
refuses a manifest whose signature is missing, made by another key, or does not match the bytes, and one whose `signed_at` is older than the
newest it has accepted (anti-rollback, `state/manifest.signed_at`). A refused manifest never updates anything and never replaces the cache:
the run continues from the last verified cached manifest (re-verified on every read) and records `manifest signature invalid: <reason>` in
`self_heal.errors`. With no key configured, unsigned manifests are accepted with a logged warning (bootstrap). `python -m wrapper_core trust --show`
lists the pinned key ids; the operator signs with `tools/sign_registry.py` (private seed never leaves the operator's machine).

## 5. Payload descriptor (`automation.yaml`, inside every automation payload)

```yaml
name: automation-example-hello
version: 1.0.0
runtime: python            # python | node | powershell | exe
entrypoint: main.py        # relative to payload dir. python → `<python> main.py`; node → `<node> main.js`
args: []
requires: {wrapper_core: ">=0.1.0,<1.0.0", execution_framework: ">=0.1.0,<1.0.0"}
timeout_s: 1800            # hard kill after this
```

## 6. Log schemas (all JSON, `schema_version: 1`)

### 6.1 Events (`events.jsonl`, one JSON object per line, written by SDKs; any language)

Common fields: `ts` (ISO-8601 UTC), `event` (string), `data` (object), `sensitive_keys` (list of keys inside `data` that are sensitive).

| `event` | `data` |
|---|---|
| any custom name (`processed`, `flagged`…) | free-form |
| `item_processed` | `item_id`, `outcome` (`success`\|`skipped`\|`failed`), `duration_ms`, optional `reason`, `error_category`, `detail`, `routed_to`, … |
| `llm_call` | `caller`, `prompt_chars`, `duration_ms`, `status` |
| `llm_decision` | `caller`, `template`, `input_hash`, `decision`, `reasoning`, `error_type`?, `outcome`? |
| `llm_schema_failure` | `caller`, `error` |
| `retry_event` | `attempt`, `error_type`, `error_message`, `category`, `decision` (`retry`\|`skip`\|`escalate`), `source` (`deterministic`\|`llm`\|`fallback`), `reasoning`, `wait_s`, `outcome` |
| `run_hung` | `elapsed_ms`, `expected_ms` |
| `trigger` | `target` (automation), `params`, `reason` |
| `phase` | `name` (`self_heal`,`execute`,`log`), `status` |

Redaction: before ANY event content is placed into an LLM prompt, every key named in `sensitive_keys` (and every value of key `sensitive=true` passed to
`log_event`/`log_item`) is replaced with `"[REDACTED]"`. Sensitive fields are still written to the raw run log (which is access-controlled by GitLab).

### 6.2 Run log (`<run_id>.json`)

```json
{
  "schema_version": 1, "kind": "run",
  "run_id": "20261012T130000Z-ab12cd34", "automation": "automation-example-hello", "slot": "daily",
  "automation_version": "1.0.0", "wrapper_version": "0.1.0", "framework_version": "0.1.0", "ring": "pilot",
  "host": "VDI-0042", "user_hash": "sha256-prefix-12",
  "started_at": "...Z", "ended_at": "...Z", "duration_ms": 1234,
  "expected_finish_at": "...Z",
  "status": "success|partial|failed|halted_paused|skipped_maintenance|hung|self_heal_failed",
  "exit_code": 0,
  "params": {"date_range": "yesterday"},
  "events": [ ... §6.1 ... ],
  "items": {"total": 3, "success": 2, "skipped": 1, "failed": 0},
  "error": {"category": "NETWORK_ERROR", "type": "ConnectionError", "message": "...", "traceback_tail": ["..."], "attempts": 3} ,
  "synopsis": "Plain-English summary…", "synopsis_source": "llm|deterministic",
  "self_heal": {"installed": ["wrapper-core 0.1.0 -> 0.2.0"], "errors": []}
}
```
`status` meanings: `success` all done; `partial` run ok but ≥1 item failed; `failed` run error; `halted_paused` kill-switch; `skipped_maintenance` maintenance window; `self_heal_failed` payload missing/incompatible/unverifiable; `hung` the watchdog fired AND the run was killed at `timeout_s`. Extra fields: `hung_detected` (bool; watchdog fired even if the run later finished), `halt_reason` (kill-switch), `trigger_id` (event-triggered runs), `result` (object set via `set_result`). The watchdog fires when elapsed > `baseline × warn_threshold`; `expected_finish_at` = start + `baseline`.

### 6.3 Heartbeat (artefact kind `heartbeat`, path `heartbeat/<host>/<id>.json`; analytics keeps the latest per host)

`{"schema_version":1,"kind":"heartbeat","host","user_hash","ts","phase":"login|run_start|run_end|idle","automation":null|"name","run_id":null|"id","expected_finish_at":null|"...Z","versions":{"wrapper-core":"0.1.0","execution-framework":"0.1.0","automations":{"name":"1.0.0"}},"ring":"pilot"}`

### 6.4 Lifecycle event (artefact kind `lifecycle`, path `lifecycle/<automation-or-host>/<id>.json`)

`{"schema_version":1,"kind":"lifecycle","event":"installed|updated|uninstalled|decommissioned|paused_seen|run_hung","automation":"name"|null,"host","user_hash","ts","detail":{}}`

## 7. Transport & ingestion

wrapper-core writes every artefact through a `LogTransport` (`wrapper_core.logging.transport`):

* `git-ssh` (production): the shared key is write-only **by construction** — the endpoint never fetches. For each artefact it creates a throwaway
  repo, commits one file, and pushes the orphan commit to a **new unique ref** `refs/heads/ingest/<ring>/<kind>/<automation-or-host>/<id>`.
  GitLab push rules on `automation-logging` allow only `ingest/*` (protected, no force, no delete). Compromise ⇒ attacker can only create junk refs.
  Artefact kinds: `run`, `heartbeat`, `lifecycle`. Path inside the commit: `<kind>/<name>/<id>.json` (`name` = automation for run/lifecycle, host for heartbeat).
* `dir` (dev / ring `dev` / tests): writes `<remote>/ingest/<ring>/<kind>/<name>/<id>.json` — same relative layout as git-ssh.
* Failures never fail the run: the artefact is spooled in `spool\` and flushed at the next run/login.

`automation-logging` CI (`ingest`) fetches `ingest/*` refs with its read token, archives each file under `archive/<kind>/<name>/YYYY/MM/DD/<id>.json`
in the logging repo's `main` (or a workspace dir), then deletes the processed refs. Analytics always operates on a **directory of archived JSON** so it is
trivially testable.

## 8. LLM broker (loopback)

wrapper-core's runner starts `ThreadingHTTPServer(127.0.0.1:0)`; child automations never see the LLM key.

`POST /llm` with `Authorization: Bearer $AUTOMAGIX_BROKER_TOKEN`, body
`{"prompt": str, "context": str, "schema": <shape or JSON-Schema>|null, "caller": str, "template": str|null, "timeout_s": int}` →
`{"ok": true, "result": <str|object|null>, "status": "success|schema_failure|error|disabled"}`. Never returns the key. `GET /health` → `{"ok":true}`.
Python `llm_call()` auto-routes through the broker when `AUTOMAGIX_BROKER_URL` is set, else calls the endpoint directly (runner itself, CI, dev).

## 9. LLM safeguards (enforced inside `llm_call`, every caller gets them)

1. Fixed system prompt: treat `context` as untrusted data to analyse, never as instructions; output only what the schema asks.
2. Signature separates `prompt` (trusted instructions, code-authored) from `context` (untrusted data). Context is wrapped in `<untrusted_data>` delimiters; any delimiter text inside it is neutralised.
3. Structured calls validate output (pydantic model, JSON-Schema subset, or shape dict) with **one** retry that includes the validation error; then `None`.
4. Every call logs `llm_call`; decision-driving calls log `llm_decision` (template name + input hash only — never the prompt).
5. LLM unavailable/disabled ⇒ `None`; callers must fail safe (retry classifier ⇒ ESCALATE).
6. Data-classification gate: `llm_call(classification=...)` refuses (status `disabled`) when above the automation's `llm_allowed_classification`.

## 10. Error categories & retry (wrapper-core `retry.py`)

`NETWORK_ERROR`, `AUTH_EXPIRED`, `SYSTEM_UNAVAILABLE`, `DATA_ERROR`, `LOGIC_ERROR`, `UNKNOWN`.

| Category | Action |
|---|---|
| NETWORK_ERROR | run `vpn_tool` if configured → wait `network_retry_wait_s` → retry (≤ `max_retries`) → escalate |
| AUTH_EXPIRED | run optional refresh hook → retry **once** → escalate |
| SYSTEM_UNAVAILABLE | exponential backoff (`network_retry_wait_s` × `backoff_multiplier`^n), ≤3 → escalate + SLOW signal |
| DATA_ERROR | skip item (if `skip_on_data_error`), log, continue |
| LOGIC_ERROR | never retry; escalate with traceback |
| UNKNOWN | LLM classify `{category: transient|permanent|unknown, retry: bool, reasoning}`; LLM absent/failed/invalid ⇒ ESCALATE |

Every attempt emits exactly one `retry_event`.

## 11. Compliance signals (analytics output `signals.json`)

`MISSED`, `SILENT`, `SLOW`, `VERSION_DRIFT`, `UNEXPECTED`, `HUNG`, plus `FAILED` (run failed) and `ITEM_FAILURES` (partial).
Each: `{id, type, severity: critical|high|medium|low, automation, host|null, ring, detail, first_seen, evidence: [run_ids], diagnosis: {...}|null, owner}`.
`id` = stable fingerprint (`sha1(type|automation|host|window)`), used by alerting for de-duplication.

## 12. Triggers (event-driven + on-demand)

CI computes `triggers.json` (published next to `manifest.yaml`): list of `{id, automation, slot, params, created_at, source}`.
Sources: manifest `triggers:` rules matched against newly-ingested run logs, or an operator action (admin panel → pipeline → `registry trigger add`).
`poll`-type slots call `python -m wrapper_core run <name> --slot poll`; self-heal fetches `triggers.json`, consumes unseen ids for that automation
(recorded in `state\consumed_triggers.json`), and runs once per trigger with its params. No poll hit ⇒ the run exits quietly with no log noise.

## 13. Versioning / compatibility

SemVer for components. `registry validate` + CI smoke test (`tools/compat_smoke.py`) enforce the compatibility matrix.
`schema_version` bumps are breaking; readers must reject unknown major schema versions.
