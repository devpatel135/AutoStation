## What and why

<!-- One or two sentences. Link the intake / migration issue if there is one. -->

## Type of change

- [ ] Automation code (a payload under `components/automations/`)
- [ ] Platform code (wrapper-core, execution-framework, builder, logging, registry, tools)
- [ ] Registry change (`manifest.yaml`: promotion, pause/resume, rollback, decommission, catalog edit)
- [ ] Workflow / pipeline / docs / site only

## Checklist for every change

- [ ] `python tools/test_all.py` is green locally (or CI is)
- [ ] No secrets, tokens, keys or personal data in the diff (the packager and CI refuse key-looking files, but look anyway)
- [ ] Released versions are never edited: a changed payload bumps its `version` (`VERSION` / `automation.yaml`)

## Promotion checklist (registry PRs that move a version up a ring)

Standard: [docs/AUTOMATION_STANDARD.md](../docs/AUTOMATION_STANDARD.md) · Governance: [docs/GOVERNANCE.md](../docs/GOVERNANCE.md)

**dev -> pilot**
- [ ] `python -m wrapper_core assist review main.py` has no HIGH findings (CI shows the result)
- [ ] Manifest entry complete: owner (a list), owner_team, systems, `data_classification`, `llm_allowed_classification`, schedule with a realistic `expected_duration_ms`, `error_handling`, `catalog` block
- [ ] Security reviewer signed off the data classification and any LLM use (comment below)
- [ ] Lifecycle test passes offline (`python -m wrapper_core dev .`) and the automation is unattended (no prompts, no manual step)
- [ ] Installed on a test VDI from the dev-ring installer; first run logged and the dashboard row looks right

**pilot -> broad**
- [ ] At least 5 consecutive scheduled pilot runs with no FAILED / HUNG / MISSED signals (link the dashboard)
- [ ] No open critical alerts for this automation; owner approves
- [ ] Rollback target noted (previous version: `____`)

**pause / rollback / decommission**
- [ ] Reason is accurate and visible to users; incident or ticket linked
- [ ] For decommission: paused first, no runs since, uninstall plan communicated

## Notes for reviewers
