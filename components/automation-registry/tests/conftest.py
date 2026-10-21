from __future__ import annotations

import copy
import hashlib
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "wrapper-core"))

from wrapper_core import miniyaml  # noqa: E402


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_manifest() -> dict:
    """A small, valid manifest: automation `auto-a` is mid-rollout (broad 1.0.0 / pilot 1.1.0 / dev 1.2.0)."""
    req = {"wrapper_core": ">=0.1.0,<1.0.0", "execution_framework": ">=0.1.0,<1.0.0"}
    return {
        "schema_version": 1,
        "fleet": {"paused": False, "paused_reason": ""},
        "rings": ["dev", "pilot", "broad"],
        "compatibility": [{"wrapper_core": ">=0.1.0,<1.0.0", "execution_framework": ">=0.1.0,<1.0.0"}],
        "components": {
            "wrapper-core": {"rings": {"dev": "0.2.0", "pilot": "0.1.0", "broad": "0.1.0"},
                             "versions": {"0.1.0": {"url": "artifacts/components/wrapper-core-0.1.0.zip", "sha256": sha("wc1")},
                                          "0.2.0": {"url": "artifacts/components/wrapper-core-0.2.0.zip", "sha256": sha("wc2")}}},
            "execution-framework": {"rings": {"dev": "0.1.0", "pilot": "0.1.0", "broad": "0.1.0"},
                                    "versions": {"0.1.0": {"url": "artifacts/components/execution-framework-0.1.0.zip", "sha256": sha("ef1")}}},
        },
        "assets": {"default": {"internal_api_url": "https://api.example.internal"}},
        "automations": {
            "auto-a": {
                "description": "Routes invoices", "owner": "owner@example.com", "owner_team": "AP Automation",
                "systems": ["System X"], "data_classification": "internal", "status": "active", "paused_reason": "",
                "llm_allowed_classification": "internal",
                "rings": {"dev": "1.2.0", "pilot": "1.1.0", "broad": "1.0.0"},
                "versions": {v: {"url": f"artifacts/automations/auto-a-{v}.zip", "sha256": sha("a" + v), "requires": req}
                             for v in ("1.0.0", "1.1.0", "1.2.0")},
                "schedule": {"timezone": "America/New_York",
                             "expected_duration_ms": {"baseline": 120000, "warn_threshold": 2.0},
                             "maintenance_windows": [{"days": ["Saturday", "Sunday"]}, {"daily": "23:00-02:00"}],
                             "slots": [{"id": "daily", "type": "daily", "time": "09:00", "params": {"date_range": "yesterday"}},
                                       {"id": "weekly", "type": "weekly", "days": ["Monday"], "time": "08:00", "params": {"date_range": "last_week"}},
                                       {"id": "poll", "type": "poll", "every_minutes": 5, "params": {}}]},
                "error_handling": {"max_retries": 3, "network_retry_wait_s": 60, "backoff_multiplier": 2.0,
                                   "skip_on_data_error": True, "escalate_after_retries": True},
                "notify_on_complete": False,
                "catalog": {"summary": "Routes invoices to AP.", "systems": ["System X"], "owner_team": "AP Automation",
                            "data_classification": "internal", "process_owner": "process@example.com",
                            "links": [{"title": "Runbook", "url": "https://wiki.example.internal/runbook"}],
                            "minutes_per_item": 4, "tags": ["finance", "ap"]},
            },
            "auto-b": {
                "description": "Second automation", "owner": "b@example.com", "owner_team": "Platform", "systems": ["none"],
                "data_classification": "public", "status": "active", "paused_reason": "",
                "llm_allowed_classification": "internal",
                "rings": {"dev": "1.0.0", "pilot": "1.0.0", "broad": "1.0.0"},
                "versions": {"1.0.0": {"url": "artifacts/automations/auto-b-1.0.0.zip", "sha256": sha("b1"), "requires": req}},
                "schedule": {"timezone": "UTC", "slots": [{"id": "daily", "type": "daily", "time": "06:30", "params": {}}]},
                "triggers": [],
            },
        },
    }


@pytest.fixture
def manifest() -> dict:
    return make_manifest()


@pytest.fixture
def mpath(tmp_path, manifest) -> Path:
    p = tmp_path / "manifest.yaml"
    p.write_text(miniyaml.dumps(manifest), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _pin_time(monkeypatch):
    monkeypatch.setenv("REGISTRY_TODAY", "2026-10-12")
    monkeypatch.setenv("REGISTRY_NOW", "2026-10-12T13:00:00Z")
    monkeypatch.delenv("REGISTRY_ALLOW_PLACEHOLDERS", raising=False)
    monkeypatch.setenv("GITLAB_USER_LOGIN", "tester")


def make_zip(path: Path, files: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
    return path


def automation_zip(path: Path, name: str, version: str, requires: dict | None = None) -> Path:
    desc = {"name": name, "version": version, "runtime": "python", "entrypoint": "main.py"}
    if requires is not None:
        desc["requires"] = requires
    return make_zip(path, {"automation.yaml": miniyaml.dumps(desc), "main.py": "print(1)\n"})


def run_cli(*argv: str) -> int:
    from registry.cli import main
    return main(list(argv))
