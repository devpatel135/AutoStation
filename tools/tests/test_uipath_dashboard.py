from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UIPATH = ROOT / "tools" / "uipath"
SAMPLES = UIPATH / "sample_projects"
MIGRATION = ROOT / "migration"
sys.path.insert(0, str(UIPATH))
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

import dashboard as dash  # noqa: E402
import inventory as inv  # noqa: E402
from wrapper_core import miniyaml  # noqa: E402


@pytest.fixture(scope="module")
def inventory():
    return inv.scan(SAMPLES)


def status(**projects) -> dict:
    return {"projects": projects}


def test_funnel_is_cumulative_and_defaults_to_inventoried(inventory):
    d = dash.compute(inventory, status(Invoice_Processing_REF={"status": "pilot"}, Portal_Scraper={"status": "scaffolded"}), {}, date(2026, 12, 1))
    counts = {f["stage"]: f["count"] for f in d["funnel"]}
    assert counts == {"inventoried": 3, "classified": 2, "scaffolded": 2, "in_dev": 1, "pilot": 1, "broad": 0, "retired": 0}
    stages = {p["name"]: p["stage"] for p in d["projects"]}
    assert stages["SAP_Desktop_Posting"] == "inventoried"                    # not in the status file


def test_sample_status_files_load_and_compute(inventory):
    status_doc = miniyaml.load(MIGRATION / "status.sample.yaml")
    program = miniyaml.load(MIGRATION / "program.sample.yaml")
    d = dash.compute(inventory, status_doc, program, date(2026, 12, 20))
    assert d["warnings"] == [] and d["totals"]["projects"] == 3 and d["totals"]["blocked"] == 2
    assert d["parity"]["GO"] == 1 and len(d["history"]) == 3
    by = {p["name"]: p for p in d["projects"]}
    assert by["Invoice_Processing_REF"]["overdue"] is True and by["Portal_Scraper"]["overdue"] is False       # 2026-12-15 < now
    assert [w["wave"] for w in d["waves"]] == ["1", "2"] and d["waves"][0]["name"] == "Pilot wave"
    assert d["licence"]["annual_potential"] == 3 * 8000 + 3 * 1500 + 20000


def test_burn_down_uses_spent_days_else_stage(inventory):
    eff = {p["name"]: p["effort_days"] for p in inventory["projects"]}["Invoice_Processing_REF"]
    d = dash.compute(inventory, status(Invoice_Processing_REF={"status": "in_dev", "effort_spent_days": 1}), {}, date(2026, 1, 1))
    assert {p["name"]: p["remaining_days"] for p in d["projects"]}["Invoice_Processing_REF"] == round(eff - 1, 1)
    d = dash.compute(inventory, status(Invoice_Processing_REF={"status": "retired", "effort_spent_days": 0}), {}, date(2026, 1, 1))
    assert {p["name"]: p["remaining_days"] for p in d["projects"]}["Invoice_Processing_REF"] == 0


def test_licence_estimate_counts_only_retired_projects(inventory):
    prog = {"licence": {"currency": "EUR", "annual_cost_per_unattended_robot": 10000, "annual_cost_per_attended_robot": 1000, "annual_orchestrator_cost": 5000},
            "robots": {"unattended_total": 4, "attended_total": 2}}
    st = status(Invoice_Processing_REF={"status": "retired", "robots_retired": {"unattended": 2, "attended": 1}},
                Portal_Scraper={"status": "pilot", "robots_retired": 3})                    # not retired: ignored
    d = dash.compute(inventory, st, prog, date(2026, 1, 1))
    lic = d["licence"]
    assert lic["unattended_retired"] == 2 and lic["attended_retired"] == 1 and lic["annual_realised"] == 21000.0
    assert lic["annual_potential"] == 4 * 10000 + 2 * 1000 + 5000 and lic["annual_remaining"] == lic["annual_potential"] - 21000 and not lic["orchestrator_cost_counted"]
    all_retired = status(**{p["name"]: {"status": "retired"} for p in inventory["projects"]})
    assert dash.compute(inventory, all_retired, prog, date(2026, 1, 1))["licence"]["orchestrator_cost_counted"] is True
    assert dash.compute(inventory, {}, {}, date(2026, 1, 1))["licence"]["configured"] is False
    assert "ESTIMATE" in d["licence"]["disclaimer"]


def test_warnings_for_bad_status_and_unknown_projects(inventory):
    d = dash.compute(inventory, status(Ghost={"status": "pilot"}, Portal_Scraper={"status": "almost-done"}), {}, date(2026, 1, 1))
    text = " | ".join(d["warnings"])
    assert "Ghost" in text and "not in the inventory" in text and "unknown status 'almost-done'" in text
    assert {p["name"]: p["stage"] for p in d["projects"]}["Portal_Scraper"] == "inventoried"


def test_html_is_escaped_and_links_are_safe(inventory, tmp_path):
    st = status(Portal_Scraper={"status": "in_dev", "owner": "<img src=x onerror=alert(1)>", "blockers": ["<script>alert(2)</script>"],
                                "parity": {"verdict": "NO-GO", "report": "javascript:alert(3)"}},
                Invoice_Processing_REF={"parity": {"verdict": "GO", "report": "https://reports.example.com/p?a=1&b=2"}})
    d = dash.compute(inventory, st, {}, date(2026, 1, 1))
    html = dash.render(d)
    assert "<script>alert(2)</script>" not in html and "<img src=x" not in html and "&lt;script&gt;alert(2)&lt;/script&gt;" in html
    assert "javascript:" not in html and 'href="https://reports.example.com/p?a=1&amp;b=2"' in html and "noopener" in html
    assert dash.safe_link("../parity/x.html") == "../parity/x.html" and dash.safe_link("data:text/html,x") is None and dash.safe_link("") is None


def test_cli_outputs_and_exit_codes(inventory, tmp_path):
    invfile = tmp_path / "inventory.json"
    invfile.write_text(json.dumps(inventory), encoding="utf-8")
    out = tmp_path / "dash"
    assert dash.main(["--inventory", str(invfile), "--status", str(MIGRATION / "status.sample.yaml"), "--program", str(MIGRATION / "program.sample.yaml"),
                      "--out", str(out), "--now", "2026-12-20"]) == 0
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "Migration funnel" in html and "ESTIMATE" in html and "<svg" in html and "overdue" in html
    data = json.loads((out / "dashboard.json").read_text(encoding="utf-8"))
    assert data["as_of"] == "2026-12-20" and data["totals"]["projects"] == 3
    assert dash.main(["--inventory", str(tmp_path / "nope.json"), "--out", str(tmp_path / "x")]) == 2
    assert dash.main(["--inventory", str(invfile), "--out", str(tmp_path / "y"), "--now", "yesterday"]) == 2
    assert dash.main(["--inventory", str(invfile), "--out", str(tmp_path / "z")]) == 0            # no status file: everything inventoried
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: &anchor 1\n", encoding="utf-8")
    assert dash.main(["--inventory", str(invfile), "--status", str(bad), "--out", str(tmp_path / "w")]) == 2


def test_deterministic_output(inventory, tmp_path):
    a = dash.render(dash.compute(inventory, {}, {}, date(2026, 1, 1)))
    b = dash.render(dash.compute(inventory, {}, {}, date(2026, 1, 1)))
    assert a == b
