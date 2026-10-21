# automation-certification-site

Browser automation example (Node + Playwright on **execution-framework**).

**What it does:** opens `https://develop.deloitte.com/ai-season-of-certifications#subpage/res`; when the Microsoft
authentication "Pick an account" page appears it clicks the tile for `devpatel@deloitte.com`, **then stops**.

**What it deliberately does not do:** it never types a password, a code or a token, and it does nothing after the click.
Authentication is the signed-in user's — the automation runs in the user's Edge profile (Windows SSO / cert). If Microsoft
asks for a password or MFA instead of showing the picker, the automation stops and reports `manual_step_required`.

| Platform feature | Where |
|---|---|
| User-profile credentials (Edge persistent profile) | `fw.browser.launch` — no credentials in code |
| Account tile selection with fallbacks (`data-test-id` → role/text) | `lib/login.js` `accountTileSelectors` |
| Selector self-healing if Microsoft changes the markup (LLM Pattern 3, validated + cached) | `fw.resilience.resilientPage` |
| Page-state classification (target / account-picker / credential / other), look-alike-host safe | `lib/login.js` `classifyPage` |
| Retry of navigation; typed errors for the platform | `w.runWithRetry`, `entrypoint` |
| Failure evidence (screenshot + redacted DOM) | execution-framework ≥ 0.2 |
| Dry-run: navigates and classifies, never clicks | `--dry-run` |

Parameters (schedule slot `params`): `portal_url`, `account` (default `devpatel@deloitte.com`; validated as an e-mail address
before it is used in selectors), `headless` (default `true`; use `false` to watch), `channel` (default `msedge`),
`timeout_s` (default 60), `picker_host` (local demo only).

## Try it

```bash
node --test                                         # pure-logic tests (page classification, selector safety)
node demo/server.js &                               # local demo: portal on 127.0.0.1:8099, fake picker on localhost:8099
python -m wrapper_core dev . --param portal_url=http://127.0.0.1:8099/portal.html --param picker_host=localhost:8099 --param headless=false
python -m wrapper_core dev . --param portal_url="http://127.0.0.1:8099/portal.html?v2" --param picker_host=localhost:8099   # redesigned picker (no data-test-id)
# the real thing (needs Edge + you signed in to Windows/Microsoft):
python -m wrapper_core dev . --param headless=false
```

Needs Node 18+, `npm ci --omit=dev` in `components/execution-framework`, and Microsoft Edge (no browser downloads).
The real site could not be exercised from the build environment: the Microsoft tile selectors follow the picker's
documented markup (`data-test-id="<email>"`, `#tilesHolder`) with text-based fallbacks, and Pattern 3 healing covers markup drift.
