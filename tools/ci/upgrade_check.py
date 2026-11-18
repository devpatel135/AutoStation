#!/usr/bin/env python3
"""The decisions of the upgrade smoke test (tools/ci/upgrade-smoke.ps1), kept in Python so they are unit-tested on any OS.

The PowerShell script installs the PREVIOUS released installer on a Windows runner, serves the NEW registry output locally and runs the
scheduled path; this file answers the questions it must not get wrong:

  expected   what versions must the machine have after the upgrade (the ring targets of the new manifest)?
  versions   what is installed now (VERSION markers are authoritative, exactly as wrapper_core.selfheal reads them)?
  verify     does the machine match the expectation? (exit 0 / 1, one line per problem)
  break      stage a BROKEN wrapper-core as ``wrapper-core.pending`` (its self-test fails)
  rollback   after ``bin\\apply-pending.cmd`` ran on the broken copy: was it rejected and does the old copy still stand?

    python tools/ci/upgrade_check.py expected --manifest registry-out/manifest.yaml --automation NAME --ring dev
    python tools/ci/upgrade_check.py versions --home %LOCALAPPDATA%\\AUTOMAGIX --automation NAME
    python tools/ci/upgrade_check.py verify --home HOME --automation NAME --expected expected.json
    python tools/ci/upgrade_check.py break --home HOME
    python tools/ci/upgrade_check.py rollback --home HOME --before before.json
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

BROKEN_VERSION = "99.0.0"
COMPONENTS = ("wrapper-core", "execution-framework")


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def expected(manifest: dict, automation: str, ring: str) -> dict:
    """{package: version} the ring should run after the upgrade. Raises ValueError when the manifest cannot say."""
    out: dict = {}
    for name in COMPONENTS:
        v = ((manifest.get("components") or {}).get(name) or {}).get("rings", {}).get(ring)
        if not v:
            raise ValueError(f"the new manifest has no {name} version in ring {ring}")
        out[name] = str(v)
    entry = (manifest.get("automations") or {}).get(automation)
    if not entry:
        raise ValueError(f"the new manifest does not list {automation}")
    v = (entry.get("rings") or {}).get(ring)
    if not v:
        raise ValueError(f"the new manifest has no {automation} version in ring {ring}")
    out[automation] = str(v)
    return out


def installed_versions(home: Path, automation: str) -> dict:
    return {"wrapper-core": _read(home / "wrapper-core" / "VERSION"),
            "execution-framework": _read(home / "execution-framework" / "VERSION"),
            automation: _read(home / "automations" / automation / "VERSION")}


def verify(home: Path, automation: str, want: dict) -> list[str]:
    problems: list[str] = []
    have = installed_versions(home, automation)
    for name, ver in want.items():
        if have.get(name) != ver:
            problems.append(f"{name}: installed {have.get(name) or 'nothing'}, the registry says {ver}")
    for stale in ("wrapper-core.pending", "wrapper-core.bad"):
        if (home / stale).exists():
            problems.append(f"{stale} is still present: the wrapper-core swap did not complete cleanly")
    rejected = home / "state" / "rejected-wrapper-core.txt"
    if rejected.exists():
        problems.append(f"a wrapper-core release was rejected during the upgrade ({_read(rejected)})")
    log = _read(home / "logs" / "wrapper.log") or ""
    if re.search(r"apply-pending: .*(rolled back|could not swap)", log):
        problems.append("wrapper.log reports that the wrapper-core swap was rolled back")
    return problems


def make_broken_pending(home: Path) -> str:
    """Copy the current wrapper-core to ``wrapper-core.pending`` and make its ``python -m wrapper_core selftest`` fail."""
    cur = home / "wrapper-core"
    if not (cur / "wrapper_core").is_dir():
        raise ValueError(f"no wrapper-core at {cur}")
    pend = home / "wrapper-core.pending"
    if pend.exists():
        shutil.rmtree(pend)
    shutil.copytree(cur, pend)
    (pend / "wrapper_core" / "__main__.py").write_text("import sys\nsys.exit(1)   # a deliberately broken release\n", encoding="utf-8")
    (pend / "VERSION").write_text(BROKEN_VERSION + "\n", encoding="utf-8")
    return BROKEN_VERSION


def rollback_problems(home: Path, before: dict) -> list[str]:
    problems: list[str] = []
    cur = _read(home / "wrapper-core" / "VERSION")
    if cur != before.get("wrapper-core"):
        problems.append(f"the broken wrapper-core was NOT rolled back: running {cur}, expected {before.get('wrapper-core')}")
    if (home / "wrapper-core.pending").exists():
        problems.append("wrapper-core.pending is still staged: apply-pending neither swapped nor rejected it")
    if not (home / "wrapper-core.bad").exists():
        problems.append("the broken copy was not kept as wrapper-core.bad")
    rej = _read(home / "state" / "rejected-wrapper-core.txt")
    if rej != BROKEN_VERSION:
        problems.append(f"state\\rejected-wrapper-core.txt says {rej!r}, expected {BROKEN_VERSION!r}")
    if not (home / "wrapper-core" / "wrapper_core" / "__init__.py").exists():
        problems.append("the old wrapper-core is not back in place")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("expected")
    e.add_argument("--manifest", required=True)
    e.add_argument("--automation", required=True)
    e.add_argument("--ring", default="dev")
    v = sub.add_parser("versions")
    v.add_argument("--home", required=True)
    v.add_argument("--automation", required=True)
    c = sub.add_parser("verify")
    c.add_argument("--home", required=True)
    c.add_argument("--automation", required=True)
    c.add_argument("--expected", required=True)
    b = sub.add_parser("break")
    b.add_argument("--home", required=True)
    r = sub.add_parser("rollback")
    r.add_argument("--home", required=True)
    r.add_argument("--before", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "expected":
        from wrapper_core import miniyaml
        try:
            doc = miniyaml.loads(Path(a.manifest).read_text(encoding="utf-8"))
            print(json.dumps(expected(doc, a.automation, a.ring)))
        except (OSError, ValueError) as exc:
            print(f"::error title=upgrade smoke::{exc}")
            return 1
        return 0
    if a.cmd == "versions":
        print(json.dumps(installed_versions(Path(a.home), a.automation)))
        return 0
    if a.cmd == "verify":
        problems = verify(Path(a.home), a.automation, json.loads(Path(a.expected).read_text(encoding="utf-8")))
        for p in problems:
            print(f"::error title=upgrade smoke::{p}")
        if not problems:
            print("upgrade OK: every package matches the new registry")
        return 1 if problems else 0
    if a.cmd == "break":
        print(make_broken_pending(Path(a.home)))
        return 0
    if a.cmd == "rollback":
        problems = rollback_problems(Path(a.home), json.loads(Path(a.before).read_text(encoding="utf-8")))
        for p in problems:
            print(f"::error title=upgrade smoke::{p}")
        if not problems:
            print("rollback OK: the broken wrapper-core was rejected and the old copy stands")
        return 1 if problems else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
