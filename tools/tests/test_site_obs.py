"""Reliability / Errors pages, metrics export, fleet status, badges and the global search index."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "site"))

import check_site  # noqa: E402
import site_common as sc  # noqa: E402
import site_obs  # noqa: E402
from automation_logging import analytics  # noqa: E402
from automation_logging.synthetic import run_record  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
CFG = sc.SiteConfig(built_at="2026-10-21T12:00:00Z", commit="abcdef1234567890")
XSS = '<script>alert(1)</script><img src=x onerror=alert(2)>"\'&'
ERR = {"category": "NETWORK_ERROR", "type": "RuntimeError", "message": "timeout after 30s calling https://x.example/a/1"}
MANIFEST = {"automations": {"auto-a": {"status": "active", "rings": {"pilot": "1.0.0"}, "versions": {"1.0.0": {"sha256": "cd" * 32}},
                                       "schedule": {"timezone": "UTC", "slots": [{"id": "daily", "type": "daily", "time": "09:00"}]}}}}


def run(status="success", hours_ago=1.0, auto="auto-a", **kw):
    if status in ("failed", "hung") and "error" not in kw:
        kw["error"] = ERR
    return run_record(auto, NOW - timedelta(hours=hours_ago), status=status, **kw)


def data(*runs):
    return analytics.Data(list(runs), [], [], [])


def read(out, rel):
    return (out / rel).read_text(encoding="utf-8")


def touch_targets(out: Path) -> None:
    """The index links to pages other builders write; create them so only the observability files are under test."""
    for it in json.loads(read(out, "search-ops.json")):
        f = out / it["u"].split("#")[0]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()


def everything(out: Path) -> str:
    return "".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(out.rglob("*")) if p.is_file() and p.suffix in (".html", ".json", ".prom", ".svg"))


# ----------------------------------------------------------------------------------------------------------- waiting state
def test_no_data_means_waiting_pages_unknown_status_and_no_invented_numbers(tmp_path):
    pages = site_obs.build_obs(tmp_path, CFG, MANIFEST, None, NOW)
    for rel in ("reliability/index.html", "errors/index.html"):
        assert "Waiting for the first run" in read(tmp_path, rel)
    fs = json.loads(read(tmp_path, "fleet-status.json"))
    assert fs["status"] == "unknown" and fs["automations"] == {"auto-a": "no data"}
    assert "no data" in read(tmp_path, "badges/fleet.svg") and "no data" in read(tmp_path, "badges/automation-auto-a.svg")
    m = json.loads(read(tmp_path, "metrics.json"))
    assert m["fleet"]["runs"] == 0 and m["fleet"]["success_ratio"] is None
    assert {"metrics.json", "metrics.prom", "fleet-status.json", "search-ops.json", "assets/gsearch.js", "badges/fleet.svg"} <= set(pages)
    touch_targets(tmp_path)
    assert check_site.check_observability(tmp_path) == []


# --------------------------------------------------------------------------------------------------------- numbers + pages
def scenario():
    # hourly timeline (hours ago): ok 30, failed 29, failed 28, ok 26 (episode 1: 3 h to recover), ok 20, failed 10, ok 9 (episode 2: 1 h)
    seq = [("success", 30), ("failed", 29), ("failed", 28), ("success", 26), ("success", 20), ("failed", 10), ("success", 9)]
    return [run(s, h) for s, h in seq]


def test_reliability_page_and_metrics_carry_the_hand_computed_numbers(tmp_path):
    site_obs.build_obs(tmp_path, CFG, MANIFEST, data(*scenario()), NOW)
    m = json.loads(read(tmp_path, "metrics.json"))
    a = m["automations"]["auto-a"]
    # 7 counted runs, 3 failed: success ratio 4/7 ; SLO 95 % -> allowed 0.35, consumed 3/0.35 = 8.5714, remaining -7.5714
    assert (a["runs"], a["failed"]) == (7, 3) and a["success_ratio"] == pytest.approx(0.5714, abs=1e-4)
    assert a["error_budget_remaining"] == pytest.approx(-7.5714, abs=1e-3)
    # episodes: 29 h->26 h = 3 h (10800 s) and 10 h->9 h = 1 h (3600 s): MTTR 7200 s ; MTBF = (10 h) - (26 h) -> 16 h = 57600 s
    assert a["mttr_seconds"] == 7200.0 and a["mtbf_seconds"] == 57600.0 and a["failure_streak"] == 0
    page = read(tmp_path, "reliability/index.html")
    assert "auto-a" in page and "2.0 h" in page and "Budgets exhausted" in page and "<svg" in page
    prom = read(tmp_path, "metrics.prom")
    assert 'autostation_mttr_seconds{automation="auto-a"} 7200' in prom
    assert 'autostation_error_budget_remaining_ratio{automation="auto-a"} -7.5714' in prom
    assert json.loads(read(tmp_path, "fleet-status.json"))["status"] in ("red", "amber")


def test_errors_page_shows_signatures_regressions_machines_and_remediation(tmp_path):
    old = [run("success", 200 - i, version="1.0.0") for i in range(4)]
    bad = [run("failed", 40, version="1.1.0", host="SICK"), run("failed", 30, version="1.1.0", host="SICK")]
    ev = [{"ts": "2026-10-21T10:00:00Z", "event": "remediation_attempt",
           "data": {"playbook": "restart-browser", "category": "NETWORK_ERROR", "outcome": "not_fixed", "detail": "restarted msedge"}},
          {"ts": "2026-10-21T10:00:01Z", "event": "remediation_attempt",
           "data": {"playbook": "refresh-session", "category": "AUTH_EXPIRED", "outcome": "recommended", "detail": "sign in once"}}]
    last = run("failed", 3, version="1.1.0", host="SICK", extra_events=ev)
    last["remediation"] = {"category": "AUTH_EXPIRED", "fixed": False, "playbooks": ["restart-browser"]}
    site_obs.build_obs(tmp_path, CFG, MANIFEST, data(*old, *bad, last), NOW)
    page = read(tmp_path, "errors/index.html")
    for needle in ("NETWORK_ERROR", "Regressions after a release", "1.0.0 -&gt; 1.1.0", "restart-browser", "sign in once", "Refresh the certificate",
                   'id="sig-', "Needs attention", "href=\"../jobs/index.html#run="):
        assert needle in page, needle
    assert "machine problem" not in page or "SICK" in page


def test_errors_page_without_remediation_events_says_so_honestly(tmp_path):
    site_obs.build_obs(tmp_path, CFG, MANIFEST, data(run("failed", 2), run("success", 1)), NOW)
    assert "No remediation events reported yet" in read(tmp_path, "errors/index.html")


# ---------------------------------------------------------------------------------------------------------------- hostile
def test_hostile_telemetry_never_reaches_any_output_unescaped(tmp_path):
    runs = [run("failed", 5, auto="auto-a", host="H1", error={"category": XSS, "message": XSS * 500, "type": XSS}),
            run("failed", 4, auto="auto-a", host=XSS[:50] and "H2", error={"category": "NETWORK_ERROR", "message": "\x00\x1b[31m" + "A" * 100000}),
            run("success", 3, auto="auto-a", host="H1", synopsis=XSS, version=XSS[:30])]
    r = runs[2]
    r["events"].append({"ts": "2026-10-21T10:00:00Z", "event": "remediation_attempt",
                        "data": {"playbook": XSS, "category": XSS, "outcome": "recommended", "detail": XSS * 100}})
    site_obs.build_obs(tmp_path, CFG, MANIFEST, data(*runs), NOW)
    blob = everything(tmp_path)
    assert "<script>alert" not in blob and "<img src=x" not in blob
    for m in re.finditer(r"alert\(1\)", blob):
        assert "&lt;script&gt;" in blob[max(0, m.start() - 24): m.start()] or "\\u003c" in blob[max(0, m.start() - 24): m.start()] or "<script>" not in blob[max(0, m.start() - 12): m.start()]
    assert "\x00" not in blob and "\x1b" not in blob
    assert len(blob) < 3_000_000                                  # 100 kB fields were clamped, not copied
    touch_targets(tmp_path)
    assert check_site.check_observability(tmp_path) == []


def test_unsafe_automation_names_get_no_metric_badge_or_file(tmp_path):
    evil = run("success", 2, auto="auto-a")
    evil["automation"] = '../../etc/passwd"x'
    site_obs.build_obs(tmp_path, CFG, MANIFEST, data(evil, run("success", 3)), NOW)
    assert sorted(p.name for p in (tmp_path / "badges").iterdir()) == ["automation-auto-a.svg", "fleet.svg"]
    assert "passwd" not in read(tmp_path, "metrics.prom") and not (tmp_path.parent / "etc").exists()


def test_cardinality_is_bounded(tmp_path):
    runs = [run("success", 2, auto=f"auto-{i:03d}") for i in range(230)]
    site_obs.build_obs(tmp_path, CFG, {}, data(*runs), NOW)
    m = json.loads(read(tmp_path, "metrics.json"))
    assert len(m["automations"]) == site_obs.MAX_TELEMETRY_ONLY            # telemetry-only names are capped much lower than registered ones
    registered = {"automations": {f"auto-{i:03d}": {"status": "active", "rings": {"pilot": "1.0.0"}} for i in range(230)}}
    site_obs.build_obs(tmp_path / "r", CFG, registered, data(*runs), NOW)
    m = json.loads(read(tmp_path / "r", "metrics.json"))
    assert len(m["automations"]) == site_obs.MAX_AUTOMATIONS and len(list((tmp_path / "r" / "badges").glob("*.svg"))) <= site_obs.MAX_AUTOMATIONS + 1
    assert len(json.loads(read(tmp_path, "search-ops.json"))) <= site_obs.MAX_SEARCH


def test_prometheus_label_escaping_and_badge_escaping():
    assert site_obs._label('a"b\\c\nd') == 'a\\"b\\\\c\\nd'
    svg = site_obs.badge_svg("<x>", "passing")
    assert "<x>" not in svg and "&lt;x&gt;" in svg and "<script" not in svg


# ------------------------------------------------------------------------------------------------------------ determinism
def test_outputs_are_deterministic(tmp_path):
    d = data(*scenario())
    a, b = tmp_path / "a", tmp_path / "b"
    site_obs.build_obs(a, CFG, MANIFEST, d, NOW)
    site_obs.build_obs(b, CFG, MANIFEST, d, NOW)
    for rel in ("metrics.json", "metrics.prom", "fleet-status.json", "search-ops.json", "badges/automation-auto-a.svg", "badges/fleet.svg",
                "reliability/index.html", "errors/index.html"):
        assert read(a, rel) == read(b, rel), rel


# ---------------------------------------------------------------------------------------------------------------- search
def test_search_index_links_are_safe_and_resolve_and_the_script_has_no_sinks(tmp_path):
    site_obs.build_obs(tmp_path, CFG, MANIFEST, data(*scenario()), NOW)
    idx = json.loads(read(tmp_path, "search-ops.json"))
    kinds = {i["t"] for i in idx}
    assert {"page", "automation", "error", "run"} <= kinds
    for it in idx:
        assert re.match(r"^[A-Za-z0-9_\-/.#=&%]+$", it["u"]) and ".." not in it["u"] and ":" not in it["u"] and not it["u"].startswith("/")
    js = read(tmp_path, "assets/gsearch.js")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in js, sink
    assert "textContent" in js


def test_check_site_rejects_a_forged_search_index(tmp_path):
    site_obs.build_obs(tmp_path, CFG, MANIFEST, data(*scenario()), NOW)
    (tmp_path / "search-ops.json").write_text(json.dumps([{"t": "x", "n": "x", "s": "", "u": "javascript:alert(1)"}]), encoding="utf-8")
    assert any("unsafe" in p for p in check_site.check_observability(tmp_path))
    (tmp_path / "search-ops.json").write_text(json.dumps([{"t": "x", "n": "x", "s": "", "u": "//evil.example/x"}]), encoding="utf-8")
    assert check_site.check_observability(tmp_path)
    (tmp_path / "metrics.prom").write_text("not a metric line!!\n", encoding="utf-8")
    assert any("Prometheus" in p for p in check_site.check_observability(tmp_path))
    (tmp_path / "badges" / "x.svg").write_text('<svg onload="alert(1)"></svg>', encoding="utf-8")
    assert any("script or event" in p for p in check_site.check_observability(tmp_path))


def test_layout_has_the_global_search_box_and_nav_links(tmp_path):
    site_obs.build_obs(tmp_path, CFG, MANIFEST, data(*scenario()), NOW)
    page = read(tmp_path, "errors/index.html")
    assert 'id="gsearch"' in page and 'src="../assets/gsearch.js"' in page and 'data-base="../"' in page
    assert "reliability/index.html" in page and "errors/index.html" in page and "Content-Security-Policy" in page


def test_a_forged_automation_named_fleet_cannot_overwrite_the_fleet_badge(tmp_path):
    evil = dict(run("failed", 1), automation="fleet")
    site_obs.build_obs(tmp_path, CFG, MANIFEST, data(evil, run("success", 3)), NOW)
    assert (tmp_path / "badges" / "automation-fleet.svg").is_file()
    assert "failing" not in read(tmp_path, "badges/fleet.svg") or "autostation" in read(tmp_path, "badges/fleet.svg")


def test_junk_telemetry_names_cannot_evict_registered_automations(tmp_path):
    junk = [dict(run("success", i % 3), automation=f"000-junk-{i:03d}", run_id=f"j{i}") for i in range(250)]
    c = site_obs.compute_all(MANIFEST, data(*junk, run("success", 3)), NOW)
    assert "auto-a" in c["per"]
    assert len([n for n in c["per"] if n.startswith("000-junk")]) <= site_obs.MAX_TELEMETRY_ONLY


def test_a_page_builder_crash_becomes_a_placeholder_not_a_failed_build(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OverflowError("date value out of range")

    monkeypatch.setattr(site_obs, "build_reliability", boom)
    pages = site_obs.build_obs(tmp_path, CFG, MANIFEST, data(run("success", 3)), NOW)
    html = (tmp_path / "reliability" / "index.html").read_text(encoding="utf-8")
    assert "could not be built" in html and "reliability/index.html" in pages
    assert (tmp_path / "errors" / "index.html").is_file() and (tmp_path / "metrics.json").is_file()


def test_check_site_flags_an_inline_script_the_csp_does_not_cover(tmp_path):
    ok = sc.layout("T", "<p>hello</p>", cfg=CFG, depth=0, extra_js="<script>var a=1;</script>")
    assert "'unsafe-inline'" not in ok.split("script-src", 1)[1].split(";", 1)[0]
    page = tmp_path / "x.html"
    page.write_text(ok, encoding="utf-8")
    assert check_site._check_inline_scripts("x.html", check_site.parse(page)) == []
    page.write_text(ok.replace("</main>", "<script>steal()</script></main>"), encoding="utf-8")      # as if injected through telemetry
    bad = check_site._check_inline_scripts("x.html", check_site.parse(page))
    assert len(bad) == 1 and "steal" in bad[0]
