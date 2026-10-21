# Rollback runbook

**When:** a promoted version misbehaves (failures, wrong output, SLOW/HUNG signals right after a promotion).

1. **Stop the bleeding if data is at risk**: pause the automation (`pause --reason`, see `kill-switch.md`).
2. **Pick the last good version** - the detail page (`automation/<name>.html`) and `history/CHANGELOG.md` list versions and when each ring moved.
3. **Roll back**:

   ```
   python -m registry rollback automation-x --ring pilot --to 1.0.0 --reason "1.1.0 drops invoices with no vendor code"
   ```

   This caps `pilot` **and `broad`** at `1.0.0` (rings that are already at or below it are untouched) so a bad version can
   never remain in a later ring than the one you rolled back. `dev` keeps the new build for debugging unless you
   roll back `--ring dev`.
4. Open the MR, get approval, merge. Machines install the older package (sha256-verified) on their next scheduled run -
   typically within one schedule interval. Nothing runs on the machine in between that is not the old, known version.
5. **Resume** if you paused.
6. Fix forward: register the fix as a **new** version number (released numbers are immutable), then promote normally.

Notes

* Rollback is a manifest edit; the old zip must still be listed under `versions:`. Never delete old versions from the manifest
  while any ring references them (`validate` refuses a ring pointing at a missing version).
* A rollback of `wrapper-core` or `execution-framework` must keep the pair inside `compatibility:` and satisfy every
  automation's `requires` - `registry compat` shows the result before you open the MR.
* wrapper-core updates itself through a staged swap applied by the launcher before the next run, so a rollback of
  wrapper-core takes effect one run later than an automation rollback.
