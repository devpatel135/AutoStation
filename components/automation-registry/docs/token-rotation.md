# Token and key rotation (registry-related secrets)

| Secret | Who holds it | Blast radius | Rotation | How |
|---|---|---|---|---|
| **Self-heal read token** | Every endpoint (Windows Credential Manager `AUTOMAGIX/registry_token`, DPAPI file fallback) | Read-only: manifest + packages | Quarterly, and immediately on developer offboarding | Create the new token (group or project access token, role *Reporter*, scope `read_api` / `read_repository`/ package read); update the endpoints (below); revoke the old token after the overlap window. |
| **Ops bot token** (`OPS_BOT_TOKEN`) | Protected + masked CI variable in this project only | Can push branches, open MRs, push the `triggers` branch, start pipelines | Every 6 months, or if any maintainer with Maintainer role leaves | New project access token (role Developer, scopes `api`, `write_repository`), replace the variable, revoke the old one. |
| **Logging SSH key** | Every endpoint (DPAPI blob) and the logging project | Create junk refs under `ingest/*` only | Annually (see automation-logging docs) | Not a registry secret; listed for completeness. |
| **LLM API key** | Endpoints (Credential Manager) / logging CI variable | Calls the internal LLM | Per the LLM platform's policy | Not stored in this repo. |

## How the endpoint token reaches machines without a reinstall

### Option A — Fleet-wide push via manifest `secret_refresh` (recommended for 10+ machines)

Self-heal supports a `secret_refresh` section in `manifest.yaml` that delivers new token values to every enrolled machine at its next scheduled run, without requiring any out-of-band tooling or reinstall.

**How it works:**
1. Write the new plaintext token to a file and publish it as a registry asset (a separate URL from `manifest.yaml` itself; access controlled by the same read token the endpoint already holds).
2. Add a `secret_refresh` entry to the signed manifest, naming the asset and its sha256:

```yaml
# manifest.yaml  (excerpt)
secret_refresh:
  registry_token:
    url: assets/registry_token.txt      # relative to the registry base URL; access requires the read token
    sha256: "abc123..."                 # sha256 of the plaintext file; the manifest signature covers this too
```

3. Sign and publish the updated manifest (the `secret_refresh` section is covered by the `sig2` / `seq` signature).
4. At each machine's next self-heal cycle, wrapper-core fetches the asset, DPAPI-encrypts it locally (the plaintext never touches disk), and atomically replaces the DPAPI blob in the `secrets/` directory.
5. The new token is live on that machine from the next run onward.

**Overlap window:** keep both the old and new tokens valid for at least two full schedule cycles of your slowest automation before revoking the old one. `self_heal.offline: true` in the run log is your signal that a machine is still using a stale token.

**Allowed secret names** (enforced in `secrets.refresh_dpapi_secret`): `logging_key`, `registry_token`, `logging_token`, `llm_key`.

**Credential Manager tokens** (e.g., tokens stored by `set-credential.ps1` at install time) are not refreshed by this mechanism — those live in Windows Credential Manager, not in the `secrets/` DPAPI blobs. See Option B for those.

### Option B — Manual push or endpoint management tool

For smaller fleets or Credential Manager tokens:

1. Publish the new token to the fleet through an operator-run command on each machine, or push it via your endpoint management tool:
   ```
   python -m wrapper_core secrets credman-set AUTOMAGIX/registry_token --stdin < new_token.txt
   ```
2. Keep both tokens valid for at least two schedule cycles of your slowest automation.
3. Revoke the old token. Machines that still hold it fail the self-heal fetch and fall back to their **cached manifest** — `self_heal.offline: true` in the run log, and analytics raises `VERSION_DRIFT` if it persists.

If your tokens are *group-level and read-only*, prefer one rotation for the whole fleet over per-automation tokens.

## Fleet-wide non-secret config (`platform_overrides`)

For non-secret operational config — currently `llm_endpoint` and `llm_model` — the manifest carries a
`platform_overrides` section that self-heal writes into each machine's `platform.json` on its next run.
No installer or reinstall is required.

```yaml
# manifest.yaml  (excerpt)
platform_overrides:
  llm_endpoint: "https://ai-gateway.internal/v1"
  llm_model: "gpt-4o-internal"
```

**Rules:**
- Values are covered by the manifest signature (`sig2` / `seq`), so they cannot be tampered with in transit.
- `llm_endpoint` must be a valid `https://` URL; an empty string clears the value.
- Only the keys listed in `_PLATFORM_OVERRIDE_KEYS` (wrapper-core source) are accepted; unknown keys are silently ignored.
- Changes appear in the run log as `platform.<key> updated via manifest`.
- Remove the section from the manifest once all machines have picked it up (verified via the analytics dashboard).

**Not for secrets.** Token and key rotation uses the `secret_refresh` section (see above).

## Checklist after any rotation

- [ ] A test machine in the `dev` ring ran an automation successfully after the change (`self_heal.errors` empty).
- [ ] Old token revoked; revocation date recorded in the MR that documents the rotation.
- [ ] `OPS_BOT_TOKEN` rotated -> run `ops-action` with a harmless `trigger-add` on a test automation to prove the bot still works.
