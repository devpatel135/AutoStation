# automation-logging

Log ingestion, analytics, compliance signals, LLM diagnosis, alerting and the fleet dashboard for AUTOSTATION.
Implements `docs/SPEC.md` §6 (artefacts), §7 (transport & ingestion), §11 (signals), §12 (triggers).
Runs in GitLab CI only; there is no server. Python 3.9+, stdlib only, plus `wrapper_core` (shared `miniyaml`, `llm`,
`redact`, `manifest`, `mockllm`).

```
endpoint ──push new ref──▶ logging repo  refs/heads/ingest/<ring>/<kind>/<name>/<id>      (write-only key)
                              │ ingest job (Maintainer token): fetch ingest/*, archive, then delete refs
                              ▼
                       `archive` branch   archive/<kind>/<name>/YYYY/MM/DD/<id>.json  +  state/*.json
                              │
   analyze ─ analytics (KPIs, item metrics, retry/LLM stats, 8 compliance signals) ─ diagnose (LLM, redacted, cached)
   alert   ─ dedupe/remind/resolve → outbox · Teams/Slack · Jira · GitLab issue · e-mail
   pages   ─ public/index.html · data.json · signals.json · runs-latest.json · triggers.json
```

## The write-only SSH key (how it is actually write-only)

Git has no "write-only" permission: any key that can push can also fetch. The design therefore removes the *need* to read and
limits what a push can do. The endpoint never fetches; for each artefact it creates a throw-away repo with one commit and pushes it
to a **brand-new unique ref** `ingest/<ring>/<kind>/<name>/<id>` (see `wrapper_core.transport.GitSshTransport`). Configure the
logging repo like this (Settings > Repository):

| Setting | Value |
|---|---|
| Deploy key `automagix-logging-writer` | the **shared public key**, *Grant write permissions* on |
| Protected branch `main` | Allowed to push: **No one** (deploy key is *not* in the list); merge: Maintainers |
| Protected branch `archive` | Allowed to push: **Maintainers** (the CI token), force push off |
| Protected branch **`ingest/*`** (wildcard) | Allowed to push and merge: **the deploy key** (use "Deploy keys" in the list) and nobody else; **Allowed to force push: off** |
| Push rule *Branch name* (Premium) | `^(ingest/(dev\|pilot\|broad)/(run\|heartbeat\|lifecycle)/[A-Za-z0-9._-]{1,128}/[A-Za-z0-9._-]{1,128}\|main\|archive)$` |
| Push rule *Maximum file size* | `1` MB (run logs are a few KB; this bounds abuse) |
| Push rule *Prohibited filenames* | `(^\|/)(\.gitlab-ci\.yml\|\.gitattributes)$` (an artefact must never be able to alter CI) |

Why this holds up: the key can only *create* refs under `ingest/`; protected branches **cannot be deleted or force-pushed by it**
(deleting a protected branch is a Maintainer-only operation), so it cannot erase or overwrite other machines' pending logs. It cannot
touch `main`/`archive`. The ingest job uses a project access token with role **Maintainer** (scopes `read_repository`,
`write_repository`) precisely because deleting protected `ingest/*` refs needs that role. Compromise of the endpoint key means: an
attacker can create junk refs that match the name pattern (which `ingest` quarantines under `_rejected/` if the content is invalid)
and **write fake log entries** - the same blast radius the assessment accepted. Without GitLab Premium push rules the branch-name
restriction is unavailable: junk non-`ingest/` branches can then be created; the ingest job ignores them and a janitor schedule may
delete them.

Rotation: generate a new key pair, replace the deploy key, rebuild installers (runbook in `docs/`); the old key is revoked by removing it.

## Data flow, state and retention

* **Archive** lives on the orphan `archive` branch of this repo (durable, diffable, auditable; CI artifacts expire). Layout
  `archive/<kind>/<name>/YYYY/MM/DD/<id>.json`; `ring` is stored in the JSON. Duplicate artefacts (spool retries) are detected and
  dropped; invalid ones go to `archive/_rejected/` with a `.reason` file.
* **State** (`state/analytics.json` target-change timestamps for VERSION_DRIFT, `state/diagnoses.json` LLM cache,
  `state/alerts.json` delivery state, `state/alerts-outbox.jsonl`) is committed to the same branch so dedupe survives between pipelines.
* **Retention**: analytics loads 30 days (`window_days`); the `archive-prune` schedule (`PRUNE=1`, default 90 days via
  `ARCHIVE_RETENTION_DAYS`) deletes older day folders. The branch still grows in git history: squash it monthly
  (`git checkout --orphan new && git add -A && git commit && git push -f origin new:archive` with a Maintainer token) or move old
  months to a package/bucket.
* **Schedules**: every 15 min `INGEST_ONLY=1` (ingest only); hourly full pipeline; monthly `PRUNE=1`.

## Compliance signals (SPEC §11)

| Signal | Rule | Default severity |
|---|---|---|
| `MISSED` | a non-poll slot's expected time (manifest timezone) + grace `max(15 min, 0.25 x baseline)` passed with no run of that slot within a window of `max(4 x grace, 6 h)` after it; skips paused automations, fleet pause, maintenance windows, automations never seen | high (critical if >=2) |
| `SILENT` | latest heartbeat *and* run for a host older than `silent_after_h` (36) while it still has installed automations (decommissioned ones excluded) | high |
| `SLOW` | latest run duration > `warn_threshold x baseline` | medium |
| `VERSION_DRIFT` | latest run on a version other than the ring's target, but only after `drift_grace_h` (6) since the target changed (tracked in state) **or** when the run reports self-heal errors; also wrapper-core / execution-framework | medium |
| `UNEXPECTED` | a run started inside a declared maintenance window | medium |
| `HUNG` | run killed by the watchdog timeout, or watchdog fired (`hung_detected`) | critical |
| `FAILED` | latest run failed; trailing streak counted | high (critical at 3) |
| `ITEM_FAILURES` | run completed but items failed | medium (high at >=30%) |

Signals are computed per `(automation, host)` from the *latest* run, so they resolve by themselves when a good run arrives.
`id = sha1(type|automation|host|window)[:16]` where `window` is the error fingerprint (digits/ids normalised), the expected
slot time, or a constant - so the same ongoing problem keeps one alert id across days. Known limitation: `MISSED` is evaluated per
automation, not per host - one healthy host masks a silent one for the same automation; per-host silence is covered by `SILENT`.

## LLM diagnosis safeguards

Eligible: `FAILED`, `HUNG`, `ITEM_FAILURES`. Events are redacted by `sensitive_keys` *before* the context is built; log text only ever
goes in the untrusted `context` (the prompt is a constant); output is schema-validated (`root_cause`, `category`,
`suggested_action`, `confidence`); automations whose `data_classification` exceeds `llm_allowed_classification` - or that are not in
the manifest - are never sent; results are cached by error fingerprint; one call serves the same error on many hosts; hard cap per run
(`--max-llm-calls`, default 20). No endpoint configured = diagnosis skipped, never an error.

## Run it locally with sample data

```bash
cd components/automation-logging
python -m automation_logging sample --out ./demo            # archive/, manifest.yaml, alerting.yaml (14 days, 4 automations, 6 hosts)
export AUTOSTATION_LLM_ENDPOINT=mock://                      # offline deterministic "LLM"
python -m automation_logging build --archive demo/archive --manifest demo/manifest.yaml \
    --state demo/state --out demo/public --alert-config demo/alerting.yaml
# open demo/public/index.html ; demo/state/alerts-outbox.jsonl holds the alerts that would be delivered
python -m automation_logging build ... --dry-run-alerts       # show what would be sent, change nothing
python -m pytest                                              # 65 tests, no network
```

The sample contains every signal type: a 3-day `LOGIC_ERROR` streak, a run that hid 12 failed items, a hung run, a slow run
on a drifted host with failing self-heal, a run inside a maintenance window, a skipped day (`MISSED`) and a host that went dark.

Commands: `ingest`, `analyze`, `diagnose`, `alert`, `triggers`, `dashboard`, `build` (all of them), `prune`, `sample`.
Exit codes: **signals never fail a job**; a stage exception or a failing alert channel does (so a broken Jira connection is noticed),
but the remaining stages - notably the dashboard - still run.

## Alert channels

`alerting.yaml` (see the file for the full schema): `channels` (type `file`, `stdout`, `webhook` [`teams`|`slack`|`json`], `email`,
`jira`, `gitlab-issue`), `routing` per severity, per-automation `owners` overrides, `quiet_hours`, `dedupe_window_h`,
`re_alert_interval_h`, `notify_resolved`. Lifecycle: `open` -> `reminder` (every interval while active) -> `escalated` (severity rose)
-> `resolved`; flaps within `dedupe_window_h` are silent; an alert counts as sent only if >=1 channel accepted it (otherwise it is
retried next run). Every alert carries automation, signal type, error category, LLM diagnosis, retry history, run synopsis and a log link
(`LOG_BASE_URL` + archive path). Secrets come only from environment variables (`JIRA_TOKEN`, `TEAMS_WEBHOOK_URL`, `SMTP_*`,
`GITLAB_ALERT_TOKEN`).

**Add a channel**: subclass `Channel` in `automation_logging/alerting.py`, implement `send(alert) -> {"ok": True, "detail": ...}`
(raise `ChannelError` on failure; use `self.ctx.http` / `self.ctx.env` so tests can inject fakes), add it to `CHANNELS`, reference its
`type` in `alerting.yaml`, and add a test with `FakeHttp` (see `tests/test_alerting.py`).

## Triggers (SPEC §12)

`triggers.json` is computed here from (1) manifest `triggers:` rules vs recent runs, (2) `trigger` events emitted by payloads
(`wrapper.emit_trigger`), (3) operator request files `requests/*.json` (`{automation, slot, params, requested_by}`). Ids are stable
hashes; chains are depth-limited (3); entries older than 7 days are pruned; paused targets never get triggers. The file is published
in this project's Pages (`public/triggers.json`); wrapper-core reads it from the registry base URL, so the registry pipeline must
publish/copy it next to `manifest.yaml` (contract point for the registry component).

## CI variables

See the header of `.gitlab-ci.yml`. In the monorepo `../wrapper-core` is used automatically; in a split project set
`WRAPPER_CORE_PROJECT_ID` / `WRAPPER_CORE_VERSION` to fetch the released package.
