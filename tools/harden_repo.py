#!/usr/bin/env python3
"""Apply (idempotently) the GitHub settings this platform's security model assumes - and print what each one did.

    python tools/harden_repo.py [--owner devpatel135] [--repo AutoStation] [--dry-run]

Uses $GITHUB_TOKEN if set, else the token your Git Credential Manager holds for github.com (scopes repo + workflow, repo admin needed). The token is read
into memory only: never printed, logged or written. What it sets:

  * Dependabot alerts + security updates, secret scanning (+ non-provider patterns, push protection), private vulnerability reporting
  * Actions: only GitHub-owned actions may run; workflows default to READ-only; fork PRs from outsiders need approval
  * Rulesets: `main` (no deletion, no force-push), `registry-artifacts` and `triggers` (no deletion, no force-push), and on the private
    logs repo the `ingest` + default branches (the installer-embedded credential must not be able to delete or rewrite history)
  * Workflow tokens may NOT approve pull requests (can_approve_pull_request_reviews = false)
  * NOT set, on purpose: required pull-request reviews / status checks on `main`. The release workflow commits the regenerated manifest
    straight to `main` with the Actions token, so a required-PR rule would stop releases. If you want a hard review gate, create a
    GitHub App (or a deploy key) for the release bot, add it as the ruleset's only bypass actor and require PRs for everyone else.
  * Pages source = GitHub Actions
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def credential() -> str:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True).stdout
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if not kv.get("password"):
        raise SystemExit("no GitHub credential found in the Git credential store (run `git push` once or `gh auth login`)")
    return kv["password"]


class Api:
    def __init__(self, token: str, dry: bool = False):
        self._t, self.dry = token, dry

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        if self.dry and method != "GET":
            return 0, {"dry_run": True}
        req = urllib.request.Request("https://api.github.com" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self._t}", "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "autostation-harden"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:  # noqa: S310
                raw = r.read().decode() or "{}"
                return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {})
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode() or "{}")
            except ValueError:
                detail = {}
            return exc.code, detail


def report(label: str, code: int, detail: dict) -> None:
    ok = code in (0, 200, 201, 202, 204)
    note = "" if ok else f"  <- {detail.get('message', '')[:140]}"
    print(f"[{'ok' if ok else 'SKIP/FAIL'}] {label} (HTTP {code or 'dry-run'}){note}")


def ruleset(api: Api, base: str, name: str, branch_pattern: str) -> None:
    code, existing = api.call("GET", f"{base}/rulesets")
    body = {"name": name, "target": "branch", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"include": [branch_pattern], "exclude": []}},
            "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}]}
    found = next((r for r in (existing if isinstance(existing, list) else []) if r.get("name") == name), None)
    if found:
        report(f"ruleset {name} (update)", *api.call("PUT", f"{base}/rulesets/{found['id']}", body))
    else:
        report(f"ruleset {name} (create)", *api.call("POST", f"{base}/rulesets", body))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="devpatel135")
    ap.add_argument("--repo", default="AutoStation")
    ap.add_argument("--logs-repo", default="AutoStation-logs", help="private run-log repository (its `ingest` branch gets a no-delete / no-force-push ruleset)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    api = Api(credential(), a.dry_run)
    base = f"/repos/{a.owner}/{a.repo}"

    code, repo = api.call("GET", base)
    if code != 200 or not (repo.get("permissions") or {}).get("admin"):
        raise SystemExit(f"need admin on {a.owner}/{a.repo} (HTTP {code})")

    report("security: secret scanning + push protection + non-provider patterns + dependabot updates",
           *api.call("PATCH", base, {"security_and_analysis": {
               "secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"},
               "secret_scanning_non_provider_patterns": {"status": "enabled"}, "dependabot_security_updates": {"status": "enabled"}}}))
    report("dependabot vulnerability alerts", *api.call("PUT", f"{base}/vulnerability-alerts"))
    report("private vulnerability reporting", *api.call("PUT", f"{base}/private-vulnerability-reporting"))
    report("actions: only GitHub-owned actions", *api.call("PUT", f"{base}/actions/permissions", {"enabled": True, "allowed_actions": "selected"}))
    report("actions: allow-list", *api.call("PUT", f"{base}/actions/permissions/selected-actions",
                                              {"github_owned_allowed": True, "verified_allowed": False, "patterns_allowed": []}))
    report("actions: default token permissions = read", *api.call("PUT", f"{base}/actions/permissions/workflow",
                                                                  {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}))
    report("actions: outside contributors' fork PRs need approval", *api.call(
        "PUT", f"{base}/actions/permissions/fork-pr-contributor-approval", {"approval_policy": "all_external_contributors"}))
    ruleset(api, base, "main: no deletion / force-push", "~DEFAULT_BRANCH")
    ruleset(api, base, "registry-artifacts: append-only", "refs/heads/registry-artifacts")
    ruleset(api, base, "triggers: no deletion / force-push", "refs/heads/triggers")
    # the logs repo is written with a credential that ships inside every installer: whoever extracts it must not be able to delete or rewrite history
    logs_base = f"/repos/{a.owner}/{a.logs_repo}"
    lcode, _ = api.call("GET", logs_base)
    if lcode == 200:
        ruleset(api, logs_base, "ingest: no deletion / force-push", "refs/heads/ingest")
        ruleset(api, logs_base, "default branch: no deletion / force-push", "~DEFAULT_BRANCH")
    else:
        print(f"[skip] logs repo {a.logs_repo} not found (HTTP {lcode}): create it with tools/setup_live.py")
    code, pages = api.call("GET", f"{base}/pages")
    if code == 200 and pages.get("build_type") != "workflow":
        report("pages: source = GitHub Actions", *api.call("PUT", f"{base}/pages", {"build_type": "workflow"}))
    else:
        print(f"[ok] pages: build_type={pages.get('build_type') or '(not enabled)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
