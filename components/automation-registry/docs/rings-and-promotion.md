# Rings and promotion

Every machine belongs to one **ring** (`dev`, `pilot` or `broad`; chosen at install time, stored in `platform.json`).
`manifest.yaml` says which version each ring runs:

```yaml
rings: {dev: "1.2.0", pilot: "1.1.0", broad: "1.0.0"}
```

`wrapper-core` heals every machine toward `rings[<its ring>]` at the start of each scheduled run, so a manifest change reaches
the fleet on the next run - no installer, no per-machine action.

## The promotion path

```
add-version  ->  dev  --promote-->  pilot  --promote-->  broad
```

| Step | Command | Rule enforced by the tool and by CI |
|---|---|---|
| Register a build | `python -m registry add-version automation <name> <pkg.zip>` | New versions land in **dev only**. A registered version number is immutable: same number + different bytes is refused. |
| Promote to pilot | `python -m registry promote <name> --to pilot` | Only the version dev already runs (or an older registered one). |
| Promote to broad | `python -m registry promote <name> --to broad` | Only a version pilot already runs. You cannot skip a ring. |
| Roll back | `python -m registry rollback <name> --ring pilot --to 1.0.0 --reason "..."` | Caps the ring **and every later ring** at the target version; a reason is mandatory. |

`registry validate` fails any manifest where `broad` is newer than `pilot`, or `pilot` newer than `dev`, so a hand edit cannot
bypass the order either.

**Absent ring = not released there yet.** For an automation, a missing entry under `rings:` simply means nobody in that ring
has it (the installer is not offered; self-heal reports "no version assigned"). Components (`wrapper-core`,
`execution-framework`) must have all three rings because every machine needs them.

## Every change is a merge request

Promotions, rollbacks, pauses and decommissions are manifest edits, and the **merge request approval is the governance gate**
with a full audit trail (who asked, who approved, when). Each command prints a suggested MR title. The `ops-action`
pipeline (admin panel) does the branch + MR for you.

## Compatibility

`compatibility:` lists supported `wrapper_core` x `execution_framework` pairs; each automation version declares `requires`.
CI refuses a manifest where any ring's component versions are outside the matrix or do not satisfy an automation's
`requires`. Promote components first, automations after.

## Suggested soak times (policy, not enforced)

dev: until the integration test + one real run pass; pilot: at least 1 business cycle of the automation's schedule
(a weekly automation needs a week); broad: after the pilot owner signs off in the MR.
