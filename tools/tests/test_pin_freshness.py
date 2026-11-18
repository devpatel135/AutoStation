from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import pin_freshness as pf  # noqa: E402

TODAY = date(2026, 11, 11)
PINS = {"node": "20.18.1", "python": "3.12.7", "mingit": "2.56.0.2", "tzdata": "2025.2", "playwright-core": "1.48.2"}
FTP = '<a href="3.12.7/">3.12.7/</a> <a href="3.12.10/">3.12.10/</a> <a href="3.13.1/">3.13.1/</a>'


def data(**over):
    d = {
        "node_index": [{"version": "v22.11.0"}, {"version": "v20.19.5"}, {"version": "v20.18.1"}],
        "node_schedule": {"v20": {"end": "2026-04-30"}, "v22": {"end": "2027-04-30"}},
        "python_ftp": FTP,
        "python_cycle": {"3.12": {"end_of_life": "2028-10"}},
        "mingit": {"tag_name": "v2.56.0.windows.2"},
        "playwright": {"version": "1.48.2"},
        "tzdata": {"info": {"version": "2025.2"}},
    }
    d.update(over)
    return d


def by(findings, prefix):
    return next(f for f in findings if f["name"].startswith(prefix))


def test_the_real_repository_pins_are_readable():
    pins = pf.read_pins(ROOT)
    assert set(pins) == {"node", "python", "mingit", "tzdata", "playwright-core"}
    assert all(pins.values()) and pins["node"].count(".") == 2


def test_node_past_end_of_life_is_reported_with_its_date():
    f = by(pf.evaluate(PINS, data(), TODAY), "Node 20")
    assert f["status"] == "eol" and "2026-04-30" in f["detail"] and f["latest"] == "20.19.5"


def test_node_near_end_of_life_and_supported():
    near = pf.evaluate(PINS, data(node_schedule={"v20": {"end": "2027-01-15"}}), TODAY)
    assert by(near, "Node 20")["status"] == "eol-soon"
    ok = pf.evaluate({**PINS, "node": "20.19.5"}, data(node_schedule={"v20": {"end": "2028-01-01"}}), TODAY)
    assert by(ok, "Node 20")["status"] == "current"
    patch = pf.evaluate(PINS, data(node_schedule={"v20": {"end": "2028-01-01"}}), TODAY)
    assert by(patch, "Node 20")["status"] == "outdated"


def test_python_patch_is_compared_numerically_and_eol_month_is_understood():
    f = by(pf.evaluate(PINS, data(), TODAY), "Python 3.12")
    assert f["status"] == "outdated" and f["latest"] == "3.12.10"             # 3.12.10 > 3.12.7 (not a string comparison)
    eol = by(pf.evaluate(PINS, data(python_cycle={"3.12": {"end_of_life": "2026-10"}}), TODAY), "Python 3.12")
    assert eol["status"] == "eol"


def test_mingit_tzdata_playwright():
    f = pf.evaluate(PINS, data(mingit={"tag_name": "v2.57.0.windows.1"}, tzdata={"info": {"version": "2026.1"}}, playwright={"version": "1.55.0"}), TODAY)
    assert by(f, "MinGit")["status"] == "outdated" and by(f, "MinGit")["latest"] == "2.57.0"
    assert by(f, "tzdata")["status"] == "outdated"
    pw = by(f, "playwright-core")
    assert pw["status"] == "outdated" and "pinned exactly" in pw["detail"]
    same = pf.evaluate(PINS, data(), TODAY)
    assert by(same, "MinGit")["status"] == "current" and by(same, "tzdata")["status"] == "current"


def test_unreachable_sources_are_unknown_and_never_alert():
    f = pf.evaluate(PINS, {k: None for k in data()}, TODAY)
    assert all(x["status"] in ("unknown", "unpinned") for x in f)
    assert pf.alerts(f) == []


def test_nsis_is_reported_unpinned_never_outdated():
    assert by(pf.evaluate(PINS, data(), TODAY), "NSIS")["status"] == "unpinned"


def test_issue_detail_is_one_short_string_and_severity_follows_eol():
    f = pf.evaluate(PINS, data(), TODAY)
    sev, detail = pf.issue_detail(f)
    assert sev == "high" and "Node 20" in detail and len(detail) <= 400
    medium = pf.evaluate({**PINS, "node": "20.19.5"}, data(node_schedule={"v20": {"end": "2028-01-01"}}), TODAY)
    assert pf.issue_detail(medium)[0] == "medium"


def test_markdown_table_lists_every_pin():
    md = pf.to_markdown(pf.evaluate(PINS, data(), TODAY), TODAY)
    for name in ("Node 20", "Python 3.12", "MinGit", "playwright-core", "tzdata", "NSIS"):
        assert name in md


def test_cli_with_fake_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pf, "fetch_all", lambda timeout=30: data())
    calls = []
    monkeypatch.setattr(pf, "_ops", lambda args: calls.append(args))
    out = tmp_path / "f.json"
    summ = tmp_path / "s.md"
    assert pf.main(["--today", "2026-11-11", "--json", str(out), "--summary", str(summ), "--raise-issue"]) == 0
    assert any(x["status"] == "eol" for x in json.loads(out.read_text(encoding="utf-8")))
    assert calls and calls[0][0] == "raise" and "pin-freshness" in calls[0] and "Pin freshness" in summ.read_text(encoding="utf-8")
    # everything current -> the issue is resolved
    calls.clear()
    monkeypatch.setattr(pf, "fetch_all", lambda timeout=30: data(node_schedule={"v20": {"end": "2030-01-01"}}, python_ftp='<a href="3.12.7/">'
                                                                  , node_index=[{"version": "v20.18.1"}]))
    monkeypatch.setattr(pf, "read_pins", lambda root=ROOT: PINS)
    assert pf.main(["--today", "2026-11-11", "--raise-issue"]) == 0
    assert calls and calls[0][0] == "resolve"


def test_cli_exit_2_when_a_pin_cannot_be_read(tmp_path):
    assert pf.main(["--root", str(tmp_path)]) == 2
