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
4. Recommended: in the logs repo add a **ruleset** on the default branch (restrict updates, deletions, force pushes) so the token can only ever add files on the `ingest` branch.
5. Run the **installers** and **site** workflows (or push any commit). The installer is then built *connected* and the dashboards turn live as soon as an installed automation has run once.

Verify on a machine: `python -m wrapper_core doctor` (shows `logging token (DPAPI): present` and where logs go) — or open the logs repo and look for `ingest/…` files.

## Security notes (read these)

* The connected installer **contains the token** (DPAPI-protected at install time, but present inside the `.exe`) and the installer is a **public download**. Anyone who extracts it can add or overwrite files in the logs repo (that is all the token can do, given step 4). Treat the logs repo as untrusted input: the site escapes everything, and analytics ignore malformed artefacts.
  If that is unacceptable, build installers privately (publish them to an internal share instead of the public release) or keep the repo private and distribute the installer through your software-distribution tool.
* GitHub Pages is **public**. The live dashboards show run logs: host names, hashed user ids, automation events, failure screenshots/context. No secrets or LLM prompts are logged, and `sensitive_keys` are redacted before any LLM sees them, but the *raw* run log is in the logs repo and the dashboards render it. Do not put sensitive data in events.
* Token expiry: fine-grained tokens expire. When it does, new runs spool locally (nothing is lost) and the site shows stale data. Rotate by creating a new token, updating `LOGGING_TOKEN`, and re-running **installers** (existing machines keep spooling until reinstalled or updated; see docs/RUNBOOKS.md §4).

## Self-healing / updates

Each run starts with a self-heal pass against the registry above (manifest + sha256-verified zips). `release` publishes every push to `main` to **all rings** automatically, and installed machines also check at every Windows sign-in (`wrapper_core maintain`). The kill-switch (`status: paused`) is honoured on the same path.
