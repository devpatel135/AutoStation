# Security policy

## Reporting a vulnerability
Use GitHub's **private vulnerability reporting** (Security tab -> Report a vulnerability). Please do not open a public issue.

## What is protected, and how
| Risk | Control |
| --- | --- |
| Tampered updates reaching machines | Registry zips are immutable on an append-only branch (ruleset: no deletion / force-push); every download is sha256-verified; zip extraction is zip-slip safe; the manifest is Ed25519-signed with a monotonic sequence number (replays of older releases are refused) and the queued-runs file `triggers.json` is covered by the signature (see docs/SECURITY.md). |
| Malicious workflow changes | Every action pinned to a commit SHA (`tools/pin_actions.py --check` in CI); inputs reach shells only through `env:`; jobs have timeouts; secret-bearing jobs run only from `main`; default token is read-only; only GitHub-owned actions may run. |
| Leaked credentials | `tools/check_secrets.py` in CI + GitHub secret scanning with push protection; secrets on endpoints are DPAPI/Credential Manager protected and wiped after use. |
| Unverifiable installers | Installers are published with SHA-256 files and GitHub build-provenance attestations (`gh attestation verify <file> --repo devpatel135/AutoStation`). They are not Authenticode-signed unless a certificate is configured, so SmartScreen warns. |

## Honest limits
* Installed automations push run logs with a credential that ships inside the public installer. A GitHub token cannot be write-only, so **anyone who extracts it can read the private logs repo and write to it** (a ruleset stops history rewrites on `ingest`; see docs/LIVE_LOGGING.md for the full exposure, the optional read-only token and rotation). Treat all log data as **advisory telemetry**, never as a control signal: it can be forged.
* GitHub Pages is public: dashboards and the registry are world-readable.
* Public repository: anything committed is public. Never commit customer data or secrets.

Details: [docs/SECURITY.md](docs/SECURITY.md), [docs/LIVE_LOGGING.md](docs/LIVE_LOGGING.md), [docs/PIPELINES.md](docs/PIPELINES.md).
