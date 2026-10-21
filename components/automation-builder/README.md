# automation-builder

The factory that produces every distributable: deterministic payload zips, per-automation **signed NSIS installers**,
the Docker build image, and the versioned **prompt library** used by the developer-assist CLI.

```
components/automation-builder/
  scripts/package.py            payload/component -> deterministic zip (+ JSON {name,version,url,sha256,size})
  scripts/build_installer.py    config + staged secrets -> rendered .nsi -> makensis -> sign -> .exe + .sha256
  scripts/validate_nsi.py       static linter for the rendered installer (we lint what we cannot always compile)
  scripts/ci_installer_config.py  CI glue: fetch bootstrap wrapper-core from the registry, write build.json
  scripts/ci_registry_mr.py     open a DEV-ring MR against the registry (never promotes)
  installer/autostation.nsi.in  the NSIS template (thin, per-user, no admin)
  installer/scripts/*.ps1       PowerShell 5.1 helpers (DPAPI, Credential Manager, ACL, checks, signing, config)
  docker/                       Dockerfile (internal containeryard base), entrypoint, make_context.py
  prompts/                      scaffold.j2 review.j2 test_gen.j2 design_doc.j2 + loader.py
  templates/                    GitLab CI includes for downstream automation / component repos
  tests/                        pytest (~110 tests; PowerShell tests run for real on Windows)
```

## How a build flows

```
automation repo CI                                         automation-builder image (CI)
------------------                                         -----------------------------
tag v1.2.3 ──► lint + pytest
           ──► package.py automation ... ──► dist/automations/<name>-1.2.3.zip   (sha256, reproducible)
           ──► ci_installer_config.py ──► fetches wrapper-core for the ring from the registry (sha256-verified)
           ──► build_installer.py  ──► stage {wrapper-core, [python-embed], scripts, secrets} ──► render .nsi
                                   ──► validate_nsi ──► makensis ──► osslsigncode/sign.ps1 ──► <name>-1.2.3-setup.exe(.sha256)
           ──► ci_registry_mr.py   ──► registry MR: add-version (ring dev only) + installers/<name>-1.2.3-setup.exe
                                       └─ approval of THAT MR is the governance gate; pilot/broad = separate `registry promote` MRs
```

### What the installer does (per user, no admin, `%LOCALAPPDATA%\AUTOMAGIX`)

1. `install-check.ps1`: Windows 10+, writable dir, disk space, `git` + `ssh` on PATH. Nothing is written before this passes.
2. Lays down the **embedded Python** (if built with one) in `python\` and rewrites its `._pth` so `..\wrapper-core` is importable
   (embeddable Python ignores `PYTHONPATH` when a `._pth` exists - `configure-python.ps1`).
3. Lays down **wrapper-core** only if missing or older than the bundled copy (`VersionCompare`); self-heal keeps it current afterwards.
4. Writes `config\platform.json` (`write-platform-config.ps1`, real JSON escaping, preserves operator-added keys) and `known_hosts`.
5. DPAPI-protects the logging key, stores the registry token in Credential Manager (table below).
6. Hands over to `python -m wrapper_core install <automation>` - `lifecycle.install_automation` does the *dynamic assembly*
   (execution-framework + payload, only what is missing/stale), registers Task Scheduler per slot and the Startup-folder
   maintenance script. The installer itself stays thin.
7. Writes the uninstaller and the per-user Add/Remove Programs entry (HKCU), then starts the first run (hidden, detached).

Switches: `/S` silent, `/RING=dev|pilot|broad`, `/D=<dir>` (must be last), `/NORUN` skip the first run. Exit codes:
2 prerequisites, 3 bad ring, 4 python, 5 config, 6 key, 7 token, 8 wrapper-core install.

**Uninstall** (`uninstall-<automation>.exe`, also `/S`): `python -m wrapper_core uninstall <automation>` removes tasks and payload,
ref-counts the shared components, sends the final `decommissioned` event *before* the key is deleted, removes the DPAPI blob and
Credential Manager entries when it was the last automation. The uninstaller then removes installer-managed pieces (`python\`,
config) only when no other `uninstall-*.exe` remains.

## Secret handling

| Secret | Injected | At rest on the endpoint | Rotation |
|---|---|---|---|
| Logging SSH key (write-only deploy key, pushes only to `ingest/*` refs) | build time, CI **file** variable `LOGGING_SSH_KEY_FILE`, staged to a temp dir that is wiped in `finally` | `secrets\logging_key.dpapi` - DPAPI CurrentUser blob, owner-only ACL; decrypted **in memory** by wrapper-core | annual: new key pair, update CI variable + GitLab deploy key, rebuild/redistribute installers (see `docs/` runbook) |
| Registry read token (manifest + artefact reads) | build time, `REGISTRY_READ_TOKEN_FILE` (or masked `REGISTRY_READ_TOKEN`) | Windows Credential Manager target `AUTOMAGIX/registry_token` (UTF-16LE generic credential) | quarterly or on offboarding: update CI variable **and** the credential; installers must be rebuilt unless self-heal rotates it |
| LLM API key | **not in the installer** | Credential Manager `AUTOMAGIX/llm_key`, provisioned by operators | follows GitLab protected-variable policy |
| Code-signing certificate | CI **file** variables `SIGNING_PFX_FILE` + `SIGNING_PFX_PASSWORD_FILE` (or `SIGNING_THUMBPRINT` on a Windows runner) | never leaves CI | per PKI policy |
| Registry bot token (opens the dev MR) | CI masked variable `REGISTRY_BOT_TOKEN` | never leaves CI | quarterly |

Rules enforced in code and tests: secrets are never in `build.json`, never in argv (passwords go via files, `-readpass`, or the
child's environment; git/API tokens via `GIT_CONFIG_*`/headers), never in the rendered `.nsi` (`validate_nsi.py` rejects PEM
blocks, token patterns and any `--forbid-file` value), and the staging directory is zero-overwritten and deleted after every build.

## Local development workflow

```bash
export WRAPPER_CORE_PATH=../wrapper-core                      # miniyaml/versions/secrets (auto-detected in the monorepo)
python scripts/package.py component wrapper-core ../wrapper-core --version 0.1.0 --out dist
python scripts/package.py automation automation-example-hello ../automations/automation-hello-world --version 1.0.0 --out dist
cp examples/build.sample.json dist/build.json                 # edit registry_url / log_remote
python scripts/build_installer.py --config dist/build.json --out dist --render-only --allow-missing-secrets
python scripts/validate_nsi.py dist/installers/automation-example-hello-1.0.0.nsi
```

Full build (needs `makensis` or the Docker image): drop `--render-only`; real builds supply `LOGGING_SSH_KEY_FILE` and
`REGISTRY_READ_TOKEN_FILE`, and release builds add `--require-signing` (fails closed with no certificate; plain `--sign` without a
certificate prints a loud UNSIGNED warning and builds anyway - dev only).

Prompt library: `from loader import render; render("scaffold", description=..., api=..., manifest_schema=...)`
(also `list_templates()`, `template_version(name)`). Templates are `StrictUndefined`, versioned by front matter and carry a worked
few-shot example each; `design_doc` takes `step=requirements|design|scaffold|tests|document` plus `previous_output`.

### Testing without makensis

`python -m pytest` (from this directory) renders the template for several configs, runs `validate_nsi.py` on each, runs a full
`run_build` with a fake `makensis`/signer, and asserts the secret invariants. The PowerShell tests execute the real scripts on
Windows: **DPAPI round trip in both directions** (PowerShell -> `wrapper_core.secrets` and back, plus `wrapper_core secrets
protect`), ACL restriction, `install-check`, `._pth` rewrite, `platform.json` consumed by `wrapper_core.paths.platform_config`,
NSIS-style quoted/empty command lines, and real Authenticode signing of a freshly compiled test exe with an in-memory PFX
(no certificate store, Credential Manager or registry is touched; `set-credential.ps1` is only run with `-DryRun`).

## Docker image

`docker/Dockerfile` builds `FROM ${BASE_IMAGE}` (default `containeryard.example.internal/base/python:3.12-slim`; there is no
public-registry reference anywhere - a test enforces it) and installs `nsis`, `osslsigncode`, `git`, `openssh-client`, plus pip packages
from `PIP_INDEX_URL`; `APT_MIRROR` repoints apt. `docker/make_context.py` stages the context so the same file works from the
monorepo and a split project. In CI the `prepare-context` + `build-image` (kaniko) jobs publish `$CI_REGISTRY_IMAGE/builder`.
Downstream repos `include:` `templates/build-installer.gitlab-ci.yml` (jobs `.package-payload`, `.build-installer`,
`.open-registry-mr`); `templates/automation.gitlab-ci.yml` and `templates/component.gitlab-ci.yml` are ready-to-copy pipelines.
Use the image: `docker run --rm -v "$PWD":/work $BUILDER_IMAGE installer --config build.json --out dist --require-signing`.

## Honest limitations

* **The NSIS script and the Docker image were never compiled/built here** (no `makensis`, Docker or Node on the authoring machine).
  The template is linted by `validate_nsi.py` and every PowerShell script it calls is executed in tests, but the first real
  `makensis` run may need small fixes (plugin availability in your NSIS build: MUI2, LogicLib, FileFunc, WordFunc, TextFunc,
  nsExec, System). `osslsigncode` command construction is unit-tested, not run.
* **ssh needs a key file for the seconds of a push.** OpenSSH on Windows cannot read a key from memory, so wrapper-core decrypts the
  DPAPI blob to an owner-only temp file for one `git push` and wipes it afterwards. The assessment's "plaintext never on disk" is
  not achievable with stock OpenSSH; the exposure window is bounded and the key is write-only by construction.
* **The installer binary carries the logging key and registry token** (DPAPI protects them only once installed). Treat installers as
  confidential artefacts: restrict who can download `installers/`, and rely on the key being write-only (`ingest/*` push rules) and the
  token being read-only. Plaintext also exists for moments as NSIS extracts the staged secret files to `%TEMP%`; the PowerShell
  scripts overwrite and delete them immediately, and NSIS removes its plugin dir at exit.
* **Interpreter location differs from SPEC section 2.** The embedded Python lives in `AUTOMAGIX\python\` (installer-managed), not in
  `execution-framework\python-embed\`: self-heal swaps the framework directory while the interpreter is running, and Windows cannot
  replace files in use. Built without `python_embed_zip`, a system Python 3.9+ on PATH is required and used via `PYTHONPATH`.
* Bundled wrapper-core versions are compared numerically (`major.minor.patch`; pre-release tags are ignored by the installer).
* A failed install is not rolled back; it is idempotent, so re-running the installer (or the next self-heal) repairs it.
* The tasks run as the interactive user (`InteractiveToken`): they fire only while that user is logged on, which matches the VDI model.
* `RMDir /r` is applied only to AUTOMAGIX's own subfolders, never to `$INSTDIR` itself, so a bad `/D=` cannot wipe an arbitrary tree.
* Unsigned installers will be flagged by SmartScreen/AV; use `--require-signing` for anything beyond a developer's machine.
* Timezone-aware slot times rely on `zoneinfo`; stock Windows Python has no tz database, so bundle the `tzdata` wheel in the
  embedded Python (otherwise slot times are interpreted in machine-local time - noted in wrapper-core).
