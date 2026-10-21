# The AutoStation site

A single static site, built by `tools/build_site.py` and deployed to GitHub Pages by `.github/workflows/site.yml`. No server, no database, no tracking, and the only
scripts are the small ones in `assets/site.js` and the portal/admin pages.

## What is on it

| Path | Content | Built from |
|---|---|---|
| `/` | Landing with role-based entry (user, developer, operator, approver, value) | `tools/site/site_landing.py` |
| `/install/` | Install center; one guide per automation (download, verify, silent install, Intune/SCCM, what changes on the machine, troubleshooting, report a problem) | manifest + `tools/site/site_install.py` |
| `/install/catalog.json` | Machine-readable catalog (rings, installer URL + SHA-256, package hashes) | same |
| `/install/bootstrap.ps1` | Download-verify-install script (SHA-256 against the published hash; no `iex`) | `tools/site/bootstrap.ps1` |
| `/docs/` | Every `docs/*.md` and component README rendered, with sidebar and client-side search | `tools/site/site_docs.py` |
| `/portal/` | Registry catalog, compatibility, governance, **fleet admin panel** (GitHub mode: `ops-action` workflow, PR and edit links) | `registry build-portal` |
| `/dashboard/` | Fleet dashboard: signals, item-level metrics, retries, LLM, versions, failure evidence | `automation_logging` |
| `/program/` | Programme/ROI: hours saved, licences avoided, success, drift, failure causes | `automation_logging/program.py` + `program.yaml` |
| `/jobs/` | **Jobs**: run history (newest 2000) with client-side search, filters, sorting and paging, a details drawer (retry audit, events, failure evidence) and deep links (`#status=failed&run=<id>`) | archive runs + `runs-detail.json`, `tools/site/site_ops.py`, `tools/site/jobs.js` |
| `/machines/` | **Machines**: heartbeat freshness, installed versions vs the ring target (drift), last run, SILENT / VERSION_DRIFT / LAST_RUN_FAILED signals | archive heartbeats + manifest |
| `/queues/` | **Queues & approvals**: latest queue statistics, dead letters, SLA breaches, approvals waiting and decided | `queue_stats`, `approval_requested`, `approval_decided` events |
| `/schedules/` | **Schedules**: next fire times for every manifest slot (daily, weekdays, weekly, monthly, hourly, interval, poll, cron), maintenance windows and paused automations flagged | manifest |
| `/audit/` | **Audit**: registry signing status, operator actions, pause / kill-switch state, every release with SHA-256 and the rings running it | `history/CHANGELOG.md`, manifest, `registry/manifest.yaml.sig`, lifecycle events |
| `/registry/` | `manifest.yaml`, `triggers.json`, `index.json`, `artifacts/` - what endpoints self-heal from | `tools/build_registry.py` |
| `/status.json` | Build info: commit, time, mode, registry hash, notes | `build_site.py` |

## Building locally

```bash
python tools/build_site.py --out site --demo            # everything, synthetic fleet data (labelled DEMO DATA)
python tools/check_site.py site                          # every link/anchor resolves, every install page has download + SHA-256 + verify steps
python -m http.server --directory site 8000              # then open http://localhost:8000/
```

| Option | Meaning |
|---|---|
| `--registry DIR` | Registry produced by `tools/build_registry.py`; also published under `/registry/` |
| `--demo` | Generate synthetic history (all signal types) and label dashboards DEMO DATA |
| `--demo-data DIR` | Use an archive (`DIR/archive`, optional `DIR/manifest.yaml`) but label it demo - e.g. the output of `tools/e2e_demo.py` |
| `--live-data DIR` | Real data (`DIR/archive`); no demo label. See the data-exposure notes in `docs/PIPELINES.md` before publishing |
| `--fetch-installer-sha` | Read `<automation>-setup.exe.sha256` from the `installers-latest` release so install pages show the installer SHA-256 (offline builds show "see the release page") |
| `--installer-sha-file JSON` | `{automation: sha256}` instead of fetching |
| `--program-config` | Input for the programme dashboard |

## Design rules

* **Escape everything.** Names, descriptions, owners, links, log text and evidence are HTML-escaped; stored HTML evidence is never rendered (it is shown as escaped text and
  saved as `.html.txt`). Tests inject script payloads into every manifest field and fail if any raw payload reaches a page.
* **Relative links only**, so the site works under `https://<owner>.github.io/<repo>/` and when served from a folder. `check_site.py` is the gate in CI.
* **Honest empty states.** No fleet data -> placeholder pages that say how to turn it on, never invented numbers. Placeholder hashes are shown as `PLACEHOLDER`.
* **One theme** (dark/light by system preference, toggle remembered), responsive, keyboard-accessible, no third-party scripts. The only external reference is an optional Google Fonts stylesheet.
* **Install pages are self-contained**: someone with only the URL and a Windows PC can install, verify and uninstall without asking anyone.

## Operator console notes

* The telemetry pages (Jobs, Machines, Queues) show a "waiting for the first run" state until real data arrives; Schedules and Audit are built from the manifest and always render.
* Everything from telemetry is clamped and HTML-escaped; the Jobs page keeps the table server-rendered and its script (`assets/jobs.js`) only uses `textContent`, so a forged
  run cannot inject markup. `runs-detail.json` is fetched same-origin (the page CSP allows `connect-src 'self'`).
* Queue and approval events are optional: automations that never emit `queue_stats` / `approval_*` simply leave those sections empty.
* The UiPath migration page is no longer part of the site; the migration kit under `tools/uipath/` and `docs/UIPATH_*.md` stay in the repository.

## Adding a page or section

Add a `site_*.py` module under `tools/site/` that writes files with `site_common.write` and wraps content with `site_common.layout`, call it from `build_site.build`, add the output to
`status.json`'s page list, and add the file to `REQUIRED` in `tools/check_site.py` if it must always exist.
