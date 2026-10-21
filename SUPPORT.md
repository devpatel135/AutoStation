# Getting help

| I need to... | Go here |
|---|---|
| install an automation | the install center: https://devpatel135.github.io/AutoStation/install/ |
| check an installed machine | `python -m wrapper_core doctor` (PASS / WARN / FAIL with fixes) |
| report that an automation failed or did not run | New issue, "Automation failure / problem" (the run id is enough: logs and evidence are on the dashboard) |
| request a new automation | New issue, "New automation request" |
| report an outage or a bad release | New issue, "Incident" (operators: pause the automation first, see docs/RUNBOOKS.md section 1) |
| report a security problem | **privately**: https://github.com/devpatel135/AutoStation/security/advisories/new (never a public issue) |
| understand how it works | the docs: https://devpatel135.github.io/AutoStation/docs/ |

Automatic `ops-signal` issues are opened by the health pipeline when the fleet looks wrong (a machine went silent, a scheduled run was missed, a run hung,
a machine is on the wrong version, or the daily installer canary failed). They close themselves when the problem clears; see docs/PIPELINES.md.
