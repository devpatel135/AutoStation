# AUTOSTATION Governance Model

The technical foundation is solid; what makes AUTOSTATION a *platform* rather than a collection of scripts is governance,
discovery, developer experience and a support model. This document is rendered into the registry portal.

## Roles

| Role | Can | How it is enforced |
|---|---|---|
| **Automation developer** | Create an automation repo (scaffolder), merge to its `main`, propose a dev-ring registry MR | GitLab group membership; repo template with protected `main` + required reviewer |
| **Automation owner** (distribution list in the manifest) | Receives alerts, triages, owns the SLA | `owner` / `owner_team` in the manifest; alert routing |
| **Platform approver** | Approves registry MRs: ring promotion, pause/resume, rollback, fleet pause, decommission | CODEOWNERS on `manifest.yaml`; required approvals (≥1 platform approver + ≥1 owner for promotions); MR history **is** the audit trail |
| **Operator** | Uses the admin panel to generate the MR/pipeline action; may trigger on-demand runs | On-demand `trigger-add` is operator-authorised directly; everything else goes through MR |
| **Security reviewer** | Reviews data classification and LLM approval level before pilot | Checklist in `AUTOMATION_STANDARD.md` §B; recorded in the promotion MR |

## Change control

* **dev → pilot → broad is staged and health-gated, not approved one MR at a time.** Merging to `main` makes `release` register the new
  version in ring **dev only** (after the platform tests, the Windows tests and the upgrade smoke test have passed). `promote.yml`
  then moves it ONE ring per run: dev → pilot after `AUTOSTATION_SOAK_PILOT_H` hours (default 6), pilot → broad after
  `AUTOSTATION_SOAK_BROAD_H` hours (default 24), and only while the live health report is fresh and trustworthy and holds no
  `FAILED`/`HUNG`/`REGRESSION_AFTER_RELEASE`/`ERROR_BUDGET_BURN` signal for that version (`MACHINE_UNHEALTHY` holds back the shared
  components), no open `ops-signal` issue names the package, and no delivery-pipeline alert is open. A package that is blocked for
  more than 24 h raises one `promote-stuck-<name>` ops issue. `registry validate` still rejects skipping a ring.
* **Manual override:** an operator can promote any version at any time (`registry promote`, the admin panel, `ops-action.yml`), and can run
  `promote.yml` with `dry_run` to see the decision. Overrides are manifest edits like every other change and land in the registry history.
* **Pilot entry (policy, checked by people before the first release of an automation):** passing CI, `assist review` clean of HIGH
  findings, manifest + catalog complete, security reviewer sign-off on `data_classification` and LLM use. The gate checks health;
  it does not replace this review.
* **Emergency stop:** `pause` (one automation) or `fleet-pause` (all). Takes effect on each machine's next scheduled run
  (and on login via the maintenance script). **Pause and rollback never wait** for a soak, a gate or a promotion: they are manifest edits
  that go straight out, and the promotion gate refuses to promote anything that is paused.
* **Rollback:** `registry rollback` — a manifest edit; machines converge on their next run through self-heal.

## Data classification & the LLM

* Every automation declares `data_classification` (`public|internal|confidential|restricted`) and the highest level the LLM
  endpoint is **approved** for (`llm_allowed_classification`). Above the approved level, the platform withholds all content from
  the LLM (`llm_call` returns `None`, the synopsis and CI diagnosis fall back to deterministic text).
* Fields that carry PII/sensitive values are logged with `sensitive_keys`; redaction happens before any LLM context is built —
  on the endpoint (synopsis, retry classification) **and** in the CI analytics pipeline (error diagnosis).
* LLM-driven decisions are auditable: an `llm_decision` event (caller, template name, input hash, decision, reasoning, outcome)
  — never the prompt text, so PII does not leak into audit records.
* Confirm the endpoint's approval level with the endpoint team **before** onboarding anything above `internal`.

## Ownership & support

* Alerts name the automation, signal, error category, LLM diagnosis, retry history, run synopsis and a log link, and are routed
  to the owner list (Jira / Teams / email — channel is a policy decision, see runbooks).
* Triage SLA by classification (suggested): restricted/confidential 4 business hours; internal 1 business day; public best effort.
* Repeated escalations on one automation mean redesign, not more exception machinery.
* Every automation has a decommission path: pause → `registry decommission` → installer uninstall (ref-counted shared components,
  final `decommissioned` log event, DPAPI blob + Credential Manager cleanup).

## Policy defaults (editable via MR to this file)

| Policy | Default |
|---|---|
| Installer signing | Required for pilot/broad releases (`--require-signing`); dev builds may be unsigned |
| Secret rotation | Self-heal read token quarterly + on developer offboarding; logging SSH key annually; LLM key per GitLab protected-variable policy |
| Log retention | Raw run logs 13 months; dashboards regenerate from the archive |
| Registry visibility | Internal GitLab users (pilot); revisit gating via the usercredentials endpoint before broad |
| Attended automation | Not offered; trigger-based unattended runs only |

## Pipelines as controls (what enforces the rules above)

| Rule | Enforced by |
|---|---|
| Nothing unreviewed changes what runs on endpoints, how it is released or who can release it | `.github/CODEOWNERS` (installer, secrets, selfheal, transport, registry, workflows, release tooling) + a ruleset requiring Code Owner review once more than one person works here |
| Released versions never change | `tools/build_registry.py` fails on same-version/different-content in `release` and, before merge, in the `version-gate` job of `ci.yml` (`--check`, naming the files to bump; `tools/bump_version.py` bumps them together); `registry-artifacts` is append-only (workflow check + ruleset) |
| A broken source tree cannot block a pause or rollback | `site` publishes with `build_registry.py --publish-only` (released zips verified by SHA-256, no source comparison) |
| Workflows cannot be turned against the repo | `tools/check_workflows.py` in CI: SHA-pinned GitHub-owned actions, timeouts, least privilege, no injection into shells, no `pull_request_target`, secrets never in scripts |
| No credentials in the repo | `tools/check_secrets.py` (CI) + GitHub secret scanning with push protection |
| Dependencies are known | Dependabot, `security.yml` (pip/npm audit, dependency-review), SBOM attached to every installer release |
| Downloads are verifiable | SHA-256 beside every installer, build-provenance attestation (`gh attestation verify`), signed registry manifest once a key is pinned |
| The fleet is watched | `health.yml` opens `ops-signal` issues, `canary.yml` proves the published installer still works; both close themselves when the problem clears |
| The pipelines are watched | `pipeline-alert.yml` raises one `ops-signal` issue per failing workflow (`pipe-<workflow>`) and `pipe-site-stale` when the live site is over 12 h old; each closes on the next success |
| New versions reach machines gradually and only while healthy | `build_registry.py --auto-promote` registers new versions in ring dev only; `promote.yml` / `tools/promote_gate.py` promote one ring per run after a soak and a clean, fresh health report |
| An upgrade never strands an installed machine | `release.yml` job `windows`: the upgrade smoke (`tools/ci/upgrade-smoke.ps1`) installs the previous installer, upgrades it from the new registry and proves a broken wrapper-core is rolled back; publishing needs it |
| What machines download is verified after every deploy | `site.yml` job `verify` (and `canary.yml`): `tools/verify_live.py` checks the live signature against the pinned key, the seq floor and every zip's SHA-256; a failure raises `pipe-site-verify` |
| The only copy of the released packages is not the only copy | `backup-registry.yml`: weekly and after every release, mirrored to the backup repository (secret `BACKUP_TOKEN`) or kept as a 90-day bundle artifact; every release is tagged `rel-<sha7>` |
| Pinned runtimes do not silently expire | `pin-freshness.yml` (monthly): one `pin-freshness` ops issue listing outdated or end-of-life pins (Node, embedded Python, MinGit, playwright-core, tzdata) |
| Every pull request is triaged | `pr.yml` labels by area and size and posts the promotion checklist when `manifest.yaml` changes |

**Ownership of an `ops-signal` issue.** The automation's `owner` (manifest) is named in the issue. Critical: acknowledge the same day and pause or roll back first (RUNBOOKS section 1 and 2); high: within one business day; the platform owner triages anything without an owner. An issue that is closed by the pipeline but was never acknowledged is still a finding: note it in the weekly review.
