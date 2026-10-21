# Token and key rotation (registry-related secrets)

| Secret | Who holds it | Blast radius | Rotation | How |
|---|---|---|---|---|
| **Self-heal read token** | Every endpoint (Windows Credential Manager `AUTOMAGIX/registry_token`, DPAPI file fallback) | Read-only: manifest + packages | Quarterly, and immediately on developer offboarding | Create the new token (group or project access token, role *Reporter*, scope `read_api` / `read_repository`/ package read); update the endpoints (below); revoke the old token after the overlap window. |
| **Ops bot token** (`OPS_BOT_TOKEN`) | Protected + masked CI variable in this project only | Can push branches, open MRs, push the `triggers` branch, start pipelines | Every 6 months, or if any maintainer with Maintainer role leaves | New project access token (role Developer, scopes `api`, `write_repository`), replace the variable, revoke the old one. |
| **Logging SSH key** | Every endpoint (DPAPI blob) and the logging project | Create junk refs under `ingest/*` only | Annually (see automation-logging docs) | Not a registry secret; listed for completeness. |
| **LLM API key** | Endpoints (Credential Manager) / logging CI variable | Calls the internal LLM | Per the LLM platform's policy | Not stored in this repo. |

## How the endpoint token reaches machines without a reinstall

The installer stores the token in Credential Manager. To rotate without reinstalling every machine:

1. Publish the new token to the fleet through an **asset-style secret delivery you already trust** (for example an
   operator-run `wrapper_core secrets credman-set AUTOMAGIX/registry_token --stdin` pushed by your endpoint management tool).
   Do **not** put tokens in `manifest.yaml` `assets:` - assets are public to everyone who can read the registry and are for
   non-secret config only.
2. Keep both tokens valid for at least two schedule cycles of your slowest automation.
3. Revoke the old token. Machines that still hold it fail the self-heal fetch and fall back to their **cached manifest**
   (the run still happens, `self_heal.offline` is true in the run log, and analytics raises `VERSION_DRIFT` if it persists) -
   that is your signal to chase stragglers.

If your tokens are *group-level and read-only*, prefer one rotation for the whole fleet over per-automation tokens.

## Checklist after any rotation

- [ ] A test machine in the `dev` ring ran an automation successfully after the change (`self_heal.errors` empty).
- [ ] Old token revoked; revocation date recorded in the MR that documents the rotation.
- [ ] `OPS_BOT_TOKEN` rotated -> run `ops-action` with a harmless `trigger-add` on a test automation to prove the bot still works.
