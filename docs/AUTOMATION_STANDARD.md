# AUTOSTATION Automation Standard

The checklist every automation must satisfy before it can be promoted from the **dev** ring to **pilot**, and from pilot
to **broad**. Rule ids (`AS-nn`) are referenced by `python -m wrapper_core assist review`, which runs the
deterministic checks locally and in CI (HIGH findings fail the build).

> **Unattended by default.** The most common automation failures come from user interaction, user dependency and user
> error in setup. When a process "seems" to need a person, ask what that person provides or decides that the system
> cannot — the answer is almost always a file on a shared path, a rule that can be encoded, or a trigger that can be a
> schedule or an event. Attended mode is a last resort, not a default.

## A. Technical rules (enforced by `assist review`)

| Id | Severity | Rule |
|---|---|---|
| AS-00 | HIGH | The entry point parses (valid syntax). |
| AS-01 | HIGH | **No hard-coded credentials.** Automations run as the user (Windows session, cert store, browser profile). Supplemental secrets live in Windows Credential Manager, never in the repo or payload. |
| AS-02 | HIGH | Use `wrapper_core` (Python) or `execution-framework`'s `fw.wrapper` (Node). No private logging, retry or LLM plumbing. |
| AS-03 | MEDIUM | `main` is wrapped in `@wrapper.entrypoint` / `fw.wrapper.entrypoint` so failures are classified and the retry/alert pipeline sees a typed error. |
| AS-04 | MEDIUM | Telemetry: `run_item`/`log_item` **per item** (so item failures are not hidden by a green run) and `log_event` at key steps. |
| AS-05 | HIGH | **No interactive prompts** (`input()`, dialogs, "press any key"). Parameters come from the schedule slot (`wrapper.param`), files, or rules. |
| AS-06 | MEDIUM | No `shell=True` / `os.system` with data that came from a target system. Pass an argv list. |
| AS-07 | HIGH | TLS verification is never disabled. |
| AS-08 | LOW | No long fixed sleeps; poll with a timeout. |
| AS-09 | HIGH | **Prompt-injection safeguard:** `llm_call` prompts are static instructions; scraped/log/document data goes only in `context=`. Never f-string data into `prompt`. |
| AS-10 | LOW | Every `llm_call` has `caller="<automation>/<purpose>"` (audit trail). |
| AS-11 | MEDIUM | No `except: pass` — swallowed exceptions defeat error classification. |

## B. Platform rules (checked in review / by `registry validate`)

1. **Manifest entry complete:** description, owner, owner_team, systems, `data_classification`, schedule with a
   realistic `expected_duration_ms.baseline`, `error_handling`, and the `catalog` block.
2. **Data classification is honest.** If it exceeds the LLM endpoint's approved level (`llm_allowed_classification`),
   the automation must not depend on the LLM for correctness (the platform withholds all content from the LLM anyway).
   Mark PII/sensitive fields `sensitive=True` / `sensitive_keys=[...]` when logging; they are redacted before any LLM sees them.
3. **LLM use is fail-safe.** `llm_call` returns `None` when disabled/failed/invalid. Every call site handles `None`
   with a deterministic fallback or an explicit escalation. Use the right pattern: 1 (decisions) and 2 (summaries) first;
   3 (selector healing) only for browser flows; 4 (agentic) only where scripting genuinely cannot work.
4. **Idempotent / re-runnable.** A re-run after a partial failure must not duplicate side effects (dedupe by item id).
5. **Irreversible actions are guarded.** Anything that submits, sends, pays or deletes is behind an explicit parameter
   and a dry-run path (`wrapper.is_dry_run()` must skip side effects).
6. **Headless first.** If the target system has an API or supports certificate auth, use `fw.http` / direct calls instead of a browser.
7. **Browser flows** use the user's Edge profile via `fw.browser.launch` (no downloaded browsers), `fw.resilience` for
   selector self-healing, and stable selectors (role/label/test-id) over positional CSS.
8. **Declares what it needs:** `requires` in `automation.yaml` pins compatible wrapper-core / execution-framework ranges.
9. **Tests:** unit tests for logic + the lifecycle test (`python -m wrapper_core dev .`) pass in CI before dev→pilot promotion.
10. **Exits cleanly** within `timeout_s`, leaves no child processes or temp files behind (the run dir is pruned after 14 days).

## C. Governance & ownership

* Every automation has an **owner** (a distribution list, not a person) listed in the manifest; alerts route to them.
* Pilot promotion requires the dev-ring MR approval plus a passing `assist review`; broad requires N pilot runs without
  `FAILED`/`HUNG` signals (default 5 consecutive scheduled runs) and an approving MR.
* The **kill-switch** (`status: paused` or `fleet.paused`) is an MR like any other; approval is the audit trail.
* Decommissioning: `registry decommission` after `pause`; the installer's uninstall path pushes the final `decommissioned` event.
* Support model: alert → owner triage SLA (set per data classification) → fix via CI/CD → promote. High escalation frequency on one automation means redesign it, not more exception machinery.
