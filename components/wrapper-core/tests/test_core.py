from __future__ import annotations

import json

import pytest

from wrapper_core import __version__ as WC_VERSION

from wrapper_core import manifest as mf, miniyaml, schema, versions
from wrapper_core.redact import REDACTED, redact_event


# ---- miniyaml -------------------------------------------------------------------------------------
def test_yaml_roundtrip_and_subset():
    doc = {"a": 1, "b": [1, 2, {"c": "x y", "d": None}], "e": {"f": True, "g": "09:00", "h": "yes", "i": ""},
           "multi": "line1\nline2\n", "flow": [], "empty": {}}
    assert miniyaml.loads(miniyaml.dumps(doc)) == doc


def test_yaml_parses_flow_comments_and_inline_maps():
    text = """
# comment
slots:
  - {id: daily, type: daily, time: "09:00", params: {a: 1, b: [x, y]}}   # trailing
  - id: weekly
    days: [Monday, Friday]
name: 'it''s'
"""
    d = miniyaml.loads(text)
    assert d["slots"][0]["params"] == {"a": 1, "b": ["x", "y"]}
    assert d["slots"][1] == {"id": "weekly", "days": ["Monday", "Friday"]}
    assert d["name"] == "it's"


def test_yaml_rejects_anchors_and_duplicates():
    with pytest.raises(miniyaml.YamlError):
        miniyaml.loads("a: &x 1\nb: *x\n")
    with pytest.raises(miniyaml.YamlError):
        miniyaml.loads("a: 1\na: 2\n")


# ---- versions -------------------------------------------------------------------------------------
def test_version_ranges():
    assert versions.satisfies("0.1.0", ">=0.1.0,<1.0.0")
    assert not versions.satisfies("1.0.0", ">=0.1.0,<1.0.0")
    assert versions.compare("1.0.0-rc1", "1.0.0") < 0
    assert versions.satisfies("2.3.4", None)


# ---- manifest -------------------------------------------------------------------------------------
def _valid_manifest(registry):
    registry.add_component("wrapper-core", WC_VERSION)
    registry.add_component("execution-framework", "0.1.0")
    registry.add_automation("auto-a", "1.0.0", "print(1)")
    return mf.parse(miniyaml.dumps(registry.manifest))


def test_manifest_validates_clean(registry):
    assert mf.validate(_valid_manifest(registry)) == []


def test_manifest_catches_problems(registry):
    m = _valid_manifest(registry)
    m["automations"]["auto-a"]["rings"]["broad"] = "9.9.9"
    m["automations"]["auto-a"]["status"] = "paused"
    m["automations"]["auto-a"]["versions"]["1.0.0"]["sha256"] = "nope"
    errs = " | ".join(mf.validate(m))
    assert "not in versions" in errs and "paused_reason" in errs and "sha256" in errs


def test_manifest_promotion_order(registry):
    m = _valid_manifest(registry)
    m["automations"]["auto-a"]["versions"]["1.1.0"] = dict(m["automations"]["auto-a"]["versions"]["1.0.0"])
    m["automations"]["auto-a"]["rings"]["broad"] = "1.1.0"
    assert any("promote through pilot" in e for e in mf.validate(m))


def test_kill_switch_resolution(registry):
    m = _valid_manifest(registry)
    assert mf.paused(m, "auto-a") == (False, "")
    m["automations"]["auto-a"]["status"] = "paused"
    m["automations"]["auto-a"]["paused_reason"] = "bad data"
    assert mf.paused(m, "auto-a") == (True, "bad data")
    m["fleet"]["paused"] = True
    assert mf.paused(m, "auto-a")[0] is True
    assert mf.paused(m, "ghost")[0] is True  # decommissioned / unregistered


def test_maintenance_windows():
    from datetime import datetime, timezone
    sched = {"timezone": "UTC", "maintenance_windows": [{"days": ["Saturday", "Sunday"]}, {"daily": "23:00-02:00"}]}
    sat = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)   # Saturday
    mon_night = datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc)
    tue_early = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)
    mon_noon = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    assert mf.in_maintenance(sched, sat) and mf.in_maintenance(sched, mon_night) and mf.in_maintenance(sched, tue_early)
    assert mf.in_maintenance(sched, mon_noon) is None


# ---- schema ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("sch,value,ok", [
    ({"vendor": "string", "amount": "number"}, {"vendor": "x", "amount": 1.5}, True),
    ({"vendor": "string", "amount": "number"}, {"vendor": "x", "amount": "1"}, False),
    ({"kind": "a|b"}, {"kind": "c"}, False),
    ({"tags": ["string"]}, {"tags": ["x", 2]}, False),
    ({"note": "string?"}, {"note": None}, True),
    ({"type": "string", "vendor": "string"}, {"type": "invoice", "vendor": "v"}, True),   # shape with a field named "type"
    ({"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}, "required": ["n"]}, {"n": 0}, False),
    ({"type": "json_object"}, {"anything": 1}, True),
])
def test_schema_validation(sch, value, ok):
    assert schema.validate(value, sch)[0] is ok


def test_schema_example_is_valid():
    sh = {"vendor": "string", "n": "integer", "k": "x|y", "o": {"p": "boolean"}}
    assert schema.validate(schema.example(sh), sh)[0]


# ---- redaction ------------------------------------------------------------------------------------
def test_redaction_nested_keys():
    ev = {"event": "x", "data": {"customer": {"ssn": "123", "name": "n"}, "ssn": "9"}, "sensitive_keys": ["ssn"]}
    out = redact_event(ev)
    assert out["data"]["ssn"] == REDACTED and out["data"]["customer"]["ssn"] == REDACTED
    assert out["data"]["customer"]["name"] == "n" and "sensitive_keys" not in out
