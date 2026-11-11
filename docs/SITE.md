# The AutoStation site

One static **single-page app**, built by `tools/build_site.py` and deployed to GitHub Pages by `.github/workflows/site.yml`. There is one entry page
(`index.html`) with **one header and one tab bar of exactly four tabs: Home | Install | Fleet | Docs**. Nothing opens a page with a different bar and there is no
second row of tabs anywhere. No server, no database, no tracking, no third-party scripts, fonts or CDNs.

## The four tabs

Hash routes: `#/<tab>[/<second part>][?key=value&...]`. Back/forward, deep links and bookmarks work; the active tab is highlighted; arrow keys / Home / End move between tabs.

| Tab | Routes | What it shows |
|---|---|---|
| **Home** | `#/home` | One sentence about the platform, **one big status line** in plain words (green "Everything is running", amber / red "N problems need attention", grey "Waiting for the first run"), four key numbers (automations, runs in 30 days, success rate, open problems), the five most serious problems, and two buttons: *Install an automation* and *Open Fleet*. |
| **Install** | `#/install`, `#/install/<automation>`, `#/install?open=<automation>` | One list. Each automation is one card: name, status, ring versions, a one-line summary and **one obvious Download button**. A collapsed *details* section holds everything else (verify the SHA-256 and the build attestation, what the installer changes, silent install, Intune / SCCM, troubleshooting). `#/install/<name>` opens that card's details. A final collapsed block covers prerequisites, the bootstrap script and team rollout. |
| **Fleet** | `#/fleet`, `#/fleet/<section>`, `#/fleet/a/<automation>`, plus query filters such as `#/fleet/runs?status=failed&auto=<name>&run=<id>` | **One scrolling page** with a sticky *jump menu* (anchors, not tabs): **Problems**, **Automations**, **Runs**, **Machines**, **Schedules**, **Releases**, **Admin actions**, **Value**. A status line sits on top; every section opens with one plain sentence ("11 open problems, most serious first."), then one table or list. Details open in place: click an automation for its versions, owner, schedule, recent runs and the buttons *Install*, *Its runs*, *Admin actions*; click a run for the details drawer. Problems shows the serious ones first with a collapsed *Reliability, errors, queues and approvals* block behind it; Value (hours saved, cost) is collapsed by default. Empty sections say "Nothing to show yet" and how to get data. |
| **Docs** | `#/docs`, `#/docs/<DOC>[?a=<heading-id>]`, `#/docs/assessment` | Sidebar + search over every heading, in four groups: **Start here**, **Operate**, **Build**, **Security and design** (plus the architecture assessment under *Background*). Component READMEs are not published here (links to them go to GitHub). |

The search box (press `/`) is on every view and searches automations, machines, error signatures, recent runs, docs sections and pages (`search-ops.json`).

### Old addresses keep working

Every address used by earlier versions of the site is mapped to its place in the four tabs, both as a **page URL** (a tiny redirect stub) and as a **hash route** (aliased by the router):

| Old | New |
|---|---|
| `#/jobs`, `jobs/` | `#/fleet/runs` |
| `#/machines`, `machines/` | `#/fleet/machines` |
| `#/catalog`, `portal/`, `portal/compatibility.html` | `#/fleet/automations` |
| `#/catalog/a/<name>`, `portal/automation/<name>.html` | `#/fleet/a/<name>` |
| `#/dashboard`, `#/reliability`, `#/errors`, `#/queues`, `portal/health.html` | `#/fleet/problems` |
| `#/schedules` | `#/fleet/schedules` |
| `#/audit`, `portal/audit.html` | `#/fleet/releases` |
| `#/admin`, `portal/admin.html` | `#/fleet/admin` |
| `#/program` | `#/fleet/value` |
| `portal/overview.html`, `#/catalog/overview` | `#/fleet` |
| `#/catalog/governance`, `portal/governance.html` | `#/docs/GOVERNANCE` |
| `install/<name>.html` | `#/install/<name>` |
| `docs/<DOC>.html` | `#/docs/<DOC>` |

The two tables (`OLD_TABS` in `tools/site/site_app.py` and `OLD` in `tools/site/app.js`) must agree; a test enforces it.

## Files that are published

| Path | Purpose |
|---|---|
| `index.html` | The app shell: header, the four-tab bar, footer, CSP |
| `views/home.json`, `views/install.json`, `views/fleet.json`, `views/docs.json` | One file per tab: `{"": main view, "<sub>": sub view}` (Docs has one view per document); each view is a build-time HTML string with every telemetry value already escaped |
| `views/runs.json` | The Runs table (up to 2000 rows) - **loaded on demand** when the Runs section nears the screen or a link points at it, so Fleet itself stays light |
| `assets/app.js` | Router (with the old-route aliases), tab bar, search, lazy loader |
| `assets/views.js` | Scripts of individual views (generated hooks from the staged pages + `views_static.js`: the Home status, the Fleet problems list, headlines and the automation rows), each registered with `AS.hook(key, fn)` |
| `assets/jobs.js`, `assets/site.css`, `assets/redirect.js` | Runs-table script, the one stylesheet, the old-URL redirector |
| `jobs/runs-detail.json`, `dashboard/*.json`, `program/program.json`, `metrics.json`, `metrics.prom`, `fleet-status.json`, `badges/*.svg`, `search-ops.json`, `docs/search-index.json`, `install/catalog.json`, `install/bootstrap.ps1`, `status.json` | Data the views read (and machine-readable outputs for other tools) |
| `registry/` | `manifest.yaml`, `manifest.yaml.sig`, `triggers.json`, `artifacts/` - what endpoints self-heal from |
| `docs/assessment.html` | The hand-authored architecture assessment, shown inside the Docs tab |
| old page URLs (`jobs/index.html`, `docs/RUNBOOKS.html`, `portal/admin.html`, ...) | Tiny **redirect stubs** into the matching `#/...` route, so bookmarks and links in docs, issues and installers keep working |
| `404.html` | Stand-alone not-found page (links back to the app) |

## Building locally

```bash
python tools/build_site.py --out site --demo            # everything, synthetic fleet data (labelled DEMO DATA)
python tools/check_site.py site                          # shell + views + stubs + install view + observability files
python -m http.server --directory site 8000              # then open http://localhost:8000/  (needs http(s): browsers block file:// fetches)
```

| Option | Meaning |
|---|---|
| `--registry DIR` | Registry produced by `tools/build_registry.py`; also published under `/registry/` |
| `--demo` | Generate synthetic history (all signal types) and label the app DEMO DATA |
| `--demo-data DIR` | Use an archive (`DIR/archive`, optional `DIR/manifest.yaml`) but label it demo - e.g. the output of `tools/e2e_demo.py` |
| `--live-data DIR` | Real data (`DIR/archive`); no demo label. See the data-exposure notes in `docs/PIPELINES.md` before publishing |
| `--fetch-installer-sha` | Read `<automation>-setup.exe.sha256` from the `installers-latest` release so install cards show the installer SHA-256 (offline builds show "paste the SHA-256 from the release page") |
| `--installer-sha-file JSON` | `{automation: sha256}` instead of fetching |
| `--program-config` | Input for the Value section |

## How it is built (and how to add to it)

1. The page builders (`site_install`, `site_docs`, `registry.portal`, `automation_logging`, `site_ops`, `site_obs`) still write ordinary pages into the output folder, which
   acts as a **staging area**; nothing from the staging pages is published as a page.
2. `tools/site/site_app.py` (`assemble`) extracts the main content of every staged page, rewrites its links to in-app routes (or site-root-relative files), keeps only the page
   scripts the app still needs (the Runs table and the admin panel), and then **composes the four views** with `tools/site/site_simple.py`: `home_view`, `install_view`
   (each automation's guide becomes a collapsed *details* block), `fleet_view` (the jump menu and sections; each embedded page loses its own heading and its intro paragraph
   becomes a collapsed *About this section*). Every old page is replaced by a redirect stub; the search index is merged; the shell is written.
3. **To add a section to Fleet**: add `(id, title)` to `SECTIONS` in `site_simple.py`, build its body in `fleet_view` (a staged page or a table built from the manifest), add its
   headline sentence to the `leads` map in `views_static.js`, add the id to `FLEET_SECTIONS` in `tools/check_site.py`, and map the old page to it in `FLEET_OF` / `PORTAL_OF`
   (`site_app.py`) so its old URL redirects there. **A fifth tab is deliberately not supported** without changing `TABS` in `site_app.py` *and* `check_site.py` - the design rule is four.
4. Page-specific behaviour goes in `AS.hook("<tab>", fn)` (or `AS.hook("lazy:<view>", fn)` for a lazily loaded view). Use `AS.params()` / `AS.setParams()` for state in the
   URL, `AS.url(path)` for data files (cache-busted), and `textContent` for anything that came from telemetry.
5. `status.json` lists every published file; `check_site.py` is the gate in CI. It fails the build on a second row of tabs, a link to a route that does not exist, an inline
   script the CSP does not list, an external load, a missing Fleet section or jump menu, and an incomplete install card.

## Layout

Full width: the header, tab bar and every view span the whole window with `clamp(1rem, 2.5vw, 2.5rem)` side padding. Tables use the room; the Runs table gets its details drawer
beside it from 1100 px; the Docs sidebar sits next to its article; paragraphs and instruction lists keep a readable measure (`max-width: 90ch`) inside the full-width container.
Wide tables scroll inside their own box, never the page. The tab bar and the Fleet jump menu are sticky and scroll horizontally on a phone. Checked at 375 px, 1440 px and 2560 px
in light and dark.

## Design rules

* **Four tabs, one page per tab.** Sub-views are sections or panels that open in place, never another row of tabs.
* **Plain language first.** Section titles and headline sentences use everyday words (Problems, Runs, Machines); internal signal names (SILENT, MISSED ...) appear only in detail panels.
* **Escape everything.** Names, descriptions, owners, links, log text and evidence are HTML-escaped when the views are built; browser-side code only uses `textContent` / `setAttribute`.
  Stored HTML evidence is never rendered. Tests inject script payloads into every manifest field and fail if any raw payload reaches a view or page.
* **Strict CSP, no inline scripts.** The shell allows same-origin scripts plus the one theme bootstrap by SHA-256 (never `'unsafe-inline'` for scripts); views contain no executable
  script or inline event handler (`check_site.py` fails the build otherwise); the assessment document keeps its own hash-based CSP and loads nothing from outside.
* **Nothing external.** No CDN, no web fonts (system fonts), no third-party scripts; the only `https://` links are anchors (GitHub, docs).
* **Relative URLs only**, so the site works under `https://<owner>.github.io/<repo>/` and from any folder served over http(s).
* **Cache-safe deployments.** Every build has an id (`<meta name="build">`); scripts, views and data files are fetched with `?v=<id>` so a new deployment is never mixed with cached old files.
* **Honest empty states.** No fleet data -> "Waiting for the first run" / "Nothing to show yet" with how to turn it on, never invented numbers. Demo data is only ever shown under a DEMO DATA banner.
* **Advisory data.** Fleet numbers come from unauthenticated telemetry and say so on Home and Fleet.
* **One theme** (dark/light by system preference, toggle remembered), keyboard-accessible (ARIA tabs, skip link, focus management), responsive.
* **Install cards are self-contained**: someone with only the URL and a Windows PC can install, verify and uninstall without asking anyone.

## Operator notes

* The telemetry sections (Runs, Machines, queues) show a waiting state until real data arrives; Schedules and Releases are built from the manifest and always render.
* Everything from telemetry is clamped and HTML-escaped; the Runs table is server-rendered and `assets/jobs.js` only uses `textContent`, so a forged run cannot inject markup.
  `runs-detail.json` is fetched same-origin (the page CSP allows `connect-src 'self'`).
* Queue and approval events are optional: automations that never emit `queue_stats` / `approval_*` simply leave those blocks empty; the how-to for deciding an approval
  (`wrapper_core approvals ...`) is shown anyway.
* The UiPath migration page is not part of the site; the migration kit under `tools/uipath/` and `docs/UIPATH_*.md` stay in the repository.

## Hardening notes

* **A forged record cannot fail the build.** Every page builder runs through `site_common.guarded`: if one raises, the page becomes a visible placeholder ("could not be built from the current telemetry"), the failure is listed under `build_warnings` in `/status.json`, and the rest of the site - including the registry and any emergency pause - still deploys. The `site` workflow also rebuilds without live data if the live-data steps fail. Rows the dashboard or program report cannot render are shown as "row omitted" and counted (`data_quality.rows_omitted`), never silently dropped.
* **Timestamps are bounded and normalised** at ingest and again on read: before 2020 or more than 5 minutes in the future is rejected, everything else is converted to UTC so string order is time order (MTTR / MTBF can never be negative).
* **Script policy.** Generated pages allow same-origin scripts plus exactly the inline blocks this generator authored (by SHA-256) - no `'unsafe-inline'` for scripts - and `tools/check_site.py` fails the build if a page contains an inline script its CSP does not list. Style attributes still need `'unsafe-inline'` for styles; a `<meta>` CSP cannot carry `frame-ancestors`.
* **Names and badges.** Registered automations are listed first; names that appear only in telemetry are capped at 50 and can never evict a real automation. Badges are `badges/automation-<name>.svg`, so no automation can overwrite `badges/fleet.svg`.
* **Embedded page styles are scoped** under `.dash` with their comments removed first (a comment in front of a selector once leaked a stray `max-width` rule), so a page's own stylesheet cannot restyle the shell.
