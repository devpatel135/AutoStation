"""Evidence artefacts (ingest, dashboard, never rendered) and the programme/ROI dashboard."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from automation_logging import analytics, dashboard, ingest, pipeline, program
from automation_logging.common import KINDS
from factories import NOW, T, heartbeat_record, manifest, run_record, write_archive, write_dir_transport

PNG = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00" * 64


def file_entry(name, ctype, blob: bytes) -> dict:
    return {"name": name, "content_type": ctype, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
            "data_b64": base64.b64encode(blob).decode("ascii")}


def evidence_record(run_id, files, automation="auto-a", ts=None, host="VDI-1") -> dict:
    return {"schema_version": 1, "kind": "evidence", "run_id": run_id, "automation": automation, "host": host,
            "ts": (ts or T(0, 9, 5)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "files": files}


def failed_run(**kw):
    return run_record("auto-a", T(0, 9), host="VDI-1", status="failed",
                      error={"category": "LOGIC_ERROR", "type": "KeyError", "message": "KeyError: 'vendor_id'", "attempts": 1}, **kw)


# ---- ingest --------------------------------------------------------------------------------------------------------
def test_evidence_is_a_known_kind():
    assert "evidence" in KINDS


def send(root, rec, ring="pilot"):
    target = root / "ingest" / ring / "evidence" / rec["automation"] / f"{rec['run_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rec), encoding="utf-8")


def test_valid_evidence_is_archived_with_the_date_layout(tmp_path):
    run = failed_run()
    rec = evidence_record(run["run_id"], [file_entry("screenshot.png", "image/png", PNG), file_entry("dom.txt", "text/plain", b"<redacted dom>")])
    src, arch = tmp_path / "logs", tmp_path / "archive"
    send(src, rec)
    stats = ingest.ingest_dir(src, arch)
    assert stats["archived"] == 1 and stats["by_kind"] == {"evidence": 1} and not stats["rejected"]
    assert (arch / "evidence" / "auto-a" / "2026" / "10" / "14" / f"{run['run_id']}.json").exists()


@pytest.mark.parametrize("mutate,why", [
    (lambda r: r["files"][0].update(content_type="application/x-msdownload"), "not allowed"),
    (lambda r: r["files"][0].update(content_type="image/svg+xml"), "not allowed"),
    (lambda r: r["files"][0].update(name="../../evil.png"), "unsafe evidence file name"),
    (lambda r: r["files"][0].update(name="a/b.png"), "unsafe evidence file name"),
    (lambda r: r["files"][0].update(size=1), "size does not match"),
    (lambda r: r["files"][0].update(sha256="0" * 64), "sha256 does not match"),
    (lambda r: r["files"][0].update(data_b64="@@@not-base64@@@"), "not valid base64"),
    (lambda r: r.update(files=[]), "no files"),
    (lambda r: r.update(files=r["files"] * 13), "too many"),
])
def test_bad_evidence_is_quarantined(tmp_path, mutate, why):
    rec = evidence_record("RID1", [file_entry("shot.png", "image/png", PNG)])
    mutate(rec)
    src, arch = tmp_path / "logs", tmp_path / "archive"
    send(src, rec)
    stats = ingest.ingest_dir(src, arch)
    assert stats["archived"] == 0 and stats["rejected"] == 1
    assert why in next((arch / "_rejected").glob("*.reason")).read_text(encoding="utf-8")


def test_png_that_is_not_a_png_and_oversize_evidence_are_rejected(tmp_path):
    fake_png = evidence_record("R1", [file_entry("shot.png", "image/png", b"<html><script>alert(1)</script></html>")])
    huge = evidence_record("R2", [file_entry("big.zip", "application/zip", b"PK" + bytes([3, 4]) + b"\x00" * (ingest.EVIDENCE_MAX_BYTES + 10))])
    for rec in (fake_png, huge):
        assert ingest.validate_evidence(rec) is not None
    assert "does not look like image/png" in ingest.validate_evidence(fake_png)
    assert "exceeds" in ingest.validate_evidence(huge)


# ---- dashboard -----------------------------------------------------------------------------------------------------
def build_dashboard(tmp_path, evidence_files):
    run = failed_run()
    arch = tmp_path / "archive"
    write_archive(arch, [run, heartbeat_record("VDI-1", T(0, 9, 5), automations={"auto-a": "1.0.0"})])
    ev = evidence_record(run["run_id"], evidence_files)
    p = arch / "evidence" / "auto-a" / "2026" / "10" / "14" / f"{run['run_id']}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ev), encoding="utf-8")
    data = analytics.load_archive(arch, NOW)
    report, _ = analytics.compute(data, manifest(), NOW)
    out = tmp_path / "public"
    dashboard.build_site(report, data, out)
    return out, run, data


def test_dashboard_shows_image_inline_and_text_escaped(tmp_path):
    html_evidence = b"<html><body><script>alert('pwned')</script><img src=x onerror=alert(2)></body></html>"
    out, run, data = build_dashboard(tmp_path, [file_entry("screenshot.png", "image/png", PNG),
                                                file_entry("page.html", "text/html", html_evidence),
                                                file_entry("trace.zip", "application/zip", b"PK" + bytes([3, 4]) + b"zipdata")])
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "Failure evidence" in page and run["run_id"] in page
    assert "data:image/png;base64," in page                                    # small image inlined
    assert "<script>alert('pwned')" not in page and "<img src=x" not in page          # stored HTML is never rendered as markup
    assert "&lt;script&gt;alert(&#x27;pwned&#x27;)" in page                    # ...it appears as escaped text
    ev_dir = out / "evidence" / run["run_id"]
    names = sorted(p.name for p in ev_dir.iterdir())
    assert names == ["page.html.txt", "screenshot.png", "trace.zip"]           # html is saved as .html.txt => served as text/plain
    assert not list(out.rglob("*.html.html")) and not [p for p in ev_dir.iterdir() if p.suffix == ".html"]


def test_large_image_is_a_link_not_inlined(tmp_path):
    big = PNG + b"\x00" * (dashboard.INLINE_IMAGE_MAX + 100)
    out, run, data = build_dashboard(tmp_path, [file_entry("big.png", "image/png", big)])
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "data:image/png" not in page and f"evidence/{run['run_id']}/big.png" in page


def test_safe_evidence_file_names():
    assert dashboard.safe_evidence_file("x.html", "text/html") == "x.html.txt"
    assert dashboard.safe_evidence_file("../../a b.png", "image/png") == "a_b.png"
    assert dashboard.safe_evidence_file("t.exe", "application/zip") == "t.zip"


def test_no_evidence_no_section(tmp_path):
    arch = tmp_path / "archive"
    write_archive(arch, [failed_run()])
    data = analytics.load_archive(arch, NOW)
    report, _ = analytics.compute(data, manifest(), NOW)
    dashboard.build_site(report, data, tmp_path / "pub")
    assert "Failure evidence" not in (tmp_path / "pub" / "index.html").read_text(encoding="utf-8")


# ---- programme dashboard -------------------------------------------------------------------------------------------
def make_program(tmp_path, cfg=None, catalog=None, extra=()):
    mf = manifest(catalog=catalog if catalog is not None else {"minutes_per_item": 6})
    runs = [run_record("auto-a", T(d, 9), host="VDI-1", version="1.0.0",
                       items=[(f"i{d}{k}", "success", 900, None) for k in range(10)]) for d in range(1, 6)]
    runs.append(failed_run())
    arch = tmp_path / "archive"
    write_archive(arch, runs + [heartbeat_record("VDI-1", T(0, 9, 5), automations={"auto-a": "1.0.0"})] + list(extra))
    data = analytics.load_archive(arch, NOW)
    report, _ = analytics.compute(data, mf, NOW)
    return program.compute(report, data, mf, cfg, NOW), report, data, mf


def test_hours_saved_and_money(tmp_path):
    cfg = {"hourly_cost": 50, "currency": "EUR", "uipath": {"license_cost_per_robot_per_year": 10000, "robots_retired": 2, "orchestrator_cost_per_year": 5000}}
    p, *_ = make_program(tmp_path, cfg)
    assert p["per_automation"]["auto-a"]["items_success_30d"] == 50
    assert p["per_automation"]["auto-a"]["hours_saved_30d"] == 5.0            # 50 items x 6 min = 300 min
    assert p["headline"]["hours_saved_30d"] == 5.0
    assert p["money"]["licence_avoided_per_year"] == 25000.0
    assert p["money"]["labour_value_per_year"] == 5.0 * 12 * 50
    assert p["money"]["net_value_per_year"] == 25000.0 + 3000.0 and p["money"]["currency"] == "EUR"


def test_unconfigured_costs_and_missing_estimates_are_called_out(tmp_path):
    p, *_ = make_program(tmp_path, None, catalog={})
    assert p["money"]["configured"] is False
    assert p["automations_without_minutes_per_item"] == ["auto-a"] and p["hours_saved_estimated_for_all"] is False
    html = program.render(p)
    assert "Cost inputs are not configured" in html and "no estimate" in html


def test_failure_causes_evidence_and_versions(tmp_path):
    run = failed_run()
    ev = evidence_record(run["run_id"], [file_entry("s.png", "image/png", PNG)])
    arch_extra = []
    p, report, data, mf = make_program(tmp_path)
    # attach evidence for the failed run and recompute
    path = tmp_path / "archive" / "evidence" / "auto-a" / "2026" / "10" / "14" / f"{run['run_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ev), encoding="utf-8")
    data = analytics.load_archive(tmp_path / "archive", NOW)
    p = program.compute(report, data, mf, None, NOW)
    assert p["failure_causes"][0]["category"] == "LOGIC_ERROR" and p["failure_causes"][0]["runs"] >= 1
    assert p["evidence"] == {"failed_runs": 1, "with_evidence": 1, "files": 1}
    assert p["versions"]["hosts"] == 1 and p["versions"]["drifted"] == []


def test_program_html_is_escaped_and_complete(tmp_path):
    evil = failed_run(synopsis="x")
    evil["error"]["message"] = "<script>alert('x')</script>"
    evil["run_id"] = "R-EVIL"
    p, *_ = make_program(tmp_path, {"hourly_cost": 10}, extra=[evil])
    html = program.render(p)
    assert "<script>alert" not in html and "&lt;script&gt;alert" in html
    for needle in ("Headline", "Value", "Top failure causes", "Fleet versions", "Retries &amp; LLM"):
        assert needle in html


def test_program_build_writes_files_and_pipeline_stage(tmp_path):
    p, report, data, mf = make_program(tmp_path)
    out = tmp_path / "prog"
    res = program.build(report, data, mf, out, None, NOW)
    assert (out / "index.html").exists() and json.loads((out / "program.json").read_text())["schema_version"] == 1 and res["program"]["automations"] == 1
    summary = pipeline.build(tmp_path / "archive", None, tmp_path / "state", tmp_path / "pub", NOW)
    assert not summary["errors"] and (tmp_path / "pub" / "program" / "index.html").exists()


def test_program_config_file_roundtrip(tmp_path):
    f = tmp_path / "program.yaml"
    f.write_text("currency: GBP\nhourly_cost: 40\nuipath:\n  robots_retired: 3\n  license_cost_per_robot_per_year: 9000\n", encoding="utf-8")
    cfg = program.load_config(f)
    assert cfg["currency"] == "GBP" and cfg["uipath"]["robots_retired"] == 3 and cfg["uipath"]["orchestrator_cost_per_year"] == 0.0
