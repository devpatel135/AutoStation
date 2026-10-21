from __future__ import annotations

import pytest

pytest.importorskip("jinja2")

from wrapper_core import assist, context, mockllm  # noqa: E402
from wrapper_core.testing import run_payload  # noqa: E402

BAD = '''
import os, subprocess
import wrapper_core as wrapper
password = "hunter2hunter2"

def main():
    name = input("who? ")
    subprocess.run("echo " + name, shell=True)
    wrapper.llm_call(f"classify {name}", caller="x")
    try:
        pass
    except Exception:
        pass
'''

GOOD = '''
import wrapper_core as wrapper

@wrapper.entrypoint
def main():
    for i in range(3):
        wrapper.run_item(i, lambda: None)
    wrapper.llm_call("classify", context="data", caller="a/b")
'''


def test_static_review_flags_standard_violations():
    rules = {(f["rule"], f["severity"]) for f in assist.static_review(BAD)}
    assert {("AS-01", "HIGH"), ("AS-05", "HIGH"), ("AS-09", "HIGH"), ("AS-06", "MEDIUM"), ("AS-11", "MEDIUM"), ("AS-03", "MEDIUM")} <= rules
    assert assist.static_review(GOOD) == []


def test_review_exit_codes(tmp_path):
    bad, good = tmp_path / "bad.py", tmp_path / "good.py"
    bad.write_text(BAD)
    good.write_text(GOOD)
    findings, code = assist.review(bad)
    assert code == 1 and findings[0]["severity"] == "HIGH"
    assert assist.review(good) == ([], 0)
    assert assist.review(bad, fail_on="HIGH")[1] == 1


def test_context_assembler_uses_real_api_and_standard():
    api = context.build_context(["api"])
    assert "def run_item(" in api and "def llm_call(" in api and "Escalation" in api
    std = context.build_context(["standard"])
    assert "AS-09" in std


def test_scaffold_output_actually_runs(tmp_path):
    out = tmp_path / "scaf"
    res = assist.scaffold("Pull open AP invoices and route them", out)
    assert (out / "main.py").exists() and (out / "automation.yaml").exists() and res["written"]
    r = run_payload(out, {})                      # the generated payload passes through the real lifecycle
    assert r.record["status"] == "success" and r.record["items"]["success"] == 2
    assert assist.review(out / "main.py")[1] == 0  # and satisfies the standard


def test_scaffold_refuses_path_traversal(tmp_path):
    mockllm.register(lambda c, i, d, s: {"files": [{"path": "../evil.py", "content": "x = 1"}]} if c == "assist/scaffold" else None)
    with pytest.raises(SystemExit):
        assist.scaffold("x", tmp_path / "out")
    assert not (tmp_path / "evil.py").exists()


def test_design_chain_runs_five_narrow_steps(tmp_path):
    seen = []
    mockllm.register(lambda c, i, d, s: seen.append(c) and None)
    res = assist.design("Automate the quarterly compliance report on portal Y", tmp_path / "d")
    assert res["steps"] == 5
    assert [c.split("/")[-1] for c in seen] == ["1-requirements", "2-design", "3-scaffold", "4-tests", "5-document"]
    assert (tmp_path / "d" / "design.md").read_text().startswith("# Design")
    assert (tmp_path / "d" / "scaffold" / "main.py").exists() and (tmp_path / "d" / "scaffold" / "tests" / "test_main.py").exists()


def test_design_step_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSTATION_LLM_ENDPOINT")
    with pytest.raises(SystemExit, match="design step 1"):
        assist.design("x", tmp_path / "d")


def test_gen_tests_for_function(tmp_path):
    src = tmp_path / "mod.py"
    src.write_text("def add(a, b):\n    return a + b\n")
    out = assist.gen_tests(f"{src}::add", None)
    assert out.name == "test_mod.py" and out.read_text().startswith("import main")
