"""Developer Assist CLI: ``python -m wrapper_core assist <scaffold|review|test|design>``.

Combines the context assembler, the versioned Jinja2 prompt library (``automation-builder/prompts``) and schema +
parse-retry in ``llm_call``. ``design`` is a multi-step chain: each step is a narrow prompt fed the previous output.
``review`` always runs a deterministic static pass first, so it is useful (and CI-gating) even with no LLM.

Needs ``jinja2`` (``pip install wrapper-core[assist]``) and the prompt library (set ``AUTOSTATION_PROMPTS_DIR``
or run inside the monorepo).
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

from .context import build_context
from .llm import llm_call

SCAFFOLD_SCHEMA = {"files": [{"path": "string", "content": "string"}]}
REVIEW_SCHEMA = {"findings": [{"rule": "integer", "severity": "HIGH|MEDIUM|LOW", "line": "integer?", "message": "string",
                               "suggestion": "string?"}], "summary": "string"}
TEST_SCHEMA = {"tests": "string", "covers": ["string"]}
REQ_SCHEMA = {"summary": "string", "inputs": ["string"], "outputs": ["string"], "target_systems": ["object"],
              "data_classification": "public|internal|confidential|restricted", "llm_allowed": "boolean", "schedule": "string",
              "constraints": ["string"], "risks": ["string"], "open_questions": ["string"]}
DESIGN_SCHEMA = {"approach": "string", "reason": "string", "patterns": ["string"], "components": ["object"], "items": "string",
                 "error_handling": "object", "schedule": "object", "manifest_entry": "object"}
DOC_SCHEMA = {"markdown": "string"}


# --- prompt library --------------------------------------------------------------------------------
def _find_prompts_dir() -> Path:
    env = os.environ.get("AUTOSTATION_PROMPTS_DIR")
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        cand = parent / "automation-builder" / "prompts"
        if cand.is_dir():
            return cand
        cand = parent / "components" / "automation-builder" / "prompts"
        if cand.is_dir():
            return cand
    raise SystemExit("prompt library not found; set AUTOSTATION_PROMPTS_DIR")


def _loader():
    d = _find_prompts_dir()
    spec = importlib.util.spec_from_file_location("amx_prompt_loader", d / "loader.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def render(template: str, **vars) -> str:
    base = dict(description="", api="", manifest_schema="", standard="", example="", source="", existing_tests="", previous_output="")
    base.update(vars)
    return _loader().render(template, **base)


def _template_version(template: str) -> str:
    try:
        return f"{template}@{_loader().template_version(template)}"
    except Exception:  # noqa: BLE001
        return template


# --- static review (no LLM needed) ----------------------------------------------------------------
_SECRET_RX = re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*=\s*['\"][^'\"]{6,}['\"]")


def static_review(source: str, filename: str = "<source>") -> list[dict]:
    """Deterministic compliance findings against docs/AUTOMATION_STANDARD.md (rule ids AS-xx)."""
    out: list[dict] = []

    def add(sev, rule, line, msg, fix=None):
        out.append({"severity": sev, "rule": rule, "line": line, "message": msg, **({"fix": fix} if fix else {}), "source": "static"})

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [{"severity": "HIGH", "rule": "AS-00", "line": exc.lineno, "message": f"syntax error: {exc.msg}", "source": "static"}]
    for i, line in enumerate(source.splitlines(), 1):
        if _SECRET_RX.search(line) and "os.environ" not in line:
            add("HIGH", "AS-01", i, "hard-coded credential-like literal; automations run as the user, use the user profile / Credential Manager", "remove the literal")
    uses_wrapper = "wrapper_core" in source
    has_entry = any(isinstance(n, ast.FunctionDef) and any((isinstance(d, ast.Attribute) and d.attr == "entrypoint") or (isinstance(d, ast.Name) and d.id == "entrypoint") for d in n.decorator_list) for n in ast.walk(tree))
    if not uses_wrapper:
        add("HIGH", "AS-02", 1, "does not use wrapper_core (no logging/retry/LLM seam)", "import wrapper_core as wrapper")
    elif not has_entry:
        add("MEDIUM", "AS-03", 1, "main is not wrapped in @wrapper.entrypoint, so failures are not classified for the retry/alert pipeline", "decorate main with @wrapper.entrypoint")
    if uses_wrapper and not re.search(r"log_event|log_item|track_item|run_item", source):
        add("MEDIUM", "AS-04", 1, "no telemetry calls: log_item/run_item per item and log_event for key steps are required", "call wrapper.run_item(...) per item")
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            fn = ast.unparse(n.func)
            if fn == "input":
                add("HIGH", "AS-05", n.lineno, "input() makes the automation attended; unattended by default", "take the value from a param, file or rule")
            if fn in ("os.system",) or (fn.startswith("subprocess.") and any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in n.keywords)):
                add("MEDIUM", "AS-06", n.lineno, "shell=True/os.system: injection risk with scraped data", "pass an argv list")
            if any(k.arg == "verify" and isinstance(k.value, ast.Constant) and k.value.value is False for k in n.keywords):
                add("HIGH", "AS-07", n.lineno, "TLS verification disabled", "remove verify=False")
            if fn in ("time.sleep",) and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, (int, float)) and n.args[0].value > 120:
                add("LOW", "AS-08", n.lineno, "long fixed sleep; prefer polling with a timeout", None)
            if fn.endswith("llm_call") and n.args and isinstance(n.args[0], ast.JoinedStr):
                add("HIGH", "AS-09", n.lineno, "f-string prompt: untrusted data must go in context=, never be interpolated into the prompt (prompt-injection safeguard)", "move data to context=")
            if fn.endswith("llm_call") and not any(k.arg == "caller" for k in n.keywords):
                add("LOW", "AS-10", n.lineno, "llm_call without caller= (audit trail)", "add caller='<automation>/<purpose>'")
        if isinstance(n, ast.ExceptHandler) and (n.type is None or ast.unparse(n.type) in ("Exception", "BaseException")):
            if len(n.body) == 1 and isinstance(n.body[0], ast.Pass):
                add("MEDIUM", "AS-11", n.lineno, "except: pass swallows errors the retry classifier needs to see", "let it propagate or log + re-raise")
    return out


# --- commands --------------------------------------------------------------------------------------
def _worked_example() -> str:
    return ('import wrapper_core as wrapper\n\n@wrapper.entrypoint\ndef main():\n    for row in fetch():\n'
            '        wrapper.run_item(row["id"], process, row)\n    wrapper.log_event("done", {"rows": len(rows)})\n')


def scaffold(description: str, out_dir: Path) -> dict:
    prompt = render("scaffold.j2", description=description, api=build_context(["api"]),
                    manifest_schema=build_context(["manifest"]), standard=build_context(["standard"]), example=_worked_example())
    res = llm_call(prompt, "", schema=SCAFFOLD_SCHEMA, caller="assist/scaffold", template=_template_version("scaffold.j2"), timeout_s=120)
    if not res or not res.get("files"):
        raise SystemExit("LLM returned no usable scaffold (endpoint unavailable or schema failure)")
    problems = []
    for f in res["files"]:
        if f["path"].endswith(".py"):
            try:
                ast.parse(f["content"])
            except SyntaxError as exc:
                problems.append(f"{f['path']}: {exc.msg} (line {exc.lineno})")
    if problems:  # one corrective retry, as for schema failures
        res = llm_call(prompt + "\n\nThe previous scaffold had syntax errors: " + "; ".join(problems) + "\nFix them.", "",
                       schema=SCAFFOLD_SCHEMA, caller="assist/scaffold", template=_template_version("scaffold.j2"), timeout_s=120) or res
    written = []
    for f in res["files"]:
        target = (out_dir / f["path"]).resolve()
        if out_dir.resolve() not in target.parents:
            raise SystemExit(f"refusing to write outside output dir: {f['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], encoding="utf-8")
        written.append(str(target))
    return {"written": written, "notes": res.get("notes")}


def review(path: Path, fail_on: str = "HIGH") -> tuple[list[dict], int]:
    source = path.read_text(encoding="utf-8")
    findings = static_review(source, str(path))
    prompt = render("review.j2", standard=build_context(["standard"]), api=build_context(["api"]), source=source)
    res = llm_call(prompt, "", schema=REVIEW_SCHEMA, caller="assist/review", template=_template_version("review.j2"), timeout_s=120)
    if res:
        for f in res.get("findings", []):
            findings.append({"severity": f["severity"], "rule": f"LLM-{f['rule']}", "line": f.get("line"), "message": f["message"],
                             **({"fix": f["suggestion"]} if f.get("suggestion") else {}), "source": "llm"})
    order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    findings.sort(key=lambda f: -order.get(f["severity"], 0))
    code = 1 if any(order.get(f["severity"], 0) >= order[fail_on] for f in findings) else 0
    return findings, code


def gen_tests(target: str, out_dir: Path | None) -> Path:
    file_part, _, func = target.partition("::")
    src_path = Path(file_part)
    source = src_path.read_text(encoding="utf-8")
    if func:
        tree = ast.parse(source)
        node = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func), None)
        if node is None:
            raise SystemExit(f"function {func} not found in {src_path}")
        source = ast.get_source_segment(source, node) or source
    existing = ""
    tdir = src_path.parent / "tests"
    if tdir.is_dir():
        existing = "\n\n".join(p.read_text(encoding="utf-8")[:3000] for p in sorted(tdir.glob("test_*.py"))[:2])
    prompt = render("test_gen.j2", source=source, existing_tests=existing, api=build_context(["api"]))
    res = llm_call(prompt, "", schema=TEST_SCHEMA, caller="assist/test", template=_template_version("test_gen.j2"), timeout_s=120)
    if not res:
        raise SystemExit("LLM returned no usable tests")
    ast.parse(res["tests"])
    out = (out_dir or tdir) / f"test_{src_path.stem}.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(res["tests"], encoding="utf-8")
    return out


def design(goal: str, out_dir: Path) -> dict:
    """Multi-step chain: requirements -> design -> scaffold -> tests -> document. Each step sees only the last output."""
    tpl = _template_version("design_doc.j2")
    common = dict(api=build_context(["api"]), manifest_schema=build_context(["manifest"]), standard=build_context(["standard"]))

    def step(n: int, name: str, schema, previous: str = "", **extra):
        prompt = render("design_doc.j2", step=name, description=goal, previous_output=previous, **{**common, **extra})
        res = llm_call(prompt, "", schema=schema, caller=f"assist/design/{n}-{name}", template=tpl, timeout_s=180)
        if res is None:
            raise SystemExit(f"design step {n} ({name}) failed: endpoint unavailable or output did not match the schema")
        return res

    reqs = step(1, "requirements", REQ_SCHEMA)
    plan = step(2, "design", DESIGN_SCHEMA, json.dumps(reqs, indent=2))
    code = step(3, "scaffold", SCAFFOLD_SCHEMA, json.dumps(plan, indent=2))
    scaffold_dir = out_dir / "scaffold"
    for f in code["files"]:
        target = (scaffold_dir / f["path"]).resolve()
        if scaffold_dir.resolve() not in target.parents:
            raise SystemExit(f"refusing to write outside output dir: {f['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], encoding="utf-8")
    tests = step(4, "tests", TEST_SCHEMA, json.dumps(code, indent=2))
    (scaffold_dir / "tests").mkdir(parents=True, exist_ok=True)
    (scaffold_dir / "tests" / "test_main.py").write_text(tests["tests"], encoding="utf-8")
    everything = json.dumps({"requirements": reqs, "design": plan, "scaffold_files": [f["path"] for f in code["files"]], "tests": tests["covers"]}, indent=2)
    doc = step(5, "document", DOC_SCHEMA, everything)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "design.md").write_text(doc["markdown"], encoding="utf-8")
    return {"design": str(out_dir / "design.md"), "scaffold": str(scaffold_dir), "steps": 5}




def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="wrapper_core assist")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scaffold")
    s.add_argument("description")
    s.add_argument("--out", default="./scaffold")
    r = sub.add_parser("review")
    r.add_argument("file")
    r.add_argument("--fail-on", default="HIGH", choices=["HIGH", "MEDIUM", "LOW"])
    r.add_argument("--json", action="store_true")
    t = sub.add_parser("test")
    t.add_argument("target", help="file.py or file.py::function")
    t.add_argument("--out", default=None)
    d = sub.add_parser("design")
    d.add_argument("goal")
    d.add_argument("--out", default="./design")
    a = p.parse_args(argv)
    if a.cmd == "scaffold":
        print(json.dumps(scaffold(a.description, Path(a.out)), indent=2))
        return 0
    if a.cmd == "review":
        findings, code = review(Path(a.file), a.fail_on)
        if a.json:
            print(json.dumps(findings, indent=2))
        else:
            for f in findings:
                print(f"[{f['severity']:<6}] {f['rule']} line {f.get('line') or '-'}: {f['message']}  ({f.get('source')})")
            print(f"\n{len(findings)} finding(s); exit {code}")
        return code
    if a.cmd == "test":
        print(gen_tests(a.target, Path(a.out) if a.out else None))
        return 0
    if a.cmd == "design":
        print(json.dumps(design(a.goal, Path(a.out)), indent=2))
        return 0
    return 2
