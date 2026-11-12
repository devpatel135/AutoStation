"""Phase 1 (1.4): the bundled tz data resolves every zone the registry uses, with NO host tz database."""
from __future__ import annotations

import importlib.util
import sys
import zoneinfo
from datetime import datetime
from pathlib import Path

import pytest

from wrapper_core import manifest as mf

ROOT = Path(__file__).resolve().parents[3]


def _tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def no_host_tzdb(monkeypatch):
    def boom(*a, **k):
        raise zoneinfo.ZoneInfoNotFoundError("no host tz db")
    monkeypatch.setattr(zoneinfo, "ZoneInfo", type("Z", (), {"__new__": staticmethod(boom), "from_file": zoneinfo.ZoneInfo.from_file}))
    monkeypatch.setattr(mf, "_TZ_CACHE", {})


def test_bundled_zone_resolves_without_a_host_database(no_host_tzdb):
    z = mf._tz("America/New_York", strict=True)
    winter, summer = datetime(2026, 1, 15, 12, tzinfo=z), datetime(2026, 7, 15, 12, tzinfo=z)
    assert winter.utcoffset().total_seconds() == -5 * 3600 and summer.utcoffset().total_seconds() == -4 * 3600


def test_bundle_is_the_pinned_release():
    ft = _tool("fetch_tzdata")
    assert ft.check(ft.DEST) == []


def test_every_manifest_timezone_resolves_with_only_bundled_data():
    ct = _tool("check_timezones")
    zones = ct.collect(ROOT / "components" / "automation-registry" / "manifest.yaml", ROOT / "components" / "automations")
    assert ct.check(ROOT / "components" / "automation-registry" / "manifest.yaml", ROOT / "components" / "automations") == []
    assert isinstance(zones, dict)


def test_check_flags_an_unbundled_zone(tmp_path):
    ct = _tool("check_timezones")
    m = tmp_path / "manifest.yaml"
    m.write_text("schema_version: 1\nautomations:\n  a:\n    schedule:\n      timezone: Mars/Olympus_Mons\n", encoding="utf-8")
    problems = ct.check(m, None)
    assert problems and "Mars/Olympus_Mons" in problems[0]


def test_bundled_names_are_path_safe():
    for bad in ("../x", "a/../../x", "..\\x", "C:/x", ""):
        with pytest.raises(KeyError):
            mf.bundled_tz(bad)
