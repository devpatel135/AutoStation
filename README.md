# AutoStation

An unattended-automation platform for locked-down Windows VDI fleets: a **thin per-user runtime** (Task Scheduler +
self-heal + logging + LLM seam), a **git-native control plane** (GitLab only — no new server), and **governed rollout**
(dev → pilot → broad rings, MR approval as the gate, kill-switch, rollback).

> Design rationale: [`docs/assessment.html`](docs/assessment.html) (open in a browser). Contracts between components:
> [`docs/SPEC.md`](docs/SPEC.md). Start building automations: [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

## What is in this repo

```
components/
  wrapper-core/          Python, stdlib-only. Bootstrapper + run lifecycle: Task Scheduler/startup-folder persistence,
                         self-heal & updates, kill-switch, maintenance windows, item telemetry, intelligent retry,
                         LLM seam (broker, safeguards, schema+retry), run synopsis, endpoint feedback, dev-assist CLI.
  execution-framework/   Node. Playwright(+Edge) · CDP · MCP server · site adapters · headless HTTP/cert client ·
                         LLM Pattern 3 (selector self-healing) · Pattern 4 (agent loop) · Node SDK mirroring wrapper-core.
  automation-builder/    CI image (internal containeryard), NSIS installer template + generator, DPAPI/Credential-Manager
                         scripts, signing, deterministic packaging, versioned Jinja2 prompt library, GitLab CI templates.
  automation-logging/    Write-only ingest (unique-ref push), analytics, compliance signals (MISSED/SILENT/SLOW/HUNG/…),
                         LLM error diagnosis, alerting (Jira/webhook/email/GitLab), triggers, static dashboard.
  automation-registry/   manifest.yaml + schema + operator CLI (promote/pause/rollback/…) + Pages portal:
                         catalog, installers, fleet admin panel (MR/pipeline generator), governance, compatibility.
  automations/           hello-world (test) · invoice-router (headless + LLM patterns 1&2) ·
                         portal-report (Playwright + self-healing) · notifier (dog-fooded alerting)
tools/                   new_automation.py scaffolder · test_all.py · e2e_demo.py · compat_smoke.py
docs/                    SPEC · AUTOMATION_STANDARD · GOVERNANCE · RUNBOOKS · SECURITY · API reference · getting started
```

In production each `components/*` directory is its own GitLab project (each carries its own `.gitlab-ci.yml`;
`git subtree split --prefix=components/<name>` gives you the history-preserving split).

## Download an installer (real `.exe`, no admin)

Every automation has a self-contained per-user installer (bundled Python + payload; works before any server exists):

| Automation | Download |
|---|---|
| automation-hello-world | [setup.exe](https://github.com/devpatel135/AutoStation/releases/download/installers-latest/automation-hello-world-setup.exe) · [sha256](https://github.com/devpatel135/AutoStation/releases/download/installers-latest/automation-hello-world-setup.exe.sha256) |
| automation-invoice-router | [setup.exe](https://github.com/devpatel135/AutoStation/releases/download/installers-latest/automation-invoice-router-setup.exe) · [sha256](https://github.com/devpatel135/AutoStation/releases/download/installers-latest/automation-invoice-router-setup.exe.sha256) |
| automation-notifier | [setup.exe](https://github.com/devpatel135/AutoStation/releases/download/installers-latest/automation-notifier-setup.exe) · [sha256](https://github.com/devpatel135/AutoStation/releases/download/installers-latest/automation-notifier-setup.exe.sha256) |
| automation-portal-report (Deloitte develop login example) | [setup.exe](https://github.com/devpatel135/AutoStation/releases/download/installers-latest/automation-portal-report-setup.exe) · [sha256](https://github.com/devpatel135/AutoStation/releases/download/installers-latest/automation-portal-report-setup.exe.sha256) |

Unsigned builds trigger Windows SmartScreen ("More info → Run anyway"); compare the SHA-256 first. Install center with per-automation
pages: https://devpatel135.github.io/AutoStation/install/ . Troubleshoot any machine: `python -m wrapper_core doctor`.
Replacing UiPath? Start at [`docs/UIPATH_PARITY.md`](docs/UIPATH_PARITY.md) and [`docs/UIPATH_MIGRATION.md`](docs/UIPATH_MIGRATION.md); `python tools/amx.py help` lists every developer/operator command.

## Quick tour (no installs beyond Python)

```bash
python -m pip install pytest pydantic jinja2
python tools/test_all.py                                   # all suites
python tools/e2e_demo.py --out e2e-out                     # whole platform end to end → e2e-out/public/index.html
python -m wrapper_core dev components/automations/automation-hello-world --param simulate=network
python tools/new_automation.py automation-my-thing --description "…" --owner me-team@example.com
```

`tools/e2e_demo.py` builds real packages, a real registry, installs and runs automations through the production run
lifecycle (offline: dir transport + mock LLM), ingests the logs, computes compliance signals, diagnoses failures, builds the
dashboard + portal, and runs the notifier automation over the resulting `signals.json`.

## Architecture in one picture

```
 ┌──────────── GitLab (control plane, no new server) ────────────┐
 │ registry (Pages): catalog · installers · admin · manifest.yaml │◀── MR approval = governance gate
 │ logging (CI): ingest → analytics → LLM diagnosis → alerts → Pages dashboard                   │
 └─────────────▲───────────────────────────────▲─────────────────┘
               │ read-only token                │ write-only push of unique refs
 ┌─────────────┴────────── VDI session (per user, %LOCALAPPDATA%\AUTOMAGIX, no admin) ───────────┐
 │ Task Scheduler + Startup maintenance script → wrapper-core                                    │
 │   self-heal · kill-switch · retry · item telemetry · run synopsis · broker ──▶ internal LLM    │
 │   └─ runs automation payload (Python / Node / PowerShell) on execution-framework              │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## CI on GitHub

`.github/workflows/`: **ci.yml** (all Python suites on Windows+Linux, Node syntax + tests, end-to-end walkthrough),
**installer-dev.yml** (manual: builds an unsigned dev-ring NSIS installer on a Windows runner), **pages-demo.yml** (manual:
publishes the demo dashboard + portal to GitHub Pages). Production pipelines for GitLab live in each component's `.gitlab-ci.yml`.

## Status and honesty

See [`docs/SECURITY.md`](docs/SECURITY.md) (*Known gaps*) and [`docs/VERIFY_IN_YOUR_ENVIRONMENT.md`](docs/VERIFY_IN_YOUR_ENVIRONMENT.md).
In short: CI exercises the Python platform (Windows + Linux, 3.9/3.12), the Node framework tests, the end-to-end walkthrough, and a real NSIS
build with a silent install/run/uninstall smoke test on a Windows runner. The Dockerfile, GitLab CI files, code signing, a real browser
run and your LLM endpoint still need one verification pass in your environment.
