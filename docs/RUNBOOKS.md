# Runbooks

## 1. Kill-switch (pause one automation or the fleet)

1. `python -m registry pause automation-x --reason "bad upstream data, ticket INC123"` (or `fleet-pause --reason …`).
2. Open the MR, get approval (approvers are on call for this path), merge. The pipeline republishes `manifest.yaml`.
3. On each machine the **next scheduled run** (or login maintenance) reads the manifest, halts *before the payload executes*,
   writes `state\toast\<automation>.status.txt` ("intentionally paused: …"), shows a toast, and logs `halted_paused` +
   a `paused_seen` lifecycle event. Nothing to do per machine.
4. Verify on the dashboard: the automation's runs show `halted_paused`; no new item telemetry.
5. Resume: `registry resume automation-x` (MR). Status file and notice clear on the next run.

*Fallback if the registry is down:* machines use the last cached manifest. If an emergency stop is needed while the registry is
unreachable, disable the scheduled tasks via Group Policy / EDR script (`schtasks /Change /TN "AUTOMAGIX\<name>-<slot>" /DISABLE`).

## 2. Rollback

`python -m registry rollback automation-x --ring pilot --to 1.2.3` (rolls back the ring and every ring above it). Machines
converge on their next run (self-heal compares installed `VERSION` markers to the ring's target). wrapper-core updates are staged
and applied by the launcher before the *next* launch (a running package cannot replace itself on Windows).

## 3. Failure triage

Alert → open the dashboard card → read *synopsis*, *LLM diagnosis*, *retry history*.

| Signal | Meaning | First move |
|---|---|---|
| FAILED | Run failed after retries | Read error category: LOGIC → fix code; AUTH → cert/session; NETWORK/SYSTEM → check target + VPN; then `registry trigger-add` to re-run |
| ITEM_FAILURES | Run "succeeded" but items failed | Look at `error_category` distribution; usually a data/upstream change |
| MISSED | Expected run never logged | Check machine heartbeat (SILENT?), `state\tasks.json`, Task Scheduler, startup script |
| SILENT | No heartbeat from a machine | VDI recycled? user signed out? wrapper-core dead? Re-run `python -m wrapper_core maintain` |
| SLOW | Duration > threshold × baseline | Data-volume spike or slow target; adjust baseline if it's the new normal |
| HUNG | Watchdog fired (and possibly killed at `timeout_s`) | Check stderr.log in the run folder; hung browser/subprocess |
| VERSION_DRIFT | Ran a version ≠ ring target | Self-heal failing (token? share?) — read `self_heal.errors` in the run log |
| UNEXPECTED | Ran inside a maintenance window | Window not propagating; check manifest + `maintenance_windows` |

## 4. Secret rotation

| Secret | Where it lives | Cadence | Procedure |
|---|---|---|---|
| **Self-heal read token** | GitLab token → endpoint Credential Manager `AUTOMAGIX/registry_token` | Quarterly + offboarding | Create the *new* read-only token **without revoking the old one** → update the `REGISTRY_TOKEN` CI file variable used by installer builds → rebuild installers → push the silent installer (`/S`, idempotent; it only rewrites what changed, including the credential) through your software-distribution tool → watch the heartbeat `versions`/self-heal errors until the fleet is converged → revoke the old token. The overlap window is what makes this safe. |
| **Logging SSH key (write-only)** | CI file var `LOGGING_SSH_KEY_FILE` → per-install DPAPI blob | Annually | Generate keypair → add public key to `automation-logging` as a deploy key (push to `ingest/*` only) → update CI variable → rebuild + redistribute installers → remove old deploy key after the version-drift report is clean. Compromise impact: junk refs only. |
| **LLM API key (CI)** | GitLab protected CI variable on `automation-logging` | Per policy | Update the variable; next pipeline uses it. |
| **LLM API key (endpoint)** | Credential Manager `AUTOMAGIX/llm_key` | Per policy | Same mechanism as the read token. |

> **Design note — fleet-wide secret rotation.** Endpoint-resident secrets (registry read token, LLM key, logging key) are written
> once at install time into per-user DPAPI / Credential Manager storage; they are not fetched dynamically from the registry (a
> registry-delivered credential would let anyone who can edit the registry feed machines a token of their choosing). Rotation is
> therefore "overlap + silent re-install". If the fleet grows beyond what software distribution can reach, add a signed
> secret-update component as a deliberate, security-reviewed feature — it does not exist today (see `docs/SECURITY.md`, *Known gaps*).

## 5. Alerting channel decision (open question Q10)

Both paths are implemented; pick the one the team actually watches.

* **Jira:** enable the `jira` channel in `automation-logging/alerting.yaml` (`JIRA_URL`, `JIRA_PROJECT`, `JIRA_TOKEN` as protected variables).
  One issue per signal fingerprint; later occurrences comment on it; resolution comments when the signal clears.
* **Dog-fooded notifier:** register `automation-notifier` in the manifest (hourly slot); set `signals_url` to the logging Pages
  `signals.json`, `channel: teams` (webhook in Credential Manager) and/or `email`.

## 6. Decommissioning an automation

`pause` (MR) → verify no runs → `registry decommission` (MR) → users run the installer's **uninstall** (or push it via your
software-distribution tool): removes tasks, payload, ref-counted shared components, DPAPI blob, Credential Manager entries, and
sends a final `decommissioned` event. Machines that never uninstall will see "not registered in the manifest" and halt on their own.

## 7. Onboarding a new ring/dev environment

The `dev` ring uses a **separate logging remote** (set `log_remote` in its installer config / `platform.json`) and a registry namespace
(own `manifest.yaml` branch or project), so dev runs never contaminate production logs or alerts. Developers run
`python -m wrapper_core dev .` for fully offline loops.
