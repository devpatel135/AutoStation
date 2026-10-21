#!/usr/bin/env python3
"""Build the whole AutoStation site (GitHub Pages): landing, install center, docs, registry portal, dashboards, program, migration.

    python tools/build_site.py --out site --demo                       # everything, with synthetic fleet data (clearly labelled)
    python tools/build_site.py --out site --registry registry-out      # real registry (from tools/build_registry.py) + no fleet data yet
    python tools/build_site.py --out site --live-data ingest-out       # real fleet data: DIR/archive (+ DIR/manifest.yaml)
    python tools/build_site.py --out site --demo-data e2e-out          # any archive, labelled DEMO DATA (e.g. tools/e2e_demo.py output)

Layout of ``site/``::

    index.html  install/ (index, <automation>.html, catalog.json, bootstrap.ps1)  docs/ (*.html, search-index.json, assessment.html)
    portal/ (registry catalog + admin panel)  dashboard/  program/  migration/  registry/ (manifest.yaml, artifacts/...)  status.json

Everything is static. Nothing is fetched at build time unless ``--fetch-installer-sha`` is given (reads the ``.sha256`` release assets so
install pages can show the installer hash).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "site"))

import site_common as sc  # noqa: E402
from site_common import ROOT, SiteConfig, esc, now_iso, write  # noqa: E402
import site_docs, site_install, site_landing  # noqa: E402

from registry import store as reg_store  # noqa: E402
from registry.portal import build as portal_build  # noqa: E402
from wrapper_core import miniyaml  # noqa: E402

DEMO_PROGRAM_YAML = """currency: USD
hourly_cost: 55
uipath: {license_cost_per_robot_per_year: 12000, robots_retired: 4, orchestrator_cost_per_year: 30000}
llm_cost_per_1k_calls: 0
"""


def _inject(html_path: Path, snippet: str) -> None:
    if not html_path.is_file():
        return
    text = html_path.read_text(encoding="utf-8")
    marker = '<div class="page">'
    if marker in text and snippet not in text:
        html_path.write_text(text.replace(marker, marker + snippet, 1), encoding="utf-8")


def decorate_dashboard_pages(out: Path, cfg: SiteConfig) -> None:
    back = '<p style="margin:.2rem 0 .8rem"><a href="../index.html">&larr; AutoStation home</a> &middot; <a href="../program/index.html">Program</a> &middot; <a href="../dashboard/index.html">Fleet dashboard</a> &middot; <a href="../portal/index.html">Catalog</a></p>'
    demo = ('<div style="border:1px dashed #fbbf24;border-radius:6px;padding:.5rem .9rem;margin-bottom:1rem;font-size:12.5px">'
            "<strong>DEMO DATA.</strong> Synthetic fleet history for illustration - not your fleet. See docs/PIPELINES.md to enable live analytics.</div>") if cfg.demo else ""
    for rel in ("dashboard/index.html", "program/index.html"):
        _inject(out / rel, back + demo)


def sha256_of(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build_dashboards(out: Path, archive: Path, manifest: Path | None, cfg: SiteConfig, program_cfg: Path | None, now=None) -> dict:
    """Run the logging pipeline into a temp dir, publish dashboard/ and program/ separately."""
    from automation_logging import pipeline
    old_env = {k: os.environ.get(k) for k in ("AUTOMAGIX_HOME", "AUTOSTATION_LLM_ENDPOINT")}
    with tempfile.TemporaryDirectory(prefix="amx-site-") as tmp:
        os.environ["AUTOMAGIX_HOME"] = str(Path(tmp) / "home")
        if cfg.demo and not os.environ.get("AUTOSTATION_LLM_ENDPOINT"):
            os.environ["AUTOSTATION_LLM_ENDPOINT"] = "mock://"   # demo diagnoses come from the offline mock model
        try:
            tmp_out = Path(tmp) / "pub"
            summary = pipeline.build(archive, manifest, Path(tmp) / "state", tmp_out, now, program_config=program_cfg)
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        if (out / "dashboard").exists():
            shutil.rmtree(out / "dashboard")
        if (out / "program").exists():
            shutil.rmtree(out / "program")
        prog = tmp_out / "program"
        if prog.is_dir():
            shutil.copytree(prog, out / "program")
            shutil.rmtree(prog)
        shutil.copytree(tmp_out, out / "dashboard")
    return {"errors": summary.get("errors", []), "stages": {k: v for k, v in summary.get("stages", {}).items() if k != "alert"}}


def build_migration(out: Path, repo: Path, cfg: SiteConfig, inventory: Path | None, status: Path | None) -> str:
    dash = repo / "tools" / "uipath" / "dashboard.py"
    inv = inventory or (repo / "migration" / "inventory.json")
    stat = status or (repo / "migration" / "status.yaml")
    problem = ""
    if dash.is_file() and Path(inv).is_file():
        args = [sys.executable, str(dash), "--inventory", str(inv), "--out", str(out / "migration")]
        if Path(stat).is_file():
            args += ["--status", str(stat)]
        r = subprocess.run(args, capture_output=True, text=True, cwd=repo)
        if r.returncode == 0 and (out / "migration" / "index.html").is_file():
            _inject(out / "migration" / "index.html", '<p style="margin:.2rem 0 .8rem"><a href="../index.html">&larr; AutoStation home</a></p>')
            return "migration/index.html"
        problem = f"The migration dashboard generator failed: {(r.stderr or r.stdout).strip()[-300:]}"
        cfg.notes.append(problem)
    msg = ("<p>The UiPath migration dashboard shows inventory, wave progress and licence retirement. It is generated from an inventory of your UiPath projects.</p>"
           "<ol><li>Run the inventory scanner over your UiPath projects (<code>python tools/uipath/inventory.py &lt;projects-dir&gt; --out migration/</code>) and commit "
           "<code>migration/inventory.json</code>.</li><li>Track status in <code>migration/status.yaml</code>.</li><li>Re-run the <em>site</em> workflow; this page fills in.</li></ol>"
           f"<p><a href=\"../docs/index.html\">Documentation</a> &middot; <a href=\"{esc(cfg.repo_url)}/issues/new?template=uipath-migration-request.yml\">Request a migration</a></p>")
    if problem:
        msg = f'<div class="warnbox">{esc(problem)}</div>' + msg
    return site_landing.build_placeholder(out, cfg, "migration/index.html", "UiPath migration", "migration", msg)


def build(out: Path, repo: Path = ROOT, *, registry: Path | None = None, manifest: Path | None = None, demo: bool = False,
          demo_data: Path | None = None, live_data: Path | None = None, github_repo: str | None = None, fetch_installer_sha: bool = False,
          installer_sha_file: Path | None = None, program_config: Path | None = None, migration_inventory: Path | None = None,
          migration_status: Path | None = None, installer_url_template: str | None = None, now=None) -> dict:
    out, repo = Path(out), Path(repo)
    if out.exists() and any(out.iterdir()) and not any((out / m).exists() for m in ("status.json", ".nojekyll")):
        raise SystemExit(f"error: --out {out} is not empty and does not look like a site build; refusing to wipe it")
    if out.exists():  # clear the contents, not the directory itself (a dev server or a Windows shell may be sitting in it)
        for child in out.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)

    cfg = SiteConfig(repo=github_repo or os.environ.get("GITHUB_REPOSITORY") or sc.DEFAULT_REPO, built_at=now_iso(), commit=sc.git_commit())
    portal_cfg_file = repo / "components" / "automation-registry" / "portal.config.json"
    portal_cfg = sc.read_json(portal_cfg_file, {}) or {}
    cfg.installer_url_template = installer_url_template or portal_cfg.get("installer_url_template") or sc.DEFAULT_INSTALLER_TEMPLATE
    tmp_root = Path(tempfile.mkdtemp(prefix="amx-site-build-"))

    # ---- data sources -----------------------------------------------------------------------------------
    data_dir = None
    if demo:
        from automation_logging import sample
        data_dir = tmp_root / "demo"
        sample.write(data_dir, now, 7, 14)
        cfg.demo = True
    elif demo_data:
        data_dir, cfg.demo = Path(demo_data), True
    elif live_data:
        data_dir = Path(live_data)
    archive = (data_dir / "archive") if data_dir and (data_dir / "archive").is_dir() else None
    data_manifest = None
    if data_dir:
        for cand in (data_dir / "manifest.yaml", data_dir / "registry" / "manifest.yaml"):
            if cand.is_file():
                data_manifest = cand
                break

    # ---- registry manifest for catalog/install pages ------------------------------------------------------
    if registry:
        manifest_path = Path(registry) / "manifest.yaml"
    else:
        manifest_path = Path(manifest) if manifest else repo / "components" / "automation-registry" / "manifest.yaml"
        cfg.notes.append("no --registry given: install pages use the source manifest (hashes may be placeholders; run tools/build_registry.py)")
    m = reg_store.load_manifest(manifest_path)

    sc.write_assets(out)
    pages: list[str] = ["index.html"]

    # ---- dashboards first (the portal wants their signals/runs) --------------------------------------------
    program_cfg = Path(program_config) if program_config else None
    if cfg.demo and not program_cfg:
        program_cfg = tmp_root / "program.yaml"
        program_cfg.write_text(DEMO_PROGRAM_YAML, encoding="utf-8")
    elif not program_cfg:
        cand = repo / "components" / "automation-logging" / "program.yaml"
        program_cfg = cand if cand.is_file() else None
    dash_info: dict = {}
    if archive:
        dash_info = build_dashboards(out, archive, data_manifest or manifest_path, cfg, program_cfg, now)
        decorate_dashboard_pages(out, cfg)
        pages += ["dashboard/index.html", "program/index.html"]
        for e in dash_info.get("errors", []):
            cfg.notes.append(f"dashboard stage {e.get('stage')}: {e.get('error')}")
    else:
        msg = ("<p>No fleet data has been published yet, so there is nothing to chart. Endpoints push run logs to the logging repository; the analytics workflow turns them "
               "into this dashboard.</p><ol><li>Install an automation (see the <a href=\"../install/index.html\">install center</a>).</li>"
               "<li>Enable live analytics (<code>AUTOSTATION_LIVE_ANALYTICS</code>, see <a href=\"../docs/PIPELINES.html\">PIPELINES</a>).</li></ol>"
               "<p>Want to see what it looks like? Build the site with <code>--demo</code>.</p>")
        for rel, title, active in (("dashboard/index.html", "Fleet dashboard", "dashboard"), ("program/index.html", "Program dashboard", "program")):
            pages.append(site_landing.build_placeholder(out, cfg, rel, title, active, msg))

    # ---- registry portal --------------------------------------------------------------------------------------
    pcfg = dict(portal_cfg)
    pcfg.update({"home_url": "../index.html", "github_repo": cfg.repo, "platform": "github", "installer_url_template": cfg.installer_url_template})
    pcfg_file = tmp_root / "portal.config.json"
    pcfg_file.write_text(json.dumps(pcfg), encoding="utf-8")
    sig = out / "dashboard" / "signals.json"
    runs = out / "dashboard" / "runs-latest.json"
    gov = repo / "docs" / "GOVERNANCE.md"
    portal_build.build_portal(manifest_path, out / "portal", signals=str(sig) if sig.is_file() else None,
                              runs=str(runs) if runs.is_file() else None, config_path=pcfg_file,
                              governance=str(gov) if gov.is_file() else None)
    pages += ["portal/index.html", "portal/admin.html"]

    # ---- registry files for self-heal (endpoints read <pages>/registry/manifest.yaml) -----------------------
    if registry:
        shutil.copytree(registry, out / "registry")
        for junk in ("build-summary.json",):
            (out / "registry" / junk).unlink(missing_ok=True)
        pages.append("registry/manifest.yaml")

    # ---- install center -----------------------------------------------------------------------------------------
    shas: dict[str, str | None] = {}
    given = sc.read_json(Path(installer_sha_file), {}) if installer_sha_file else {}
    for name in (m.get("automations") or {}):
        s = given.get(name)
        if not s and fetch_installer_sha:
            s = site_install.fetch_installer_sha(cfg.installer_url(name))
        shas[name] = s
    runtimes = {}
    for name in (m.get("automations") or {}):
        d = repo / "components" / "automations" / name / "automation.yaml"
        if d.is_file():
            runtimes[name] = (miniyaml.loads(d.read_text(encoding="utf-8")) or {}).get("runtime")
    rb = repo / "docs" / "RUNBOOKS.md"
    inst = site_install.build_install_center(out, m, cfg, runbooks_md=rb.read_text(encoding="utf-8") if rb.is_file() else "", runtimes=runtimes, shas=shas)
    pages += inst["pages"] + ["install/catalog.json", "install/bootstrap.ps1"]

    # ---- docs -----------------------------------------------------------------------------------------------------
    docs_info = site_docs.build_docs_site(out, repo, cfg)
    pages += docs_info["pages"]

    # ---- migration ---------------------------------------------------------------------------------------------
    pages.append(build_migration(out, repo, cfg, Path(migration_inventory) if migration_inventory else None,
                                 Path(migration_status) if migration_status else None))

    # ---- landing + 404 + status -------------------------------------------------------------------------------
    have = {"migration": True} if (out / "migration" / "index.html").is_file() else {}
    if (out / "registry" / "manifest.yaml").is_file():
        have["registry"] = True
    site_landing.build_landing(out, m, cfg, set(docs_info["docs"]), have)
    nf = ('<h1>Page not found</h1><p>That page does not exist (or has not been built yet). Try the <a href="/">home page</a>, the install center, or search the docs.</p>')
    write(out, "404.html", sc.layout("Not found", nf.replace('href="/"', f'href="{esc(cfg.pages_url)}"'), cfg=cfg, depth=0))
    status = {"schema_version": 1, "built_at": cfg.built_at, "commit": cfg.commit, "repo": cfg.repo, "pages_url": cfg.pages_url,
              "mode": "demo" if cfg.demo else ("live" if archive else "no-fleet-data"),
              "registry": {"manifest_sha256": sha256_of(manifest_path), "automations": len(m.get("automations") or {}),
                           "components": sorted((m.get("components") or {}))},
              "installers": {n: {"url": cfg.installer_url(n), "sha256": s} for n, s in shas.items()},
              "docs": docs_info["docs"], "pages": sorted(set(pages)), "notes": cfg.notes, "dashboard": dash_info.get("stages", {})}
    write(out, "status.json", json.dumps(status, indent=2, sort_keys=True) + "\n")
    shutil.rmtree(tmp_root, ignore_errors=True)
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="site")
    ap.add_argument("--repo", default=str(ROOT), help="repository root (default: this checkout)")
    ap.add_argument("--registry", default=None, help="registry dir produced by tools/build_registry.py (also published under /registry/)")
    ap.add_argument("--manifest", default=None, help="manifest to use when no --registry is given")
    ap.add_argument("--demo", action="store_true", help="generate synthetic fleet data and label every dashboard DEMO DATA")
    ap.add_argument("--demo-data", default=None, help="directory with archive/ (+ manifest.yaml); labelled DEMO DATA")
    ap.add_argument("--live-data", default=None, help="directory with archive/ (+ manifest.yaml); real fleet data")
    ap.add_argument("--github-repo", default=None, help="owner/name (default $GITHUB_REPOSITORY or devpatel135/AutoStation)")
    ap.add_argument("--fetch-installer-sha", action="store_true", default=os.environ.get("AUTOSTATION_FETCH_SHA") == "1",
                    help="read <automation>-setup.exe.sha256 from the installers-latest release at build time")
    ap.add_argument("--installer-sha-file", default=None, help="JSON {automation: sha256} instead of fetching")
    ap.add_argument("--program-config", default=None)
    ap.add_argument("--migration-inventory", default=None)
    ap.add_argument("--migration-status", default=None)
    a = ap.parse_args(argv)
    st = build(Path(a.out), Path(a.repo), registry=Path(a.registry) if a.registry else None, manifest=Path(a.manifest) if a.manifest else None,
               demo=a.demo, demo_data=Path(a.demo_data) if a.demo_data else None, live_data=Path(a.live_data) if a.live_data else None,
               github_repo=a.github_repo, fetch_installer_sha=a.fetch_installer_sha,
               installer_sha_file=Path(a.installer_sha_file) if a.installer_sha_file else None,
               program_config=Path(a.program_config) if a.program_config else None,
               migration_inventory=Path(a.migration_inventory) if a.migration_inventory else None,
               migration_status=Path(a.migration_status) if a.migration_status else None)
    print(f"site built in {a.out}: {len(st['pages'])} page(s), mode {st['mode']}, {st['registry']['automations']} automation(s)")
    for n in st["notes"]:
        print("note:", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
