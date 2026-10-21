# Verify in your environment

> **Update:** GitHub Actions now verifies the Python suites (Windows + Linux, Python 3.9 and 3.12), the Node syntax + tests, the
> end-to-end walkthrough, and — on a real Windows runner — the NSIS build plus a **silent per-user install -> scheduled task ->
> first run logged -> uninstall** smoke test (`installer-dev.yml`). What remains for you is what CI cannot reach: your GitLab,
> your containeryard Docker image, your PKI signing cert, a real browser/Edge run of the Playwright example, your LLM
> endpoint, and a standard-user (non-admin) VM install. Sections below list those checks.

This repository was built on a Windows machine that had Python and PowerShell but **no Node.js, NSIS, Docker, or GitLab**.
Everything below was therefore written and statically checked but not executed. Run this list once, in order, before the pilot.
Each step says what "good" looks like.

## 1. Python platform (5 min) — already green where it was built

```bash
python -m pip install pytest pydantic jinja2
python tools/test_all.py
```
Good: every Python suite `PASS`; Node suites `SKIPPED (node not installed)` until step 2.
Also run it on the **oldest Python your VDI image ships** (wrapper-core supports 3.9+; it was exercised on 3.13 and 3.15).

## 2. Node framework (10 min) — GitHub Actions already does steps 2a/2b on every push

The `node` job in `.github/workflows/ci.yml` syntax-checks every JS file and runs `node --test`. The Node suites were additionally
executed at authoring time in an embedded V8 with Node shims (`components/execution-framework/tools/v8-harness`), which is good
evidence for the pure logic but **not** a substitute for real Node + a real browser (step 2c). Generate `package-lock.json` once
(`npm install --package-lock-only` against your mirror) and commit it — CI `npm ci` requires it.


```bash
cd components/execution-framework
node --version                     # >= 18
find . -name "*.js" -not -path "./node_modules/*" -print0 | xargs -0 -n1 node --check
node --test test/                  # unit tests (pure logic + fakes; no browser needed)
cd ../automations/automation-certification-site && node --test test/
```
Good: no syntax errors, all tests pass. If a test fails it is a real defect in code that could not be executed at authoring
time — fix it before continuing. Then the live check (needs Edge and `npm ci` from your internal mirror):
```bash
cd components/execution-framework && npm ci --omit=dev
node ../automations/automation-certification-site/demo/server.js &      # local demo portal
python -m wrapper_core dev ../automations/automation-certification-site --param portal_url=http://127.0.0.1:8099/v2.html
```
Good: run status `success`; the `selector_healed` event appears for the changed button (v2 page) and the LLM mock proposes
the new selector.

## 3. Installer build (15 min, CI or a box with NSIS)

```bash
cd components/automation-builder
python scripts/build_installer.py --config tests/fixtures/sample-build.json --out out   # renders .nsi
makensis out/*.nsi                                                                        # compiles
python scripts/validate_nsi.py out/*.nsi
```
Good: `makensis` exits 0 with no warnings about unresolved tokens. Then on a **throwaway Windows 10/11 VM, standard user (not admin)**:
run the installer; confirm — files under `%LOCALAPPDATA%\AUTOMAGIX`; `secrets\logging_key.dpapi` exists and no plaintext key
anywhere; `schtasks /Query /TN "AUTOMAGIX\<automation>-<slot>"`; `Startup\AUTOMAGIX-maintain.cmd`; first run logged;
**uninstall** removes tasks/payload/blob/credentials and ref-counts shared components. Re-run the installer: it must be a no-op.

## 4. Logging repo and the write-only key (15 min)

1. Create the `automation-logging` project; add a **deploy key** with write access; set Push Rules to allow only branches matching
   `^ingest/` (see `components/automation-logging/README.md`), protect `ingest/*` (no force-push, no delete).
2. From a test endpoint, run an automation. Verify the ref `ingest/<ring>/run/<automation>/<run_id>` appears and that the key
   **cannot** push to `main` or delete refs.
3. Run the ingest CI job; verify the archive appears and the refs are deleted.

## 5. Registry portal + CI (15 min)

Push `components/automation-registry` as its own project, enable Pages, run the pipeline, open the portal. Verify: catalog
renders, installer links resolve, the admin panel produces a valid MR link, `ops-action` pipeline opens an MR.

## 6. LLM endpoint (Q6)

Set `AUTOSTATION_LLM_ENDPOINT` (and the key in Credential Manager / CI variable) and run `python -m wrapper_core assist design "…"`.
Confirm: the endpoint accepts `messages` chat format and `response_format: json_object` (the code works without it by
parsing + schema validation); the non-production endpoint for dev; and the **data-classification approval level** — set
`llm_allowed_classification` per automation accordingly.

## 7. Compatibility + smoke

`python tools/compat_smoke.py` against your real manifest before every ring promotion (CI runs it on every registry MR).

## What "done" looks like

A real pilot automation is installed on 2–3 VMs, you see its runs and synopses on the dashboard, you pause it from the admin
panel and watch the next runs report `halted_paused`, you roll it back, and an induced failure produces an alert with an LLM
diagnosis in the channel the team watches.
