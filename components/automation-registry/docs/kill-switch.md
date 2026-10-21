# Kill-switch (pause / resume)

Two levels, both plain manifest fields read by `wrapper-core` at the self-heal step of **every** run, before any payload code:

| Level | Field | Effect |
|---|---|---|
| One automation | `automations.<name>.status: paused` + `paused_reason` | That automation halts on every machine at its next scheduled run. |
| Whole fleet | `fleet.paused: true` + `paused_reason` | Every automation halts. |

A halted run runs **no payload**, writes a `halted_paused` run log (so the dashboard shows it), drops a status note
(`state\toast\<name>.status.txt`) and a toast on the user's machine ("intentionally paused ... will resume automatically").
Resuming is the reverse edit; the next scheduled run picks it up.

An automation that is **not registered** in the manifest also halts ("decommissioned"), so deleting an entry is a stop too.

## Commands

```
python -m registry pause automation-x --reason "upstream data format changed, INC-1234"
python -m registry resume automation-x --reason "fixed in 1.2.1"
python -m registry fleet-pause --reason "suspected bad release"
python -m registry fleet-resume --reason "all clear"
```

A reason is **required** - it is shown to users and kept in `history/CHANGELOG.md`.

## Speed vs. governance

The default path is a merge request, so an approver must act. For genuine emergencies the `ops-action` pipeline supports
`EMERGENCY=1` with `ACTION=pause` or `fleet-pause`: if the person who started the pipeline is listed in the protected CI
variable `EMERGENCY_PAUSE_USERS`, the bot also merges the MR immediately (subject to your approval rules - configure a
bypass for the bot in "Approval rules" if you want zero-approver emergency stops). Review emergency merges the next
business day; they carry the `emergency` label.

Rollback is usually the better first move when a *new version* is the problem (it restores known-good behaviour); pause
when the problem is outside the automation (target system, data).

## Decommissioning

`python -m registry decommission <name> --reason ...` requires the automation to be paused first (so machines have already
stopped it), removes the entry (and trigger rules that point at it) and writes `history/decommissioned/<name>.md`.
Users remove the local install with the NSIS uninstaller, which pushes a final `decommissioned` event and cleans up
scheduled tasks, files and keys (shared components are reference counted).
