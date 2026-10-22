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
| ERROR_BUDGET_BURN | Failing faster than the SLO allows (>=10x over 24 h, >=2x over 7 d, or the window budget is gone) | Open Reliability; fix the top signature on the Errors page before the budget is spent; if the SLO is wrong, set `catalog.slo` |
| FLAKY | Flips between ok and failed (>=4 flips in the last 20 runs) | Retries are hiding a root cause: open Errors, find the signature that alternates, fix it |
| DURATION_ANOMALY | Latest run is a statistical outlier against its own history | Compare with the target system and data volume; a fast outlier usually means skipped work |
| REGRESSION_AFTER_RELEASE | A new error signature appeared within 48 h of a new version and never on the older one | Roll back (`ops-action rollback`) or hot-fix; the signal names both versions |
| REMEDIATION_FAILING | A remediation playbook failed >=3 times in 7 days and mostly does not fix | The playbook is not helping: fix the cause, adjust or retire it (Errors page, Proactive remediation) |
| PERSISTENT_MANUAL_STEP | A run has needed a person (sign-in, MFA, untrusted redirect) for >24 h | Do the manual step on that machine (see `manual_step_required` in the run events) |
| MACHINE_UNHEALTHY | One machine fails across automations while others are healthy | Machine problem: profile, VPN, disk, session, Edge policy; run `python -m wrapper_core doctor` there |

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

## Certification automation stops with `untrusted_redirect`
The Playwright path only clicks the Microsoft account tile when the sign-in's `redirect_uri` points back at the portal or an allow-listed host (default `develop.deloitte.com`). Look at the run's `events.jsonl` for `untrusted_redirect`: `redirect_host` is the host Microsoft would return to. If it is a Deloitte host you trust, add it as a slot param in the registry manifest (`schedule.slots[].params.allowed_hosts: "host1,host2"`, exact hosts only) and promote; if you do not recognise it, do not add it. Machines whose Edge policy forbids DevTools (`RemoteDebuggingAllowed=0`) use native mode, which opens your normal Edge and cannot check redirects (it only presses a tile inside the window it opened).

## Signing the registry manifest
`python tools/setup_live.py` (answer `y` to the signing key) stores `REGISTRY_SIGNING_SEED` and the repo variable `AUTOSTATION_REGISTRY_PUBKEY`. The next `site` run signs `registry/manifest.yaml`; the next installer build pins the public key in `platform.json` (`registry_pubkeys`). Machines installed before that hold no key and keep accepting the registry until reinstalled. To rotate: generate a new key, list both public keys in the variable (comma-separated), release, then drop the old one. Verify any machine with `python -m wrapper_core.trust --show`.



### Rollback protection (`seq`) and what to do when it blocks you
Each signed manifest carries a sequence number (the `site` workflow run number, `SIGN_SEQ`) and endpoints remember the highest one per key. A
validly signed manifest with a LOWER number is refused (`rollback refused: seq N is lower than the newest accepted M`) and the machine keeps
running from its last verified manifest. That is the point: a replayed old release cannot be installed. To roll a release back on purpose, do not
re-publish an old manifest: revert the change on `main` (a new commit, a new run, a higher `seq`) or use the `rollback` ops-action, which also
produces a new signed manifest. If the repository itself was re-created (run numbers restart from 1), rotate the signing key (below) or delete
`state/manifest.seq.json` on the affected machines. `python tools/readiness.py` shows the current `seq` and whether `triggers.json` is covered.

### Key rotation
1. `python tools/sign_registry.py --gen-key new-seed.txt` prints the new PUBLIC key. Put the seed in the repo secret `REGISTRY_SIGNING_SEED`
   only AFTER step 2 has reached the machines.
2. Add the new public key next to the old one in the repo variable `AUTOSTATION_REGISTRY_PUBKEY` (comma separated) or in `platform.json`
   `registry_pubkeys`, release, and let the fleet pick it up (installers built afterwards pin both).
3. Switch the secret to the new seed; the next `site` run signs with the new key (its own `seq` counter starts fresh).
4. After the fleet has verified a manifest signed by the new key, remove the old key from the list.
`triggers.json` is signed together with the manifest on every `site` run, so a trigger added with `ops-action trigger-add` takes effect after the
next site build, not instantly; a `triggers.json` that does not match the signed digest is ignored by endpoints with a pinned key.

## "I don't see the train icon while an automation runs"
1. Windows 11 parks new notification-area icons behind the **up-caret** (^) at the bottom right. Click the caret: the train is in that flyout. To keep it visible drag it onto the taskbar, or turn it on under *Settings -> Personalization -> Taskbar -> Other system tray icons*.
2. `python -m wrapper_core doctor` shows `tray indicator ... off: <reason>` when it is disabled (`AUTOMAGIX_NO_TRAY`, `AUTOMAGIX_NO_TOAST`, `tray: false` in `platform.json` or the automation's `automation.yaml`).
3. `python -m wrapper_core tray --demo 8 --verify` adds the icon for 8 seconds and prints `{"added": true, "registered": true, ...}` when Windows accepted it. `added: false` means the shell refused it (no desktop session, or explorer not running yet).
4. A **ghost icon** (train left behind after a crash or a force-kill) disappears as soon as the mouse passes over it, or after restarting Explorer. Normal runs remove their icon themselves.
5. Runs started as a service or in a session without a desktop show no icon by design.

## Canary failed
The daily `canary` workflow downloads the **published** installer and runs it on a clean Windows runner. A red canary means new users would hit the same problem, so treat it as high severity.
1. Open the `ops-signal` issue (title starts `[CANARY]`) and the linked run. The failing step tells you where: **download** (release assets missing: re-run `installers`), **sha256** (the file or its `.sha256` is wrong: do not tell anyone to install, rebuild), **attestation** (built before attestations existed or uploaded by hand: re-run `installers` on `main`), **install** or **run** (a real regression).
2. If it is a real regression and users are affected, pause the automation (section 1) or roll back (section 2), then fix forward.
3. The issue closes itself on the next green canary. Re-run it from the Actions tab (`canary`, *Run workflow*) once the fix is published.

## Reading an ops-signal issue
`health` opens one issue per fleet signal at severity high or critical, labelled `ops-signal`, `severity:<x>` and `signal:<type>`; the body names the automation, host, ring and first/last seen. Everything in it came from machine telemetry, which is **advisory** (the credential that pushes it ships inside the installer): confirm on the machine or the Jobs/Machines pages before acting.
| Signal | Usual cause | First move |
|---|---|---|
| `SILENT` | machine off, asleep, offline, or the task was removed | `python -m wrapper_core doctor` on the machine; if the machine is gone, decommission it from the fleet view |
| `MISSED` | a scheduled run did not happen (machine off at the scheduled time, task disabled) | check the schedule and the machine; a single miss on a laptop is normal |
| `HUNG` | a run exceeded its timeout and was killed | open the run on the Jobs page, read the evidence; consider raising `timeout_s` or fixing the wait |
| `VERSION_DRIFT` | a machine is still on an old version long after the target changed | is the registry reachable from the machine; is the machine signing in; roll back if the new version is the problem |
| `FAILED` | the run failed after retries | section 3 (failure triage) |
Close nothing by hand: the issue closes when the signal clears. To stop a signal permanently, fix the cause or pause the automation.


## Automatic remediation (what the platform fixes by itself)
When a run fails, a pre-flight check fails, or a login/idle heartbeat finds trouble, wrapper-core classifies the problem (`remediation.diagnose`, no network, no LLM) and runs a **safe, bounded, reversible** playbook. At most **one** re-run of the payload per run, and only when the playbook says it can help.

| Diagnosis | What it does automatically | Re-runs? |
|---|---|---|
| `DEVTOOLS_BLOCKED` (Edge policy `RemoteDebuggingAllowed=0`) | writes `state/overrides/<automation>.json` = native mode (30 days). The policy is never touched. | yes |
| `STALE_BROWSER_PROFILE_LOCK` | stops Edge processes whose command line names **this automation's** profile folder (exact match, not look-alikes), removes `SingletonLock`/`lockfile` | yes |
| `CORRUPT_BROWSER_PROFILE` | moves the profile to `profiles.bad/` (never deleted, newest 3 kept), creates a fresh one, asks a person to sign in | no |
| `STALE_LOCK` | removes a run lock whose owner process is gone | no |
| `MISSING_FILE` / `COMPONENT_CORRUPT` / `NODE_MISSING` | re-fetches the component from the signature-verified registry (hash-checked) and re-asserts tasks | yes |
| `SCHEDULED_TASK_MISSING` / `_DISABLED` | re-registers / re-enables the Task Scheduler tasks and the login script | no |
| `DISK_LOW` | deletes this tool's own old run folders, rotated logs, debug bundles, quarantined profiles, leftover `.old`/`.staging` dirs (bounded, inside the install folder only) | yes |
| `SIGNIN_REQUIRED` / `AUTH_EXPIRED` | one deduplicated "needs a person" notice (toast + `state/toast/<name>.needs-attention.txt` + one approval task) | **never** |
| `DEPENDENCY_VERSION_MISMATCH` | runs the self-heal pass to pick up a compatible component | yes |
| `NETWORK_UNREACHABLE` / `DNS_FAILURE` / `PROXY_FAILURE` / `TLS_FAILURE` / `CLOCK_SKEW` | read-only diagnosis incl. clock skew vs the registry's `Date` header; recommendation only | no |
| `PYTHON_RUNTIME_BROKEN` / `EDGE_MISSING` / `GIT_MISSING` | recommendation only | no |
| `UNKNOWN` | writes a redacted debug bundle (once per failure signature per day) | no |

**Never:** change a machine policy, touch `secrets/`, delete outside the install folder, run elevated, kill a process it cannot tie to the automation's own profile, retry in a loop (each playbook has a per-24 h budget and a cool-down in `state/remediation.json`), hide the original error (`record.remediation.original_error`; the run's status is whatever the last attempt produced, plus `remediated: true` if the re-run succeeded).

**Switch off:** `AUTOMAGIX_NO_REMEDIATION=1`, or `"remediation": false` in `config/platform.json`; `"remediation": {"disable": ["prune_local_data"]}` disables single playbooks. Paused automations are never touched.

**See what it did:** each attempt is a `remediation_attempt` event in the run record and a line in `logs/remediation.jsonl`. On the machine:
* `python -m wrapper_core remediate --dry-run` - diagnose the last failed run and the install, show what it *would* do; without `--dry-run` it applies the safe fixes (`--category X`, `--automation N`, `--include-tasks`, `--json`; `--clear-overrides` removes the native-mode override).
* `python -m wrapper_core doctor --fix` - apply the safe playbooks for FAIL/WARN checks.
* `python -m wrapper_core debug-bundle [--run RUN_ID] [--out FILE]` - one redacted zip for a support ticket (doctor output, versions, `platform.json` minus secret-like keys, log and run tails, scheduled-task state, registry cache, remediation history; never `secrets/`, profiles, screenshots).

## Pinning the registry key on an existing install (no reinstall)
Machines installed before the signing key was published hold no key and accept an unsigned or older registry. Pin it in place:

    python -m wrapper_core trust --pin <public key hex>

(`<install>\python\python.exe` with `AUTOMAGIX_HOME` and `PYTHONPATH=<install>\wrapper-core` set, or the installed `bin` launcher.) The command first downloads the live registry's `manifest.yaml` and `manifest.yaml.sig` and checks the signature against the key you give it; only a key that verifies is written to `platform.json` (`registry_pubkeys`), so a mistyped or wrong key can never lock the machine out. The public key is the value of the repository variable `AUTOSTATION_REGISTRY_PUBKEY` (Settings > Secrets and variables > Actions > Variables). Check with `python -m wrapper_core trust --show`.
