# Pipelines on GitHub

Everything user- and developer-facing runs on **GitHub Pages + Releases + Actions**, with **only GitHub-owned actions** (`actions/*`, `github/*`): the repository's
Actions allow-list (`tools/harden_repo.py`) refuses anything else, so every helper is a script in `tools/`. (The `.gitlab-ci.yml` files in each component remain
the enterprise option if you later move the control plane to GitLab; the manifest, packages and endpoints do not change.)

## The pieces

```
 push / PR ──▶ ci ───────────▶ hygiene (pinned actions, workflow rules, secrets scan, py3.9 syntax) + tests (Python 3.9/3.12 on Windows+Linux, Node, end-to-end demo)
        PR ──▶ pr ───────────▶ path + size labels, one sticky triage comment (promotion checklist when the manifest changes)
        PR ──▶ security ─────▶ dependency-review (GitHub-owned) + audits

 push to main
   ├─▶ release ──▶ tests ─▶ build_registry.py ─▶ NEW immutable zips ─▶ branch `registry-artifacts` (append-only)
   │                              └────▶ manifest (all rings follow main) ─▶ committed to main [skip ci]
   ├─▶ installers ──▶ NSIS installer (bundled Python, Node + Playwright, MinGit) ─▶ smoke: silent install, real headless-Edge run, uninstall
   │                              └────▶ attestation, SBOM, release notes ─▶ Release `installers-latest` (<automation>-setup.exe + .sha256)
   └─▶ site ──▶ registry (signed when REGISTRY_SIGNING_SEED exists) + live logs ─▶ dashboards ─▶ GitHub Pages
                   ├─ /registry/  manifest.yaml(.sig), triggers.json, artifacts/      ◀── endpoints self-heal from here
                   ├─ /install/   install center, catalog.json, bootstrap.ps1
                   └─ /docs/  /portal/  /dashboard/  /program/  /jobs/  /machines/  /queues/  /schedules/  /audit/

 every 30 min ──▶ health ──▶ live logs ─▶ fleet signals ─▶ `ops-signal` GitHub issues (opened, commented, reopened, auto-closed)
 daily + after every installer build ──▶ canary ──▶ download the PUBLISHED installer, verify sha256 + attestation, install, run, uninstall ─▶ issue on failure
 weekly ──▶ security ──▶ pip/npm audit, posture check, SBOM artifact          weekly ──▶ codeql
 operators ──▶ admin panel ──▶ ops-action ──▶ PR (pause, resume, promote, rollback, fleet-*)  or  `triggers` branch (run now)
 endpoints ──▶ push run logs (git over ssh with a deploy key, or the GitHub API) ──▶ private logs repo ──▶ health + site
```

## Workflows

| Workflow | Triggers | What it does | Needs |
|---|---|---|---|
| `ci.yml` | push to main, pull request | `hygiene` (tools/check_workflows.py with a real YAML parser, tools/pin_actions.py --check, tools/check_secrets.py, py3.9 syntax), every Python suite (3.9/3.12, Windows+Linux), Node syntax + tests, end-to-end walkthrough | nothing |
| `pr.yml` | pull request | Path labels (`area:*`, `registry-change`, `security-sensitive`), a `size:*` label and one sticky comment (promotion checklist, risk notes). Reads changed files through the API, never runs PR code; labelling is skipped on fork PRs | `GITHUB_TOKEN` |
| `release.yml` | push to `main` touching `components/**`, manual | Tests, `tools/build_registry.py` (immutability enforced), appends new zips to `registry-artifacts`, auto-promotes the new versions to every ring and commits the manifest to `main` | `contents: write` |
| `installer-dev.yml` (`installers`) | push to builder/wrapper-core/automation paths, manual | Builds the NSIS installer on Windows, smoke-tests install/run/uninstall, writes the SBOM (`autostation-sbom.cdx.json`) and release notes (`tools/release_notes.py`), attests build provenance, publishes to the rolling `installers-latest` release | `contents: write`, `id-token: write`, `attestations: write`; `LOGGING_TOKEN` makes it a connected build; `AUTOSTATION_REGISTRY_PUBKEY` pins the signing key |
| `site.yml` | push to `main`, every 30 min, after `release`/`installers`, manual | Builds registry + site (signs the manifest when `REGISTRY_SIGNING_SEED` exists), ingests the live logs, checks links, deploys to Pages | Pages source = GitHub Actions |
| `health.yml` | every 30 min, manual | Fetches the live logs, recomputes the fleet signals and keeps `ops-signal` issues in sync (see the operator guide below). Does nothing, and closes nothing, without live data | `LOGGING_TOKEN`; `issues: write` |
| `canary.yml` | daily, after `installers`, manual | Downloads the published installer, verifies SHA-256 and attestation, installs it silently, runs the automation against its local demo page, uninstalls. Opens/refreshes one `ops-signal` issue on failure, closes it when green. Never writes to the live logs | `issues: write` |
| `security.yml` | weekly, manual, pull requests touching dependencies or workflows | `tools/check_posture.py` (policy files, pinned actions, rulesets, secret scanning), `pip-audit`, `npm audit --omit=dev`, SBOM artifact; `dependency-review` on pull requests | nothing (live settings checks need an admin token and are skipped otherwise) |
| `ops-action.yml` | manual (admin panel / `gh workflow run`) | Applies pause/resume/promote/rollback/fleet-pause/fleet-resume as a **pull request**; `trigger-add` to the `triggers` branch | `OPS_BOT_TOKEN` (optional), `Allow GitHub Actions to create pull requests` |
| `codeql.yml` | push, PR, weekly | CodeQL for Python and JavaScript | nothing |
| `dependabot.yml` | monthly | Updates for Actions, npm (framework, minor/patch only), pip (wrapper-core, builder image) | nothing |

### Rules every workflow obeys (enforced by `tools/check_workflows.py`, run in CI, tested)

* every `uses:` is pinned to a full commit SHA (`python tools/pin_actions.py` resolves tags; Dependabot bumps them) and is GitHub-owned
* every job has `timeout-minutes` and least-privilege `permissions`; checkout sets `persist-credentials: false` unless the job pushes
* no `${{ inputs.* }}`, `${{ github.event.* }}`, `${{ github.ref_name }}`, `${{ github.actor }}` or `${{ secrets.* }}` inside a `run:` script: pass them through `env:`
* no `pull_request_target`; a `pull_request` workflow may not use secrets (fork PRs would not get them and must never be able to read them)
* secret-bearing jobs run from `main` only; secrets are put in `env:` of the one step that needs them
* no YAML plain scalar containing `: ` (it silently invalidates the file, so GitHub starts no jobs)

### Secrets and variables

| Name | Kind | Used by | Purpose |
|---|---|---|---|
| `LOGGING_TOKEN` | secret | installers, site, health | fine-grained PAT (Contents read+write on `AutoStation-logs` only): turns on connected installers, live dashboards and health issues, see [LIVE_LOGGING](LIVE_LOGGING.md) |
| `REGISTRY_SIGNING_SEED` | secret | site | Ed25519 private seed (hex) that signs `registry/manifest.yaml`; create with `tools/sign_registry.py --gen-key` or `tools/setup_live.py`; the registry is unsigned until it is set |
| `LOGS_READ_TOKEN` | secret (optional) | site, health | a SECOND fine-grained PAT, Contents: read only on the logs repo; the dashboards and the health workflow prefer it over `LOGGING_TOKEN`, so the read side no longer needs the write token that ships inside installers (see [LIVE_LOGGING](LIVE_LOGGING.md)) |
| `AUTOSTATION_REGISTRY_PUBKEY` | variable (optional) | installers | public Ed25519 key(s) (hex, comma-separated) pinned into new installers so they only accept a manifest signed by `REGISTRY_SIGNING_SEED`; set it only after the secret exists (`tools/setup_live.py` does it in that order) |
| `AUTOSTATION_LOG_REPO` | variable (optional) | installers, site, health | `owner/name` of the private logs repo (default `<owner>/AutoStation-logs`) |
| `OPS_BOT_TOKEN` | secret (optional) | release, ops-action | Fine-grained PAT (Contents RW, Pull requests RW, Actions RW). PRs opened with the default `GITHUB_TOKEN` do not trigger `ci`, so required status checks would never report |
| `AUTOSTATION_EMERGENCY_PAUSE_USERS` | variable | ops-action | Comma-separated GitHub logins allowed to auto-merge an *emergency* pause PR |

`GITHUB_TOKEN` is the only credential `health`, `canary`, `pr` and `security` use, scoped per job (`issues: write` / `pull-requests: write` where needed).
Workflows never fail just because an optional secret is missing: they print a notice and skip that part.

## ops-signal issues: the proactive-ops loop

`health.yml` runs every 30 minutes. It ingests the live logs, runs the same analytics the dashboards use and reconciles GitHub issues with the **open signals**
(SILENT machine, MISSED scheduled run, HUNG run, VERSION_DRIFT, failed runs...). `tools/ops_issues.py` does the reconciliation:

| Situation | What happens |
|---|---|
| New signal at severity high or critical | One issue is opened (label `ops-signal`, `severity:<x>`, `signal:<type>`); at most 5 new issues per run, most severe first, the rest next run |
| Issue exists and the signal is still seen | One "still happening" comment at most once per 24 h |
| Signal disappears | The issue gets a comment and is **closed automatically** (only when the live logs were really read: a missing logs repo or a failed analysis never closes anything) |
| Signal returns within 14 days | The closed issue is reopened instead of opening a duplicate |
| Duplicates | The lowest-numbered issue wins, the others are closed |
| Medium/low signals | Dashboards only (no issue), unless an issue already exists for them |

The dedupe key is the signal fingerprint stored in an HTML comment in the issue body. **Telemetry is untrusted**: endpoints push it with a credential that ships
inside the installer, so every field is validated, control characters are stripped, markup, `@mentions` and issue references are neutralised and lengths are
capped before anything is written to GitHub. Each issue links the runbook for its signal type ([RUNBOOKS](RUNBOOKS.md)) and the Jobs/Machines pages.

`canary.yml` uses the same machinery with the fixed id `canary-install`: red opens/refreshes the issue, the next green run closes it.
To silence a noisy signal permanently, fix its cause (or pause the automation); do not delete the label.

## Registry on GitHub

* **Endpoints** self-heal from `https://devpatel135.github.io/AutoStation/registry/` (`manifest.yaml`, `manifest.yaml.sig` when signed, `artifacts/...`). It is a public static
  site, so no token is needed; the platform only ever sends a token to the registry's own origin.
* **Packages are immutable.** `tools/build_registry.py` zips every component/automation deterministically and compares *content* with what is already
  on the `registry-artifacts` branch. New version -> appended. Same version, different content -> the run **fails** ("bump the version"). The branch is
  append-only: the workflow refuses to commit a modified or deleted file, and a repository ruleset forbids deleting or force-pushing it.
* **`manifest.yaml` is the source of truth** (`components/automation-registry/manifest.yaml`). `release` auto-promotes a new version to every ring and commits the
  manifest to `main` (continuous delivery for a small fleet). **Pause, resume, rollback and decommission** stay deliberate: `ops-action` opens a pull request you approve.
* **`framework` and `node_modules`.** The framework bundle (Node + playwright-core) is built in CI by `tools/build_framework_bundle.py`; the pinned versions are listed
  in the SBOM.

## Installers

`installer-dev.yml` builds one installer per automation and publishes them to the rolling release `installers-latest` as `<automation>-setup.exe` +
`<automation>-setup.exe.sha256` (plus versioned copies, `autostation-sbom.cdx.json` and generated release notes). Install pages and the portal link to
`https://github.com/devpatel135/AutoStation/releases/download/installers-latest/<automation>-setup.exe`. Verify a download with
`gh attestation verify <file> --repo devpatel135/AutoStation`. The release notes are produced by `tools/release_notes.py` (commits grouped by conventional subject
plus a manifest diff); the base commit is remembered in the notes so the next build starts exactly there. `release.yml` deliberately does **not** publish installers,
so there is one publisher of `installers-latest`.

## Promotion, pause, rollback

| You want to... | Do this | What happens |
|---|---|---|
| Release a new version | Merge your change to `main` with a bumped `version` | `release` publishes the zip, promotes it to every ring and commits the manifest; machines pick it up on their next run |
| Promote or move a ring | Admin panel -> *Promote* -> run `ops-action` (or `gh workflow run ops-action.yml -f action=promote -f target=<name> -f params='{"to":"pilot"}'`) | A PR `ops/promote-...` edits the manifest; the checklist in the PR template applies; approve + merge |
| Stop one automation | `ops-action` *pause* with a reason | PR `ops/pause-...`; once merged and the site republishes, every machine halts it at its next run |
| Stop everything (incident) | `ops-action` *fleet-pause*, optionally *emergency* | Same PR path; with `AUTOSTATION_EMERGENCY_PAUSE_USERS` the bot may merge it for a listed user (review afterwards) |
| Roll back | `ops-action` *rollback* `{"ring":"pilot","to":"1.0.0"}` | PR resets that ring and the rings above it |
| Run now / retry | `ops-action` *trigger-add* | Commit to the `triggers` branch (no manifest change, no PR); site republishes `triggers.json`; poll slots pick it up within minutes |

## Settings

`python tools/setup_live.py` (run it yourself, with an admin token) applies most of this: it creates the private logs repo and the `production` environment, stores
`LOGGING_TOKEN` and the signing key, and runs `tools/harden_repo.py`, which enables secret scanning + push protection, Dependabot alerts and security updates, private
vulnerability reporting, restricts Actions to GitHub-owned actions with a read-only default token, requires approval for outside contributors' fork PRs, adds rulesets
that forbid deleting or force-pushing `main`, `registry-artifacts` and `triggers`, and sets the Pages source to GitHub Actions. `python tools/check_posture.py` (and the
weekly `security` workflow) reports which of these are really on.

Still manual, in the web UI:

1. **Actions permissions:** *Settings -> Actions -> General -> Workflow permissions* -> tick **Allow GitHub Actions to create and approve pull requests** (needed by `ops-action`).
2. **Code owners:** edit `.github/CODEOWNERS` (replace `@devpatel135` with your approvers' team) and, once more than one person works here, add a ruleset requiring a pull
   request with Code Owner review and the status checks `python (windows-latest, 3.12)`, `python (ubuntu-latest, 3.9)`, `node`, `hygiene`. (Not applied automatically: the release bot
   commits the regenerated manifest straight to `main`.)
3. **Pages environment:** *Settings -> Environments -> github-pages* -> deployment branches: `main` only.

## Live data and the exposure risk

Run logs describe the fleet: host names, item ids, error text, evidence. The logs repo is **private**, but **GitHub Pages is public**, so the dashboards built from the live
logs are public too. Read [LIVE_LOGGING](LIVE_LOGGING.md) before enabling live data, and see [SECURITY](SECURITY.md) for what is and is not protected. Telemetry pushed from
installed machines is advisory: the credential ships inside the installer, so nothing automatic (alerts, issues, pauses) should rely on it without a person's confirmation;
`health` therefore only opens issues and never changes the registry.

## Troubleshooting the pipelines

| Symptom | Cause | Fix |
|---|---|---|
| A workflow shows as a file path and starts no jobs | The YAML is invalid for GitHub (often a `: ` inside a plain scalar) | `python tools/check_workflows.py` (uses PyYAML when installed) |
| `site` fails at *configure-pages* | Pages source is not GitHub Actions | `python tools/harden_repo.py` or *Settings -> Pages -> Source: GitHub Actions* |
| `release` fails: "already released with different content" | A released version was edited | Bump `version` (VERSION / automation.yaml) |
| `release` or `ops-action` cannot open a PR | Actions may not create PRs | Settings step 1 |
| PR checks never start on bot PRs | Created with `GITHUB_TOKEN` | Add `OPS_BOT_TOKEN` |
| `health` says nothing to monitor | `LOGGING_TOKEN` missing or nothing has reported yet | Set the secret; install the connected installer and run it once |
| `canary` fails at the attestation step | The installer was built before attestations existed, or the release was uploaded by hand | Re-run `installers` on `main`, then re-run `canary` |
| `canary` fails at install/run | A real regression in the published installer | Open the `ops-signal` issue it created; roll back with `ops-action` if users are affected |
| Install page says "not available at build time" for the SHA-256 | `installers-latest` has no `.sha256` asset yet | Run `installers`; the next `site` run picks it up |
| Endpoints report registry unreachable | Pages not deployed or the registry dir missing | Check the site deployment and `https://<owner>.github.io/<repo>/registry/manifest.yaml` |
| Endpoints log "unsigned" | No key pinned on that machine, or the manifest is not signed | Set `REGISTRY_SIGNING_SEED`, then `AUTOSTATION_REGISTRY_PUBKEY`, rebuild the installer (see RUNBOOKS) |

## Safety rules the pipelines follow (second hardening round)

* **`production` environment.** Jobs that read secrets (`site` build, `health`, `installers` build) declare `environment: production`, which `tools/setup_live.py` restricts to the default branch; store the secrets there with `--env-secrets` and a branch or re-run from elsewhere cannot read them.
* **Fresh-tip signing.** When `REGISTRY_SIGNING_SEED` exists, `site` refuses to sign or deploy unless its commit is still the tip of `main`, so re-running an old run can never re-sign an older manifest with a fresh timestamp. A follow-up (needs endpoint work in wrapper-core) is a monotonic sequence number inside the signed document.
* **The registry always deploys.** The live-data steps of `site` are `continue-on-error`; if they fail, the site is rebuilt without live data, so an emergency pause or a rollback still reaches the endpoints.
* **No all-clear from bad data.** `health` closes issues only when the report carries trustworthy `data_quality` (enough records, fresh, few rejects, no failed signal builder), only for signals the analytics can produce (never the canary), and only after **two** consecutive clean runs (label `signal-clearing`). A `wontfix` label stops reopening. At most 50 ops-signal issues are open at once; the overflow is one `[DIGEST]` issue. Signals for automations the manifest does not list never open issues.
* **Release plumbing.** `release` checks out without credentials and hands the write token only to the steps that push; `ops-action` has `contents: read` at workflow level and its write scopes on the job only, and its summary output is allow-listed and fenced.
* **Installers.** The SBOM is attested together with the installer; the stable exe is uploaded before its `.sha256` (a window of seconds in which they disagree is possible - the canary retries it); the canary pins `gh attestation verify` to the installers workflow and fails (never silently passes) if the demo portal is missing.


> Scheduled runs (`site` every 30 minutes, `health` every 30 minutes, `canary` daily) are best-effort on GitHub: they can start several minutes late or be skipped under load. Every push to `main` also rebuilds the site, so a push is the quickest way to refresh the dashboards after new telemetry arrives.
