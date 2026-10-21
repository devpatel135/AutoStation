# Triggers: on-demand runs and event-driven chaining

There is no orchestrator server. Triggers are a JSON file that machines poll, published next to `manifest.yaml`.

```
operator / admin panel --ops-action--> protected `triggers` branch --Pages--> triggers.json
automation A finishes --(analytics CI matches manifest `triggers:` rules)--> triggers.json
machine (poll slot, every N minutes): self-heal fetches triggers.json, runs once per unseen id addressed to it
```

## A poll slot is required

An automation only consumes triggers if its schedule has a `poll` slot:

```yaml
slots:
  - {id: poll, type: poll, every_minutes: 10, params: {}}
```

The poll task starts `wrapper_core run <name> --slot poll`. With no pending trigger it exits immediately and logs nothing, so
polling is cheap and quiet. `registry validate` rejects `triggers:` rules on an automation with no poll slot (they could never
fire) and `trigger-add` refuses such a target.

## On-demand run (the "attended / last resort" path)

Prefer redesigning the process so nobody has to press a button. When someone genuinely must start a run:

```
python -m registry trigger-add automation-x --slot poll --param date_range=today
```

or use **Admin panel > Run now / retry**. The user provides the *trigger*, not interaction during execution.
Parameters become `AUTOMATION_PARAM_*` environment variables for that run, overriding the slot's defaults.

Governance: `trigger-add` does **not** change the manifest, so it needs no merge request. The `ops-action` job commits it to the
protected `triggers` branch (only the ops bot may push there) and re-runs the Pages job. It is refused for paused automations.
Entries older than 30 days are pruned automatically; each machine remembers the ids it consumed.

## Event-driven chaining

In the *downstream* automation's manifest entry:

```yaml
triggers:
  - {on_automation: automation-invoice-router, on_status: failed, slot: poll, params: {source: invoice-router}}
```

`on_status` is `success`, `failed`, `partial` or `any` (default). The analytics pipeline matches newly ingested run logs against
these rules and adds entries to `triggers.json` (`source: manifest-rule`); `build-portal --extra-triggers` merges them by id.
`automation-notifier` in the starter manifest uses exactly this to react to failures within one poll interval.

## Trigger entry format

```json
{"id": "t-20261012T130000Z-3fa9c1d2", "automation": "automation-x", "slot": "poll",
 "params": {"date_range": "today"}, "created_at": "2026-10-12T13:00:00Z", "source": "operator", "requested_by": "alice"}
```
