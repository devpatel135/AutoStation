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

* **dev → pilot → broad.** A version reaches a ring only through an approved MR; `registry validate` rejects skipping a ring.
* **Pilot entry:** passing CI, `assist review` clean of HIGH findings, manifest + catalog complete, security reviewer sign-off
  on `data_classification` and LLM use.
* **Broad entry:** ≥5 consecutive scheduled pilot runs with no `FAILED`/`HUNG`/`MISSED` signals, no open critical alerts, owner approval.
* **Emergency stop:** `pause` (one automation) or `fleet-pause` (all). Takes effect on each machine's next scheduled run
  (and on login via the maintenance script). Approval is still an MR, but approvers are on call for this path.
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
| Released versions never change | `tools/build_registry.py` fails on same-version/different-content; `registry-artifacts` is append-only (workflow check + ruleset) |
| Workflows cannot be turned against the repo | `tools/check_workflows.py` in CI: SHA-pinned GitHub-owned actions, timeouts, least privilege, no injection into shells, no `pull_request_target`, secrets never in scripts |
| No credentials in the repo | `tools/check_secrets.py` (CI) + GitHub secret scanning with push protection |
| Dependencies are known | Dependabot, `security.yml` (pip/npm audit, dependency-review), SBOM attached to every installer release |
| Downloads are verifiable | SHA-256 beside every installer, build-provenance attestation (`gh attestation verify`), signed registry manifest once a key is pinned |
| The fleet is watched | `health.yml` opens `ops-signal` issues, `canary.yml` proves the published installer still works; both close themselves when the problem clears |
| Every pull request is triaged | `pr.yml` labels by area and size and posts the promotion checklist when `manifest.yaml` changes |

**Ownership of an `ops-signal` issue.** The automation's `owner` (manifest) is named in the issue. Critical: acknowledge the same day and pause or roll back first (RUNBOOKS section 1 and 2); high: within one business day; the platform owner triages anything without an owner. An issue that is closed by the pipeline but was never acknowledged is still a finding: note it in the weekly review.
