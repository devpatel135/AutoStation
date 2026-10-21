# Live logging: how every run is captured, and the one-time setup

## What is captured

Every time an installed automation runs, wrapper-core records and ships (over HTTPS, no git/ssh needed):

| Artefact | Content | Where it ends up |
|---|---|---|
| **run log** | status, duration, params, every event, per-item outcomes, retries (one event per attempt), LLM calls/decisions (hashes, never prompts), self-heal result, plain-English synopsis, error with category + traceback tail | `ingest/<ring>/run/<automation>/<run_id>.json` |
| **heartbeat** | host, ring, installed versions, phase (login / run start / run end), expected finish time | `ingest/<ring>/heartbeat/<host>/…` |
| **lifecycle** | installed / updated (with versions) / paused-seen / run-hung / decommissioned | `ingest/<ring>/lifecycle/…` |
| **evidence** (failures only) | screenshot, redacted page snapshot, error context | `ingest/<ring>/evidence/<automation>/<run_id>.json` |

Failure to deliver never fails a run: artefacts are spooled on the machine and retried on the next run / sign-in.

## The path

```
installed automation ──HTTPS (Contents API, DPAPI-protected token)──▶ private repo <owner>/AutoStation-logs, branch `ingest`
        ▲                                                                      │
        │ self-heal: every run + every sign-in checks the registry              ▼   every 30 min + after each release
 https://<owner>.github.io/<repo>/registry/  ◀── `release` workflow       `site` workflow: ingest → signals → dashboards → Pages
```

## One-time setup (3 minutes)

Run `tools/setup_logging.ps1` (uses the GitHub CLI), or do it by hand:

1. Create a **private** repo `AutoStation-logs` (with a README so it has a default branch).
2. Create a **fine-grained personal access token** (https://github.com/settings/personal-access-tokens/new): repository access *only* `AutoStation-logs`; permission *Contents: Read and write*.
3. Add it to the code repo as the secret **`LOGGING_TOKEN`** (Settings → Secrets and variables → Actions → New repository secret).
4. `tools/setup_live.py` / `tools/harden_repo.py` add a **ruleset** on the logs repo (no deletion, no force-push on `ingest` and the default branch). If you set things up by hand, add it yourself: Settings > Rules > Rulesets.
5. Run the **installers** and **site** workflows (or push any commit). The installer is then built *connected* and the dashboards turn live as soon as an installed automation has run once.

Verify on a machine: `python -m wrapper_core doctor` (shows `logging token (DPAPI): present` and where logs go) — or open the logs repo and look for `ingest/…` files.

## Security notes (read these)

* **The exposure, stated plainly.** The connected installer **contains the token** (DPAPI-protected at install time, but present inside the `.exe`) and the installer is a **public download**. A GitHub personal access token cannot be made write-only: *Contents: read and write* lets whoever extracts it **clone the whole private logs repo** (host names, hashed user ids, automation events) **and** add, overwrite or delete files on branches the ruleset does not protect. What reduces the damage:
  * `tools/harden_repo.py` (run by `tools/setup_live.py`) puts a ruleset on the logs repo that forbids deleting or force-pushing the `ingest` branch and the default branch, so history cannot be rewritten or wiped with the stolen token (rulesets on *private* repos need a paid GitHub plan; on a free plan do step 4 by hand if your plan allows it, or accept the residual risk).
  * The read side (dashboards, health issues) can use a **second, read-only token** (`LOGS_READ_TOKEN`, Contents: read only) so the write token is needed nowhere except inside installers; keep both in the `production` environment (`tools/setup_live.py --env-secrets`).
  * **Treat every logged value as untrusted and advisory**: it can be forged by anyone holding the token. The site escapes everything, ingest validates and bounds every record (timestamps in the future or before 2020 are rejected, hosts must be plain names), analytics ignore malformed artefacts, forged signals for automations the manifest does not list never open issues, and nothing auto-closes on a thin, stale or damaged data set (`tools/ops_issues.py`).
  * **Rotation**: create a new token, run `python tools/setup_live.py --only-secrets` (add `--env-secrets` if you use the environment), re-run the **installers** workflow, then revoke the old token. Machines installed with the old token keep spooling locally until they are reinstalled or updated. Do this on a schedule (the token expires anyway) and immediately if you suspect extraction.
  * If even this is unacceptable, do not use the public installer for connected builds: build privately and distribute through your software-distribution tool, or move logging behind a relay that authenticates each install.
  If that is unacceptable, build installers privately (publish them to an internal share instead of the public release) or keep the repo private and distribute the installer through your software-distribution tool.
* GitHub Pages is **public**. The live dashboards show run logs: host names, hashed user ids, automation events, failure screenshots/context. No secrets or LLM prompts are logged, and `sensitive_keys` are redacted before any LLM sees them, but the *raw* run log is in the logs repo and the dashboards render it. Do not put sensitive data in events.
* Token expiry: fine-grained tokens expire. When it does, new runs spool locally (nothing is lost) and the site shows stale data. Rotate by creating a new token, updating `LOGGING_TOKEN`, and re-running **installers** (existing machines keep spooling until reinstalled or updated; see docs/RUNBOOKS.md §4).

## Self-healing / updates

Each run starts with a self-heal pass against the registry above (manifest + sha256-verified zips). `release` publishes every push to `main` to **all rings** automatically, and installed machines also check at every Windows sign-in (`wrapper_core maintain`). The kill-switch (`status: paused`) is honoured on the same path.

## What the site derives from the logs

Every run record, heartbeat and lifecycle event is validated on ingest (`validate.py`) and again when the archive is read; a malformed or oversized record is quarantined and never reaches a page. From the validated data the site computes, in `automation_logging/reliability.py`:

* **Reliability / SLO** (`/reliability/`): success rate, error-budget burn, MTTR / MTBF, flakiness, duration percentiles and anomalies, on-time rate.
* **Errors & remediation** (`/errors/`): error categories (a closed set: anything else is recorded as `UNKNOWN`), error signatures, regressions after a release, machine / version concentration, remediation outcomes.
* **Signals** (alerts): `ERROR_BUDGET_BURN`, `FLAKY`, `DURATION_ANOMALY`, `REGRESSION_AFTER_RELEASE`, `REMEDIATION_FAILING`, `PERSISTENT_MANUAL_STEP`, `MACHINE_UNHEALTHY`, alongside the compliance signals.
* **Exports**: `/metrics.json`, `/metrics.prom` (Prometheus text), `/fleet-status.json` and `/badges/*.svg`.

Optional producer events (all validated strictly; absent = the sections stay empty): `remediation_attempt` `{playbook, category, action, outcome in fixed|not_fixed|skipped|escalated|recommended, detail <= 300 chars, duration_ms, attempt}`, the run key `remediation` `{category, fixed, playbooks}`, `queue_stats`, `approval_requested` / `approval_decided`, and `manual_step_required` / `untrusted_redirect` (drive the "needs a person" signal).

Because the logging credential ships in a public installer, none of this is authenticated: every figure is advisory and every page says so.
