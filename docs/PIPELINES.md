# Pipelines on GitHub

Everything user- and developer-facing runs on **GitHub Pages + Releases + Actions**. (The `.gitlab-ci.yml` files in each component remain
the enterprise option if you later move the control plane to GitLab; the manifest, packages and endpoints do not change.)

## The pieces

```
 push to main ──▶ ci ──────────────▶ tests (Python 3.9/3.12 on Windows+Linux, Node, end-to-end demo)
        │
        ├──▶ release ──▶ build_registry.py ──▶ NEW zips ─▶ branch `registry-artifacts` (append-only, immutable)
        │                              └────▶ manifest ─▶ PR `registry/auto-dev-ring` (dev ring only)  ◀── human approves
        │
        ├──▶ installer-dev ──▶ NSIS installers ──▶ Release `installers-latest` (<automation>-setup.exe + .sha256)
        │
        └──▶ site ──▶ build_registry (main + artifacts branch) ─▶ build_site ─▶ GitHub Pages
                          ├─ /registry/   manifest.yaml, triggers.json, artifacts/   ◀── endpoints self-heal from here
                          ├─ /install/    install center, catalog.json, bootstrap.ps1
                          ├─ /docs/       searchable docs   /portal/ catalog + admin   /dashboard/ /program/ /migration/
 operators ──▶ admin panel ──▶ ops-action ──▶ PR (pause, resume, promote, rollback, fleet-*)   or   `triggers` branch (run now)
 endpoints ──▶ git push of unique ingest/* refs ──▶ logging repo ──▶ analytics (opt-in) ──▶ dashboards
```

## Workflows

| Workflow | Triggers | What it does | Needs |
|---|---|---|---|
| `ci.yml` | push, PR | All Python suites (3.9/3.12, Windows+Linux), Node syntax + tests, end-to-end walkthrough | nothing |
| `installer-dev.yml` | push to builder/wrapper-core paths, manual | Builds the NSIS installers on Windows, smoke-tests install/run/uninstall, publishes to the rolling `installers-latest` release | `contents: write` |
| `release.yml` | push to `main` touching `components/**`, manual | Runs the tests, `tools/build_registry.py`, publishes new package zips to `registry-artifacts`, opens/refreshes PR `registry/auto-dev-ring` | `OPS_BOT_TOKEN` (optional, see below) |
| `site.yml` | push to `main`, every 6 h, after `installer-dev`, manual | Builds registry + site, validates links/install pages, deploys to Pages | Pages source = GitHub Actions |
| `ops-action.yml` | manual (admin panel / `gh workflow run`) | Applies pause/resume/promote/rollback/fleet-pause/fleet-resume as a **pull request**; `trigger-add` to the `triggers` branch | `OPS_BOT_TOKEN` (optional), `Allow GitHub Actions to create pull requests` |
| `analytics.yml` | every 30 min, manual — **off by default** | Ingests run logs from the private logging repo, computes signals, diagnoses, alerts, builds dashboards | variables/secrets below |
| `codeql.yml` | push, PR, weekly | CodeQL for Python and JavaScript | nothing |
| `dependabot.yml` | weekly | Updates for Actions, npm (framework), pip (wrapper-core, builder image) | nothing |

### Secrets and variables

| Name | Kind | Used by | Purpose |
|---|---|---|---|
| `OPS_BOT_TOKEN` | secret | release, ops-action | Fine-grained PAT (Contents RW, Pull requests RW, Actions RW). **Optional but recommended**: PRs opened with the default `GITHUB_TOKEN` do not trigger `ci` on the PR, so required status checks would never report. |
| `LOGGING_READ_TOKEN` | secret | analytics | Token for the **private** logging repo: contents read **and write** (writes the `archive` branch and deletes processed `ingest/*` refs) |
| `LLM_KEY` | secret | analytics | LLM API key for failure diagnosis (CI side only; endpoints never see it) |
| `JIRA_TOKEN` | secret | analytics | Jira API token (alert channel `jira`) |
| `TEAMS_WEBHOOK_URL` | secret | analytics | Teams incoming webhook (alert channel `teams`) |
| `AUTOSTATION_LIVE_ANALYTICS` | variable | analytics | `true` enables the workflow at all |
| `AUTOSTATION_LOGGING_REPO` | variable | analytics | `owner/name` of the private logging repo |
| `AUTOSTATION_ALLOW_PUBLIC_DASHBOARD` | variable | analytics | `true` lets analytics run in a **public** repo (see data exposure below) |
| `AUTOSTATION_LIVE_TO_PAGES` | variable | analytics, site | `true`: analytics builds the full site with LIVE data and deploys it; `site.yml` then stands down |
| `AUTOSTATION_ALERTS_LIVE` | variable | analytics | `true` sends real alerts (default: dry run) |
| `AUTOSTATION_SITE_DATA` | variable | site | `demo` (default; synthetic data, labelled) or `none` (placeholder dashboards) |
| `AUTOSTATION_EMERGENCY_PAUSE_USERS` | variable | ops-action | Comma-separated GitHub logins allowed to auto-merge an *emergency* pause PR |
| `LLM_ENDPOINT` | variable | analytics | Chat-completions URL for diagnosis |
| `JIRA_URL`, `JIRA_PROJECT` | variable | analytics | Jira base URL and project key |

Workflows never fail just because an optional secret is missing: they print a notice and skip that part.

## Registry on GitHub

* **Endpoints** self-heal from `https://devpatel135.github.io/AutoStation/registry/` (`manifest.yaml`, `artifacts/...`). It is a public static
  site, so no token is needed; the platform only ever sends a token to the registry's own origin.
* **Packages are immutable.** `tools/build_registry.py` zips every component/automation deterministically and compares *content* with what is already
  on the `registry-artifacts` branch. New version -> appended. Same version, different content -> the run **fails** ("bump the version"). The branch is
  append-only: the workflow refuses to commit a modified or deleted file.
* **`manifest.yaml` is the source of truth** (`components/automation-registry/manifest.yaml`, edited only through pull requests). A new version enters ring
  `dev` only (`registry/auto-dev-ring` PR). **Promotion to pilot/broad, pause, rollback and decommission are human decisions** - a PR you approve.
* **First run (bootstrap).** The starter manifest has placeholder hashes. Run `release` manually with *bootstrap = true* (it also auto-bootstraps while
  `registry-artifacts` does not exist): it publishes the first zips, replaces the placeholders with real SHA-256 values and puts the current version in every
  ring so the install pages work immediately. Merge the PR it opens.
* **`framework` and `node_modules`.** The framework zip contains `node_modules` only if `components/execution-framework/package-lock.json` is committed (a pinned
  tree is the only way the zip is reproducible). Generate it once with `npm install --package-lock-only` and commit it.

## Installers

`installer-dev.yml` (owned separately) builds one installer per automation and publishes them to the rolling release
`installers-latest` as `<automation>-setup.exe` + `<automation>-setup.exe.sha256` (plus versioned copies). Install pages and the portal link to
`https://github.com/devpatel135/AutoStation/releases/download/installers-latest/<automation>-setup.exe`. The installer is ring-independent: `/RING=` selects the ring
and the self-heal pass installs the version that ring is assigned. `release.yml` deliberately does **not** publish installers, so there is one publisher
of `installers-latest`. Signed production installers use the GitLab pipeline (`--require-signing`) or a signing step you add to `installer-dev`.

## Promotion, pause, rollback

| You want to... | Do this | What happens |
|---|---|---|
| Release a new version | Merge your change to `main` with a bumped `version` | `release` publishes the zip and opens/updates PR `registry/auto-dev-ring`; merge it -> dev ring runs it |
| Promote dev -> pilot -> broad | Admin panel -> *Promote* -> run `ops-action` (or `gh workflow run ops-action.yml -f action=promote -f target=<name> -f params='{"to":"pilot"}'`) | A PR `ops/promote-...` edits the manifest; the checklist in the PR template applies; approve + merge |
| Stop one automation | `ops-action` *pause* with a reason | PR `ops/pause-...`; once merged and the site republishes, every machine halts it at its next run |
| Stop everything (incident) | `ops-action` *fleet-pause*, optionally *emergency* | Same PR path; with `AUTOSTATION_EMERGENCY_PAUSE_USERS` the bot may merge it for a listed user (review afterwards) |
| Roll back | `ops-action` *rollback* `{"ring":"pilot","to":"1.0.0"}` | PR resets that ring and the rings above it |
| Run now / retry | `ops-action` *trigger-add* | Commit to the `triggers` branch (no manifest change, no PR); site republishes `triggers.json`; poll slots pick it up within minutes |

## Settings to apply (exact click-paths)

1. **Pages:** repository *Settings -> Pages -> Build and deployment -> Source: GitHub Actions*.
2. **Workflow permissions:** *Settings -> Actions -> General -> Workflow permissions* -> **Read and write permissions** and tick **Allow GitHub Actions to create and approve pull requests**.
3. **Protect `main`:** *Settings -> Branches -> Add branch ruleset* (or *Add classic branch protection rule*) -> pattern `main` -> require a pull request (1 approval) ->
   **Require review from Code Owners** -> require status checks `python (windows-latest, 3.12)`, `python (ubuntu-latest, 3.9)`, `node`, `integration` -> require conversations resolved ->
   block force pushes.
4. **Protect the data branches:** add rules for `registry-artifacts` (block force pushes, block deletions) and `triggers` (block deletions; allow only the bot to push where your plan supports it).
5. **Pages environment:** *Settings -> Environments -> github-pages* -> deployment branches: `main` only.
6. **Fork PRs:** *Settings -> Actions -> General -> Fork pull request workflows* -> *Require approval for all outside collaborators*.
7. **Secrets/variables:** *Settings -> Secrets and variables -> Actions* -> add what you need from the table above (nothing is required for the demo site).
8. **Code owners:** edit `.github/CODEOWNERS` (replace `@devpatel135` with your approvers' team).
9. **Private vulnerability reporting:** *Settings -> Code security -> Private vulnerability reporting -> Enable*.

### First-run order

1. Settings 1-2 above.
2. Run **release** manually with *bootstrap = true* -> merge the `registry/auto-dev-ring` PR (real hashes).
3. Let **installer-dev** publish `installers-latest` (push to builder paths or run it manually).
4. **site** deploys by itself after the merge and after the installers exist (the install pages then show the installer SHA-256).

## Live analytics and the data-exposure risk

Run logs and dashboards describe your fleet: host names, item ids (invoice numbers...), error text, LLM diagnoses, evidence screenshots. In a **public**
repository, workflow artifacts and GitHub Pages are public. Therefore:

* `analytics.yml` is **off** unless `AUTOSTATION_LIVE_ANALYTICS=true`, and it **refuses to run in a public repo** unless `AUTOSTATION_ALLOW_PUBLIC_DASHBOARD=true`.
* In a private repository its default output is a **workflow artifact** (`fleet-dashboard`), visible only to people with repository access.
* It never publishes to Pages unless `AUTOSTATION_LIVE_TO_PAGES=true`. Pages of a private repository are public on most plans unless you use GitHub Enterprise Cloud's
  private Pages access control - verify that before enabling, or keep the dashboards inside the logging repository / an internal host instead.
* Redaction (`sensitive_keys`) happens before any LLM sees data, but run logs are stored raw in the (private) archive - treat the archive as confidential.
* Until you decide, the public site shows **synthetic demo data**, clearly labelled.

Write-only logging: endpoints push orphan commits to new `ingest/*` refs of the logging repo with a deploy key; protect `ingest/*` as described in
`components/automation-logging/README.md` (GitHub: deploy key with write access, no force-push/delete on `ingest/*`; the analytics token deletes processed refs).

## Troubleshooting the pipelines

| Symptom | Cause | Fix |
|---|---|---|
| `site` fails at *configure-pages* | Pages source is not GitHub Actions | Setting 1 |
| `release` fails: "already released with different content" | A released version was edited | Bump `version` (VERSION / automation.yaml) |
| `release` cannot open the PR | Actions may not create PRs | Setting 2 |
| PR checks never start on bot PRs | Created with `GITHUB_TOKEN` | Add `OPS_BOT_TOKEN` |
| Install page says "not available at build time" for the SHA-256 | `installers-latest` has no `.sha256` asset yet | Run `installer-dev`; the next `site` run picks it up |
| Endpoints report registry unreachable | Pages not deployed or the registry dir missing | Check the site deployment and `https://<owner>.github.io/<repo>/registry/manifest.yaml` |
