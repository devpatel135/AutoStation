# wrapper-core API reference

`import wrapper_core as wrapper` — stdlib only, Python 3.9+. Node equivalents live in `execution-framework` (`fw.wrapper.*`,
same `events.jsonl` format). Everything here is also introspected live by `wrapper_core.context` for the dev-assist LLM
prompts, so this page cannot drift from what models see.

## Telemetry

| Call | Purpose |
|---|---|
| `log_event(event, data=None, *, sensitive=False, sensitive_keys=None, **fields)` | Structured event in the run log. `sensitive_keys=["ssn"]` keeps the value in the raw log but replaces it with `[REDACTED]` before **any** LLM sees it. |
| `log_item(item_id, outcome, *, duration_ms=None, **fields)` | One item's outcome: `success` \| `skipped` \| `failed`. Feeds throughput, P50/P95/P99, skip-rate and item-failure-rate analytics. |
| `track_item(item_id, **fields)` | Context manager: times the block, logs success, or failed (+ re-raises). Set `it["outcome"]="skipped"` inside to override. |
| `set_result(**values)` | Small structured result returned to the runner (appears as `result` in the run log). |
| `emit_trigger(target, params, reason)` | Ask the platform to start another automation (event-driven orchestration; CI turns it into a `triggers.json` entry). |

## Resilience

| Call | Purpose |
|---|---|
| `run_item(item_id, fn, *args, policy=None, **kw)` | Run `fn` for one item with retry + telemetry. DATA_ERROR → item skipped (run continues, reason logged). Unresolvable → logs `failed`, raises `Escalation`. |
| `run_with_retry(fn, policy=None, *, item_id=None, auth_refresh=None, use_llm=True)` | The retry engine without item logging. Returns the value or a `Skipped`; raises `Escalation`. |
| `@entrypoint` | Wrap `main`: uncaught errors are classified, `error.json` written (category, type, message, traceback tail, `escalated`), non-zero exit. Always use it. |
| `RetryPolicy` | `max_retries, network_retry_wait_s, backoff_multiplier, vpn_tool, skip_on_data_error, escalate_after_retries`. Default comes from the manifest's `error_handling` via `AUTOMAGIX_ERROR_POLICY`. |
| Exceptions | `NetworkError`, `AuthExpired`, `SystemUnavailable`, `DataError`, `LogicError`, `Escalation`, base `AutomationError`. Raise them to steer the classifier; ordinary exceptions are classified automatically (`ConnectionError` → NETWORK, `KeyError` → LOGIC, `ValueError` → DATA, HTTP 401/403/5xx …). |

Retry behaviour (every attempt logs exactly one `retry_event` with the outcome):

| Category | Action |
|---|---|
| NETWORK_ERROR | run `vpn_tool` → wait `network_retry_wait_s` → retry up to `max_retries` → escalate |
| AUTH_EXPIRED | `auth_refresh()` once → retry once → escalate |
| SYSTEM_UNAVAILABLE | exponential backoff, ≤3 → escalate (+ SLOW signal) |
| DATA_ERROR | skip item (if `skip_on_data_error`), continue |
| LOGIC_ERROR | never retry; escalate with traceback |
| UNKNOWN | LLM classifies `{transient\|permanent\|unknown}`; no LLM/invalid ⇒ **ESCALATE** (fail safe) |

## LLM seam

```python
out = wrapper.llm_call(
    "Classify the document and extract key fields.",    # trusted instructions, written by you
    context=document_text,                              # UNTRUSTED data: scraped text, logs, documents
    schema={"type": "invoice|tax_form|other", "vendor": "string?", "amount": "number?"},
    caller="my-automation/extract",                     # audit trail
    template="extract@1",                               # optional: logged with an input hash (never the prompt)
    classification="confidential",                      # optional: refused if above the automation's approved LLM level
    decision_log=True,                                  # emit an `llm_decision` audit event
)
if out is None:   # disabled / failed / invalid after one corrective retry  ->  you MUST have a fallback
    ...
```

* `schema` may be a **shape dict** (above; `"x?"` nullable, `"a|b"` enum, `["string"]` array), a **JSON-Schema subset**, or a **pydantic model** if installed.
* Returns `str` (no schema) / validated `dict`|`list` (schema) / `None`. Never raises for LLM problems.
* Payloads never hold the key: in a run the call is routed through the runner's loopback broker.
* Safeguards you do not have to write: anti-injection system prompt, `<untrusted_data>` delimiting (delimiter text inside context is neutralised), schema validation + one retry, classification gate, call logging.
* `llm_call_ex(...)` returns `LLMResult(status, value)` when you need to distinguish `disabled | error | schema_failure`.
* `log_llm_decision(caller, template, input_hash, decision, reasoning)` for custom audit events.

## Parameters, assets, environment

| Call | Purpose |
|---|---|
| `params()` / `param(name, default)` | The slot's (or trigger's) `params` — `AUTOMATION_PARAM_*`. |
| `is_dry_run()` | `--dry-run`: skip every side effect. |
| `assets()` | Dynamic non-secret config for this ring (manifest `assets`), refreshed every run. |
| `run_dir()` | Scratch directory for this run. |

Secrets: never in the manifest or payload. Use the user profile / Windows Credential Manager (`wrapper_core.secrets.credman_read("AUTOMAGIX/<name>")`).

## Operational commands

```bash
python -m wrapper_core run <automation> [--slot S] [--dry-run]    # what Task Scheduler launches
python -m wrapper_core install|uninstall <automation>            # what the installer calls
python -m wrapper_core maintain                                   # login: re-assert tasks + heartbeat
python -m wrapper_core status                                     # installed versions, last runs, spool depth
python -m wrapper_core dev <payload_dir> [--param k=v]            # offline lifecycle
python -m wrapper_core assist scaffold|review|test|design …       # LLM-assisted development
python -m wrapper_core secrets protect|credman-set …              # DPAPI / Credential Manager helpers
```

## Run lifecycle

```
lock → flush log spool → self-heal (manifest, kill-switch, install/update, assets, triggers)
     → maintenance window → compatibility check → [broker up] execute payload (watchdog, process-level retry)
     → status (success|partial|failed|hung) → synopsis (LLM, or deterministic) → run log + heartbeat → notify
```
