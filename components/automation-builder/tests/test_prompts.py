from __future__ import annotations

import json
import re

import pytest

import loader

API = "log_event(event, data=None, *, sensitive=False)\nrun_item(item_id, fn, *args)\nllm_call(prompt, context='', schema=None, caller='unknown')"
SCHEMA = "rings: {dev: str, pilot: str, broad: str}\nschedule: {slots: [...]}"
STANDARD = "1. Entry point is decorated.\n2. Items go through run_item.\n3. Sensitive data is flagged.\n4. Untrusted text only in context."
SOURCE = "import wrapper_core as wrapper\n\n@wrapper.entrypoint\ndef main():\n    wrapper.log_event('x', {})\n"
TESTS = "def test_a():\n    assert True\n"

VARS = {
    "scaffold": dict(description="Pull open invoices and route them", api=API, manifest_schema=SCHEMA, example="# example automation"),
    "review": dict(standard=STANDARD, source=SOURCE),
    "test_gen": dict(source=SOURCE, existing_tests=TESTS),
}


def test_all_templates_are_listed_with_versions():
    assert set(loader.list_templates()) == {"scaffold", "review", "test_gen", "design_doc"}
    for name in loader.list_templates():
        assert re.match(r"^\d+\.\d+\.\d+$", loader.template_version(name))
        assert loader.template_meta(name)["description"]


@pytest.mark.parametrize("name", ["scaffold", "review", "test_gen"])
def test_render_has_sections_example_and_version(name):
    text = loader.render(name, **VARS[name])
    assert "## Worked example" in text, "every template carries at least one few-shot example"
    assert "## Output format" in text and "Return ONLY" in text
    assert f"{name}@{loader.template_version(name)}" in text
    assert "{{" not in text and "{%" not in text and "---\nversion" not in text   # no leaked template syntax / front matter


def test_scaffold_injects_inputs_and_rules():
    text = loader.render("scaffold", **VARS["scaffold"])
    assert "Pull open invoices" in text and "run_item(item_id, fn, *args)" in text and "rings: {dev" in text
    assert "# example automation" in text
    assert "ONLY in the `context=` argument" in text  # prompt-injection rule is part of every scaffold
    no_example = loader.render("scaffold", description="d", api=API, manifest_schema=SCHEMA)
    assert "Existing automation for style reference" not in no_example


def test_review_numbers_source_lines_and_lists_standard():
    text = loader.render("review", **VARS["review"])
    assert "1: import wrapper_core as wrapper" in text and "5:     wrapper.log_event" in text
    assert "4. Untrusted text only in context." in text
    assert "HIGH" in text and "MEDIUM" in text and "LOW" in text
    assert "Existing tests" not in text
    assert "Existing tests" in loader.render("review", standard=STANDARD, source=SOURCE, existing_tests=TESTS)


def test_worked_examples_contain_valid_json():
    for name in ("scaffold", "review", "test_gen"):
        text = loader.render(name, **VARS[name])
        blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
        assert blocks, name
        for b in blocks:
            json.loads(b)


@pytest.mark.parametrize("step,extra,marker", [
    ("requirements", {}, '"open_questions"'),
    ("design", {"previous_output": "REQ-OUTPUT", "api": API, "manifest_schema": SCHEMA}, "REQ-OUTPUT"),
    ("scaffold", {"previous_output": "DESIGN-OUTPUT", "api": API, "standard": STANDARD}, "DESIGN-OUTPUT"),
    ("tests", {"previous_output": "SCAFFOLD-OUTPUT"}, "SCAFFOLD-OUTPUT"),
    ("document", {"previous_output": "ALL-OUTPUTS"}, "ALL-OUTPUTS"),
])
def test_design_doc_each_step_is_narrow_and_self_contained(step, extra, marker):
    text = loader.render("design_doc", step=step, description="Automate the quarterly report", **extra)
    assert f"## Current step: {step}" in text and marker in text
    assert "Worked example" in text and "Return ONLY" in text
    others = [s for s in loader.DESIGN_STEPS if s != step]
    for o in others:                       # only the active step's section is rendered
        assert f"## Current step: {o}" not in text
    for block in re.findall(r"```json\n(.*?)\n```", text, re.S):
        json.loads(block)


def test_design_doc_validates_step_and_inputs():
    with pytest.raises(loader.PromptError, match="step must be"):
        loader.render("design_doc", step="deploy", description="d")
    with pytest.raises(loader.PromptError, match="previous_output"):
        loader.render("design_doc", step="design", description="d")
    with pytest.raises(loader.PromptError, match="missing required"):
        loader.render("design_doc", step="requirements")


def test_missing_variables_are_errors_never_silent():
    with pytest.raises(loader.PromptError, match="missing required variable"):
        loader.render("scaffold", description="x")
    with pytest.raises(loader.PromptError, match="missing required variable"):
        loader.render("review", source=SOURCE)
    with pytest.raises(loader.PromptError, match="unknown prompt template"):
        loader.render("nope")


def test_strict_undefined_catches_template_typos(tmp_path, monkeypatch):
    bad = tmp_path / "typo.j2"
    bad.write_text("---\nversion: 1.0.0\nrequires: []\n---\nHello {{ not_passed }}\n")
    monkeypatch.setattr(loader, "PROMPTS_DIR", tmp_path)
    with pytest.raises(loader.PromptError, match="not_passed"):
        loader.render("typo")


def test_front_matter_is_required(tmp_path, monkeypatch):
    (tmp_path / "x.j2").write_text("no front matter\n")
    monkeypatch.setattr(loader, "PROMPTS_DIR", tmp_path)
    with pytest.raises(loader.PromptError, match="front-matter"):
        loader.render("x")


def test_untrusted_content_is_data_not_template_syntax():
    # source containing jinja-looking text must render literally (it is a variable, not template code)
    text = loader.render("review", standard=STANDARD, source="x = '{{ 7*7 }}'\n")
    assert "{{ 7*7 }}" in text and "49" not in text


def test_accepts_name_with_extension():
    assert loader.render("review.j2", **VARS["review"]) == loader.render("review", **VARS["review"])
