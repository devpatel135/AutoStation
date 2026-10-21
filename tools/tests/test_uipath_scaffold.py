from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UIPATH = ROOT / "tools" / "uipath"
SAMPLES = UIPATH / "sample_projects"
ORCH = SAMPLES / "orchestrator_export"
WRAPPER_CORE = ROOT / "components" / "wrapper-core"
sys.path.insert(0, str(UIPATH))
sys.path.insert(0, str(WRAPPER_CORE))

import inventory as inv  # noqa: E402
import scaffold as sc  # noqa: E402
from wrapper_core import manifest as mf, miniyaml  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_uipath_inventory import make_project  # noqa: E402

FAKES = ["FAKE-SYNTHETIC-SECRET-1234", "P@ssw0rd-SYNTHETIC", "FAKE-SYNTHETIC-KEY-0001", "FAKE-ASSET-SECRET-VALUE-999"]


@pytest.fixture(scope="module")
def scaffolds(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sc")
    invfile = tmp / "inventory.json"
    invfile.write_text(json.dumps(inv.scan(SAMPLES, ORCH)), encoding="utf-8")
    out = tmp / "out"
    assert sc.main([str(invfile), "--out", str(out), "--owner", "ap-team@example.com", "--owner-team", "AP Automation"]) == 0
    return out, invfile


def test_one_scaffold_per_project_with_the_right_template(scaffolds):
    out, _ = scaffolds
    idx = {s["project"]: s for s in json.loads((out / "scaffold-index.json").read_text(encoding="utf-8"))["scaffolds"]}
    assert {k: v["template"] for k, v in idx.items()} == {"Invoice_Processing_REF": "transactional", "Portal_Scraper": "python", "SAP_Desktop_Posting": "python"}
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == ["automation-invoice-processing-ref", "automation-portal-scraper", "automation-sap-desktop-posting"]
    ref = out / "automation-invoice-processing-ref"
    assert "queue.open" in (ref / "main.py").read_text(encoding="utf-8") and "def perform(" in (ref / "main.py").read_text(encoding="utf-8")


def test_stubs_list_activities_and_equivalents(scaffolds):
    out, _ = scaffolds
    text = (out / "automation-invoice-processing-ref" / "main.py").read_text(encoding="utf-8")
    ast.parse(text)
    assert "def migrate_process():" in text and "SendMail x1  ->  wrapper_core.activities.mail.send_smtp" in text
    assert "migrate_init_all_settings" not in text and "migrate_get_transaction_data" not in text      # Framework/ is the template's job
    portal = (out / "automation-portal-scraper" / "main.py").read_text(encoding="utf-8")
    ast.parse(portal)
    assert "ATTENDED activities here (InputDialog)" in portal and "NO DIRECT EQUIVALENT" in portal
    assert portal.index("MIGRATION MAP") < portal.index('if __name__ == "__main__"')


def test_manifest_entry_is_valid_for_the_registry(scaffolds):
    out, _ = scaffolds
    for d in sorted(p for p in out.iterdir() if p.is_dir()):
        entry = miniyaml.loads((d / "manifest-entry.yaml").read_text(encoding="utf-8"))
        assert list(entry) == [d.name]
        m = {"schema_version": 1, "rings": ["dev", "pilot", "broad"], "components": {}, "automations": entry}
        assert mf.validate(m) == [], (d.name, mf.validate(m))
        assert entry[d.name]["status"] == "paused" and entry[d.name]["owner"] == "ap-team@example.com"
    ref = miniyaml.loads((out / "automation-invoice-processing-ref" / "manifest-entry.yaml").read_text(encoding="utf-8"))["automation-invoice-processing-ref"]
    assert ref["schedule"]["timezone"] == "America/New_York"
    assert ref["schedule"]["slots"][0]["type"] == "weekly" and ref["schedule"]["slots"][0]["params"] == {"mode": "both"}
    assert len(ref["schedule"]["slots"]) == 1                                  # the disabled sub-hourly schedule is not carried over
    assert ref["error_handling"]["max_retries"] == 3 and ref["data_classification"] == "confidential"
    assert ref["llm_allowed_classification"] == "internal"                     # confidential data: LLM gate stays shut by default
    sap = miniyaml.loads((out / "automation-sap-desktop-posting" / "manifest-entry.yaml").read_text(encoding="utf-8"))["automation-sap-desktop-posting"]
    assert sap["schedule"]["slots"][0]["id"] == "daily" and sap["schedule"]["timezone"] == "UTC"        # monthly cron: default slot + a note


def test_migration_md_and_no_secret_values_anywhere(scaffolds):
    out, _ = scaffolds
    md = (out / "automation-invoice-processing-ref" / "MIGRATION.md").read_text(encoding="utf-8")
    for needle in ("## Parity checklist", "## Cutover", "template: `transactional`", "SMTP_CredentialAsset", "Schedule notes", "R-SECRET"):
        assert needle in md
    assert "Credential Manager" in md
    assert "monthly" in (out / "automation-sap-desktop-posting" / "MIGRATION.md").read_text(encoding="utf-8")
    for f in out.rglob("*"):
        if f.is_file():
            t = f.read_text(encoding="utf-8", errors="ignore")
            for fake in FAKES:
                assert fake not in t, f"{fake} leaked into {f}"


def test_generated_projects_run_their_own_tests(scaffolds):
    out, _ = scaffolds
    env = dict(os.environ, WRAPPER_CORE_DIR=str(WRAPPER_CORE))
    for name in ("automation-invoice-processing-ref", "automation-sap-desktop-posting"):
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=out / name, env=env, capture_output=True, text=True)
        assert r.returncode == 0, (name, r.stdout[-1500:], r.stderr[-500:])


def test_browser_only_projects_get_a_node_scaffold(tmp_path):
    xaml = ('<Sequence><ui:OpenBrowser Url="https://portal.example.com"><ui:Click><ui:Click.Target>'
            '<ui:Target Selector="&lt;html app=\'msedge.exe\' /&gt;&lt;webctrl tag=\'A\' /&gt;" /></ui:Click.Target></ui:Click></ui:OpenBrowser></Sequence>')
    make_project(tmp_path / "src" / "Web_Only", "Web_Only", xaml)
    invfile = tmp_path / "i.json"
    invfile.write_text(json.dumps(inv.scan(tmp_path / "src")), encoding="utf-8")
    assert sc.main([str(invfile), "--out", str(tmp_path / "o")]) == 0
    d = tmp_path / "o" / "automation-web-only"
    assert "runtime: node" in (d / "automation.yaml").read_text(encoding="utf-8")
    js = (d / "main.js").read_text(encoding="utf-8")
    assert "fw.wrapper.entrypoint" in js and "resilientPage" in js and "MIGRATION MAP" in js and "Click x1" in js
    assert (d / "MIGRATION.md").exists() and (d / "manifest-entry.yaml").exists()


def test_cli_errors_and_force(scaffolds, tmp_path):
    out, invfile = scaffolds
    assert sc.main([str(invfile), "--out", str(out)]) == 1                                  # exists, no --force
    assert sc.main([str(invfile), "--out", str(out), "--force", "--project", "Portal_Scraper"]) == 0
    assert sc.main([str(invfile), "--out", str(tmp_path / "x"), "--project", "Nope"]) == 1
    assert sc.main([str(tmp_path / "missing.json"), "--out", str(tmp_path / "y")]) == 2
    assert sc.main([str(invfile), "--out", str(tmp_path / "z"), "--owner", "not-an-email"]) == 2


def test_names_are_valid_automation_names():
    import new_automation as na
    for n in ("Invoice_Processing_REF", "SAP Posting (v2)", "Ünïcode Proj", "123", "---"):
        assert na.NAME_RX.match(sc.automation_name(n)), n
