# Synthetic sample projects

**Everything in this folder is synthetic.** It was written by hand to look like real UiPath projects (project.json, XAML,
REFramework layout, Orchestrator export CSVs) so the migration kit can be demonstrated and tested without access to your
real automations. No real systems, hosts, credentials or data are referenced; every "secret" in here is a labelled fake and
exists *on purpose*, to prove the inventory finds and redacts hard-coded credentials.

| Project | Shape | What it exercises |
|---|---|---|
| `Invoice_Processing_REF` | REFramework performer: queue + Excel + HTTP + mail | REFramework detection, Config.xlsx, queue/asset/credential references, RetryScope, TryCatch, hard-coded secrets (attribute, variable default, config value), an orphan workflow |
| `Portal_Scraper` | Browser-only | Browser selectors, **image click** risk (needs redesign), **attended InputDialog** risk, no exception handling |
| `SAP_Desktop_Posting` | Small desktop/SAP flow | SAP + UIA selectors, credential/asset reads, attended MessageBox, desktop target pattern |
| `orchestrator_export/` | processes / schedules / queues / assets / robots CSVs | Schedule -> slot conversion (daily, weekdays, sub-hourly, monthly), unmatched process/schedule, asset **Value** column that must never be read |

Try it:

```bash
python tools/uipath/inventory.py tools/uipath/sample_projects --out /tmp/inv --orchestrator-export tools/uipath/sample_projects/orchestrator_export
python tools/uipath/scaffold.py /tmp/inv/inventory.json --out /tmp/scaffolds
python tools/uipath/dashboard.py --inventory /tmp/inv/inventory.json --status migration/status.sample.yaml --out /tmp/dash
```

`python tools/uipath/sample_projects/build_samples.py` regenerates the only binary fixture (`Data/Config.xlsx`).
