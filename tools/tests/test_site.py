from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import build_registry  # noqa: E402
import build_site  # noqa: E402
import check_site  # noqa: E402
import site_docs  # noqa: E402
import site_install  # noqa: E402
from wrapper_core import miniyaml  # noqa: E402

AUTOMATIONS = ["automation-hello-world", "automation-invoice-router", "automation-notifier", "automation-portal-report"]


@pytest.fixture(scope="module")
def demo_site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site") / "site"
    status = build_site.build(out, ROOT, demo=True)
    return out, status


def html_files(site: Path):
    return sorted(site.rglob("*.html"))


# ---- whole-site smoke ---------------------------------------------------------------------------------------------
def test_demo_site_has_every_section_and_a_clean_link_check(demo_site):
    site, status = demo_site
    assert status["mode"] == "demo" and status["registry"]["automations"] >= 4
    problems = check_site.check_site(site)
    assert problems == [], "\n".join(problems)
    for rel in ("index.html", "install/index.html", "docs/index.html", "portal/index.html", "portal/admin.html", "dashboard/index.html",
                "program/index.html", "migration/index.html", "404.html", ".nojekyll", "status.json", "install/bootstrap.ps1", "install/catalog.json"):
        assert (site / rel).exists(), rel
    assert len(html_files(site)) >= 30


def test_landing_has_role_based_entry_points(demo_site):
    page = (demo_site[0] / "index.html").read_text(encoding="utf-8")
    for needle in ("I need to install an automation", "I build automations", "I operate the fleet", "I approve changes",
                   "We are migrating from UiPath", "I need to show the value", "install/index.html", "portal/admin.html", "docs/GETTING_STARTED.html"):
        assert needle in page, needle


def test_every_install_page_has_download_sha_and_verify_steps(demo_site):
    site, status = demo_site
    cat = json.loads((site / "install" / "catalog.json").read_text(encoding="utf-8"))
    assert set(cat["automations"]) >= set(AUTOMATIONS)
    for name in AUTOMATIONS:
        text = (site / "install" / f"{name}.html").read_text(encoding="utf-8")
        url = f"https://github.com/devpatel135/AutoStation/releases/download/installers-latest/{name}-setup.exe"
        assert url in text and cat["automations"][name]["installer"]["url"] == url
        for needle in ("SHA-256", "Get-FileHash", "/S /RING=", "HKCU\\Software\\AUTOMAGIX", "Intune", "SCCM", "Troubleshooting",
                       "What the installer changes", "no administrator rights", "bug-automation-failure.yml"):
            assert needle in text, f"{name}: {needle}"
    assert "https://devpatel135.github.io/AutoStation/install/catalog.json" in (site / "install" / "bootstrap.ps1").read_text(encoding="utf-8")


def test_portal_in_the_site_links_to_release_assets_and_home(demo_site):
    site = demo_site[0]
    idx = (site / "portal" / "index.html").read_text(encoding="utf-8")
    assert "releases/download/installers-latest/automation-hello-world-setup.exe" in idx and 'href="installers/' not in idx
    assert "Site home" in idx
    adm = (site / "portal" / "admin.html").read_text(encoding="utf-8")
    assert "gh workflow run" in adm and "actions/workflows" in adm


def test_demo_banner_on_dashboards_but_not_on_live_data(demo_site, tmp_path):
    site = demo_site[0]
    for rel in ("dashboard/index.html", "program/index.html"):
        assert "DEMO DATA" in (site / rel).read_text(encoding="utf-8")
    from automation_logging import sample
    data = tmp_path / "live"
    sample.write(data, None, 3, 7)
    live = tmp_path / "live-site"
    build_site.build(live, ROOT, live_data=data)
    for rel in ("dashboard/index.html", "program/index.html"):
        assert "DEMO DATA" not in (live / rel).read_text(encoding="utf-8")
    assert json.loads((live / "status.json").read_text(encoding="utf-8"))["mode"] == "live"
    assert check_site.check_site(live) == []


def test_program_dashboard_is_part_of_the_site(demo_site):
    page = (demo_site[0] / "program" / "index.html").read_text(encoding="utf-8")
    for needle in ("Headline", "Hours saved", "UiPath licences avoided", "Top failure causes", "Fleet versions"):
        assert needle in page
    prog = json.loads((demo_site[0] / "program" / "program.json").read_text(encoding="utf-8"))
    assert prog["headline"]["hours_saved_30d"] > 0 and prog["money"]["configured"]


def test_no_fleet_data_gives_honest_placeholders(tmp_path):
    out = tmp_path / "site"
    st = build_site.build(out, ROOT)
    assert st["mode"] == "no-fleet-data"
    page = (out / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "No fleet data has been published yet" in page and "--demo" in page
    assert "No fleet data" in (out / "program" / "index.html").read_text(encoding="utf-8")
    assert check_site.check_site(out) == []


# ---- registry integration -------------------------------------------------------------------------------------------
def test_site_with_a_real_registry_publishes_it_and_shows_real_hashes(tmp_path):
    reg = tmp_path / "registry-out"
    res = build_registry.build(ROOT, ROOT / "components" / "automation-registry" / "manifest.yaml", reg, bootstrap=True)
    assert res["ok"], res["errors"]
    site = tmp_path / "site"
    st = build_site.build(site, ROOT, registry=reg, installer_sha_file=None)
    assert (site / "registry" / "manifest.yaml").read_text(encoding="utf-8") == (reg / "manifest.yaml").read_text(encoding="utf-8")
    assert (site / "registry" / "artifacts" / "automations" / "automation-hello-world-1.0.0.zip").is_file()
    assert not (site / "registry" / "build-summary.json").exists()
    text = (site / "install" / "automation-hello-world.html").read_text(encoding="utf-8")
    assert "PLACEHOLDER" not in text
    m = miniyaml.loads((reg / "manifest.yaml").read_text(encoding="utf-8"))
    assert m["automations"]["automation-hello-world"]["versions"]["1.0.0"]["sha256"] in text
    assert "registry manifest" in (site / "index.html").read_text(encoding="utf-8")
    assert check_site.check_site(site) == []


def test_installer_sha_from_file_and_from_the_release(tmp_path, monkeypatch):
    sha = "ab" * 32
    shafile = tmp_path / "sha.json"
    shafile.write_text(json.dumps({"automation-hello-world": sha}), encoding="utf-8")
    site = tmp_path / "site"
    build_site.build(site, ROOT, installer_sha_file=shafile)
    assert sha in (site / "install" / "automation-hello-world.html").read_text(encoding="utf-8")
    cat = json.loads((site / "install" / "catalog.json").read_text(encoding="utf-8"))
    assert cat["automations"]["automation-hello-world"]["installer"]["sha256"] == sha
    assert cat["automations"]["automation-notifier"]["installer"]["sha256"] is None

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return self.body

    seen = []

    def ok(req, timeout=0):
        seen.append(req.full_url)
        return Resp((sha.upper() + "  automation-hello-world-setup.exe\n").encode())

    monkeypatch.setattr(site_install.urllib.request, "urlopen", ok)
    assert site_install.fetch_installer_sha("https://github.com/x/y/releases/download/installers-latest/a-setup.exe") == sha
    assert seen == ["https://github.com/x/y/releases/download/installers-latest/a-setup.exe.sha256"]

    def boom(req, timeout=0):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(site_install.urllib.request, "urlopen", boom)
    assert site_install.fetch_installer_sha("https://example.com/a.exe") is None


# ---- docs ---------------------------------------------------------------------------------------------------------------------
def test_docs_site_renders_every_doc_with_nav_search_and_rewritten_links(demo_site):
    site = demo_site[0]
    stems = {p.stem for p in (ROOT / "docs").glob("*.md")}
    for stem in stems:
        assert (site / "docs" / f"{stem}.html").exists(), stem
    assert (site / "docs" / "assessment.html").exists()
    gs = (site / "docs" / "GETTING_STARTED.html").read_text(encoding="utf-8")
    assert 'href="WRAPPER_CORE_API.html"' in gs and 'href="AUTOMATION_STANDARD.html"' in gs         # .md links became site pages
    assert ".md)" not in gs and 'href="WRAPPER_CORE_API.md"' not in gs
    assert 'id="' in gs and "Edit this page on GitHub" in gs and 'aria-label="Documentation"' in gs
    idx = json.loads((site / "docs" / "search-index.json").read_text(encoding="utf-8"))
    assert any(e["doc"] == "RUNBOOKS.html" and "kill-switch" in (e["heading"] + e["text"]).lower() for e in idx)
    assert all({"doc", "title", "heading", "anchor", "text"} <= set(e) for e in idx)
    assert "docsearch" in (site / "docs" / "index.html").read_text(encoding="utf-8")


def test_rewrite_links_known_unknown_and_external():
    cfg = build_site.SiteConfig()
    a = site_docs.Doc("A", "A", "More", "", "docs/A.md", "")
    b = site_docs.Doc("B", "B", "More", "", "docs/B.md", "")
    by_src = {"docs/A.md": a, "docs/B.md": b}
    md = "[b](B.md#x) [code](../components/wrapper-core/README.md) [web](https://example.com) [anchor](#top) [mail](mailto:a@b.c)"
    out = site_docs.rewrite_links(md, a, by_src, cfg)
    assert "[b](B.html#x)" in out
    assert "(https://github.com/devpatel135/AutoStation/blob/main/components/wrapper-core/README.md)" in out
    assert "(https://example.com)" in out and "(#top)" in out and "(mailto:a@b.c)" in out


def test_parse_md_table_picks_the_runbook_triage_table():
    rows = site_install.parse_md_table((ROOT / "docs" / "RUNBOOKS.md").read_text(encoding="utf-8"), "Failure triage")
    assert rows[0][0].lower() == "signal" and any(r[0] == "MISSED" for r in rows)


# ---- security: nothing from a manifest reaches a page unescaped ----------------------------------------------------------------
PAYLOADS = ['<img src=x onerror=alert(1)>', '"><script>alert(2)</script>', "javascript:alert(3)", "'; DROP TABLE x; --"]


def test_manifest_text_is_escaped_everywhere(tmp_path):
    m = miniyaml.loads((ROOT / "components" / "automation-registry" / "manifest.yaml").read_text(encoding="utf-8"))
    e = m["automations"]["automation-hello-world"]
    e["description"] = PAYLOADS[0]
    e["owner_team"] = PAYLOADS[1]
    e["catalog"] = {"summary": PAYLOADS[1], "systems": [PAYLOADS[0]], "tags": ["evil-tag"], "process_owner": PAYLOADS[0],
                    "links": [{"title": PAYLOADS[1], "url": "https://example.com/ok"}]}
    e["schedule"]["slots"][0]["params"] = {"k": PAYLOADS[0]}
    mp = tmp_path / "manifest.yaml"
    mp.write_text(miniyaml.dumps(m), encoding="utf-8")
    site = tmp_path / "site"
    build_site.build(site, ROOT, manifest=mp)
    for page in site.rglob("*"):
        if page.suffix == ".html" and page.is_file():   # JSON files are data (served as application/json), only pages can execute
            text = page.read_text(encoding="utf-8", errors="replace")
            assert "<script>alert(" not in text, f"raw script payload in {page}"
            assert "<img src=x onerror" not in text, f"raw tag payload in {page}"
            assert 'href="javascript:' not in text.lower(), f"javascript: link in {page}"
    install = (site / "install" / "automation-hello-world.html").read_text(encoding="utf-8")
    assert "&lt;img src=x onerror=alert(1)&gt;" in install and 'href="https://example.com/ok"' in install
    assert check_site.check_site(site) == []


def test_status_json_records_the_build(demo_site):
    st = json.loads((demo_site[0] / "status.json").read_text(encoding="utf-8"))
    assert st["schema_version"] == 1 and st["repo"] == "devpatel135/AutoStation" and "index.html" in st["pages"]
    assert re.fullmatch(r"[0-9a-f]{64}", st["registry"]["manifest_sha256"])
    assert st["installers"]["automation-hello-world"]["url"].endswith("/installers-latest/automation-hello-world-setup.exe")


# ---- migration dashboard hook ------------------------------------------------------------------------------------------
def test_migration_placeholder_then_real_dashboard(tmp_path):
    site = tmp_path / "site"
    build_site.build(site, ROOT)
    if not (ROOT / "tools" / "uipath" / "dashboard.py").exists():
        assert "Run the inventory scanner" in (site / "migration" / "index.html").read_text(encoding="utf-8")
    # a repo that has the generator and an inventory gets the real page (fake generator keeps this test independent of tools/uipath)
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "docs", repo / "docs")
    (repo / "components" / "automation-registry").mkdir(parents=True)
    for f in ("manifest.yaml", "portal.config.json"):
        shutil.copy(ROOT / "components" / "automation-registry" / f, repo / "components" / "automation-registry" / f)
    (repo / "tools" / "uipath").mkdir(parents=True)
    (repo / "tools" / "uipath" / "dashboard.py").write_text(
        "import argparse, pathlib\np = argparse.ArgumentParser()\np.add_argument('--inventory'); p.add_argument('--status'); p.add_argument('--out')\n"
        "a = p.parse_args()\no = pathlib.Path(a.out); o.mkdir(parents=True, exist_ok=True)\n"
        "(o / 'index.html').write_text('<!doctype html><html><body><div class=\"page\"><h1>FAKE MIGRATION</h1></div></body></html>')\n", encoding="utf-8")
    (repo / "migration").mkdir()
    (repo / "migration" / "inventory.json").write_text("{}", encoding="utf-8")
    site2 = tmp_path / "site2"
    build_site.build(site2, repo)
    page = (site2 / "migration" / "index.html").read_text(encoding="utf-8")
    assert "FAKE MIGRATION" in page and "AutoStation home" in page
    # a failing generator degrades to a placeholder with the reason, it does not break the site
    (repo / "tools" / "uipath" / "dashboard.py").write_text("import sys\nsys.exit('boom')\n", encoding="utf-8")
    site3 = tmp_path / "site3"
    st = build_site.build(site3, repo)
    assert "boom" in (site3 / "migration" / "index.html").read_text(encoding="utf-8") and any("boom" in n for n in st["notes"])


# ---- the checker itself -----------------------------------------------------------------------------------------------------
def test_checker_catches_broken_links_anchors_and_incomplete_install_pages(tmp_path):
    site = tmp_path / "s"
    for rel in check_site.REQUIRED:
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        (site / rel).write_text("<html></html>" if rel.endswith(".html") else ("{}" if rel.endswith(".json") else ""), encoding="utf-8")
    (site / "index.html").write_text('<a href="nope.html">x</a><a href="docs/index.html#missing">y</a><a href="javascript:alert(1)">z</a>'
                                     '<script src="https://cdn.example.com/x.js"></script><a href="../outside.html">o</a>', encoding="utf-8")
    (site / "install" / "catalog.json").write_text(json.dumps({"automations": {"auto-a": {"installer": {"url": "https://x/auto-a-setup.exe"}}}}), encoding="utf-8")
    probs = "\n".join(check_site.check_site(site))
    for needle in ("broken link nope.html", "anchor #missing", "javascript: URL", "external script", "escapes the site", "install page missing for auto-a"):
        assert needle in probs, needle
    (site / "install" / "auto-a.html").write_text("<html>no download here</html>", encoding="utf-8")
    probs = "\n".join(check_site.check_site(site))
    assert "no download link" in probs and "no SHA-256 block" in probs and "no verify instructions" in probs


# ---- bootstrap.ps1 (really executed where PowerShell exists) ------------------------------------------------------------------
PS = shutil.which("powershell") if sys.platform == "win32" else shutil.which("pwsh")


def run_ps(script: Path, *args, tmp: Path) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    env.pop("PSModulePath", None) if sys.platform == "win32" else None
    cmd = [PS, "-NoProfile", "-NonInteractive"] + (["-ExecutionPolicy", "Bypass"] if sys.platform == "win32" else []) + ["-File", str(script), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env, cwd=str(tmp))
    # PowerShell wraps long error lines at the console width (narrow on CI): collapse whitespace so phrase checks are stable
    import re
    ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")  # PowerShell 7 colours error text and re-applies the colour on every wrapped line
    # PowerShell 7's concise error view prefixes every wrapped continuation line with "| "
    r.stdout = " ".join(ansi.sub("", r.stdout or "").replace("|", " ").split())
    r.stderr = " ".join(ansi.sub("", r.stderr or "").replace("|", " ").split())
    return r


@pytest.fixture
def boot(tmp_path, demo_site):
    script = tmp_path / "bootstrap.ps1"
    script.write_text((demo_site[0] / "install" / "bootstrap.ps1").read_text(encoding="utf-8"), encoding="utf-8")
    exe = tmp_path / "automation-hello-world-setup.exe"
    exe.write_bytes(b"MZ not a real installer, only bytes to hash")
    import hashlib
    sha = hashlib.sha256(exe.read_bytes()).hexdigest()

    def catalog(**over):
        entry = {"status": "active", "paused_reason": "", "rings": {"dev": "1.0.0", "pilot": "1.0.0", "broad": "1.0.0"},
                 "installer": {"url": "https://example.invalid/automation-hello-world-setup.exe", "sha256": sha}}
        entry.update(over)
        p = tmp_path / "catalog.json"
        p.write_text(json.dumps({"schema_version": 1, "automations": {"automation-hello-world": entry}}), encoding="utf-8")
        return str(p)

    return script, exe, sha, catalog


def test_bootstrap_script_never_pipes_the_network_into_iex(demo_site):
    text = (demo_site[0] / "install" / "bootstrap.ps1").read_text(encoding="utf-8")
    assert "Invoke-Expression" not in text and not re.search(r"\biex\b", text.replace("Invoke-WebRequest", ""))
    assert "@@PAGES_URL@@" not in text and "HASH MISMATCH" in text and "StartsWith('https://')" in text


@pytest.mark.skipif(PS is None, reason="PowerShell not available")
def test_bootstrap_verifies_hash_and_refuses_everything_else(boot, tmp_path):
    script, exe, sha, catalog = boot
    ok = run_ps(script, "-Automation", "automation-hello-world", "-Ring", "pilot", "-CatalogUrl", catalog(), "-InstallerPath", str(exe), "-NoRun", tmp=tmp_path)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "Hash verified" in ok.stdout and "1.0.0" in ok.stdout
    bad = run_ps(script, "-Automation", "automation-hello-world", "-CatalogUrl", catalog(installer={"url": "https://x/y.exe", "sha256": "0" * 64}),
                 "-InstallerPath", str(exe), "-NoRun", tmp=tmp_path)
    assert bad.returncode != 0 and "HASH MISMATCH" in (bad.stdout + bad.stderr)
    nosha = run_ps(script, "-Automation", "automation-hello-world", "-CatalogUrl", catalog(installer={"url": "https://x/y.exe", "sha256": None}),
                   "-InstallerPath", str(exe), "-NoRun", tmp=tmp_path)
    assert nosha.returncode != 0 and "cannot be verified" in (nosha.stdout + nosha.stderr)
    allowed = run_ps(script, "-Automation", "automation-hello-world", "-CatalogUrl", catalog(installer={"url": "https://x/y.exe", "sha256": None}),
                     "-InstallerPath", str(exe), "-NoRun", "-AllowUnverified", tmp=tmp_path)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    unknown = run_ps(script, "-Automation", "nope", "-CatalogUrl", catalog(), "-InstallerPath", str(exe), "-NoRun", tmp=tmp_path)
    assert unknown.returncode != 0 and "automation-hello-world" in (unknown.stdout + unknown.stderr)
    unreleased = run_ps(script, "-Automation", "automation-hello-world", "-Ring", "dev", "-CatalogUrl", catalog(rings={"broad": "1.0.0"}),
                        "-InstallerPath", str(exe), "-NoRun", tmp=tmp_path)
    assert unreleased.returncode != 0 and "not released" in (unreleased.stdout + unreleased.stderr)
    paused = run_ps(script, "-Automation", "automation-hello-world", "-CatalogUrl", catalog(status="paused", paused_reason="bad data"),
                    "-InstallerPath", str(exe), "-NoRun", tmp=tmp_path)
    assert paused.returncode == 0 and "PAUSED" in (paused.stdout + paused.stderr)
