"""Hostile-input tests for tools/project_fields.py and for everything that feeds it (scaffold, templates, the YAML emitter and reader)."""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import project_fields as pf
import scaffold as sc

HOSTILE_DESCRIPTIONS = [
    r"Copy files from C:\Nightly\new to D:\x",            # \N{ in a normal string is a SyntaxError
    r"path \x and \u12 and \U0001 and \N{BAD}",
    'say """hello""" then done',
    "say '''hello''' then done",
    "ends with a backslash \\",
    "$x ${y} $$ and %s {0}",
    "colon: and # hash and - dash",
    'quote " inside',
    "it's a test",
    "yes", "null", "123", "1.5", "~", "no",
]


def _project(description: str) -> dict:
    return {"name": "automation-hostile", "description": description, "owner": "owner@example.com", "classification": "internal"}


@pytest.mark.parametrize("description", HOSTILE_DESCRIPTIONS)
def test_yaml_round_trip_is_exact(description):
    text = pf.emit_flat_yaml(_project(description), order=("name", "description", "owner", "classification"))
    back = pf.parse_flat_yaml(text)
    assert back["description"] == description
    assert set(back) == {"name", "description", "owner", "classification"}


@pytest.mark.parametrize("description", HOSTILE_DESCRIPTIONS)
def test_real_yaml_parsers_agree_with_the_emitter(description):
    yaml = pytest.importorskip("yaml")
    text = pf.emit_flat_yaml(_project(description), order=("name", "description", "owner", "classification"))
    assert yaml.safe_load(text)["description"] == description


@pytest.mark.parametrize("description", HOSTILE_DESCRIPTIONS)
def test_generated_project_compiles_and_manifest_parses(tmp_path, description):
    project = tmp_path / "project.yaml"
    project.write_text(pf.emit_flat_yaml(_project(description), order=("name", "description", "owner", "classification")), encoding="utf-8")
    out = tmp_path / "automation-hostile"
    assert sc.main([str(project), "--out", str(out), "--no-verify"]) == 0
    for f in out.rglob("*.py"):
        py_compile.compile(str(f), doraise=True)
    yaml = pytest.importorskip("yaml")
    for f in out.rglob("*.yaml"):
        yaml.safe_load(f.read_text(encoding="utf-8"))          # every generated YAML file is valid


@pytest.mark.parametrize("value", [
    "line one\nregister: ../../evil.yaml",
    "line one\rregister: x.yaml",
    "tab\tinside",
    "bell\x07",
    "nul\x00byte",
    "caf\u00e9",
    "emoji \U0001F600",
    "x" * 201,
    "",
    "   ",
])
def test_description_must_be_one_clean_line(value):
    with pytest.raises(pf.FieldError):
        pf.check_description(value)


def test_typographic_characters_are_normalised_not_rejected():
    assert pf.check_description("it\u2019s \u201cfine\u201d \u2013 really\u2026") == 'it\'s "fine" - really...'


@pytest.mark.parametrize("name", ["", "x", "Automation-X", "automation-", "automation--x", "automation-x y", "../automation-x",
                                  "automation-x/../y", "automation-x\ny", "automation-" + "a" * 60])
def test_bad_names_are_refused(name):
    with pytest.raises(pf.FieldError):
        pf.check_name(name)


@pytest.mark.parametrize("owner", ["", "nobody", "a@b", "a b@example.com", "a@example.com\nb@example.com", "<x>@example.com"])
def test_bad_owners_are_refused(owner):
    with pytest.raises(pf.FieldError):
        pf.check_owner(owner)


@pytest.mark.parametrize("register", ["-x.yaml", "--help", "manifest.txt", "a.yaml\nb.yaml", ""])
def test_bad_register_paths_are_refused(register):
    with pytest.raises(pf.FieldError):
        pf.check_register(register)


def test_fallback_parser_cannot_be_given_a_second_key():
    # a newline inside a quoted value is not valid in a one-line scalar and is rejected, not split into keys
    with pytest.raises(pf.FieldError):
        pf.parse_flat_yaml('name: automation-x\ndescription: "a\nregister: ../evil.yaml"\n')
    with pytest.raises(pf.FieldError):
        pf.parse_flat_yaml("name: a\nname: b\n")                       # duplicate key
    with pytest.raises(pf.FieldError):
        pf.parse_flat_yaml("name: a\n  nested: b\n")                   # nesting
    with pytest.raises(pf.FieldError):
        pf.parse_flat_yaml('description: "unterminated\n')
    with pytest.raises(pf.FieldError):
        pf.parse_flat_yaml("not a key value line\n")


def test_load_project_rejects_register_injection(tmp_path):
    p = tmp_path / "project.yaml"
    p.write_text('name: automation-x\ndescription: "a"\nowner: o@example.com\nclassification: internal\nregister: "../evil.txt"\n',
                 encoding="utf-8")
    with pytest.raises(sc.ProjectError):
        sc.load_project(p)


def test_emitter_refuses_bad_keys():
    with pytest.raises(pf.FieldError):
        pf.emit_flat_yaml({"a: b\nregister": "x"})


def test_yaml_scalar_quotes_things_a_yaml_parser_would_retype():
    for risky in ("yes", "No", "null", "123", "1.5", "~", "a #b", "trailing "):
        assert pf.yaml_scalar(risky).startswith('"'), risky
    assert pf.yaml_scalar("simple-name_1") == "simple-name_1"
    assert pf.yaml_scalar(30) == "30"
