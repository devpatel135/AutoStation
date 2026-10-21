#!/usr/bin/env python3
"""Scaffold a new AUTOSTATION automation repo in one action (assessment item 11).

    python tools/new_automation.py automation-expense-checker \
        --description "Check expense reports against policy" --owner finops@example.com \
        --owner-team "FinOps Automation" --classification internal [--runtime python|node] [--out ./automation-expense-checker]
        [--register path/to/manifest.yaml]

Creates: starter payload that runs and logs successfully (verified by its own lifecycle test), automation.yaml,
tests, README, CI wiring that includes automation-builder's shared templates, and a registry manifest stanza.
With ``--register`` the stanza is appended to the manifest via ``python -m registry new-automation`` when available
(MR approval still gates it). The generated project is checked by running its test before the command returns.
"""
from __future__ import annotations

import argparse
import re
import shutil
import string
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME_RX = re.compile(r"^automation-[a-z0-9]+(?:-[a-z0-9]+)*$")
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")


def render_tree(src: Path, dst: Path, values: dict) -> list[Path]:
    written = []
    for f in sorted(src.rglob("*")):
        if f.is_dir():
            continue
        rel = f.relative_to(src)
        if f.suffix == ".tmpl":
            rel = rel.with_suffix("")
            if rel.name == "gitlab-ci.yml":
                rel = rel.with_name(".gitlab-ci.yml")
            text = string.Template(f.read_text(encoding="utf-8")).safe_substitute(values)
        else:
            text = f.read_text(encoding="utf-8")
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="automation-<kebab-name>")
    p.add_argument("--description", required=True)
    p.add_argument("--owner", required=True, help="owning distribution list (not a person)")
    p.add_argument("--owner-team", default="Automation Platform")
    p.add_argument("--classification", default="internal", choices=CLASSIFICATIONS)
    p.add_argument("--runtime", default="python", choices=["python", "node"])
    p.add_argument("--out", default=None)
    p.add_argument("--register", default=None, help="manifest.yaml to add the stanza to")
    p.add_argument("--no-verify", action="store_true", help="skip running the generated test")
    a = p.parse_args(argv)

    if not NAME_RX.match(a.name):
        p.error("name must look like automation-some-name (lowercase, digits, hyphens)")
    if "@" not in a.owner:
        p.error("--owner must be an email/distribution list address")
    src = HERE / "templates" / f"automation-{a.runtime}"
    if not src.is_dir():
        p.error(f"no template for runtime {a.runtime!r}")
    out = Path(a.out or a.name).resolve()
    if out.exists() and any(out.iterdir()):
        p.error(f"{out} already exists and is not empty")

    values = {"name": a.name, "description": a.description.replace('"', "'"), "owner": a.owner,
              "owner_team": a.owner_team, "classification": a.classification}
    files = render_tree(src, out, values)
    print(f"created {len(files)} files in {out}")
    for f in files:
        print("  ", f.relative_to(out))

    if not a.no_verify and a.runtime == "python":
        env_dir = HERE.parent / "components" / "wrapper-core"
        import os
        env = dict(os.environ, WRAPPER_CORE_DIR=str(env_dir))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=out, env=env, capture_output=True, text=True)
        print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()[-300:])
        if r.returncode != 0:
            print(r.stdout[-2000:] + r.stderr[-1000:])
            print("ERROR: the generated project's own test failed - template bug")
            return 1

    if a.register:
        reg_dir = HERE.parent / "components" / "automation-registry"
        cmd = [sys.executable, "-m", "registry", "new-automation", a.name, "--manifest", a.register,
               "--description", a.description, "--owner", a.owner, "--owner-team", a.owner_team, "--classification", a.classification]
        r = subprocess.run(cmd, cwd=reg_dir, env=dict(os.environ, PYTHONPATH=str(HERE.parent / "components" / "wrapper-core")),
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        if r.returncode != 0:
            print("registry registration failed; add manifest-entry.yaml by hand (see README)")
            return 1
    print(f"\nnext: cd {out.name} && python -m wrapper_core dev . && git init && git add -A")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
