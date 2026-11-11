#!/usr/bin/env python3
"""AI-guided creation of a new AUTOSTATION automation (the ``autostation new`` flow).

    python tools/guided_new.py <name> --goal "..." --owner team@x.com [options]

Steps
-----
1. Validate inputs and write a temporary project.yaml
2. Scaffold boilerplate (``scaffold.py`` — deterministic, no LLM required)
3. Copy project.yaml into the new directory
4. Optional: ``wrapper_core assist design`` — 5-step design chain
5. Optional: ``wrapper_core assist test`` — LLM test generation (graceful skip)
6. Optional: ``wrapper_core assist review`` — static + LLM code review
7. Print summary with next steps

For repeatable offline runs, or when re-scaffolding from an existing project.yaml,
use ``autostation scaffold`` directly.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
RUNTIMES = ("python", "transactional", "node")


def _run(argv: list, cwd: Path | None = None) -> int:
    env = dict(os.environ)
    wc_dir = HERE.parent / "components" / "wrapper-core"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(wc_dir), env.get("PYTHONPATH", "")]))
    return subprocess.call([str(a) for a in argv], cwd=str(cwd or HERE), env=env)


def _write_yaml(path: Path, name: str, goal: str, owner: str, owner_team: str,
                classification: str, runtime: str, version: str) -> None:
    path.write_text(
        f"name: {name}\n"
        f"description: {goal}\n"
        f"owner: {owner}\n"
        f"owner_team: {owner_team}\n"
        f"classification: {classification}\n"
        f"runtime: {runtime}\n"
        f"version: {version}\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="automation name — automation-<kebab> or just <kebab>")
    p.add_argument("--goal", required=True,
                   help="one-line description of what this automation does (becomes project.yaml 'description')")
    p.add_argument("--owner", required=True, help="owning distribution list or email (not a person)")
    p.add_argument("--owner-team", default="Automation Platform", dest="owner_team")
    p.add_argument("--classification", default="internal", choices=CLASSIFICATIONS)
    p.add_argument("--runtime", default="python", choices=RUNTIMES)
    p.add_argument("--version", default="0.1.0")
    p.add_argument("--out", default=None, help="output directory (default: the automation name)")
    p.add_argument("--no-testgen", action="store_true", help="skip LLM test generation step")
    p.add_argument("--no-review", action="store_true", help="skip code review step")
    p.add_argument("--no-verify", action="store_true", help="skip running the generated test suite after scaffold")
    p.add_argument("--design", action="store_true",
                   help="run the 5-step LLM design chain after scaffold (outputs to <name>/design/)")
    a = p.parse_args(argv)

    name = a.name if a.name.startswith("automation-") else f"automation-{a.name}"
    out = Path(a.out or name).resolve()

    if out.exists() and any(out.iterdir()):
        print(f"error: {out} already exists and is not empty", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    steps_done = 0
    steps_total = 5

    # --- step 1: write temp project.yaml and scaffold ------------------------------------------------
    print(f"[1/{steps_total}] scaffolding {name} …")
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="autostation-project-")
    os.close(tmp_fd)
    tmp_yaml = Path(tmp_path)
    try:
        _write_yaml(tmp_yaml, name, a.goal, a.owner, a.owner_team, a.classification, a.runtime, a.version)
        scaffold_args = [py, HERE / "scaffold.py", str(tmp_yaml), "--out", out]
        if a.no_verify:
            scaffold_args.append("--no-verify")
        rc = _run(scaffold_args)
        if rc != 0:
            return 1
        # project.yaml is the declarative spec — belongs in the automation directory
        shutil.copy(tmp_yaml, out / "project.yaml")
    finally:
        tmp_yaml.unlink(missing_ok=True)

    steps_done += 1

    # --- step 2: LLM design chain (optional) ---------------------------------------------------------
    if a.design:
        print(f"[2/{steps_total}] running design chain (wrapper_core assist design) …")
        rc = _run([py, "-m", "wrapper_core", "assist", "design", a.goal, "--out", out / "design"])
        if rc != 0:
            print("      design chain failed — check LLM configuration; continuing without it")
    else:
        print(f"[2/{steps_total}] design chain skipped  (pass --design to run the 5-step LLM design chain)")
    steps_done += 1

    # --- step 3: LLM test generation (graceful skip) -------------------------------------------------
    main_py = out / "main.py"
    if not a.no_testgen and main_py.exists():
        print(f"[3/{steps_total}] generating tests (wrapper_core assist test) …")
        rc = _run([py, "-m", "wrapper_core", "assist", "test", str(main_py)])
        if rc != 0:
            print("      testgen unavailable (LLM not configured?) — skeleton tests are in place")
    else:
        reason = "--no-testgen" if a.no_testgen else "main.py not found"
        print(f"[3/{steps_total}] testgen skipped  ({reason})")
    steps_done += 1

    # --- step 4: static + LLM code review -----------------------------------------------------------
    if not a.no_review and main_py.exists():
        print(f"[4/{steps_total}] running code review (wrapper_core assist review) …")
        rc = _run([py, "-m", "wrapper_core", "assist", "review", str(main_py)])
        if rc != 0:
            print("      review found issues — HIGH findings are above; address before first commit")
    else:
        reason = "--no-review" if a.no_review else "main.py not found"
        print(f"[4/{steps_total}] review skipped  ({reason})")
    steps_done += 1

    # --- step 5: summary -----------------------------------------------------------------------------
    print(f"\n[{steps_total}/{steps_total}] done")
    print(f"\n  automation : {name}")
    print(f"  goal       : {a.goal}")
    print(f"  owner      : {a.owner}")
    print(f"  location   : {out}")
    print(f"\nnext: cd {name} && python -m wrapper_core dev .")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
