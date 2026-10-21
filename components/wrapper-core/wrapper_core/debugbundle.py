"""``python -m wrapper_core debug-bundle [--run RUN_ID] [--out FILE]`` - one redacted zip to attach to a support ticket.

Contains: doctor output, versions, platform.json without anything secret-like, log tails, the failed run's error/events/stderr, scheduled-task
state, the registry cache, remediation history and a few environment facts. Never contains the ``secrets/`` folder, tokens, key material,
browser profiles, screenshots or evidence files. Every text is passed through ``remediation.scrub_text`` and every file is size-capped.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from ._version import __version__
from .paths import Paths, platform_config
from .remediation import config_enabled, load_state, scrub_text
from .util import iso, read_json, utcnow

MAX_TOTAL = 5 * 1024 * 1024
MAX_FILE = 256 * 1024
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9T.-]{7,59}$")
_SECRETISH = re.compile(r"(?i)token|secret|password|passwd|credential|private|api[_-]?key|(^|_)key($|_)")


def _tail_lines(path: Path, n: int, max_bytes: int = MAX_FILE) -> str:
    try:
        data = path.read_bytes()[-max_bytes:].decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-n:])


def sanitize_config(cfg: dict) -> dict:
    out = {}
    for k, v in cfg.items():
        if k == "registry_pubkeys" or not _SECRETISH.search(str(k)):
            out[k] = json.loads(scrub_text(json.dumps(v, default=str))) if not isinstance(v, str) else scrub_text(v, 500)
    return out


_CLASSIFIED = ("confidential", "restricted")
_META_KEYS = ("run_id", "status", "started_at", "ended_at", "automation", "automation_version", "slot", "items", "duration_ms", "remediated")


def _classification(paths: Paths, automation: Optional[str]) -> str:
    """data_classification of an automation (automation.yaml, else the cached manifest); unknown -> internal."""
    if not automation:
        return "internal"
    try:
        from . import miniyaml
        f = paths.automation_dir(automation) / "automation.yaml"
        d = miniyaml.load(f) if f.exists() else {}
        if isinstance(d, dict) and d.get("data_classification"):
            return str(d["data_classification"]).lower()
        if paths.manifest_cache.exists():
            m = miniyaml.loads(paths.manifest_cache.read_text(encoding="utf-8"))
            c = ((m.get("automations") or {}).get(automation) or {}).get("data_classification")
            if c:
                return str(c).lower()
    except Exception:  # noqa: BLE001
        pass
    return "internal"


def _automation_of_run(paths: Paths, run_id: Optional[str]) -> Optional[str]:
    if not run_id:
        return None
    try:
        for f in (paths.state / "runs").glob("*.json"):
            if (read_json(f, {}) or {}).get("run_id") == run_id:
                return f.stem
    except OSError:
        pass
    return None


def _last_run_id(paths: Paths, automation: Optional[str]) -> Optional[str]:
    if not automation:
        return None
    rid = (read_json(paths.state / "runs" / f"{automation}.json", {}) or {}).get("run_id")
    return rid if isinstance(rid, str) and _RUN_ID.match(rid) else None


def _latest_run_dir(paths: Paths) -> Optional[Path]:
    try:
        dirs = [d for d in paths.runs.iterdir() if d.is_dir()]
    except OSError:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def _task_state() -> str:
    if sys.platform != "win32":
        return "(scheduled tasks are Windows only)"
    try:
        r = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "(schtasks unavailable)"
    return "\n".join(ln for ln in (r.stdout or "").splitlines() if "AUTOMAGIX" in ln)


def _env_facts(paths: Paths) -> dict:
    from . import doctor
    free = None
    try:
        free = shutil.disk_usage(str(paths.root if paths.root.exists() else paths.root.parent)).free // (1024 * 1024)
    except OSError:
        pass
    return {"generated_at": iso(), "wrapper_core": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
            "edge": doctor._edge_path(), "free_mb": free, "proxy_env_set": sorted(k for k in os.environ if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")),
            "cwd_is_install_home": str(Path.cwd()) == str(paths.root)}


def create_bundle(paths: Optional[Paths] = None, run_id: Optional[str] = None, reason: str = "manual", out: Optional[Path] = None,
                  max_total: int = MAX_TOTAL, automation: Optional[str] = None) -> Path:
    """Write the zip atomically (a temp file renamed at the end: a failure never leaves a partial bundle). ``automation`` selects THAT
    automation's latest run when no run id is given (never another automation's run); runs of confidential/restricted automations
    contribute metadata only, because their output files can contain the business data the classification protects."""
    paths = paths or Paths()
    if run_id is not None and not _RUN_ID.match(run_id):
        raise ValueError("invalid run id")
    if run_id is None and automation:
        run_id = _last_run_id(paths, automation)
    run_dir = (paths.runs / run_id) if run_id else (None if automation else _latest_run_dir(paths))
    if run_dir is not None and not run_dir.is_dir():
        run_dir = None
    if out is None:
        d = paths.root / "debug-bundles"
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"bundle-{utcnow().strftime('%Y%m%dT%H%M%S')}-{(run_dir.name[-8:] if run_dir else 'norun')}.zip"
    out = Path(out)
    files: List[Dict] = []
    total = 0
    truncated = False
    run_automation = automation or _automation_of_run(paths, run_dir.name if run_dir else None)
    classified = _classification(paths, run_automation) in _CLASSIFIED
    part = out.with_name(out.name + ".part")
    try:
        _write_bundle(paths, part, run_dir, run_automation, classified, reason, max_total, files)
        os.replace(str(part), str(out))
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return out


def _write_bundle(paths: Paths, out: Path, run_dir: Optional[Path], run_automation: Optional[str], classified: bool, reason: str,
                  max_total: int, files: List[Dict]) -> None:
    total = 0
    truncated = False
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        def add(name: str, text: str, force: bool = False) -> None:
            nonlocal total, truncated
            text = scrub_text(text)
            data = text.encode("utf-8", "replace")[:MAX_FILE]
            if not force and total + len(data) > max_total:
                truncated = True
                return
            zf.writestr(name, data)
            total += len(data)
            files.append({"name": name, "bytes": len(data)})

        try:
            from . import doctor
            res = [{k: scrub_text(v, 300) if isinstance(v, str) else v for k, v in r.items()} for r in doctor.run_checks(paths, online=False)]
        except Exception as exc:  # noqa: BLE001 - a bundle is most needed when things are broken
            res = [{"check": "doctor", "status": "FAIL", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}]
        add("doctor.json", json.dumps(res, indent=2))
        try:
            cfg = platform_config(paths)
        except Exception:  # noqa: BLE001
            cfg = read_json(paths.platform_json, {}) or {}
        add("platform.json", json.dumps(sanitize_config(cfg if isinstance(cfg, dict) else {}), indent=2))
        add("environment.json", json.dumps(_env_facts(paths), indent=2, default=str))
        add("installed.json", json.dumps(read_json(paths.installed_json, {}), indent=2, default=str))
        add("scheduled_tasks.txt", _task_state())
        add("tasks_state.json", json.dumps(read_json(paths.state / "tasks.json", {}), indent=2, default=str))
        last = {}
        try:
            for f in (paths.state / "runs").glob("*.json"):
                rec = read_json(f, {})
                if isinstance(rec, dict) and _classification(paths, f.stem) in _CLASSIFIED:
                    rec = {k: rec[k] for k in _META_KEYS if k in rec}  # no synopsis/result text for classified automations
                last[f.stem] = rec
        except OSError:
            pass
        add("last_runs.json", json.dumps(last, indent=2, default=str))
        add("remediation_state.json", json.dumps(load_state(paths), indent=2, default=str))
        add("remediation_enabled.txt", "enabled" if config_enabled(cfg if isinstance(cfg, dict) else {})[0] else "disabled")
        add("logs/wrapper.log.tail", _tail_lines(paths.logs / "wrapper.log", 300))
        add("logs/remediation.jsonl.tail", _tail_lines(paths.logs / "remediation.jsonl", 100))
        for nm in ("manifest.cache.yaml", "manifest.cache.yaml.sig"):
            f = paths.state / nm
            if f.exists():
                add(f"registry/{nm}", f.read_text(encoding="utf-8", errors="replace")[:MAX_FILE])
        if run_dir is not None and classified:
            err = read_json(run_dir / "error.json", {}) or {}
            add("run/summary.json", json.dumps({"note": "this automation is classified confidential/restricted: run output files are not included",
                                                "error_category": err.get("category"), "error_type": err.get("type")}, indent=2, default=str))
        elif run_dir is not None:
            for nm, n in (("error.json", 200), ("result.json", 200), ("events.jsonl", 200), ("stderr.log", 200), ("stdout.log", 100)):
                f = run_dir / nm
                if f.is_file():
                    add(f"run/{nm}" + (".tail" if nm.endswith(("log", "jsonl")) else ""), _tail_lines(f, n))
        add("MANIFEST.json", json.dumps({"created_at": iso(), "reason": scrub_text(reason, 80), "wrapper_core": __version__,
                                         "run_id": run_dir.name if run_dir else None, "run_output_included": bool(run_dir and not classified),
                                         "files": files, "truncated": truncated,
                                         "redaction": "scrub_text: tokens, keys, passwords, URL credentials, long opaque strings, user names; "
                                                      "secrets/, browser profiles, screenshots and evidence are never included"}, indent=2), force=True)


def main(argv: List[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="wrapper_core debug-bundle")
    ap.add_argument("--run", help="run id (default: the most recent run)")
    ap.add_argument("--out", help="write the zip here (default: <install>/debug-bundles/)")
    a = ap.parse_args(argv)
    try:
        p = create_bundle(Paths().ensure(), run_id=a.run, reason="cli", out=Path(a.out) if a.out else None)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(p)
    return 0
