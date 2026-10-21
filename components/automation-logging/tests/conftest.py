from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                          # automation_logging
sys.path.insert(0, str(HERE.parent.parent / "wrapper-core"))  # shared wrapper_core
sys.path.insert(0, str(HERE))                                 # factories


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from wrapper_core import mockllm, runctx
    for k in list(os.environ):
        if k.startswith(("AUTOMAGIX_", "AUTOSTATION_", "JIRA_", "SMTP_", "GITLAB_", "TEAMS_", "LOG_BASE")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AUTOMAGIX_HOME", str(tmp_path / "wrapper-home"))
    monkeypatch.setenv("AUTOSTATION_LLM_ENDPOINT", "mock://")
    runctx.set_current(None)
    mockllm.clear()
    yield
    runctx.set_current(None)
    mockllm.clear()
