#!/usr/bin/env python3
"""Bump a released package's version in EVERY file that holds it (so they can never disagree).

    python tools/bump_version.py wrapper-core            [--part patch|minor|major] [--set X.Y.Z] [--dry-run]
    python tools/bump_version.py execution-framework     [--part ...]
    python tools/bump_version.py automation-certification-site [--part ...]
    python tools/bump_version.py --check                 # exit 1 if the files of any package disagree

Released versions are immutable (docs/PIPELINES.md): any change to a package's content needs a new version. Where the version lives:

  wrapper-core         components/wrapper-core/VERSION and components/wrapper-core/wrapper_core/_version.py
  execution-framework  components/execution-framework/VERSION and components/execution-framework/package.json
  <automation>         components/automations/<automation>/automation.yaml  (version:)

``python tools/build_registry.py --check --artifacts-dir <registry-artifacts checkout>`` tells you whether a bump is needed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class BumpError(Exception):
    pass


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:   # newline="" keeps CRLF/LF exactly as it is
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _targets(name: str, repo: Path) -> List[Tuple[Path, str]]:
    comps = repo / "components"
    if name == "wrapper-core":
        return [(comps / "wrapper-core" / "VERSION", "plain"), (comps / "wrapper-core" / "wrapper_core" / "_version.py", "pyversion")]
    if name == "execution-framework":
        return [(comps / "execution-framework" / "VERSION", "plain"), (comps / "execution-framework" / "package.json", "packagejson")]
    if not NAME_RE.match(name):
        raise BumpError(f"invalid package name {name!r}")
    return [(comps / "automations" / name / "automation.yaml", "yaml")]


_PY = re.compile(r'(__version__\s*=\s*["\'])(\d+\.\d+\.\d+)(["\'])')
_YAML = re.compile(r'(?m)^(version:\s*["\']?)(\d+\.\d+\.\d+)(["\']?\s*(?:#.*)?)$')
_PKG = re.compile(r'("version"\s*:\s*")(\d+\.\d+\.\d+)(")')


def read_versions(name: str, repo: Path = ROOT) -> Dict[str, str]:
    """{relative path: version} for every file that holds this package's version."""
    out: Dict[str, str] = {}
    for path, kind in _targets(name, repo):
        if not path.is_file():
            raise BumpError(f"{path.relative_to(repo).as_posix()} not found")
        text = _read(path)
        if kind == "plain":
            ver = text.strip()
        else:
            m = {"pyversion": _PY, "yaml": _YAML, "packagejson": _PKG}[kind].search(text)
            if not m:
                raise BumpError(f"no version found in {path.relative_to(repo).as_posix()}")
            ver = m.group(2)
        out[path.relative_to(repo).as_posix()] = ver
    return out


def next_version(current: str, part: str) -> str:
    m = SEMVER.match(current)
    if not m:
        raise BumpError(f"current version {current!r} is not X.Y.Z")
    major, minor, patch = (int(x) for x in m.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def bump(name: str, part: str = "patch", set_to: Optional[str] = None, repo: Path = ROOT, dry_run: bool = False) -> Tuple[str, str, List[str]]:
    """Returns (old, new, files changed). Refuses to proceed when the package's own files already disagree."""
    versions = read_versions(name, repo)
    if len(set(versions.values())) != 1:
        raise BumpError("the version files disagree, fix them first: " + ", ".join(f"{p}={v}" for p, v in versions.items()))
    old = next(iter(versions.values()))
    new = set_to if set_to else next_version(old, part)
    if not SEMVER.match(new):
        raise BumpError(f"{new!r} is not X.Y.Z")
    if tuple(int(x) for x in new.split(".")) <= tuple(int(x) for x in old.split(".")):
        raise BumpError(f"{new} is not greater than the current {old}")
    changed: List[str] = []
    for path, kind in _targets(name, repo):
        text = _read(path)
        if kind == "plain":
            nl = "\r\n" if "\r\n" in text else "\n"
            updated = new + nl
        else:
            rx = {"pyversion": _PY, "yaml": _YAML, "packagejson": _PKG}[kind]
            updated, n = rx.subn(lambda m: m.group(1) + new + m.group(3), text, count=1)
            if n != 1:
                raise BumpError(f"could not edit {path.relative_to(repo).as_posix()}")
        if kind == "packagejson":
            json.loads(updated)                      # never leave a broken package.json behind
        if not dry_run:
            _write(path, updated)
        changed.append(path.relative_to(repo).as_posix())
    return old, new, changed


def check_all(repo: Path = ROOT) -> List[str]:
    """Problems: any package whose version files disagree."""
    problems: List[str] = []
    names = ["wrapper-core", "execution-framework"]
    adir = repo / "components" / "automations"
    if adir.is_dir():
        names += sorted(p.name for p in adir.iterdir() if (p / "automation.yaml").is_file())
    for name in names:
        try:
            versions = read_versions(name, repo)
        except BumpError as exc:
            problems.append(f"{name}: {exc}")
            continue
        if len(set(versions.values())) != 1:
            problems.append(f"{name}: version files disagree: " + ", ".join(f"{p}={v}" for p, v in versions.items()))
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("name", nargs="?", help="wrapper-core, execution-framework or an automation folder name")
    ap.add_argument("--part", choices=["patch", "minor", "major"], default="patch")
    ap.add_argument("--set", dest="set_to", help="set an exact version instead of bumping a part")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="verify every package's version files agree (CI)")
    ap.add_argument("--repo", default=str(ROOT))
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    if a.check:
        problems = check_all(repo)
        for p in problems:
            print("ERROR", p, file=sys.stderr)
        print("version files agree for every package" if not problems else f"{len(problems)} package(s) have disagreeing version files")
        return 1 if problems else 0
    if not a.name:
        ap.error("a package name (or --check) is required")
    try:
        old, new, files = bump(a.name, a.part, a.set_to, repo, a.dry_run)
    except BumpError as exc:
        print("ERROR", exc, file=sys.stderr)
        return 1
    print(f"{a.name}: {old} -> {new}" + (" (dry run)" if a.dry_run else ""))
    for f in files:
        print("  edited" if not a.dry_run else "  would edit", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
