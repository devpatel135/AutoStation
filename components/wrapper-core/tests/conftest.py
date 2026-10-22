from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wrapper_core import mockllm, miniyaml, runctx  # noqa: E402
from wrapper_core.paths import Paths  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_schtasks(monkeypatch):
    """Tests must NEVER touch the machine's real Task Scheduler. A leaked /Create of a task whose start time has passed makes Windows
    run it at once (and it stays registered), which once started the real automation three times and left a stray task behind.
    Any ``schtasks`` call made through subprocess.run is answered by a harmless fake; tests that check scheduler behaviour pass their
    own fake ``runner`` to Scheduler instead."""
    import os
    import subprocess
    real_run = subprocess.run

    def guarded(args, *a, **k):
        first = args[0] if isinstance(args, (list, tuple)) and args else args
        if isinstance(first, str) and os.path.basename(first).lower() in ("schtasks", "schtasks.exe"):
            guarded.calls.append(list(args) if isinstance(args, (list, tuple)) else [args])
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, *a, **k)

    guarded.calls = []
    monkeypatch.setattr(subprocess, "run", guarded)
    yield


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for k in list(__import__("os").environ):
        if k.startswith(("AUTOMAGIX_", "AUTOSTATION_", "AUTOMATION_PARAM_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AUTOMAGIX_HOME", str(home))
    monkeypatch.setenv("AUTOMAGIX_LOG_TRANSPORT", "dir")
    monkeypatch.setenv("AUTOMAGIX_LOG_REMOTE", str(tmp_path / "logs"))
    monkeypatch.setenv("AUTOMAGIX_RING", "pilot")
    monkeypatch.setenv("AUTOMAGIX_NO_TOAST", "1")
    if not __import__("os").environ.get("AMX_TEST_REMEDIATION"):   # AMX_TEST_REMEDIATION=1 runs the whole suite with remediation ON (compat check)
        monkeypatch.setenv("AUTOMAGIX_NO_REMEDIATION", "1")       # remediation has its own tests (test_remediation.py) and opts back in
    monkeypatch.setenv("AUTOMAGIX_ALLOW_INSECURE_SECRETS", "1")
    monkeypatch.setenv("AUTOMAGIX_STARTUP_DIR", str(tmp_path / "startup"))
    monkeypatch.setenv("AUTOSTATION_LLM_ENDPOINT", "mock://")
    runctx.set_current(None)
    mockllm.clear()
    yield
    runctx.set_current(None)
    mockllm.clear()


@pytest.fixture
def paths() -> Paths:
    return Paths().ensure()


@pytest.fixture
def logs_dir(tmp_path) -> Path:
    return tmp_path / "logs"


def read_artifacts(logs: Path, kind: str) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(logs.glob(f"ingest/*/{kind}/**/*.json"))]


class Registry:
    """Builds a local-directory registry (manifest.yaml + zipped payloads) for end-to-end tests."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.manifest = {
            "schema_version": 1,
            "fleet": {"paused": False, "paused_reason": ""},
            "rings": ["dev", "pilot", "broad"],
            "compatibility": [{"wrapper_core": ">=0.1.0,<1.0.0", "execution_framework": ">=0.1.0,<1.0.0"}],
            "components": {},
            "assets": {"default": {"internal_api_url": "https://api.example.internal"}},
            "automations": {},
        }

    def _zip(self, rel: str, files: dict[str, str]) -> dict:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        return {"url": rel, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}

    def add_component(self, name: str, version: str, files: dict[str, str] | None = None, rings=("dev", "pilot", "broad")):
        rec = self._zip(f"components/{name}-{version}.zip", files or {"README.txt": name})
        entry = self.manifest["components"].setdefault(name, {"rings": {}, "versions": {}})
        entry["versions"][version] = rec
        for r in rings:
            entry["rings"][r] = version

    def add_automation(self, name: str, version: str, code: str, *, rings=("dev", "pilot", "broad"),
                       descriptor: dict | None = None, **entry_fields):
        desc = {"name": name, "version": version, "runtime": "python", "entrypoint": "main.py", "timeout_s": 60}
        desc.update(descriptor or {})
        rec = self._zip(f"automations/{name}-{version}.zip", {"automation.yaml": miniyaml.dumps(desc), "main.py": code})
        entry = self.manifest["automations"].setdefault(name, {
            "description": f"{name} test automation", "owner": "tests@example.com", "status": "active",
            "data_classification": "internal", "llm_allowed_classification": "internal",
            "rings": {}, "versions": {}, "schedule": {"timezone": "UTC", "slots": [{"id": "daily", "type": "daily", "time": "09:00", "params": {"mode": "test"}}]},
            "error_handling": {"max_retries": 2, "network_retry_wait_s": 0, "backoff_multiplier": 1.0, "skip_on_data_error": True},
        })
        entry["versions"][version] = rec
        for r in rings:
            entry["rings"][r] = version
        entry.update(entry_fields)

    def save(self):
        (self.root / "manifest.yaml").write_text(miniyaml.dumps(self.manifest), encoding="utf-8")
        return self

    def set_registry_env(self, monkeypatch):
        monkeypatch.setenv("AUTOMAGIX_REGISTRY_URL", str(self.root))


@pytest.fixture
def registry(tmp_path, monkeypatch) -> Registry:
    r = Registry(tmp_path / "registry")
    r.set_registry_env(monkeypatch)
    return r


HELLO = '''
import wrapper_core as wrapper

@wrapper.entrypoint
def main():
    wrapper.log_event("start", {"mode": wrapper.param("mode")})
    for i in range(3):
        wrapper.log_item(f"ITEM-{i}", "success", duration_ms=10 + i)
    wrapper.set_result(processed=3)

if __name__ == "__main__":
    raise SystemExit(main())
'''
