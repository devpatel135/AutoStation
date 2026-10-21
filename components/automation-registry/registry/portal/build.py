"""Static portal build: ``python -m registry build-portal --manifest manifest.yaml --out public``."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .. import store
from ..checks import validate_all
from . import admin, health, pages
from .common import DEFAULT_CONFIG, describe_schedule, installer_rel, rings_of


def load_config(config_path: Path | None, manifest_dir: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.environ.get("CI_SERVER_URL"):
        cfg["gitlab_url"] = os.environ["CI_SERVER_URL"]
    if os.environ.get("CI_PROJECT_PATH"):
        cfg["project_path"] = os.environ["CI_PROJECT_PATH"]
    if os.environ.get("CI_DEFAULT_BRANCH"):
        cfg["default_branch"] = cfg["ops_ref"] = os.environ["CI_DEFAULT_BRANCH"]
    path = config_path or (manifest_dir / "portal.config.json")
    if path.is_file():
        try:
            cfg.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if isinstance(v, str)})
        except ValueError as exc:
            raise SystemExit(f"error: {path} is not valid JSON: {exc}")
    return cfg


def find_governance(explicit: str | None, manifest_dir: Path) -> str | None:
    cands = [Path(explicit)] if explicit else []
    here = manifest_dir.resolve()
    cands += [here / "docs" / "GOVERNANCE.md"] + [p / "docs" / "GOVERNANCE.md" for p in list(here.parents)[:4]]
    for c in cands:
        if c.is_file():
            return c.read_text(encoding="utf-8")
    return None


def _merge_triggers(base: dict, extra_path: str | None) -> dict:
    doc = {"schema_version": 1, "triggers": list(base.get("triggers", []))}
    if extra_path:
        extra = store.load_triggers(Path(extra_path))
        seen = {t.get("id") for t in doc["triggers"]}
        doc["triggers"] += [t for t in extra["triggers"] if t.get("id") not in seen]
    return doc


def _write(out: Path, rel: str, text: str, files: list[str]) -> None:
    p = out / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:  # Path.write_text(newline=) needs Python 3.10
        fh.write(text)
    files.append(rel)


def build_portal(manifest_path: Path, out_dir: Path, *, signals=None, runs=None, config_path: Path | None = None,
                 governance: str | None = None, extra_triggers: str | None = None, artifacts_dir: str | None = None) -> dict:
    manifest_path, out_dir = Path(manifest_path), Path(out_dir)
    manifest_dir = manifest_path.parent
    text = manifest_path.read_text(encoding="utf-8")
    m = store.load_manifest(manifest_path)
    report = validate_all(m, allow_placeholders=True)
    if report.errors:
        raise SystemExit("refusing to publish an invalid manifest:\n  - " + "\n  - ".join(report.errors))
    cfg = load_config(config_path, manifest_dir)
    sig = health.load_signals(signals)
    run_list = health.load_runs(runs)
    names = list((m.get("automations") or {}).keys())
    hl = health.summarize(names, sig, run_list)
    trig_doc = _merge_triggers(store.load_triggers(manifest_dir / store.TRIGGERS_FILE), extra_triggers)
    hist = store.read_history(manifest_path)
    order = rings_of(m)

    here = manifest_dir.resolve()
    if out_dir.resolve() == here or out_dir.resolve() in here.parents:
        raise SystemExit(f"error: --out {out_dir} would overwrite the manifest directory")
    if out_dir.exists() and any(out_dir.iterdir()) and not (out_dir / "index.json").exists():
        raise SystemExit(f"error: --out {out_dir} is not empty and does not look like a portal build; refusing to wipe it")
    if out_dir.exists():
        for child in out_dir.iterdir():  # clean rebuild; never leave stale pages for removed automations
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []

    _write(out_dir, "index.html", pages.build_index(m, cfg, hl, text), files)
    _write(out_dir, "compatibility.html", pages.build_compat(m, cfg, text), files)
    _write(out_dir, "governance.html", pages.build_governance(find_governance(governance, manifest_dir), cfg, text), files)
    _write(out_dir, "admin.html", admin.build_admin(m, cfg, hl, sig, text, trig_doc["triggers"]), files)
    for name, entry in (m.get("automations") or {}).items():
        mine = [h for h in hist if h["target"] == name]
        pend = [t for t in trig_doc["triggers"] if t.get("automation") == name]
        _write(out_dir, f"automation/{name}.html", pages.build_detail(name, entry or {}, m, cfg, hl.get(name), mine, pend, text), files)

    _write(out_dir, "manifest.yaml", text, files)
    _write(out_dir, "triggers.json", json.dumps(trig_doc, indent=2, sort_keys=True) + "\n", files)
    _write(out_dir, "portal.config.json", json.dumps({k: cfg[k] for k in ("title", "gitlab_url", "project_path", "default_branch")}, indent=2) + "\n", files)

    index = {"schema_version": 1, "generated_at": store.now_iso(), "fleet": m.get("fleet") or {}, "rings": order,
             "components": {n: {"rings": (c or {}).get("rings") or {}, "versions": sorted(((c or {}).get("versions") or {}))}
                            for n, c in (m.get("components") or {}).items()},
             "automations": []}
    for name, e in (m.get("automations") or {}).items():
        e = e or {}
        cat = e.get("catalog") or {}
        inst = {}
        for r in order:
            v = (e.get("rings") or {}).get(r)
            if v:
                inst[r] = {"version": v, "installer": installer_rel(cfg, name, v),
                           "sha256": ((e.get("versions") or {}).get(str(v)) or {}).get("sha256")}
        index["automations"].append({
            "name": name, "status": e.get("status"), "paused_reason": e.get("paused_reason") or "", "owner": e.get("owner"),
            "owner_team": e.get("owner_team"), "description": e.get("description"), "summary": cat.get("summary"),
            "systems": cat.get("systems") or e.get("systems") or [], "data_classification": e.get("data_classification"),
            "tags": cat.get("tags") or [], "rings": e.get("rings") or {}, "schedule": describe_schedule(e), "installers": inst,
            "health": (hl.get(name) or {}).get("status")})
    _write(out_dir, "index.json", json.dumps(index, indent=2, sort_keys=True) + "\n", files)

    for sub, src in (("installers", manifest_dir / cfg.get("installers_path", "installers")),
                     ("artifacts", Path(artifacts_dir) if artifacts_dir else None)):
        if src and src.is_dir():
            shutil.copytree(src, out_dir / sub, dirs_exist_ok=True)
            files.append(f"{sub}/ (copied)")
    return {"out": str(out_dir), "files": sorted(files)}
