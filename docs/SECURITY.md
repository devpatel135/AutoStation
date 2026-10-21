# Security model, and where this implementation deliberately differs from the assessment

## Trust boundaries

```
 developer ──MR──▶ registry (manifest, GitLab RBAC)            LLM endpoint (internal, approved classification)
                      │ read-only token                              ▲   ▲
                      ▼                                              │   │ key only in runner / CI
 endpoint (user session) ── self-heal ──▶ install (sha256-verified zips, zip-slip safe)
      │  runs as the USER: Windows session, cert store, Edge profile
      │  payload ──(loopback, per-run bearer token)──▶ broker ──▶ LLM       (payload never sees the key)
      └── write-only push of unique refs ──▶ automation-logging `ingest/*`   (key can create junk refs, nothing else)
```

## Controls

| Risk | Control |
|---|---|
| Stolen **logging key** | Write-only *by construction*: the endpoint never fetches; it pushes orphan commits to new unique `ingest/<ring>/…` refs. GitLab push rules + protected `ingest/*` (no force-push, no delete) bound the blast radius to junk refs. DPAPI-protected at rest, owner-only ACL. |
| Stolen **read token** | Read-only, scoped to the registry project; Credential Manager storage; rotation with overlap (runbook §4). |
| Tampered package | Every component/payload zip is verified against the manifest's `sha256` before extraction; the manifest changes only via approved MR. Zip-slip rejected. Installers are code-signed (release builds fail closed without signing). |
| **Prompt injection** via logs / scraped content | `llm_call` separates trusted `prompt` from untrusted `context`; context is delimited, delimiter text neutralised; system prompt declares data-not-instructions; structured output schema-validated before any action; the lint rule AS-09 rejects f-string prompts; the retry classifier fails safe to ESCALATE. Declared limit: delimiting reduces but cannot eliminate injection against a model; therefore **no LLM output is ever used to select an irreversible action without deterministic validation** (e.g. the invoice router re-grounds extracted fields in the source text and routes by fixed rules; browser healing validates selectors and blocks destructive targets; the agent loop gates irreversible clicks behind an explicit parameter). |
| **PII in LLM context** | `sensitive_keys` redaction before synopsis / retry / CI diagnosis; `data_classification` vs `llm_allowed_classification` gate refuses LLM calls outright when exceeded. |
| Credentials in payloads | Standard rule AS-01 (static check) + code review; payload zips refuse `*.key`, `*.pem`, `.env`. |
| Kill-switch | Halts before payload execution on every run and login; reasons are visible to the endpoint user. |
| Audit | Registry changes = MRs; LLM-driven decisions = `llm_decision` events (template + input hash, no prompt text); retries = one `retry_event` per attempt. |
| Local broker abuse | Loopback only, random port, per-run bearer token compared in constant time, 2 MB body cap; dies with the run. A hostile *local* process of the same user could still read the child's environment — the user-profile trust model already assumes that user is the principal. |

## Deliberate deviations from the assessment text

1. **"Plaintext SSH key never on disk" is not achievable with stock OpenSSH on Windows** (it cannot read a key from memory; `ssh-add -`
   needs the agent service, which needs admin to start). The key is decrypted into a per-process file in the user-private
   `secrets\tmp` folder with an owner-only ACL, used for a single `git push` (seconds), then overwritten and deleted in a `finally`.
   If your fleet runs the OpenSSH agent, switching `GitSshTransport` to `ssh-add -` is a small, contained change.
2. **`Protect-Data` is not a built-in cmdlet.** DPAPI is done with the .NET `ProtectedData` class (installer, PowerShell 5.1) and
   `CryptProtectData` via ctypes (wrapper-core) with a shared fixed entropy — byte-compatible, tested both directions.
3. **Heartbeat/log reads are push-only.** Because the endpoint key cannot read, trigger delivery uses the *registry's* read channel
   (`triggers.json` published by CI), not the logging repo. The assessment's "poll a shared trigger file in the logging repo"
   would have needed a read key on every endpoint.
4. **LLM calls from payloads go through a loopback broker** rather than environment variables, so a payload (or a library it
   imports) can never read the key. The assessment's env-var approach was fine for wrapper-core itself; the broker extends it to payloads in any language.
5. **Time zones:** stock Windows Python has no tz database; the framework bundle ships `tzdata`. Without it, schedule times fall back to the machine's local time (logged), except `UTC`.

## Known gaps (honest list)

* **Not run against real infrastructure from this build environment:** no GitLab, no NSIS, no Docker, no Node. The Python
  platform (wrapper-core, logging, registry, builder logic, automations) is exercised by tests on Windows including real DPAPI
  and Credential Manager round-trips; the NSIS script, Dockerfile, GitLab CI files and the Node framework are statically checked
  but must be run once in your environment (see `docs/VERIFY_IN_YOUR_ENVIRONMENT.md`).
* **Fleet-wide secret rotation is "overlap + silent reinstall"**, not self-service (runbook §4).
* **Registry access control (Q7)**: the portal is static Pages; anyone who can open it can download installers. Installers
  embed the read token and the logging key — treat the *installer artifacts* as sensitive and restrict Pages/artifact access to the
  intended GitLab group before the pilot grows.
* **Attended/on-demand triggers** go through `triggers.json` + poll slots (minutes of latency), not a tray app, by design.
* **Prompt-injection residual risk** (see table) — model behaviour cannot be proven; controls are layered so a successful
  injection cannot reach an irreversible action.
