#!/usr/bin/env python3
"""Structural checker for GitHub Actions workflows (we cannot run actionlint here, and a broken workflow only shows up after a push).

    python tools/check_workflows.py [files...]        # default: .github/workflows/*.yml

Checks (errors fail the exit code):
  * no tabs, trailing newline; parses with wrapper_core.miniyaml (keep workflows to that simple YAML subset)
  * required keys: name, on (>=1 trigger), jobs; every job has runs-on + steps (or uses); every step has exactly one of uses/run
  * ``uses:`` actions are pinned to a version tag or SHA (never @main/@master)
  * ``${{ }}`` expressions are balanced and only reference known contexts; ``inputs.X`` exists in workflow_dispatch.inputs
  * ``secrets.*`` is never used inside an ``if:`` (not available there - a classic silent failure)
  * ``needs:`` points at real jobs
  * repo files mentioned by ``run:`` steps (tools/..., components/..., docs/..., .github/...) exist
  * a top-level or per-job ``permissions`` block exists (least privilege)
``collect_usage`` returns the secrets / variables / inputs each workflow reads, so docs can be checked for completeness.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

from wrapper_core import miniyaml  # noqa: E402

CONTEXTS = {"github", "inputs", "secrets", "vars", "env", "steps", "needs", "matrix", "runner", "job", "strategy", "jobs"}
FUNCS = {"hashFiles", "format", "contains", "startsWith", "endsWith", "success", "failure", "always", "cancelled", "fromJSON", "toJSON", "join"}
EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
PATH_RE = re.compile(r"(?<![\w$./{-])((?:tools|components|docs|\.github)/[A-Za-z0-9_./-]+\.(?:py|ps1|sh|yml|yaml|json|mjs|js|md|txt|in))")


def _steps(job: dict) -> list:
    return job.get("steps") or []


def check_text(text: str, name: str = "workflow.yml", repo: Path = ROOT) -> list[str]:
    errs: list[str] = []
    if "\t" in text:
        errs.append(f"{name}: contains tab characters")
    if not text.endswith("\n"):
        errs.append(f"{name}: missing trailing newline")
    opens, closes = text.count("${{"), text.count("}}")
    if opens > closes:
        errs.append(f"{name}: unbalanced expressions: {opens} '${{{{' vs {closes} '}}}}'")
    try:
        doc = miniyaml.loads(text)
    except miniyaml.YamlError as exc:
        return errs + [f"{name}: not parseable by the simple YAML subset: {exc}"]
    if not isinstance(doc, dict):
        return errs + [f"{name}: root is not a mapping"]
    for key in ("name", "on", "jobs"):
        if key not in doc:
            errs.append(f"{name}: missing top-level '{key}'")
    triggers = doc.get("on")
    if triggers in (None, {}, []):
        errs.append(f"{name}: 'on' has no triggers")
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict) or not jobs:
        errs.append(f"{name}: no jobs")
        jobs = {}
    if "permissions" not in doc and not all("permissions" in (j or {}) for j in jobs.values()):
        errs.append(f"{name}: declare 'permissions' (top level or on every job) - default token scope is too broad")
    dispatch_inputs: set = set()
    if isinstance(triggers, dict) and isinstance(triggers.get("workflow_dispatch"), dict):
        dispatch_inputs = set((triggers["workflow_dispatch"].get("inputs") or {}).keys())
    for jid, job in jobs.items():
        job = job or {}
        if "uses" not in job:
            if "runs-on" not in job:
                errs.append(f"{name}: job '{jid}' has no runs-on")
            steps = _steps(job)
            if not steps:
                errs.append(f"{name}: job '{jid}' has no steps")
            for i, st in enumerate(steps, 1):
                st = st or {}
                label = f"{name}: job '{jid}' step {i} ({st.get('name') or st.get('uses') or 'run'})"
                if ("uses" in st) == ("run" in st):
                    errs.append(f"{label}: needs exactly one of 'uses' / 'run'")
                if "uses" in st:
                    u = str(st["uses"])
                    if not u.startswith("./") and not re.search(r"@(v\d+(\.\d+){0,2}|[0-9a-f]{40})$", u):
                        errs.append(f"{label}: action '{u}' must be pinned to a version tag or commit SHA")
                if "if" in st and "secrets." in str(st["if"]):
                    errs.append(f"{label}: 'secrets.*' is not available in 'if:' (copy it to env first)")
                if st.get("shell") not in (None, "bash", "pwsh", "powershell", "python", "sh", "cmd"):
                    errs.append(f"{label}: unknown shell {st.get('shell')!r}")
                if "run" in st:
                    for m in PATH_RE.finditer(str(st["run"])):
                        rel = m.group(1)
                        if not any(c in rel for c in "*{$") and not (repo / rel).exists():
                            errs.append(f"{label}: references {rel}, which does not exist")
        if "if" in (job or {}) and "secrets." in str(job["if"]):
            errs.append(f"{name}: job '{jid}' uses secrets.* in 'if:'")
        needs = job.get("needs")
        for n in ([needs] if isinstance(needs, str) else (needs or [])):
            if n not in jobs:
                errs.append(f"{name}: job '{jid}' needs unknown job '{n}'")
    for m in EXPR.finditer(text):
        body = m.group(1)
        stripped = re.sub(r"'[^']*'|\"[^\"]*\"", "''", body)
        for ident in re.findall(r"(?<![\w.])([A-Za-z_][\w-]*)(?=\.|\s*\()", stripped):
            if ident not in CONTEXTS and ident not in FUNCS and ident not in ("true", "false", "null"):
                errs.append(f"{name}: expression '${{{{ {body.strip()[:50]} }}}}' uses unknown context/function '{ident}'")
        for inp in re.findall(r"\binputs\.([A-Za-z_][\w-]*)", stripped):
            if dispatch_inputs and inp not in dispatch_inputs:
                errs.append(f"{name}: expression references inputs.{inp} which is not declared under workflow_dispatch.inputs")
            if not dispatch_inputs and isinstance(triggers, dict) and "workflow_call" not in triggers:
                errs.append(f"{name}: expression references inputs.{inp} but workflow_dispatch declares no inputs")
    return errs


def collect_usage(text: str) -> dict:
    return {"secrets": sorted(set(re.findall(r"\bsecrets\.([A-Za-z_][A-Za-z0-9_]*)", text)) - {"GITHUB_TOKEN"}),
            "vars": sorted(set(re.findall(r"\bvars\.([A-Za-z_][A-Za-z0-9_]*)", text))),
            "inputs": sorted(set(re.findall(r"\binputs\.([A-Za-z_][\w-]*)", text)))}


def check_file(path: Path, repo: Path = ROOT) -> list[str]:
    with open(path, "r", encoding="utf-8", newline="") as fh:  # Path.read_text(newline=) only exists on Python 3.13+
        text = fh.read()
    return check_text(text, Path(path).name, repo)


def main(argv: list[str] | None = None) -> int:
    files = [Path(a) for a in (argv if argv is not None else sys.argv[1:])] or sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    bad = 0
    for f in files:
        errs = check_file(f)
        bad += len(errs)
        for e in errs:
            print("ERROR", e)
    print(f"checked {len(files)} workflow(s): {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
