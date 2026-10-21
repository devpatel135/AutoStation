#!/usr/bin/env python3
"""Compatibility smoke test (assessment: compatibility matrix + integration smoke test before ring promotion).

    python tools/compat_smoke.py [--manifest components/automation-registry/manifest.yaml] [--allow-placeholders]

Checks, failing the build (exit 1) on any problem:
  1. The manifest validates (ring order, sha256 shape, `requires` satisfiable per ring, compatibility matrix rows).
  2. The wrapper-core and execution-framework versions in THIS checkout satisfy at least one compatibility-matrix row
     (so a release cannot ship a pair the matrix does not allow).
  3. Every example automation's `requires` is satisfied by this checkout.
  4. The canonical test automation (automation-hello-world) runs end to end through the REAL run lifecycle on this
     wrapper-core: success, item telemetry, retry classification, LLM seam - the "install -> execute -> log" integration test.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
C = ROOT / "components"
sys.path.insert(0, str(C / "wrapper-core"))

from wrapper_core import manifest as mf, miniyaml, versions  # noqa: E402
from wrapper_core.testing import run_payload  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(C / "automation-registry" / "manifest.yaml"))
    ap.add_argument("--allow-placeholders", action="store_true")
    a = ap.parse_args()
    problems: list[str] = []

    wc = (C / "wrapper-core" / "VERSION").read_text().strip()
    fw = (C / "execution-framework" / "VERSION").read_text().strip()
    print(f"checkout: wrapper-core {wc}, execution-framework {fw}")

    mpath = Path(a.manifest)
    if mpath.exists():
        m = mf.load(mpath)
        errs = mf.validate(m)
        if a.allow_placeholders:
            errs = [e for e in errs if "sha256" not in e]
        problems += [f"manifest: {e}" for e in errs]
        if not mf.compatible(m, wc, fw):
            problems.append(f"manifest: wrapper-core {wc} + execution-framework {fw} is not allowed by the compatibility matrix")
    else:
        print(f"(no manifest at {mpath}; skipping manifest checks)")

    for d in sorted(list((C / "automations").iterdir()) + list((C / "examples").iterdir())):
        desc_file = d / "automation.yaml"
        if not desc_file.exists():
            continue
        req = miniyaml.load(desc_file).get("requires") or {}
        if not versions.satisfies(wc, req.get("wrapper_core")):
            problems.append(f"{d.name}: requires wrapper_core {req.get('wrapper_core')} but checkout is {wc}")
        if not versions.satisfies(fw, req.get("execution_framework")):
            problems.append(f"{d.name}: requires execution_framework {req.get('execution_framework')} but checkout is {fw}")

    r = run_payload(C / "examples" / "automation-hello-world", {"simulate": "network"},
                    error_handling={"max_retries": 3, "network_retry_wait_s": 0, "backoff_multiplier": 1.0, "skip_on_data_error": True})
    ok = r.record.get("status") == "success" and r.record["items"]["success"] == 5 and bool(r.events("retry_event")) and r.record.get("synopsis")
    print(f"lifecycle smoke: status={r.record.get('status')} items={r.record.get('items')} retries={len(r.events('retry_event'))}")
    if not ok:
        problems.append(f"lifecycle smoke failed: {r.record.get('status')} {r.record.get('error')}")

    if problems:
        print("\nCOMPATIBILITY FAILED:")
        for p in problems:
            print(" -", p)
        return 1
    print("compatibility OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
