# Reference automations (not published)

Used as test fixtures and as copy-from examples for developers; they are **not** packaged into the registry, have no
installer and do not appear on the site.

| Automation | Shows |
|---|---|
| `automation-hello-world` | every platform feature with switchable failure modes (`simulate=none\|data\|network\|logic\|flaky_llm`) |
| `automation-invoice-router` | headless document routing with LLM patterns 1 and 2 (grounded extraction), dedupe, redaction |
| `automation-notifier` | alerting dog-food: reads `signals.json`, notifies owners |

Run any of them offline through the real lifecycle: `python -m wrapper_core dev components/examples/automation-hello-world --param simulate=network`.
Tests: `python tools/test_all.py --only automation-hello-world automation-invoice-router automation-notifier`.
