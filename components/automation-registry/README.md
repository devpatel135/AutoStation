# automation-registry

The version-controlled **manifest** that tells every machine what to run (`manifest.yaml`), the tooling that edits it safely
(`python -m registry`), and the **static portal** CI publishes to GitLab Pages: storefront / catalog, per-automation pages,
compatibility view, governance page and the **fleet admin panel**. Pattern A - no server anywhere.

```
manifest.yaml        rings, versions (+sha256), schedules, kill-switch, retry policy, catalog metadata   (SPEC section 4)
triggers.json        on-demand / event triggers (lives on the protected `triggers` branch; SPEC section 12)
history/CHANGELOG.md one line per operator action (written by the tool; the MR is the approval record)
schema/manifest.schema.json   JSON-Schema (draft 2020-12) for editors; enforced by `validate` too
registry/            the CLI + portal generator (Python 3.9+, stdlib only; shares wrapper_core.manifest with the endpoints)
docs/                runbooks: rings-and-promotion, kill-switch, rollback, triggers, token-rotation, catalog
```

## Operator commands

All commands take `--manifest PATH` (default `./manifest.yaml`), `--reason`, `--dry-run`. Each rewrites the YAML
deterministically, **validates before writing and refuses to persist an invalid manifest**, appends to
`history/CHANGELOG.md`, and prints a suggested MR title. Run them in a branch and open a merge request.

| Command | What it does |
|---|---|
| `validate [--artifacts DIR] [--strict] [--schema-only] [--allow-placeholders]` | Shared rules (`wrapper_core.manifest.validate`) + JSON-Schema + governance checks (owner email, duplicate slots, catalog, trigger/poll-slot consistency, artifact existence + sha256 + package identity). Exit 1 on errors. |
| `add-version component\|automation NAME PKG.zip [--version V]` | Hashes the zip, reads `requires` from its `automation.yaml`, copies it to `artifacts/...`, registers it and assigns it to **dev only**. Re-adding identical bytes is a no-op; different bytes under the same number is refused. |
| `promote NAME --to pilot\|broad` | Enforces dev -> pilot -> broad; only the version the previous ring already runs. |
| `rollback NAME --ring R --to V --reason ...` | Caps ring R and all later rings at V. |
| `pause NAME --reason ...` / `resume NAME --reason ...` | Per-automation kill-switch. |
| `fleet-pause --reason ...` / `fleet-resume --reason ...` | Global kill-switch. |
| `decommission NAME --reason ...` | Removes a *paused* automation (+ trigger rules pointing at it); writes `history/decommissioned/NAME.md`. |
| `trigger-add NAME [--slot S] [--param k=v ...]` | Queues an on-demand run in `triggers.json` (needs a `poll` slot). |
| `new-automation NAME --owner EMAIL --description ...` | Adds a catalog-complete **paused** skeleton; becomes live after its first `add-version` + `resume`. |
| `compat` | Per-ring compatibility matrix; exit 1 if unsupported. |
| `build-portal --out public [--signals S --runs R --extra-triggers T --artifacts DIR --governance MD --config JSON]` | Generates the static site (below). |
| `ops-action [--git]` | CI entry point for the admin panel (reads `ACTION TARGET REASON PARAMS BRANCH`). |

Typical release:

```
python -m registry add-version automation automation-invoice-router build/automation-invoice-router-1.3.0.zip
python -m registry promote automation-invoice-router --to pilot --reason "dev soak passed"
# ...one schedule cycle later...
python -m registry promote automation-invoice-router --to broad --reason "pilot owner signed off"
```

Placeholders: the starter `manifest.yaml` carries all-zero sha256 values so it validates only with `--allow-placeholders`.
`add-version` replaces them with real hashes; CI's `validate-release` job (strict, never allows placeholders) becomes the blocking
gate when you set `RELEASE_STRICT=1` and remove `ALLOW_PLACEHOLDERS` from `.gitlab-ci.yml`.

Notes on the YAML: wrapper-core reads a restricted subset (no anchors/tags; always quote times `"09:00"` and versions
`"1.0.0"`). The tool writes exactly that subset; comments are not preserved when the tool rewrites the file.

## The portal (`build-portal`)

| Page | Content |
|---|---|
| `index.html` | Catalog: search, tag/status filters, ring selector, cards with version per ring, status (paused + reason), owner, systems, data classification, human-readable schedule, health badge, **installer download** (`installers/<automation>-<version>-setup.exe`) and the package sha256. |
| `automation/<name>.html` | Everything about one automation: versions by ring, requires, schedule table, maintenance windows, error-handling, triggers (rules + queued), health/signals, changelog from `history/`. |
| `admin.html` | Fleet admin panel (below). |
| `compatibility.html` | Supported wrapper-core x framework pairs and the per-ring version table (red = `requires` unmet). |
| `governance.html` | Renders `docs/GOVERNANCE.md` found next to the manifest or up to 4 parent directories (a notice if absent). |
| `manifest.yaml`, `triggers.json`, `index.json`, `portal.config.json` | Machine-readable copies. **`manifest.yaml` and package URLs are what endpoints fetch.** `index.json` is the catalog for other tooling. |

Optional inputs from `automation-logging`: `--signals signals.json` and `--runs runs.json|DIR` give health badges and signal lists;
everything degrades gracefully ("no telemetry") without them. `installers/` next to the manifest and `--artifacts DIR` are copied
into the site when present. The build **refuses** to publish an invalid manifest and refuses to wipe a non-portal `--out`.

Security: all manifest/telemetry text is HTML-escaped; embedded JSON cannot close its `<script>` tag; catalog links are anchors only
when `http(s)`/`mailto`; the admin script builds its DOM with `textContent`; external fonts are not loaded (airgapped-safe system
font stacks); no analytics, no calls home. `portal.config.json` sets `title`, `gitlab_url`, `project_path`, `default_branch`,
`ops_ref`, `installers_path` (defaults come from `CI_SERVER_URL` / `CI_PROJECT_PATH` / `CI_DEFAULT_BRANCH` in CI).

### Admin panel without a server

For each automation the panel offers only the actions that currently make sense (pause *or* resume, promote to the next ring,
rollbacks that actually move something, run-now when a poll slot exists) and shows, **computed at build time by applying the real
`registry.ops` functions to a copy of the manifest**: the exact unified diff, the CLI command (bash or PowerShell quoting), the CI
variable set for `ops-action`, and prefilled GitLab links (run pipeline / new MR / edit file). The operator's reason is the only thing
the browser substitutes.

```
ACTION=pause TARGET=automation-x REASON="..." PARAMS={} BRANCH=ops/pause-automation-x-lk3j2
```

`ops-action` (job in `.gitlab-ci.yml`, code in `registry/ci_ops.py`) validates every variable (action whitelist, target pattern,
single-line reason, known PARAMS keys), applies the operation, **re-validates the whole manifest**, then via the GitLab API: commits to
the `ops/...` branch and opens an MR labelled `ops-action` (manifest changes) - or commits straight to the `triggers` branch and re-runs
Pages (`trigger-add`). No git credentials or push rights on `main` are needed.

## GitLab settings (do these once)

1. **Protected branches**
   - `main`: *Allowed to merge*: Maintainers; *Allowed to push and merge*: **No one**; force-push off.
   - `triggers`: *Allowed to push*: **the ops bot only**; *Allowed to merge*: No one; force-push off. (Create it by running the first `trigger-add`.)
   - `ops/*`: protected wildcard, push allowed for the ops bot only (so only the bot creates operator-action branches).
2. **Merge request approvals** (the governance gate)
   - Rule "Registry owners": at least **1** approval (2 recommended for `broad` promotions and `decommission`), eligible: automation-platform team.
   - Rule "Automation owner" via CODEOWNERS (below): changes to an automation's stanza need an approval from its owner/team.
   - Enable *Prevent approval by author* and *Prevent editing approval rules in merge requests*; require *All discussions resolved* and *Pipelines must succeed*.
3. **Merge request settings**: merge method *Fast-forward* or *Merge commit* (keeps the audit trail), *Squash*: optional, *Delete source branch*: on.
4. **CODEOWNERS** (`CODEOWNERS` in this repo): platform maintainers own the tooling, CI and schema (the governance gate itself);
   the platform team owns the manifest and history; security owns `docs/GOVERNANCE.md`. CODEOWNERS is per file, so per-automation
   owner approval is by convention (the MR names the requester and lists a review checklist). Replace the example group names.
5. **CI/CD variables**: `OPS_BOT_TOKEN` (protected + masked, project access token, role Developer, scopes `api`+`write_repository`);
   optional `WRAPPER_CORE_REPO`, `BUILDER_PROJECT_ID`, `LOGGING_PROJECT_ID`, `EMERGENCY_PAUSE_USERS`; `ALLOW_PLACEHOLDERS` only until go-live.
6. **Who may run the `ops-action` pipeline**: Settings > CI/CD > Pipeline settings: restrict "Run pipeline" to Developer+; add the operators group as
   Developers. `resource_group: ops-action` serialises concurrent actions.
7. **Pipeline schedule**: every 10 minutes on `main` (republishes the portal with fresh health and rule-triggers).
8. **Pages**: access control - keep it internal; the endpoints read `manifest.yaml` through the API/read token, humans through Pages.
9. **Read token for endpoints**: a group or project access token, role *Reporter*, read-only scopes. Rotation: `docs/token-rotation.md`.

## Development

```
cd components/automation-registry
python -m pytest            # ~85 tests, no network; wrapper_core is found at ../wrapper-core
```
