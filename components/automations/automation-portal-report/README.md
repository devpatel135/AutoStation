# automation-portal-report

Browser automation example (Node + Playwright on **execution-framework**): scrapes a quarterly compliance table from an
internal portal and exports the report.

| Platform feature | Where |
|---|---|
| User-profile credentials (Edge persistent profile, SSO/cert) | `fw.browser.launch` — no credentials in code |
| Session lapse → `AuthExpired` → platform refresh-once-then-escalate | `sso-detect` adapter |
| Table scrape + pagination | `generic-table` adapter |
| Per-row telemetry; bad rows skipped, run continues | `w.runItem` + `RowError` (`category: DATA_ERROR`) |
| LLM Pattern 3 — selector self-healing (validated, cached, deny-listed) | `fw.resilience.resilientPage` around the export click |
| LLM Pattern 2 — narrative with deterministic fallback | `w.llmCall` + `narrativeFallback` |
| Dry-run | `--dry-run` scrapes/validates, never clicks export |

## Try it

```bash
node demo/server.js &                               # demo portal on :8099 (v1.html = original, v2.html = button renamed)
node --test test/                                   # pure-logic tests
python -m wrapper_core dev . --param portal_url=http://127.0.0.1:8099/v1.html
python -m wrapper_core dev . --param portal_url=http://127.0.0.1:8099/v2.html   # watch `selector_healed` in the events
```

Needs Node 18+, `npm ci --omit=dev` in `components/execution-framework`, and Microsoft Edge on the machine (no browser downloads).
`wrapper_core dev` uses the offline mock LLM, which proposes the renamed button by label similarity so the healing path is demonstrable.
Row `C-104` in `v1.html` has an invalid status on purpose: it is skipped and reported, the other rows continue.

Parameters: `portal_url`, `headless` (default `true`), `channel` (default `msedge`), `export` (default `true`).
