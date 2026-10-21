# tools/uipath - the UiPath migration kit

Stdlib-only Python (3.9+). Full guide: [`docs/UIPATH_MIGRATION.md`](../../docs/UIPATH_MIGRATION.md). Tracking files: [`migration/`](../../migration/README.md).

| Script | What it does |
|---|---|
| `inventory.py` | static scan of UiPath projects (folder / `project.json` dir / `.nupkg`) + optional Orchestrator export -> `inventory.json/.csv/.html`: activity categories, target pattern, risks, AutoStation equivalents, effort estimate. Never executes anything; secrets are reported by location only |
| `scaffold.py` | per-project AutoStation payload (transactional / python / node), TODO stubs per source workflow, `manifest-entry.yaml`, `MIGRATION.md` |
| `parity.py` | parallel-run comparison of a UiPath output vs an AutoStation output (`.xlsx/.csv/.json`) with a go/no-go verdict (exit 0 = GO) |
| `dashboard.py` | migration dashboard: funnel, waves, burn-down, parity verdicts, licence-savings **estimate** |
| `catalog.py` | the knowledge base to edit: activity -> category, complexity weights, AutoStation equivalents |
| `xlsxmini.py`, `common.py`, `htmlutil.py` | tiny xlsx/csv/json reader, secret redaction + cron conversion, single-file HTML helpers |
| `sample_projects/` | **synthetic** projects + Orchestrator export used by the tests and the demo |

```bash
python tools/uipath/inventory.py tools/uipath/sample_projects --out /tmp/inv --orchestrator-export tools/uipath/sample_projects/orchestrator_export
python tools/uipath/scaffold.py /tmp/inv/inventory.json --out /tmp/scaffolds
python tools/uipath/dashboard.py --inventory /tmp/inv/inventory.json --status migration/status.sample.yaml --program migration/program.sample.yaml --out /tmp/dash
python -m pytest tools -q
```
