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

## Security model (host trust)

Everything below is enforced in code (`lib/login.js` pure functions, covered by `test/trust.test.js`), not in prompts:

* **Portal allow-list.** `portal_url` must be `https` and its host must be in the allow-list - by default exactly `develop.deloitte.com`
  (port is part of the identity; look-alikes, `user@host` tricks, other schemes, IP addresses, backslashes, trailing dots and IDN
  homographs are refused). Extra hosts can only be added through the `allowed_hosts` param (comma/space separated exact hosts); a
  refused URL fails the run with `portal_url refused: ...` before any browser is started.
* **Redirect check before the click.** If the Microsoft sign-in URL carries `redirect_uri` / `wreply` (or a `client_id`), the redirect
  host must be the portal or allow-listed. Otherwise nothing is clicked and the run ends with state `untrusted_redirect` +
  `manual_step_required` (`untrusted_redirect` event with the offending host, never the query string). If your portal signs in through a
  separate auth host, add it to `allowed_hosts`.
* **Post-click check.** After the tile click the browser may only be on Microsoft sign-in or an allow-listed host; anything else is
  reported as `untrusted_redirect` (the run still stops - it never does anything after the click).
* **Healing only on Microsoft sign-in hosts** (`canHeal`), so LLM selector healing can never act on the portal or any other site.
* **Test overrides are env-gated.** `picker_host`, `headless`, `http://` and loopback/IP hosts are honoured only when the *environment*
  variable `AMX_ALLOW_TEST_HOSTS=1` is set (CI and the local demo set it; installed machines do not). Without it they are ignored (a
  `test_override_ignored` event is logged) - in production the browser is always headed.

CI smoke configuration (installer-dev.yml): `AMX_ALLOW_TEST_HOSTS=1`, `AUTOMATION_PARAM_PORTAL_URL=http://127.0.0.1:8099/portal.html`,
`AUTOMATION_PARAM_ALLOWED_HOSTS=127.0.0.1:8099`, `AUTOMATION_PARAM_PICKER_HOST=localhost:8099`, `AUTOMATION_PARAM_HEADLESS=true`.

Parameters (schedule slot `params`): `portal_url`, `allowed_hosts` (optional), `account` (default `devpatel@deloitte.com`; validated as an
e-mail address before it is used in selectors), `channel` (default `msedge`), `timeout_s` (default 60, clamped 5-600). Test-only (need
`AMX_ALLOW_TEST_HOSTS=1`): `picker_host`, `headless`.

Native UI-Automation mode (used only when managed Edge forbids DevTools) validates `portal_url` the same way but cannot see the
sign-in URL, so the redirect and post-click checks apply to the Playwright path only.

## Try it

```bash
node --test                                         # pure-logic tests (page classification, selector safety)
node demo/server.js &                               # local demo: portal on 127.0.0.1:8099, fake picker on localhost:8099
AMX_ALLOW_TEST_HOSTS=1 python -m wrapper_core dev . --param portal_url=http://127.0.0.1:8099/portal.html --param allowed_hosts=127.0.0.1:8099 --param picker_host=localhost:8099 --param headless=false
AMX_ALLOW_TEST_HOSTS=1 python -m wrapper_core dev . --param portal_url="http://127.0.0.1:8099/portal.html?v2" --param allowed_hosts=127.0.0.1:8099 --param picker_host=localhost:8099   # redesigned picker (no data-test-id)
# the real thing (needs Edge + you signed in to Windows/Microsoft):
python -m wrapper_core dev .
```

Needs Node 18+, `npm ci --omit=dev` in `components/execution-framework`, and Microsoft Edge (no browser downloads).
The real site could not be exercised from the build environment: the Microsoft tile selectors follow the picker's
documented markup (`data-test-id="<email>"`, `#tilesHolder`) with text-based fallbacks, and Pattern 3 healing covers markup drift.
