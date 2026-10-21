"""Test/dev harness: run a payload folder through the REAL run lifecycle, fully offline.

    from wrapper_core.testing import run_payload
    out = run_payload("path/to/automation", params={"mode": "demo"})
    assert out.record["status"] == "success"

Creates a throwaway AUTOMAGIX_HOME + dir registry + dir log transport + ``mock://`` LLM, copies the payload in,
runs ``run_automation`` (broker, retry, synopsis, logging) and returns the ``RunOutcome`` (``.record`` is the run log,
``.logs_dir`` the raw artefacts). The process environment is restored afterwards.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import miniyaml, runctx
from .paths import Paths
from .runner import RunOutcome, run_automation

_ENV_KEYS = ("AUTOMAGIX_HOME", "AUTOMAGIX_LOG_TRANSPORT", "AUTOMAGIX_LOG_REMOTE", "AUTOMAGIX_REGISTRY_URL", "AUTOMAGIX_RING",
             "AUTOMAGIX_NO_TOAST", "AUTOMAGIX_ALLOW_INSECURE_SECRETS", "AUTOSTATION_LLM_ENDPOINT", "AUTOSTATION_LLM_KEY",
             "AUTOMAGIX_LLM_ALLOWED_CLASSIFICATION", "AUTOMAGIX_DATA_CLASSIFICATION")


@dataclass
class PayloadRun:
    outcome: RunOutcome
    root: Path

    @property
    def record(self) -> dict:
        return self.outcome.record or {}

    @property
    def exit_code(self) -> int:
        return self.outcome.exit_code

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def home(self) -> Path:
        return self.root / "home"

    def events(self, name: str | None = None) -> list[dict]:
        return [e for e in self.record.get("events", []) if name is None or e["event"] == name]


def run_payload(payload_dir: str | Path, params: dict | None = None, *, env: dict | None = None, tmp_root: Path | None = None,
                llm: str | None = "mock://", ring: str = "dev", error_handling: dict | None = None,
                entry_fields: dict | None = None, slot_type: str = "daily", dry_run: bool = False, keep: bool = False) -> PayloadRun:
    src = Path(payload_dir).resolve()
    desc = miniyaml.load(src / "automation.yaml")
    name = desc["name"]
    root = Path(tmp_root) if tmp_root else Path(tempfile.mkdtemp(prefix="amx-test-"))
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    saved_extra = {k: os.environ.get(k) for k in (env or {})}
    try:
        os.environ.update({
            "AUTOMAGIX_HOME": str(root / "home"), "AUTOMAGIX_LOG_TRANSPORT": "dir", "AUTOMAGIX_LOG_REMOTE": str(root / "logs"),
            "AUTOMAGIX_RING": ring, "AUTOMAGIX_NO_TOAST": "1", "AUTOMAGIX_REGISTRY_URL": str(root / "registry"),
            "AUTOMAGIX_ALLOW_INSECURE_SECRETS": "1"})
        if llm:
            os.environ["AUTOSTATION_LLM_ENDPOINT"] = llm
        else:
            os.environ.pop("AUTOSTATION_LLM_ENDPOINT", None)
        os.environ.update(env or {})
        paths = Paths().ensure()
        dest = paths.automation_dir(name)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "tests", ".pytest_cache", "node_modules"))
        (dest / "VERSION").write_text("dev\n", encoding="utf-8")
        slot = {"id": "dev", "type": slot_type, "params": dict(params or {})}
        if slot_type in ("daily", "weekly"):
            slot["time"] = "00:00"
        entry = {"description": "test harness", "owner": "dev@example.com", "status": "active", "rings": {ring: "dev"},
                 "versions": {}, "schedule": {"timezone": "UTC", "slots": [slot]}}
        if error_handling is not None:
            entry["error_handling"] = error_handling
        entry.update(entry_fields or {})
        reg = root / "registry"
        reg.mkdir(parents=True, exist_ok=True)
        (reg / "manifest.yaml").write_text(miniyaml.dumps({"schema_version": 1, "automations": {name: entry}}), encoding="utf-8")
        runctx.set_current(None)
        out = run_automation(name, "dev", paths=paths, dry_run=dry_run)
        return PayloadRun(out, root)
    finally:
        runctx.set_current(None)
        for k, v in {**saved, **saved_extra}.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if not keep and tmp_root is None:
            pass  # caller inspects root; temp dirs are left for the OS to reap (they are small)
