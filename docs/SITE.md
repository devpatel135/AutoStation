# The AutoStation site

One static **single-page app**, built by `tools/build_site.py` and deployed to GitHub Pages by `.github/workflows/site.yml`. There is one entry page
(`index.html`) with **one header and one tab bar**; every section is a *tab* (a view) inside it, so no click ever lands on a page with a different bar.
No server, no database, no tracking, no third-party scripts, fonts or CDNs.

## Tabs and routes

Hash routes: `#/<tab>[/<sub>][?key=value&...]`. Back/forward, deep links and bookmarks work; the active tab is highlighted; arrow keys / Home / End move between tabs.

| Tab | Route(s) | What it does | Built from |
|---|---|---|---|
| Home | `#/home` | **Fleet right now** (green / amber / red verdict with reasons, success rate, open signals, latest runs), quick actions (install, dashboard, jobs, report a problem, request an automation), role-based entry points | `site_landing.py` + live panel in `views_static.js` (reads `fleet-status.json`, `metrics.json`, `dashboard/signals.json`, `dashboard/runs-latest.json`) |
| Install | `#/install`, `#/install/<automation>` | Install center: real installer download, SHA-256 + verify steps, silent install, Intune/SCCM, what changes on the machine, troubleshooting, report a problem | `site_install.py` |
| Docs | `#/docs`, `#/docs/<DOC>[?a=<heading-id>]`, `#/docs/assessment` | Every `docs/*.md` and component README with a sidebar, per-doc deep links and a search box over all headings | `site_docs.py` |
| Catalog | `#/catalog`, `#/catalog/a/<automation>`, `#/catalog/compatibility`, `#/catalog/governance` | Automation catalog (filters, ring selector, download, live reliability per card); per-automation detail (versions per ring with SHA, schedule, triggers, error handling, live health + recent runs, install page, operator actions); compatibility matrix and governance as sub-tabs | `registry/portal/` + live panels in `views_static.js` |
| Dashboard | `#/dashboard` | Fleet dashboard: signals with LLM diagnosis, item metrics, retries, versions, failure evidence | `automation_logging/dashboard.py` |
| Program | `#/program` | Hours saved, licences avoided, success rate, drift, failure causes | `automation_logging/program.py` |
| Jobs | `#/jobs?status=&auto=&host=&ring=&q=&sort=&page=&run=<id>` | Run history (newest 2000), search / filters / sorting / paging in the browser, a details drawer beside the table on wide screens (retry audit, events, failure evidence) | `site_ops.py` + `jobs.js` |
| Machines | `#/machines` | Heartbeat freshness, installed versions vs the ring target (drift), last run, SILENT / VERSION_DRIFT / LAST_RUN_FAILED | `site_ops.py` |
| Reliability | `#/reliability` | SLO and error-budget burn, MTTR/MTBF, flakiness, duration percentiles + anomalies, on-time rate | `site_obs.py` + `automation_logging/reliability.py` |
| Errors | `#/errors` | Error taxonomy and top signatures, regressions after a release, remediation outcomes, needs-attention list | `site_obs.py` |
| Queues | `#/queues` | Queue statistics, dead letters, SLA breaches, approvals waiting / decided, **and how a person decides an approval** (`wrapper_core approvals ...`) - useful before any data exists | `site_ops.py` |
| Schedules | `#/schedules` | Next fire times for every manifest slot, maintenance windows, paused state and a **Run now** recipe per slot (the `ops-action` workflow with the exact parameters, or the Admin tab) | `site_ops.py` |
| Audit | `#/audit` | Registry signing status (key id, signed-at, **sequence number**, `triggers.json` digest and whether it matches the published file), operator actions, pause / kill-switch state, every release with SHA-256 and rings | `site_ops.py` |
| Admin | `#/admin[?a=<automation>]` | Fleet admin panel: pause / resume / promote / rollback / on-demand trigger as the exact `gh` command (bash and PowerShell) or a prefilled workflow page | `registry/portal/admin.py` |

The search box (press `/`) is on every view and searches automations, machines, error signatures, recent runs, docs sections and pages (`search-ops.json`).

## Files that are published

| Path | Purpose |
|---|---|
| `index.html` | The app shell: header, tab bar, advisory banner, footer, CSP |
| `views/<tab>.json` | One file per tab: `{"": main view, "<sub>": sub view}`; each view is a build-time HTML string with every telemetry value already escaped |
| `assets/app.js` | Router, tab bar, search, view loader (cache-busted with the build id) |
| `assets/views.js` | Scripts that belong to individual views (generated hooks + `views_static.js`), each registered with `AS.hook(key, fn)` |
| `assets/jobs.js`, `assets/site.css`, `assets/redirect.js` | Jobs view script, the one stylesheet, the old-URL redirector |
| `jobs/runs-detail.json`, `dashboard/*.json`, `program/program.json`, `metrics.json`, `metrics.prom`, `fleet-status.json`, `badges/*.svg`, `search-ops.json`, `docs/search-index.json`, `install/catalog.json`, `install/bootstrap.ps1`, `status.json` | Data the views read (and machine-readable outputs for other tools) |
| `registry/` | `manifest.yaml`, `manifest.yaml.sig`, `triggers.json`, `artifacts/` - what endpoints self-heal from |
| `docs/assessment.html` | The hand-authored architecture assessment, shown inside the Docs tab |
| old page URLs (`jobs/index.html`, `docs/RUNBOOKS.html`, `portal/admin.html`, ...) | Tiny **redirect stubs** into the matching `#/...` route, so bookmarks, links in docs/issues/installers keep working |
| `404.html` | Stand-alone not-found page (links back to the app) |

## Building locally

```bash
python tools/build_site.py --out site --demo            # everything, synthetic fleet data (labelled DEMO DATA)
python tools/check_site.py site                          # shell + views + stubs + install views + observability files
python -m http.server --directory site 8000              # then open http://localhost:8000/  (needs http(s): browsers block file:// fetches)
```

| Option | Meaning |
|---|---|
| `--registry DIR` | Registry produced by `tools/build_registry.py`; also published under `/registry/` |
| `--demo` | Generate synthetic history (all signal types) and label the app DEMO DATA |
| `--demo-data DIR` | Use an archive (`DIR/archive`, optional `DIR/manifest.yaml`) but label it demo - e.g. the output of `tools/e2e_demo.py` |
| `--live-data DIR` | Real data (`DIR/archive`); no demo label. See the data-exposure notes in `docs/PIPELINES.md` before publishing |
| `--fetch-installer-sha` | Read `<automation>-setup.exe.sha256` from the `installers-latest` release so install pages show the installer SHA-256 (offline builds show "paste the SHA-256 from the release page") |
| `--installer-sha-file JSON` | `{automation: sha256}` instead of fetching |
| `--program-config` | Input for the programme dashboard |

## How it is built (and how to add a tab or view)

1. The page builders (`site_install`, `site_docs`, `registry.portal`, `automation_logging`, `site_ops`, `site_obs`, `site_landing`) still write ordinary pages into the output
   folder, which acts as a staging area.
2. `tools/site/site_app.py` (`assemble`) then turns every page into a **view**: it extracts the main content, rewrites every link to an in-app route (or a site-root-relative
   file), hoists page scripts into `assets/views.js`, scopes the stand-alone dashboard / program CSS under `.dash` so it cannot restyle the shell, replaces the old page with a
   redirect stub, merges docs sections and catalog entries into `search-ops.json`, and writes the shell.
3. **To add a view** to an existing tab: write the page with `site_common.layout` as before, add its path to `route_of()` in `site_app.py`, done.
   **To add a tab**: add it to `TABS` in `site_app.py` *and* `tools/check_site.py`, produce a page at `<tab>/index.html`, add the tab to `SIMPLE` (or a rule in `route_of`).
   Page-specific behaviour goes in `AS.hook("<tab>" or "<tab>/<sub>" or "<tab>/", fn)` (inline scripts in the page are hoisted automatically; static ones live in `tools/site/views_static.js`).
   Use `AS.params()` / `AS.setParams()` for state in the URL, `AS.url(path)` for data files (cache-busted), and `textContent` for anything that came from telemetry.
4. `status.json` lists every published file; `check_site.py` is the gate in CI.

## Layout

Full width: the header, tab bar and every view span the whole window with `clamp(1rem, 2.5vw, 2.5rem)` side padding. Tables and dashboards use the room (wide tables, multi-column
card grids, the Jobs table with its details drawer side by side from 1100 px, the Docs sidebar next to its article); paragraphs and instruction lists keep a readable measure
(`max-width: 90ch`) inside the full-width container. Wide tables scroll inside their own box, never the page. The tab bar scrolls horizontally on a phone and is sticky. Checked at
375 px, 768 px, 1440 px and 2560 px in light and dark.

## Design rules

* **Escape everything.** Names, descriptions, owners, links, log text and evidence are HTML-escaped when the views are built; browser-side code only uses `textContent` / `setAttribute`.
  Stored HTML evidence is never rendered. Tests inject script payloads into every manifest field and fail if any raw payload reaches a view or page.
* **Strict CSP, no inline scripts.** The shell allows same-origin scripts plus the one theme bootstrap by SHA-256 (never `'unsafe-inline'` for scripts); views contain no executable
  script or inline event handler (`check_site.py` fails the build otherwise); the assessment document keeps its own hash-based CSP and loads nothing from outside.
* **Nothing external.** No CDN, no web fonts (system fonts), no third-party scripts; the only `https://` links are anchors (GitHub, docs).
* **Relative URLs only**, so the site works under `https://<owner>.github.io/<repo>/` and from any folder served over http(s).
* **Cache-safe deployments.** Every build has an id (`<meta name="build">`); scripts, views and data files are fetched with `?v=<id>` so a new deployment is never mixed with cached old files.
* **Honest empty states.** No fleet data -> "Waiting for the first run" with how to turn it on, never invented numbers. Placeholder hashes are shown as `PLACEHOLDER`.
* **One theme** (dark/light by system preference, toggle remembered), keyboard-accessible (ARIA tabs, skip link, focus management), responsive.
* **Install views are self-contained**: someone with only the URL and a Windows PC can install, verify and uninstall without asking anyone.

## Operator console notes

* The telemetry pages (Jobs, Machines, Queues) show a "waiting for the first run" state until real data arrives; Schedules and Audit are built from the manifest and always render.
* Everything from telemetry is clamped and HTML-escaped; the Jobs page keeps the table server-rendered and its script (`assets/jobs.js`) only uses `textContent`, so a forged
  run cannot inject markup. `runs-detail.json` is fetched same-origin (the page CSP allows `connect-src 'self'`).
* Queue and approval events are optional: automations that never emit `queue_stats` / `approval_*` simply leave those sections empty.
* The UiPath migration page is no longer part of the site; the migration kit under `tools/uipath/` and `docs/UIPATH_*.md` stay in the repository.

## Hardening notes (second review round)

* **A forged record cannot fail the build.** Every page builder runs through `site_common.guarded`: if one raises, the page becomes a visible placeholder ("could not be built from the current telemetry"), the failure is listed under `build_warnings` in `/status.json`, and the rest of the site - including the registry and any emergency pause - still deploys. The `site` workflow also rebuilds without live data if the live-data steps fail. Rows the dashboard or program report cannot render are shown as "row omitted" and counted (`data_quality.rows_omitted`), never silently dropped.
* **Timestamps are bounded and normalised** at ingest and again on read: before 2020 or more than 5 minutes in the future is rejected, everything else is converted to UTC so string order is time order (MTTR / MTBF can never be negative).
* **Script policy.** Generated pages allow same-origin scripts plus exactly the inline blocks this generator authored (by SHA-256) - no `'unsafe-inline'` for scripts - and `tools/check_site.py` fails the build if a page contains an inline script its CSP does not list. Style attributes still need `'unsafe-inline'` for styles; a `<meta>` CSP cannot carry `frame-ancestors`. The standalone single-file pages (fleet dashboard, program report, registry portal) still use inline handlers and keep `script-src 'unsafe-inline'`; everything they print is escaped. Moving them to external scripts + hashes is a known follow-up.
* **Names and badges.** Registered automations are listed first; names that appear only in telemetry are capped at 50 and can never evict a real automation. Badges are `badges/automation-<name>.svg`, so no automation can overwrite `badges/fleet.svg`.
