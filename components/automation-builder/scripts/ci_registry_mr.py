#!/usr/bin/env python3
"""Open a DEV-ring merge request against the registry repository (never promotes to pilot/broad).

    ci_registry_mr.py --registry-dir <checkout> --kind automation --name NAME --version V --zip dist/automations/NAME-V.zip \
                      [--installer dist/installers/NAME-V-setup.exe] [--requires-wrapper ">=0.1.0,<1.0.0"] [--dry-run]

Governance: the merge-request approval IS the gate. This job only proposes a new version in ring `dev` (registry
`add-version` assigns dev only); pilot/broad promotion is a separate, reviewed registry MR (`registry promote`).

Environment (CI variables, never argv):
    REGISTRY_PROJECT_ID, REGISTRY_BOT_TOKEN (write_repository + api scopes, MASKED), CI_API_V4_URL, REGISTRY_GIT_URL
Secrets travel in the process environment (GIT_CONFIG_* for git, a header for the API) - never in argv or .git/config.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class MRError(RuntimeError):
    pass


def branch_name(kind: str, name: str, version: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in f"{name}-{version}")
    return f"dev/{kind}-{safe}"


def stage_files(registry_dir: Path, zip_path: Path, installer: Path | None) -> list[str]:
    """Copy the installer next to the registry (registry add-version copies the zip itself). Returns changed paths."""
    changed = []
    if installer:
        dest = registry_dir / "installers" / installer.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(installer, dest)
        sha = installer.with_name(installer.name + ".sha256")
        changed.append(f"installers/{installer.name}")
        if sha.exists():
            shutil.copyfile(sha, dest.with_name(dest.name + ".sha256"))
            changed.append(f"installers/{installer.name}.sha256")
    return changed


def add_version_cmd(kind: str, name: str, version: str, zip_path: Path, requires_wrapper: str | None,
                    requires_framework: str | None) -> list[str]:
    cmd = [sys.executable, "-m", "registry", "add-version", kind, name, str(zip_path), "--version", version]
    if requires_wrapper:
        cmd += ["--requires-wrapper", requires_wrapper]
    if requires_framework:
        cmd += ["--requires-framework", requires_framework]
    return cmd


def mr_payload(kind: str, name: str, version: str, branch: str, pipeline_url: str = "") -> dict:
    return {
        "source_branch": branch, "target_branch": os.environ.get("REGISTRY_TARGET_BRANCH", "main"),
        "title": f"[dev ring] {kind} {name} {version}",
        "description": (f"Automated proposal from the {name} pipeline.\n\n* Adds version `{version}` and assigns it to the **dev** ring only.\n"
                        "* Promotion to pilot/broad is a separate reviewed MR (`registry promote`).\n"
                        + (f"* Pipeline: {pipeline_url}\n" if pipeline_url else "")),
        "labels": "dev-ring,automated", "remove_source_branch": True, "squash": True,
    }


def git_env(token: str) -> dict:
    """git >= 2.31 reads config from the environment: the token never lands in argv or .git/config."""
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": f"PRIVATE-TOKEN: {token}",
            "GIT_TERMINAL_PROMPT": "0"}


def create_mr(api_url: str, project_id: str, token: str, payload: dict, opener=urllib.request.urlopen) -> str:
    req = urllib.request.Request(f"{api_url.rstrip('/')}/projects/{urllib.parse.quote(project_id, safe='')}/merge_requests",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "PRIVATE-TOKEN": token})
    try:
        with opener(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300] if hasattr(exc, "read") else ""
        raise MRError(f"GitLab API returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise MRError(f"GitLab API call failed: {exc}") from exc
    return body.get("web_url", "")


def run(args, env: dict, runner=subprocess.run, opener=urllib.request.urlopen) -> dict:
    reg = Path(args.registry_dir)
    zip_path, installer = Path(args.zip), (Path(args.installer) if args.installer else None)
    if not zip_path.is_file():
        raise MRError(f"package zip not found: {zip_path}")
    if installer and not installer.is_file():
        raise MRError(f"installer not found: {installer}")
    branch = branch_name(args.kind, args.name, args.version)
    result = {"branch": branch, "dry_run": args.dry_run}
    if args.clone and not (reg / ".git").exists():
        if not env.get("REGISTRY_GIT_URL") or not env.get("REGISTRY_BOT_TOKEN"):
            raise MRError("--clone needs REGISTRY_GIT_URL and REGISTRY_BOT_TOKEN")
        r = runner(["git", "clone", "--depth", "1", env["REGISTRY_GIT_URL"], str(reg)], capture_output=True, text=True,
                   env={**os.environ, **git_env(env["REGISTRY_BOT_TOKEN"])})
        if r.returncode != 0:
            raise MRError(f"git clone failed: {(r.stderr or r.stdout).strip()[:300]}")

    def sh(argv: list[str], extra_env: dict | None = None):
        r = runner(argv, cwd=str(reg), capture_output=True, text=True, env={**os.environ, **(extra_env or {})})
        if r.returncode != 0:
            raise MRError(f"{' '.join(argv[:3])} failed: {(r.stderr or r.stdout).strip()[:300]}")
        return r

    sh(["git", "checkout", "-B", branch])
    sh(add_version_cmd(args.kind, args.name, args.version, zip_path.resolve(), args.requires_wrapper, args.requires_framework))
    stage_files(reg, zip_path, installer)
    sh(["git", "add", "-A"])
    sh(["git", "-c", "user.name=automation-builder", "-c", "user.email=automation-builder@localhost", "commit", "-m",
        f"[dev ring] {args.kind} {args.name} {args.version}"])
    if args.dry_run:
        return result
    token = env.get("REGISTRY_BOT_TOKEN", "")
    if not token or not env.get("REGISTRY_PROJECT_ID") or not env.get("CI_API_V4_URL"):
        raise MRError("REGISTRY_BOT_TOKEN, REGISTRY_PROJECT_ID and CI_API_V4_URL are required (CI variables)")
    remote = env.get("REGISTRY_GIT_URL")
    if not remote:
        raise MRError("REGISTRY_GIT_URL is required (https clone URL of the registry project, WITHOUT credentials)")
    sh(["git", "push", "--force-with-lease", remote, f"HEAD:refs/heads/{branch}"], git_env(token))
    result["mr_url"] = create_mr(env["CI_API_V4_URL"], env["REGISTRY_PROJECT_ID"], token,
                                 mr_payload(args.kind, args.name, args.version, branch, env.get("CI_PIPELINE_URL", "")), opener)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--registry-dir", required=True)
    ap.add_argument("--kind", choices=["automation", "component"], default="automation")
    ap.add_argument("--name", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--zip", required=True)
    ap.add_argument("--installer")
    ap.add_argument("--requires-wrapper")
    ap.add_argument("--requires-framework")
    ap.add_argument("--clone", action="store_true", help="clone REGISTRY_GIT_URL into --registry-dir first (token via environment)")
    ap.add_argument("--dry-run", action="store_true", help="commit locally on the branch, push nothing")
    args = ap.parse_args(argv)
    try:
        print(json.dumps(run(args, dict(os.environ))))
    except MRError as exc:
        print(f"ci_registry_mr.py: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
