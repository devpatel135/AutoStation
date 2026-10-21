from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UIPATH = ROOT / "tools" / "uipath"
SAMPLES = UIPATH / "sample_projects"
ORCH = SAMPLES / "orchestrator_export"
sys.path.insert(0, str(UIPATH))

import common  # noqa: E402
import inventory as inv  # noqa: E402
import xlsxmini  # noqa: E402

HEADER = ('<Activity mc:Ignorable="sap" x:Class="Main" xmlns="http://schemas.microsoft.com/netfx/2009/xaml/activities" '
          'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
          'xmlns:sap="http://schemas.microsoft.com/netfx/2009/xaml/activities/presentation" '
          'xmlns:ui="http://schemas.uipath.com/workflow/activities" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">')


def make_project(root: Path, name: str, xaml: str, config: dict | None = None, extra: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.json").write_text(json.dumps({"name": name, "main": "Main.xaml", "expressionLanguage": "VisualBasic"}), encoding="utf-8")
    (root / "Main.xaml").write_text(HEADER + xaml + "</Activity>", encoding="utf-8")
    for rel, body in (extra or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(HEADER + body + "</Activity>", encoding="utf-8")
    if config:
        xlsxmini.write_xlsx(root / "Data" / "Config.xlsx", config)
    return root


@pytest.fixture(scope="module")
def sample_inv():
    return inv.scan(SAMPLES, ORCH)


def by_name(i, name):
    return next(p for p in i["projects"] if p["name"] == name)


# --- the three synthetic projects ---------------------------------------------------------------------------------
def test_samples_are_found_and_classified(sample_inv):
    assert [p["name"] for p in sample_inv["projects"]] == ["Invoice_Processing_REF", "Portal_Scraper", "SAP_Desktop_Posting"]
    got = {p["name"]: p["pattern"]["recommended"] for p in sample_inv["projects"]}
    assert got == {"Invoice_Processing_REF": "queue-transactional", "Portal_Scraper": "needs-redesign", "SAP_Desktop_Posting": "windows-uia"}


def test_reframework_detection_and_config(sample_inv):
    ref = by_name(sample_inv, "Invoice_Processing_REF")
    assert ref["reframework"]["detected"] and "Framework/ folder" in ref["reframework"]["evidence"]
    assert len(ref["reframework"]["states"]) == 4
    assert not by_name(sample_inv, "Portal_Scraper")["reframework"]["detected"]
    assert ref["config"]["found"] and ref["config"]["settings"]["OrchestratorQueueName"] == "InvoiceQueue"
    assert ref["config"]["constants"]["MaxConsecutiveSystemExceptions"] == "3"
    assert "SMTP_CredentialAsset" in ref["config"]["assets"]


def test_queue_and_asset_references_are_resolved_through_config(sample_inv):
    ref = by_name(sample_inv, "Invoice_Processing_REF")
    assert ref["queues"] == ["ExceptionsQueue", "InvoiceQueue"]            # one literal, one in_Config("OrchestratorQueueName")
    assert "PostingCompanyCode" in ref["assets"] and "SMTP_CredentialAsset" in ref["assets"]
    assert by_name(sample_inv, "SAP_Desktop_Posting")["credentials"] == ["SAP_Posting_Service"]


def test_selectors_and_systems(sample_inv):
    portal = by_name(sample_inv, "Portal_Scraper")
    assert portal["selectors"]["browser"] == 4 and portal["selectors"]["desktop"] == 0
    assert "host:vendor-portal.example.com" in portal["systems"] and "app:chrome.exe" in portal["systems"]
    sap = by_name(sample_inv, "SAP_Desktop_Posting")
    assert sap["selectors"]["sap"] == 2 and sap["selectors"]["desktop"] == 1
    assert "app:saplogon.exe" in sap["systems"]


def test_categories(sample_inv):
    portal = by_name(sample_inv, "Portal_Scraper")["categories"]
    assert portal["image_citrix"] == 1 and portal["browser_ui"] >= 5
    sap = by_name(sample_inv, "SAP_Desktop_Posting")["categories"]
    assert sap["sap_terminal"] >= 3 and sap["desktop_ui"] >= 2
    ref = by_name(sample_inv, "Invoice_Processing_REF")["categories"]
    assert ref["queues_assets"] >= 4 and ref["excel_data"] >= 4 and ref["mail"] == 1 and ref["http_api"] == 1


def test_risks(sample_inv):
    ids = lambda n: {r["id"] for r in by_name(sample_inv, n)["risks"]}  # noqa: E731
    assert {"R-SECRET", "R-ORPHAN", "R-EXCEL-INTEROP"} <= ids("Invoice_Processing_REF")
    assert {"R-IMAGE", "R-ATTENDED", "R-NOEXC"} <= ids("Portal_Scraper")
    assert {"R-SAP", "R-DESKTOP", "R-ATTENDED"} <= ids("SAP_Desktop_Posting")
    assert "R-NOEXC" not in ids("Invoice_Processing_REF")                 # TryCatch + RetryScope present
    assert by_name(sample_inv, "Invoice_Processing_REF")["orphans"] == ["Unused_Old_Export.xaml"]
    assert by_name(sample_inv, "Invoice_Processing_REF")["retry_numbers"] == [3]


def test_equivalents_use_real_autostation_names(sample_inv):
    rows = {e["activity"]: e["autostation"] for e in by_name(sample_inv, "Invoice_Processing_REF")["equivalents"]}
    assert "activities.mail.send_smtp" in rows["SendMail"] and "activities.excel.read_xlsx" in rows["ExcelReadRange"]
    assert "queue.open(name).next()" in rows["GetQueueItem"] and "run_with_retry" in rows["RetryScope"]
    portal = {e["activity"]: e["autostation"] for e in by_name(sample_inv, "Portal_Scraper")["equivalents"]}
    assert "NO DIRECT EQUIVALENT" in portal["ClickImage"] and "ATTENDED" in portal["InputDialog"]


# --- secrets -----------------------------------------------------------------------------------------------------
FAKES = ["FAKE-SYNTHETIC-SECRET-1234", "P@ssw0rd-SYNTHETIC", "FAKE-SYNTHETIC-KEY-0001", "FAKE-ASSET-SECRET-VALUE-999"]


def test_secrets_are_found_by_location_and_never_leak(sample_inv, tmp_path):
    ref = by_name(sample_inv, "Invoice_Processing_REF")
    kinds = {f["kind"] for f in ref["secret_findings"]}
    assert {"attribute", "variable-default", "config-value"} <= kinds
    assert ref["config"]["settings"]["SMTP_Password"] == common.REDACTED
    out = tmp_path / "o"
    assert inv.main([str(SAMPLES), "--out", str(out), "--orchestrator-export", str(ORCH)]) == 0
    blob = "".join((out / f).read_text(encoding="utf-8") for f in ("inventory.json", "inventory.csv", "inventory.html"))
    for fake in FAKES:
        assert fake not in blob, f"{fake} leaked into the outputs"


def test_redact_text():
    assert "hunter2" not in common.redact_text("Server=x;Password=hunter2;User=bob")
    assert "s3cret" not in common.redact_text("postgres://bob:s3cret@db.example/x")
    assert "abcdef123456789" not in common.redact_text("Authorization: Bearer abcdef123456789")
    assert common.redact_text("nothing to see") == "nothing to see"
    assert common.looks_like_secret_name("smtpPassword") and not common.looks_like_secret_name("OrchestratorAssetName_Password")


def test_asset_values_in_orchestrator_export_are_never_read(sample_inv):
    blob = json.dumps(sample_inv)
    assert "FAKE-ASSET-SECRET-VALUE-999" not in blob and "1000" not in json.dumps(sample_inv["orchestrator"])
    assert "Legacy_Password" not in blob  # that asset is not referenced by any scanned project


# --- orchestrator merge -----------------------------------------------------------------------------------------------
def test_orchestrator_schedules_become_slots(sample_inv):
    ref = by_name(sample_inv, "Invoice_Processing_REF")["orchestrator"]
    weekdays = next(s for s in ref["schedules"] if s["name"] == "Invoice Performer Weekdays")
    assert weekdays["slot"] == {"type": "weekly", "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], "time": "08:00"}
    frequent = next(s for s in ref["schedules"] if s["name"] == "Invoice Performer Frequent")
    assert frequent["slot"] is None and "poll slot" in frequent["note"] and frequent["enabled"] is False
    assert by_name(sample_inv, "Portal_Scraper")["orchestrator"]["schedules"][0]["slot"] == {"type": "daily", "time": "02:30"}
    monthly = by_name(sample_inv, "SAP_Desktop_Posting")["orchestrator"]["schedules"][0]
    assert monthly["slot"] is None and "monthly" in monthly["note"]
    assert ref["queues"][1]["max_retries"] == 3 and ref["queues"][1]["in_export"]
    assert sample_inv["orchestrator"]["unmatched_schedules"] == ["Legacy Weekly"]
    assert sample_inv["orchestrator"]["unmatched_processes"] == ["Legacy_Unused_Process"]


@pytest.mark.parametrize("cron,expected", [
    ("0 0 9 ? * * *", {"type": "daily", "time": "09:00"}),
    ("0 30 18 * * ?", {"type": "daily", "time": "18:30"}),
    ("30 6 * * *", {"type": "daily", "time": "06:30"}),
    ("0 0 7 ? * MON,WED,FRI *", {"type": "weekly", "days": ["Monday", "Wednesday", "Friday"], "time": "07:00"}),
    ("0 15 7 ? * 2-6 *", {"type": "weekly", "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], "time": "07:15"}),   # quartz numbers
    ("0 7 * * 1-5", {"type": "weekly", "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], "time": "07:00"}),     # unix numbers
    ("0 0 0/4 ? * * *", {"type": "hourly", "every": 4}),
    ("0 5 * ? * * *", {"type": "hourly", "every": 1}),
])
def test_cron_to_slot(cron, expected):
    slot, note = common.cron_to_slot(cron)
    assert slot == expected and note == ""


@pytest.mark.parametrize("cron", ["0 0/15 * ? * * *", "0 0 6 1 * ? *", "nonsense", "0 0 6 ? JAN * *", ""])
def test_cron_without_a_faithful_slot(cron):
    slot, note = common.cron_to_slot(cron)
    assert slot is None and note


# --- robustness ----------------------------------------------------------------------------------------------------------
def test_malformed_and_hostile_xaml_do_not_crash(tmp_path):
    p = make_project(tmp_path / "p", "Broken", "<Sequence>", extra=None)
    (p / "Main.xaml").write_text("<Activity><Sequence></Activity>", encoding="utf-8")           # not well formed
    (p / "Bomb.xaml").write_text('<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><Activity>&x;</Activity>', encoding="utf-8")
    i = inv.scan(tmp_path / "p")
    proj = i["projects"][0]
    errs = {e["file"]: e["error"] for e in proj["parse_errors"]}
    assert set(errs) == {"Main.xaml", "Bomb.xaml"} and "DTD" in errs["Bomb.xaml"]
    assert "R-PARSE" in {r["id"] for r in proj["risks"]}


def test_project_without_workflows_and_bad_project_json(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    (d / "project.json").write_text("{not json", encoding="utf-8")
    proj = inv.scan(tmp_path)["projects"][0]
    assert proj["name"] == "empty" and proj["workflow_count"] == 0 and proj["parse_errors"][0]["file"] == "project.json"
    assert proj["pattern"]["recommended"] == "headless-api"


def test_nupkg_input_and_zip_slip(tmp_path):
    src = make_project(tmp_path / "src", "Packed", "<Sequence><ui:ReadCsv FilePath=\"a.csv\" /></Sequence>")
    pkg = tmp_path / "Packed.nupkg"
    with zipfile.ZipFile(pkg, "w") as z:
        for f in src.rglob("*"):
            if f.is_file():
                z.write(f, "content/" + f.relative_to(src).as_posix())
    i = inv.scan(pkg)
    assert i["projects"][0]["name"] == "Packed" and i["projects"][0]["activity_counts"]["ReadCsv"] == 1
    evil = tmp_path / "evil.nupkg"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(ValueError):
        inv.scan(evil)


def test_dynamic_invokes_and_missing_workflows(tmp_path):
    make_project(tmp_path / "p", "Dyn", '<Sequence><ui:InvokeWorkflowFile WorkflowFileName="[strFile]" /><ui:InvokeWorkflowFile WorkflowFileName="Gone.xaml" /></Sequence>')
    proj = inv.scan(tmp_path / "p")["projects"][0]
    assert proj["dynamic_invokes"] == 1 and proj["missing_invokes"] == ["Gone.xaml"]
    assert {"R-DYNAMIC", "R-MISSING"} <= {r["id"] for r in proj["risks"]}


def test_classification_hint_is_whole_word(tmp_path):
    make_project(tmp_path / "a", "Invoice_Processing", "<Sequence/>")                                  # "processing" must NOT match "ssn"
    make_project(tmp_path / "b", "Payroll_Export", "<Sequence/>")
    make_project(tmp_path / "c", "Patient_Intake", "<Sequence/>")
    hints = {p["name"]: p["classification_hint"]["level"] for p in inv.scan(tmp_path)["projects"]}
    assert hints == {"Invoice_Processing": "confidential", "Patient_Intake": "restricted", "Payroll_Export": "confidential"}
    make_project(tmp_path / "d", "Weather_Report", "<Sequence/>")
    assert inv.scan(tmp_path / "d")["projects"][0]["classification_hint"]["level"] == "internal"


def test_pattern_decision_rules(tmp_path):
    cases = {
        "Api": '<Sequence><ui:HttpClient EndPoint="https://x.example/api" /></Sequence>',
        "Web": '<Sequence><ui:OpenBrowser Url="https://x.example"><ui:Click><ui:Click.Target><ui:Target Selector="&lt;html app=\'msedge.exe\' /&gt;&lt;webctrl tag=\'A\' /&gt;" /></ui:Click.Target></ui:Click></ui:OpenBrowser></Sequence>',
        "Citrix": "<Sequence><ui:ClickImage /></Sequence>",
        "Mainframe": "<Sequence><ui:TerminalSendText /></Sequence>",
        "Queue": "<Sequence><ui:GetQueueItem QueueName=\"Q1\" /></Sequence>",
    }
    for name, xaml in cases.items():
        make_project(tmp_path / name, name, xaml)
    got = {p["name"]: p["pattern"]["recommended"] for p in inv.scan(tmp_path)["projects"]}
    assert got == {"Api": "headless-api", "Web": "playwright-browser", "Citrix": "needs-redesign", "Mainframe": "needs-redesign", "Queue": "queue-transactional"}


# --- scoring -------------------------------------------------------------------------------------------------------------
def test_effort_and_complexity_are_monotonic_and_tunable(sample_inv, tmp_path):
    scores = {p["name"]: p["complexity"]["score"] for p in sample_inv["projects"]}
    assert scores["Invoice_Processing_REF"] > scores["SAP_Desktop_Posting"] > scores["Portal_Scraper"] > 0
    assert all(p["effort_days"] * 2 == int(p["effort_days"] * 2) for p in sample_inv["projects"])        # half-day rounding
    bigger = inv.scan(SAMPLES, per_point=0.5)
    assert bigger["portfolio"]["effort_days"] > sample_inv["portfolio"]["effort_days"]
    assert bigger["effort_model"]["per_point"] == 0.5


def test_inventory_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert inv.main([str(SAMPLES), "--out", str(a)]) == 0 and inv.main([str(SAMPLES), "--out", str(b)]) == 0
    for f in ("inventory.json", "inventory.csv", "inventory.html"):
        assert (a / f).read_bytes() == (b / f).read_bytes()


def test_csv_shape(sample_inv, tmp_path):
    inv.write_csv(tmp_path / "i.csv", sample_inv)
    lines = (tmp_path / "i.csv").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and lines[0].startswith("project,path,pattern,complexity_score")


def test_html_escapes_everything(tmp_path):
    make_project(tmp_path / "x", "<script>alert(1)</script>", '<Sequence><ui:InputDialog DisplayName="&lt;img src=x onerror=alert(2)&gt;" /></Sequence>')
    out = tmp_path / "o"
    assert inv.main([str(tmp_path / "x"), "--out", str(out)]) == 0
    html = (out / "inventory.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# --- CLI -------------------------------------------------------------------------------------------------------------------
def test_cli_exit_codes(tmp_path):
    run = lambda *a: subprocess.run([sys.executable, str(UIPATH / "inventory.py"), *map(str, a)], capture_output=True, text=True)  # noqa: E731
    ok = run(SAMPLES, "--out", tmp_path / "o", "--orchestrator-export", ORCH)
    assert ok.returncode == 0 and "3 project(s)" in ok.stdout and (tmp_path / "o" / "inventory.html").exists()
    assert run(tmp_path / "nope", "--out", tmp_path / "x").returncode == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(empty, "--out", tmp_path / "y").returncode == 1
    assert run(SAMPLES, "--out", tmp_path / "z", "--no-html").returncode == 0 and not (tmp_path / "z" / "inventory.html").exists()


# --- xlsx reader -------------------------------------------------------------------------------------------------------------
def test_xlsx_roundtrip_and_shared_strings(tmp_path):
    f = tmp_path / "t.xlsx"
    xlsxmini.write_xlsx(f, {"A": [["id", "name", "n"], [1, "x & <y>", 2.5], [2, "z", True]], "B": [["k"], ["v"]]})
    book = xlsxmini.read_xlsx(f)
    assert list(book) == ["A", "B"] and book["A"][1] == [1, "x & <y>", 2.5] and book["A"][2][2] is True
    assert xlsxmini.read_table(f, "A")[0] == {"id": 1, "name": "x & <y>", "n": 2.5}
    # a workbook that uses sharedStrings (what Excel writes)
    shared = tmp_path / "s.xlsx"
    with zipfile.ZipFile(shared, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Name</t></si><si><r><t>Al</t></r><r><t>ice</t></r></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row><row r="2"><c r="B2" t="s"><v>1</v></c></row></sheetData></worksheet>')
    assert xlsxmini.read_xlsx(shared)["S"] == [["Name"], ["", "Alice"]]


def test_read_table_formats(tmp_path):
    (tmp_path / "a.csv").write_text("id,v\n1,x\n", encoding="utf-8-sig")
    (tmp_path / "a.json").write_text(json.dumps({"rows": [{"id": 1, "v": "x"}]}), encoding="utf-8")
    assert xlsxmini.read_table(tmp_path / "a.csv") == [{"id": "1", "v": "x"}] and xlsxmini.read_table(tmp_path / "a.json") == [{"id": 1, "v": "x"}]
    with pytest.raises(ValueError):
        xlsxmini.read_table(tmp_path / "a.pdf")
