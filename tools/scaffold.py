#!/usr/bin/env python3
"""Read project.yaml and emit the automation boilerplate (deterministic, no LLM required).

    python tools/scaffold.py [project.yaml]  [--out dir]  [--no-verify]  [--register manifest.yaml]

project.yaml keys
-----------------
  name           automation-<kebab>                          required
  description    one-line summary                            required
  owner          team-dl@example.com                         required
  classification public | internal | confidential | restricted  required
  owner_team     human-readable team name                    optional  [Automation Platform]
  runtime        python | transactional | node               optional  [python]
  version        x.y.z                                       optional  [0.1.0]
  timeout_s      integer seconds                             optional  [1800]
  register       path/to/manifest.yaml                       optional  (register with registry after scaffold)

This is the fast, offline path.  The LLM-guided design chain lives in
``python -m wrapper_core assist design``.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))
import project_fields as pf  # noqa: E402  (validation + safe YAML/template values shared with guided_new, new_automation and the Studio)
from project_fields import CLASSIFICATIONS, NAME_RX, RUNTIMES, VERSION_RX  # noqa: E402,F401

_REQUIRED = {"name", "description", "owner", "classification"}
_DEFAULTS: dict[str, object] = {
    "owner_team": "Automation Platform",
    "runtime":    "python",
    "version":    "0.1.0",
    "timeout_s":  1800,
}
_KNOWN = _REQUIRED | set(_DEFAULTS) | {"register"}


class ProjectError(ValueError):
    pass


def _load_yaml(path: Path) -> dict:
    """project.yaml is a flat 'key: value' file. It is read by the strict parser in project_fields (never PyYAML), so the result does not
    depend on what is installed and a value can never smuggle in another key."""
    try:
        return pf.parse_flat_yaml(path.read_text(encoding="utf-8"))
    except pf.FieldError as exc:
        raise ProjectError(f"project.yaml: {exc}") from None


def load_project(path: Path) -> dict:
    """Load, validate and fill defaults for a project.yaml.  Raises ProjectError on invalid input."""
    raw = _load_yaml(path)

    unknown = set(raw) - _KNOWN
    if unknown:
        raise ProjectError(f"unknown keys in project.yaml: {sorted(unknown)}")

    missing = _REQUIRED - set(raw)
    if missing:
        raise ProjectError(f"project.yaml is missing required keys: {sorted(missing)}")

    proj: dict = {**_DEFAULTS, **{k: v for k, v in raw.items() if v is not None}}

    try:
        proj["name"] = pf.check_name(proj["name"])
        proj["description"] = pf.check_description(proj["description"])
        proj["owner"] = pf.check_owner(proj["owner"])
        proj["owner_team"] = pf.check_team(proj["owner_team"])
        proj["classification"] = pf.check_classification(proj["classification"])
        proj["runtime"] = pf.check_runtime(proj["runtime"])
        proj["version"] = pf.check_version(proj["version"])
        proj["timeout_s"] = pf.check_timeout(proj["timeout_s"])
        if "register" in proj:
            proj["register"] = pf.check_register(proj["register"])
    except pf.FieldError as exc:
        raise ProjectError(str(exc)) from None

    return proj


def scaffold(proj: dict, out: Path) -> list[Path]:
    """Emit the boilerplate files for *proj* into *out* using the existing template trees.

    Delegates to new_automation.render_tree so the two paths stay in sync automatically.
    """
    sys.path.insert(0, str(HERE))
    from new_automation import render_tree  # type: ignore[import-not-found]  # noqa: PLC0415

    runtime = proj["runtime"]
    src = HERE / "templates" / f"automation-{runtime}"
    if not src.is_dir() or not any(src.rglob("*.tmpl")):
        raise ProjectError(f"no template {runtime!r} found (looked in {src})")

    values = pf.template_values(proj)
    return render_tree(src, out, values)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project_yaml", nargs="?", default="project.yaml",
                   help="path to project.yaml (default: ./project.yaml)")
    p.add_argument("--out", default=None,
                   help="output directory (default: the automation name from project.yaml)")
    p.add_argument("--no-verify", action="store_true",
                   help="skip running the generated test suite after scaffolding")
    p.add_argument("--register", default=None,
                   help="manifest.yaml to register the automation in (overrides project.yaml 'register' key)")
    a = p.parse_args(argv)

    proj_path = Path(a.project_yaml)
    if not proj_path.exists():
        print(f"error: {proj_path} not found", file=sys.stderr)
        return 1

    try:
        proj = load_project(proj_path)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = Path(a.out or proj["name"]).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"error: {out} already exists and is not empty", file=sys.stderr)
        return 1

    try:
        files = scaffold(proj, out)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"created {len(files)} files in {out}")
    for f in files:
        print("  ", f.relative_to(out))

    if not a.no_verify and proj["runtime"] in ("python", "transactional"):
        env_dir = HERE.parent / "components" / "wrapper-core"
        env = dict(os.environ, WRAPPER_CORE_DIR=str(env_dir))
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=out, env=env, capture_output=True, text=True,
        )
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()[-300:]
        print(last)
        if r.returncode != 0:
            print(r.stdout[-2000:] + r.stderr[-1000:])
            print("ERROR: the generated project's own test failed — template bug")
            return 1

    register = a.register or proj.get("register")
    if register:
        reg_dir = HERE.parent / "components" / "automation-registry"
        cmd = [
            sys.executable, "-m", "registry", "new-automation",
            proj["name"], "--manifest", register,
            "--description", proj["description"],
            "--owner", proj["owner"],
            "--owner-team", proj["owner_team"],
            "--classification", proj["classification"],
        ]
        r = subprocess.run(
            cmd, cwd=reg_dir,
            env=dict(os.environ, PYTHONPATH=str(HERE.parent / "components" / "wrapper-core")),
            capture_output=True, text=True,
        )
        print(r.stdout.strip() or r.stderr.strip())
        if r.returncode != 0:
            print("registry registration failed; add manifest-entry.yaml by hand (see README)")
            return 1

    print(f"\nnext: cd {proj['name']} && python -m wrapper_core dev . && git init && git add -A")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
