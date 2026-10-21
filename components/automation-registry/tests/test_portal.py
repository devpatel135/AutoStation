from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from conftest import run_cli
from registry import ops, store
from registry.portal import admin, build, health, markdown
from registry.portal.common import DEFAULT_CONFIG, esc, json_for_script
from wrapper_core import miniyaml


@pytest.fixture
def site(tmp_path, mpath):
    out = tmp_path / "public"
    res = build.build_portal(mpath, out)
    return out, res


def read(out: Path, rel: str) -> str:
    return (out / rel).read_text(encoding="utf-8")


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.stack, self.errors = [], [], []
        self.void = {"meta", "br", "input", "link", "hr", "img"}

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag not in self.void:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.void:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}> (open: {self.stack[-3:]})")
        else:
            self.stack.pop()


def parse(html_text: str) -> Tags:
    p = Tags()
    p.feed(html_text)
    return p


# ---- structure ---------------------------------------------------------------------------------------
def test_all_pages_generated_and_well_formed(site):
    out, res = site
    for rel in ("index.html", "admin.html", "compatibility.html", "governance.html", "automation/auto-a.html",
                "automation/auto-b.html", "manifest.yaml", "triggers.json", "index.json", "portal.config.json"):
        assert (out / rel).exists(), rel
    for rel in ("index.html", "admin.html", "compatibility.html", "governance.html", "automation/auto-a.html"):
        p = parse(read(out, rel))
        assert p.errors == [] and p.stack == [], (rel, p.errors, p.stack)
    assert read(out, "manifest.yaml") == (out.parent / "manifest.yaml").read_text(encoding="utf-8")


def test_index_cards_installers_and_rings(site):
    out, _ = site
    page = read(out, "index.html")
    assert page.count('class="card') == 2
    assert 'href="automation/auto-a.html"' in page
    # default ring = broad => broad version's installer
    assert 'href="installers/auto-a-1.0.0-setup.exe"' in page and "Download installer v1.0.0 (broad)" in page
    for r, v in (("dev", "1.2.0"), ("pilot", "1.1.0"), ("broad", "1.0.0")):
        assert f'data-v-{r}="{v}"' in page
        assert f'data-href-{r}="installers/auto-a-{v}-setup.exe"' in page
    assert "package sha256" in page and 'id="ring"' in page and 'id="q"' in page
    assert "Daily at 09:00 America/New_York" in page and "Polls every 5 min" in page
    assert 'data-tags="finance ap"' in page and 'data-tag="finance"' in page


def test_paused_badge_and_fleet_banner(tmp_path, mpath):
    run_cli("pause", "auto-b", "--reason", "bad <b>data</b>", "--manifest", str(mpath))
    run_cli("fleet-pause", "--reason", "incident 9", "--manifest", str(mpath))
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    page = read(out, "index.html")
    assert "Paused:" in page and "bad &lt;b&gt;data&lt;/b&gt;" in page and "Fleet paused." in page and "incident 9" in page
    assert "<b>data</b>" not in page


def test_unreleased_ring_has_no_download(tmp_path, mpath):
    m = miniyaml.loads(mpath.read_text(encoding="utf-8"))
    del m["automations"]["auto-b"]["rings"]["broad"]
    del m["automations"]["auto-b"]["rings"]["pilot"]
    mpath.write_text(miniyaml.dumps(m), encoding="utf-8")
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    page = read(out, "index.html")
    assert 'data-v-broad=""' in page and "Download installer v1.0.0 (dev)" in page   # falls back to the highest released ring


def test_detail_page_content(site):
    out, _ = site
    page = read(out, "automation/auto-a.html")
    for needle in ("Versions by ring", "1.2.0", "Schedule", "weekly", "Mon at 08:00", "Error handling", "max_retries",
                   "Sat, Sun", "23:00-02:00", "AP Automation", "process@example.com", "Runbook", 'href="../installers/auto-a-1.0.0-setup.exe"',
                   "Changelog", "poll"):
        assert needle in page, needle


def test_changelog_and_pending_triggers_on_detail(tmp_path, mpath):
    run_cli("promote", "auto-a", "--to", "pilot", "--reason", "soaked a week", "--manifest", str(mpath))
    run_cli("trigger-add", "auto-a", "--param", "k=v", "--manifest", str(mpath))
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    page = read(out, "automation/auto-a.html")
    assert "soaked a week" in page and "2026-10-12" in page and "Pending on-demand triggers" in page
    assert json.loads(read(out, "triggers.json"))["triggers"][0]["automation"] == "auto-a"


def test_compatibility_page(site):
    out, _ = site
    page = read(out, "compatibility.html")
    assert "&gt;=0.1.0,&lt;1.0.0" in page and "supported" in page and "wrapper-core" in page


def test_index_json_machine_readable(site):
    out, _ = site
    idx = json.loads(read(out, "index.json"))
    a = next(x for x in idx["automations"] if x["name"] == "auto-a")
    assert a["installers"]["pilot"] == {"version": "1.1.0", "installer": "installers/auto-a-1.1.0-setup.exe", "sha256": a["installers"]["pilot"]["sha256"]}
    assert len(a["installers"]["pilot"]["sha256"]) == 64 and a["tags"] == ["finance", "ap"] and idx["rings"] == ["dev", "pilot", "broad"]


def test_governance_present_and_absent(tmp_path, mpath):
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    assert "No governance document is published yet" in read(out, "governance.html")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "GOVERNANCE.md").write_text("# Policy\n\n- one **rule**\n- [link](https://example.com) and [bad](javascript:alert(1))\n\n```\n<script>x</script>\n```\n", encoding="utf-8")
    build.build_portal(mpath, out)
    g = read(out, "governance.html")
    assert "<h1>Policy</h1>" in g and "<strong>rule</strong>" in g and 'href="https://example.com"' in g
    assert 'href="javascript' not in g and "and bad)" in g   # unsafe URL degrades to plain text
    assert "&lt;script&gt;x&lt;/script&gt;" in g


# ---- graceful telemetry ------------------------------------------------------------------------------
def test_health_absent_and_present(tmp_path, mpath):
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    assert "no telemetry" in read(out, "index.html")
    signals = tmp_path / "signals.json"
    signals.write_text(json.dumps({"signals": [{"id": "s1", "type": "MISSED", "severity": "high", "automation": "auto-a", "detail": "no run"}]}))
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps([{"automation": "auto-a", "status": "success", "started_at": "2026-10-12T09:00:00Z", "synopsis": "ok"},
                                {"automation": "auto-b", "status": "success", "started_at": "2026-10-12T06:30:00Z"}]))
    build.build_portal(mpath, out, signals=signals, runs=runs)
    page = read(out, "index.html")
    assert "failing" in page and "healthy" in page and "MISSED" in page
    assert "MISSED" in read(out, "automation/auto-a.html")


def test_runs_directory_of_run_logs(tmp_path):
    d = tmp_path / "runs" / "x"
    d.mkdir(parents=True)
    (d / "r1.json").write_text(json.dumps({"kind": "run", "automation": "auto-a", "status": "failed", "started_at": "2026-10-12T09:00:00Z", "events": [1] * 3}))
    (d / "hb.json").write_text(json.dumps({"kind": "heartbeat"}))
    runs = health.load_runs(tmp_path / "runs")
    assert len(runs) == 1 and "events" not in runs[0]
    assert health.summarize(["auto-a", "ghost"], [], runs)["auto-a"]["status"] == "failing"
    assert health.load_signals(tmp_path / "nope") == [] and health.load_runs(None) == []


# ---- security ----------------------------------------------------------------------------------------
EVIL = '"><script>alert(1)</script><img src=x onerror=alert(2)>'


def test_xss_escaping_everywhere(tmp_path, mpath):
    m = miniyaml.loads(mpath.read_text(encoding="utf-8"))
    a = m["automations"]["auto-a"]
    a["description"] = EVIL
    a["owner_team"] = EVIL
    a["systems"] = [EVIL]
    a["catalog"].update(summary=EVIL, owner_team=EVIL, process_owner=EVIL, systems=[EVIL])
    a["catalog"]["links"] = [{"title": EVIL, "url": "https://ok.example/x"}]
    a["schedule"]["slots"][0]["params"] = {"k": EVIL}
    a["status"], a["paused_reason"] = "paused", EVIL
    m["fleet"] = {"paused": True, "paused_reason": EVIL}
    mpath.write_text(miniyaml.dumps(m), encoding="utf-8")
    cfgfile = tmp_path / "portal.config.json"
    cfgfile.write_text(json.dumps({"title": EVIL, "gitlab_url": "javascript:alert(1)"}))
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    for rel in ("index.html", "automation/auto-a.html", "admin.html", "compatibility.html"):
        page = read(out, rel)
        assert "<script>alert(1)</script>" not in page, rel
        assert "<img src=x" not in page, rel
        assert 'onerror=alert(2)>' not in page.replace("&gt;", "") or "&lt;img" in page
    # only our own scripts exist: theme head, theme toggle, page script (+ json data block on admin)
    for rel in ("index.html", "admin.html"):
        scripts = re.findall(r"<script[^>]*>", read(out, rel))
        assert all(s in ("<script>", '<script type="application/json" id="admin-data">') for s in scripts), scripts
    # a hostile gitlab_url falls back to the safe default in the admin data
    data = json.loads(re.search(r'id="admin-data">(.*?)</script>', read(out, "admin.html"), re.S).group(1))
    assert data["config"]["gitlab_url"].startswith("https://")


def test_unsafe_catalog_links_are_not_anchors(tmp_path, mpath):
    m = miniyaml.loads(mpath.read_text(encoding="utf-8"))
    m["automations"]["auto-a"]["catalog"]["links"] = [{"title": "x", "url": "https://ok.example"}]
    mpath.write_text(miniyaml.dumps(m), encoding="utf-8")
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    assert 'href="https://ok.example"' in read(out, "automation/auto-a.html")
    # javascript: links cannot be written into a valid manifest at all
    m["automations"]["auto-a"]["catalog"]["links"] = ["javascript:alert(1)"]
    mpath.write_text(miniyaml.dumps(m), encoding="utf-8")
    with pytest.raises(SystemExit, match="invalid manifest"):
        build.build_portal(mpath, tmp_path / "pub2")


def test_json_for_script_cannot_break_out():
    s = json_for_script({"x": "</script><script>alert(1)</script>&"})
    assert "<" not in s and ">" not in s and "&" not in s
    assert json.loads(s)["x"] == "</script><script>alert(1)</script>&"


def test_markdown_renderer_basics():
    html_out = markdown.render("# T\n\n| a | b |\n|---|---|\n| 1 | <x> |\n\n1. one\n2. two\n\n> quote")
    assert "<h1>T</h1>" in html_out and "<th>a</th>" in html_out and "&lt;x&gt;" in html_out and "<ol>" in html_out and "<blockquote>" in html_out


def test_refuses_to_wipe_unrelated_directory(tmp_path, mpath):
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "file.txt").write_text("keep me")
    with pytest.raises(SystemExit, match="not empty"):
        build.build_portal(mpath, victim)
    with pytest.raises(SystemExit, match="overwrite the manifest directory"):
        build.build_portal(mpath, mpath.parent)
    assert (victim / "file.txt").exists()


def test_rebuild_removes_stale_pages(tmp_path, mpath):
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    run_cli("pause", "auto-b", "--reason", "retire", "--manifest", str(mpath))
    run_cli("decommission", "auto-b", "--reason", "retired", "--manifest", str(mpath))
    build.build_portal(mpath, out)
    assert not (out / "automation" / "auto-b.html").exists() and (out / "automation" / "auto-a.html").exists()


def test_installers_dir_is_copied(tmp_path, mpath):
    inst = mpath.parent / "installers"
    inst.mkdir()
    (inst / "auto-a-1.0.0-setup.exe").write_bytes(b"MZ")
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    assert (out / "installers" / "auto-a-1.0.0-setup.exe").read_bytes() == b"MZ"


# ---- admin panel -------------------------------------------------------------------------------------
def admin_data(out: Path) -> dict:
    return json.loads(re.search(r'id="admin-data">(.*?)</script>', read(out, "admin.html"), re.S).group(1))


def test_admin_actions_precomputed_from_real_ops(site, manifest):
    out, _ = site
    d = admin_data(out)
    acts = {a["label"]: a for a in d["automations"]["auto-a"]}
    pause = acts["Pause (kill-switch)"]
    assert pause["cmd"] == "python -m registry pause auto-a{{REASON_ARG}}"
    assert pause["ci"] == {"ACTION": "pause", "TARGET": "auto-a", "PARAMS": "{}"} and pause["needs_reason"]
    joined = "\n".join(pause["diff"])
    assert "+    status: paused" in joined and f"+    paused_reason: {admin.SENTINEL}" in joined and "-    status: active" in joined
    promo = acts["Promote 1.2.0 to pilot"]
    assert promo["cmd"] == "python -m registry promote auto-a --to pilot{{REASON_ARG}}" and json.loads(promo["ci"]["PARAMS"]) == {"to": "pilot"}
    assert any(l.startswith("+") and 'pilot: "1.2.0"' in l for l in promo["diff"])
    rb = acts["Roll back dev+ to 1.0.0"]
    assert json.loads(rb["ci"]["PARAMS"]) == {"ring": "dev", "to": "1.0.0"} and "--ring dev --to 1.0.0" in rb["cmd"]
    assert "Roll back broad+ to 1.2.0" not in acts                      # nothing to roll back -> not offered
    trig = acts["Run now / retry (trigger)"]
    assert trig["kind"] == "trigger" and trig["slots"] == ["poll"]
    assert "auto-b" in d["automations"] and not [a for a in d["automations"]["auto-b"] if a["kind"] == "trigger"]  # no poll slot
    assert [a["label"] for a in d["fleet"]] == ["PAUSE THE WHOLE FLEET (kill-switch)"]


def test_admin_resume_and_fleet_resume_when_paused(tmp_path, mpath):
    run_cli("pause", "auto-a", "--reason", "x", "--manifest", str(mpath))
    run_cli("fleet-pause", "--reason", "y", "--manifest", str(mpath))
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    d = admin_data(out)
    labels = [a["label"] for a in d["automations"]["auto-a"]]
    assert labels[0] == "Resume" and "Decommission (remove from registry)" in labels and "Pause (kill-switch)" not in labels
    assert not [a for a in d["automations"]["auto-a"] if a["kind"] == "trigger"]   # paused: triggers pointless
    assert d["fleet"][0]["ci"]["ACTION"] == "fleet-resume"


def test_admin_precompute_matches_actual_cli_result(tmp_path, mpath, manifest):
    """The preview must equal what the real command writes (single source of truth)."""
    d_pre = admin.build_actions(manifest)["automations"]["auto-a"]
    pause = next(a for a in d_pre if a["label"].startswith("Pause"))
    run_cli("pause", "auto-a", "--reason", "REAL", "--manifest", str(mpath))
    after = mpath.read_text(encoding="utf-8")
    for line in pause["diff"]:
        if line[:1] == "+" and not line.startswith("+++"):
            assert line[1:].replace(admin.SENTINEL, "REAL") in after


def test_admin_html_wiring(site):
    out, _ = site
    page = read(out, "admin.html")
    assert 'data-prepare="auto-a"' in page and 'id="auto-a"' in page and 'data-for="__fleet__"' in page
    assert "pipelines/new?ref=" in page and "merge_requests/new?merge_request[source_branch]=" in page and "/-/edit/" in page
    assert "Fleet compliance summary" in page and "Mid-rollout" in page and "textContent" in page
    assert "innerHTML" not in page and "document.write" not in page   # the page script only uses textContent/attributes


def test_build_links_reference_implementation():
    cfg = dict(DEFAULT_CONFIG, gitlab_url="https://gl.example.internal/", project_path="grp/registry")
    ci = {"ACTION": "pause", "TARGET": "auto-a", "REASON": "bad data & more", "PARAMS": "{}"}
    links = admin.build_links(cfg, ci, "registry: PAUSE auto-a - bad data", "Reason: bad data & more", "ops/pause-auto-a-x1")
    assert links["pipeline"].startswith("https://gl.example.internal/grp/registry/-/pipelines/new?ref=main")
    assert "&var[ACTION]=pause" in links["pipeline"] and "&var[TARGET]=auto-a" in links["pipeline"]
    assert "&var[REASON]=bad%20data%20%26%20more" in links["pipeline"] and "&var[BRANCH]=ops%2Fpause-auto-a-x1" in links["pipeline"]
    assert "&var[PARAMS]=%7B%7D" in links["pipeline"]
    assert links["merge_request"].startswith("https://gl.example.internal/grp/registry/-/merge_requests/new?merge_request[source_branch]=ops%2Fpause-auto-a-x1")
    assert "merge_request[title]=registry%3A%20PAUSE%20auto-a%20-%20bad%20data" in links["merge_request"]
    assert links["edit"] == "https://gl.example.internal/grp/registry/-/edit/main/manifest.yaml"
    # the page script builds links with the same templates
    assert "'/-/pipelines/new?ref='" in admin.ADMIN_JS and "'&var[' + k + ']='" in admin.ADMIN_JS
    assert "merge_request[source_branch]=" in admin.ADMIN_JS and "'/-/edit/'" in admin.ADMIN_JS


def test_admin_js_has_balanced_syntax():
    js = admin.ADMIN_JS
    for open_, close in ("()", "{}", "[]"):
        assert js.count(open_) == js.count(close), open_


def test_portal_config_is_applied(tmp_path, mpath):
    cfg = tmp_path / "portal.config.json"
    cfg.write_text(json.dumps({"gitlab_url": "https://gl.corp.example", "project_path": "a/b", "title": "ACME Auto"}))
    out = tmp_path / "pub"
    build.build_portal(mpath, out)
    page = read(out, "admin.html")
    assert "ACME Auto" in page and "https://gl.corp.example" in json.dumps(admin_data(out)["config"])
