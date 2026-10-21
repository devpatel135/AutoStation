#!/usr/bin/env python3
"""One-time, idempotent setup of everything that needs a GitHub admin token (run it yourself, in your own terminal).

    python tools/setup_live.py [--owner devpatel135] [--repo AutoStation] [--logs-repo AutoStation-logs]

  1. creates the PRIVATE logs repo (<owner>/AutoStation-logs) if missing
  2. creates the `production` environment restricted to the default branch (secrets reach only code merged to main)
  3. stores the LOGGING_TOKEN secret (a fine-grained PAT you create at https://github.com/settings/personal-access-tokens/new :
     only repository <owner>/AutoStation-logs, permission Contents: read and write, nothing else) - prompted with no echo
  4. runs tools/harden_repo.py (secret scanning, push protection, Actions allow-list, read-only default token, rulesets)

Token for the API calls: $GITHUB_TOKEN, else the one in your Git Credential Manager (needs repo + workflow, admin on the repo).
Nothing is printed or written to disk. Secrets are encrypted client-side with the repository's public key (libsodium sealed box;
needs `pip install pynacl`).
"""
from __future__ import annotations

import argparse
import base64
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harden_repo as hr  # noqa: E402


def seal(public_key_b64: str, value: str) -> str:
    try:
        from nacl import encoding, public
    except ImportError:
        raise SystemExit("pip install pynacl   (needed to encrypt the secret for GitHub)")
    box = public.SealedBox(public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder()))
    return base64.b64encode(box.encrypt(value.encode())).decode()


def put_secret(api: hr.Api, base: str, name: str, value: str, env: str | None = None) -> bool:
    """Store an Actions secret through the API. Returns True only if GitHub accepted it."""
    scope = f"{base}/environments/{env}" if env else base
    code, key = api.call("GET", f"{scope}/secrets/public-key")
    if code != 200:
        hr.report(f"secret {name}: public key", code, key)
        return False
    code, detail = api.call("PUT", f"{scope}/secrets/{name}", {"encrypted_value": seal(key["key"], value), "key_id": key["key_id"]})
    hr.report(f"secret {name}" + (f" (environment {env})" if env else ""), code, detail)
    return code in (201, 204)


def manual_secret_help(name: str, value: str | None = None, keep_file: Path | None = None) -> None:
    """The API refused (the stored credential lacks the secrets permission): finish by hand in the web UI."""
    print(f"  -> add {name} by hand: https://github.com/<owner>/<repo>/settings/secrets/actions/new (Name: {name})")
    if value is not None and keep_file is not None:
        keep_file.write_text(value + "\n", encoding="ascii")
        print(f"     the value is saved to {keep_file} - paste it into the secret, then DELETE that file")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="devpatel135")
    ap.add_argument("--repo", default="AutoStation")
    ap.add_argument("--logs-repo", default="AutoStation-logs")
    ap.add_argument("--skip-secret", action="store_true")
    a = ap.parse_args()
    api = hr.Api(hr.credential())
    base = f"/repos/{a.owner}/{a.repo}"
    code, repo = api.call("GET", base)
    if code != 200 or not (repo.get("permissions") or {}).get("admin"):
        raise SystemExit(f"need admin on {a.owner}/{a.repo} (HTTP {code})")
    branch = repo.get("default_branch", "main")

    code, _ = api.call("GET", f"/repos/{a.owner}/{a.logs_repo}")
    if code == 404:
        hr.report(f"create private repo {a.logs_repo}", *api.call("POST", "/user/repos", {
            "name": a.logs_repo, "private": True, "auto_init": False,
            "description": "Run logs pushed by installed AutoStation automations (private)"}))
    else:
        print(f"[ok] {a.logs_repo} exists")

    hr.report("environment production (deploy branches: default branch only)", *api.call("PUT", f"{base}/environments/production", {
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}))
    api.call("POST", f"{base}/environments/production/deployment-branch-policies", {"name": branch, "type": "branch"})

    if not a.skip_secret:
        tok = getpass.getpass(f"Paste the fine-grained PAT for {a.owner}/{a.logs_repo} (Contents read+write; Enter to skip): ").strip()
        if tok:
            if not put_secret(api, base, "LOGGING_TOKEN", tok):
                manual_secret_help("LOGGING_TOKEN")
                print("     (paste the same PAT there; this script does not keep it)")
        else:
            print("[skip] LOGGING_TOKEN not set - dashboards keep the waiting state, installers stay standalone")

    if not a.skip_secret and input("Generate + store the registry signing key (REGISTRY_SIGNING_SEED)? [y/N] ").strip().lower() == "y":
        import secrets as _s
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "components" / "wrapper-core"))
        from wrapper_core import ed25519
        seed, pub = ed25519.keypair_from_seed(_s.token_bytes(32))
        if put_secret(api, base, "REGISTRY_SIGNING_SEED", seed.hex()):
            # publish the PUBLIC key only after the private seed is safely stored: a pinned key nothing can sign for would lock
            # every newly installed machine out of the registry
            code, _ = api.call("POST", f"{base}/actions/variables", {"name": "AUTOSTATION_REGISTRY_PUBKEY", "value": pub.hex()})
            if code == 409:
                code, _ = api.call("PATCH", f"{base}/actions/variables/AUTOSTATION_REGISTRY_PUBKEY", {"name": "AUTOSTATION_REGISTRY_PUBKEY", "value": pub.hex()})
            hr.report("variable AUTOSTATION_REGISTRY_PUBKEY", code, {})
            print(f"Public key {pub.hex()}: the next site build signs the registry, and the next installer build pins it. "
                  "Machines installed earlier keep accepting the registry until reinstalled (they hold no key).")
        else:
            keep = Path.home() / "autostation-signing-seed.txt"
            manual_secret_help("REGISTRY_SIGNING_SEED", seed.hex(), keep)
            print(f"     then ALSO add the repository VARIABLE AUTOSTATION_REGISTRY_PUBKEY = {pub.hex()} "
                  "(Settings > Secrets and variables > Actions > Variables) - only after the secret exists")

    sys.argv = [sys.argv[0], "--owner", a.owner, "--repo", a.repo]
    return hr.main()


if __name__ == "__main__":
    sys.exit(main())
