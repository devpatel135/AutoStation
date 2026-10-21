# Getting started: from "I have a process to automate" to "running in pilot"

Audience: an automation developer. You need Python 3.9+ (and Node 18+ for browser automations), Git, and nothing else on
your machine — no UiPath Studio, no Orchestrator, no admin rights.

## 0. One-time setup

```bash
git clone https://github.com/devpatel135/AutoStation.git && cd AutoStation
python -m pip install -e components/wrapper-core[dev]
python -m pytest components/wrapper-core -q          # everything green before you start
```

## 1. Decide the shape (5 minutes — this is the part that matters)

| Question | If yes | If no |
|---|---|---|
| Does the target system have an API, or support certificate auth? | **Headless** Python (`urllib`/`requests` via `fw.http` for Node). Fastest, most reliable. | Browser automation (below). |
| Is it browser-only? | Node + Playwright via `execution-framework` (`fw.browser`, `fw.resilience`). Start with `npx playwright codegen` (`bin/amx-codegen.js`) to record selectors. | — |
| Does a person need to click/decide mid-run? | **Redesign.** Ask what they provide: a file on a shared path, a rule, a trigger. Attended mode is last resort (`registry trigger-add`). | Good — unattended is the default. |
| Is there an ambiguous decision (classify, extract, summarise)? | `wrapper.llm_call` (Pattern 1/2) with a deterministic fallback. | Pure deterministic code. |

## 2. Scaffold

```bash
python tools/new_automation.py automation-expense-checker \
  --description "Check expense reports against policy" \
  --owner finops@example.com --owner-team "FinOps Automation" --classification internal
cd automation-expense-checker
```

You get a payload that already runs, logs, tests and is CI-wired. Or let the LLM draft the first version
(set `AUTOSTATION_LLM_ENDPOINT`; `mock://` works offline for a skeleton):

```bash
python -m wrapper_core assist design "Check expense reports against policy and flag outliers"   # requirements → design → scaffold → tests
python -m wrapper_core assist scaffold "Pull open AP invoices from System X, route to AP team"
```

## 3. Write the logic against the wrapper API

See [WRAPPER_CORE_API.md](WRAPPER_CORE_API.md). The four calls you use 95% of the time:

```python
import wrapper_core as wrapper

@wrapper.entrypoint                                   # classifies failures for retry/alerting
def main():
    for item in fetch():
        wrapper.run_item(item["id"], process, item)   # retry + per-item telemetry (success/skipped/failed)
    wrapper.log_event("done", {"n": 3})
    wrapper.set_result(processed=3)
```

Rules you will be reviewed against: [AUTOMATION_STANDARD.md](AUTOMATION_STANDARD.md). Check locally any time:
`python -m wrapper_core assist review main.py`.

## 4. Run it exactly like production — offline

```bash
python -m wrapper_core dev . --param date_range=yesterday     # real lifecycle: broker, retry, synopsis, run log
python -m pytest                                              # your tests + the lifecycle test
```

`wrapper_core.testing.run_payload(...)` is the same harness for your tests (simulate failures, assert on the run log).
Examples to copy from: `components/automations/` — `automation-hello-world` (every feature), `automation-invoice-router`
(headless + LLM patterns 1 and 2 with grounding), `automation-notifier` (alerting), `automation-certification-site`
(Playwright + resilience).

## 5. Register and release

1. Complete `manifest-entry.yaml` (owner, systems, classification, realistic `expected_duration_ms`, `error_handling`, `catalog`).
2. Merge to `main`: CI lints, tests, packages, builds a **dev-ring** installer and opens the registry MR (dev ring only).
3. Install the dev-ring installer on a test VDI; it assembles only what is missing, registers the schedule, runs once.
4. Promote: `python -m registry promote <name> --to pilot` → MR → approve. After the pilot soak: `--to broad`.
5. Something wrong? `python -m registry pause <name> --reason "..."` (MR) halts it fleet-wide on every machine's next run, or `rollback`.

## Cheat sheet

| I want to… | Do |
|---|---|
| Stop one automation everywhere | `registry pause <name> --reason …` (MR) |
| Stop everything | `registry fleet-pause --reason …` (MR) |
| Run it now, once | `registry trigger-add <name> --slot poll --param k=v` |
| See what ran | Dashboard (logging Pages) → automation card → synopsis + events |
| Different input per day | `params:` per schedule slot in the manifest → `wrapper.param("date_range")` |
| Use the LLM safely | `wrapper.llm_call(prompt, context=data, schema=…, caller=…)` — data only in `context`; handle `None` |
| Keep PII away from the LLM | `log_event(..., sensitive_keys=["ssn"])`; set `data_classification` honestly |
