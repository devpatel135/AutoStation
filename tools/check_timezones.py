#!/usr/bin/env python3
"""Release check: every time zone named in the registry manifest (and in each automation.yaml) must resolve using ONLY the tz data
bundled in wrapper-core - the embedded Windows Python has no tz database of its own, so a zone that is not bundled would make the
schedule fail to register on every endpoint.

    python tools/check_timezones.py [--manifest components/automation-registry/manifest.yaml]

Exit 0 = all resolve, 1 = at least one does not (the names are printed). Run by CI via the test-suite (tests/test_tzdata_bundle.py).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

from wrapper_core import manifest as mf, miniyaml  # noqa: E402


def zones_in(doc) -> set[str]:
    """Every value of a ``timezone`` key anywhere in a parsed YAML document."""
    found: set[str] = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "timezone" and isinstance(v, str) and v.strip():
                    found.add(v.strip())
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(doc)
    return found


def collect(manifest: Path, automations_dir: Path | None) -> dict[str, list[str]]:
    where: dict[str, list[str]] = {}
    sources = [manifest]
    if automations_dir and automations_dir.is_dir():
        sources += sorted(automations_dir.glob("*/automation.yaml"))
    for src in sources:
        if not src.is_file():
            continue
        try:
            doc = miniyaml.load(src)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN cannot parse {src}: {exc}")
            continue
        for z in zones_in(doc):
            where.setdefault(z, []).append(str(src.relative_to(ROOT)) if src.is_relative_to(ROOT) else str(src))
    return where


def check(manifest: Path, automations_dir: Path | None) -> list[str]:
    problems = []
    for zone, files in sorted(collect(manifest, automations_dir).items()):
        if not mf.tz_resolves(zone, bundled_only=True):
            problems.append(f"time zone {zone!r} (used in {', '.join(files)}) is not in wrapper-core's bundled tz data")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", default=str(ROOT / "components" / "automation-registry" / "manifest.yaml"))
    ap.add_argument("--automations", default=str(ROOT / "components" / "automations"))
    a = ap.parse_args(argv)
    problems = check(Path(a.manifest), Path(a.automations))
    for p in problems:
        print("FAIL", p)
    print("all manifest time zones resolve with bundled data" if not problems else f"{len(problems)} unresolvable time zone(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
