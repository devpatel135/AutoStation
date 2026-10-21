# Published automations

Only what is in this folder is published: packaged into the registry, given an install page, built into an installer and
shown on the dashboards.

| Automation | Runtime | What it does |
|---|---|---|
| [`automation-certification-site`](automation-certification-site/README.md) | Node + Playwright (Edge) | Opens the Deloitte *AI Season of Certifications* resources page; when Microsoft's "Pick an account" appears it clicks the `devpatel@deloitte.com` tile, then stops. Never types a password. |

Reference/test automations (hello-world, invoice-router, notifier) live in [`../examples`](../examples) and are **not** published.
New automation: `python tools/amx.py new automation-<name> --description "..." --owner team@example.com`, then move it here.
