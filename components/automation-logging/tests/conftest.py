from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                          # automation_logging
sys.path.insert(0, str(HERE.parent.parent / "wrapper-core"))  # shared wrapper_core
sys.path.insert(0, str(HERE))                                 # factories

# hermetic guard (fake schtasks, redirected per-user folders, real AUTOMAGIX/Startup folders must stay untouched): tools/tests/_hermetic.py
import importlib.util as _ilu  # noqa: E402  (loaded by path: tools/tests must not join sys.path, its test_*.py names would shadow ours)

_spec = _ilu.spec_from_file_location("_amx_hermetic", Path(__file__).resolve().parents[3] / "tools" / "tests" / "_hermetic.py")
_mod = _ilu.module_from_spec(_spec)
sys.modules["_amx_hermetic"] = _mod
_spec.loader.exec_module(_mod)
hermetic_fixture = _mod.hermetic_fixture

_hermetic = hermetic_fixture()


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
