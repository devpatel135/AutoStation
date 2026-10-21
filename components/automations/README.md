# Example automations

| Automation | Runtime | Shows | Try |
|---|---|---|---|
| `automation-hello-world` | Python | The platform's **test automation**: every feature, switchable failure modes via `simulate=none\|data\|network\|logic\|flaky_llm` | `python -m wrapper_core dev automation-hello-world --param simulate=network` |
| `automation-invoice-router` | Python (headless) | Document understanding with LLM **Pattern 1** (grounded extraction + regex fallback), **Pattern 2** anomaly summary, deterministic routing, duplicate detection, sensitive-field redaction, dry-run | `python -m wrapper_core dev automation-invoice-router --param inbox_dir=automation-invoice-router/sample_data/inbox` |
| `automation-portal-report` | Node (Playwright) | Browser flow in the user's Edge profile, SSO lapse detection, **Pattern 3** selector self-healing, table adapter, narrative | see its README |
| `automation-notifier` | Python (headless) | The platform **dog-fooding its own alerting**: reads `signals.json`, notifies owners via Teams/email, dedupes, re-reminds, marks resolved | `python -m wrapper_core dev automation-notifier --param signals_url=path/to/signals.json` |

Each is a complete payload (`automation.yaml` + code + tests) and a template for your own: copy one, or use
`python tools/new_automation.py`. Run all example tests: `python tools/test_all.py --only automation-hello-world automation-invoice-router automation-notifier`.
