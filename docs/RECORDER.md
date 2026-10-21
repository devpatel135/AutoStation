# Recorder: from a recorded browser session to a released automation

The UiPath Recorder, in a code-first, reviewable form. `amx-record` drives `playwright codegen` against **your Edge profile** (so Windows SSO / certificate
sign-in works while you record), then converts the recording into an AUTOSTATION Node payload that already has self-healing selectors, params, session-lapse
detection, dry-run guards and a lifecycle test. You then review it like any other code. It never records credentials.

## 1. Record

```bash
cd components/execution-framework && npm ci --omit=dev          # once (internal npm mirror)
node bin/amx-record.js https://portal.internal.example.com --name automation-approve-invoices --out ../automations/automation-approve-invoices
```

A browser opens with the Playwright inspector. Click through the process once, **then close the browser window** — that ends the recording and runs the conversion.
Already have a recording (`playwright codegen --target=javascript --output rec.js`, or a `@playwright/test` spec)? Convert it without a browser:

```bash
node bin/amx-record.js --from-file rec.js --name automation-approve-invoices
```

Options: `--profile edge|chrome|<profile dir>`, `--out <dir>` (refuses a non-empty directory unless `--force`), `--url <start url>` (with `--from-file`).

## 2. What you get

| File | Purpose |
|---|---|
| `main.js` | the payload: `entrypoint` → `browser.launch` → `session.guard(...)` → recorded steps → `setResult` |
| `automation.yaml` | descriptor (`runtime: node`, `requires` ranges) |
| `README.md` | **review checklist**, the recorded steps as a table (action · target · param · notes), param list, conversion warnings |
| `params.suggested.yaml` | recorded values you can promote to schedule-slot params in the registry manifest |
| `tests/test_lifecycle.py` | runs the payload through the real wrapper-core lifecycle in `--dry-run` (skipped without Node) |

How the conversion treats each recorded statement:

| Recorded | Becomes |
|---|---|
| `page.goto(url)` | `page.goto(P.start_url)` (first navigation is a param) + `sso.assertSession(page, { expectedOrigin })` |
| `page.waitForURL(...)` | kept + `sso.assertSession` |
| `getByRole/getByLabel/getByText/getByPlaceholder/locator/frameLocator/contentFrame/filter/first/nth(...)` + `.click/.fill/.press/.check/.uncheck/.selectOption/.hover/.dblclick()` | `await rp.click((p) => p.<exact chain>)` — a **resilient** call: if the selector breaks, LLM Pattern 3 proposes a replacement, which is verified in the live DOM, retried, cached and audited |
| `fill('literal')` / `selectOption('literal')` | `P.<name>` where `<name>` comes from the label/placeholder/role name; the literal is the param's default and is listed in `params.suggested.yaml` |
| a fill that looks like a **password / token / API key / SSN / card number / OTP**, or a typed JWT | **not recorded.** `throw new Error('credential required: use the user profile / Credential Manager');` — the payload fails loudly at that step until you wire in the user's session or Windows Credential Manager |
| clicks whose target says submit / save / send / delete / confirm / pay / approve / publish / checkout … | wrapped in `if (!w.isDryRun())` |
| the same block repeated with different values (one block per table row) | `for (const row of ROWS_n) await w.runItem(String(row.v0), async () => {...})` — fill in or load `ROWS_n` yourself |
| popups (`waitForEvent('popup')`) | the new page gets its own resilient page (`rp_page1`) |
| `page.waitForTimeout(ms)` | `wait-helpers.waitForNetworkIdle` + a warning (fixed sleeps are fragile) |
| `expect(...)` assertions, dialog handlers, anything it does not understand | a comment (`// recorded assertion…` / `// TODO(unconverted): …`) and a warning — never silently dropped, never executed |
| `setInputFiles`, `focus`, `type`, `clear`, `dragTo` … | plain Playwright call via `L(...)` (not self-healing) + warning; use `adapters.get('file-upload')` for uploads |

## 3. Review (this is the part that matters)

1. Open `README.md`: read every row of the steps table and every warning. A recording is a *draft*.
2. Credentials: any step marked **throws** needs the signed-in user's session (the Edge profile already carries it) or `credman_read`-style Windows Credential Manager access — **never** a literal in the repo.
3. Add what recording cannot express — see the limits below. Wrap the unit of work in `w.runItem` / `w.logItem` so item-level telemetry and retry apply.
4. Promote the values you want to vary into the manifest's `schedule.slots[].params` (names from `params.suggested.yaml`; read them with `w.param(...)`).

## 4. Run it exactly like production

```bash
python -m wrapper_core dev components/automations/automation-approve-invoices            # real lifecycle, offline, mock LLM
python -m wrapper_core run automation-approve-invoices --dry-run                         # (installed) no side effects
python -m pytest components/automations/automation-approve-invoices/tests                # lifecycle test
```

Debugging: `AMX_HEADED=1 AMX_SLOWMO=300` to watch it; `AMX_PAUSE_ON_ERROR=1` to keep the browser open at the failure; `AMX_DEBUG_SELECTORS=1` to log every locator's match count;
`AMX_TRACE=1` for a Playwright trace. These interactive options only work in an interactive invocation — **scheduled runs ignore them** (an unattended machine never opens a window or waits).

When a run fails, its **evidence** (screenshot, redacted DOM snapshot, context, optional trace) is shipped with the run log by wrapper-core and shows what the page looked like. (`assist review` is a Python static checker; for Node payloads review against `docs/AUTOMATION_STANDARD.md` by hand.)

## 5. Release

Same as any automation (`docs/GETTING_STARTED.md`): complete the manifest entry, merge to `main`, CI builds the dev-ring package + installer, promote dev → pilot → broad through approved MRs/PRs.

## Honest limits

* **Codegen records one linear path.** It cannot capture conditions ("if the banner shows, close it"), loops over unknown-length data, error handling, or waiting for business state. Those are code you add. The row-loop detector only helps when you *recorded* the repetition (2+ times).
* **Selectors are only as good as the recording.** Prefer role/label selectors (codegen's default); positional CSS (`nth`, long chains) will be flagged by review and heal less reliably.
* **Dynamic content** (counts, dates, generated ids) recorded as literals becomes a param default — make sure the default is not stale data.
* **The converter is a syntax-level tool**, not a JavaScript interpreter: unusual codegen output falls back to a `TODO(unconverted)` comment plus a warning rather than guessing.
* **Browser only.** Desktop applications (SAP GUI, Excel, legacy Windows apps) cannot be recorded this way; use `wrapper_core.activities.windows_ui` (Windows UI Automation) and the other activities.
* **Not exercised against a real browser in the build environment** (no Node/Edge there): the conversion is tested on realistic transcripts and the generated code is executed against a recording fake framework; run `amx-record` once on your machine before relying on it.
