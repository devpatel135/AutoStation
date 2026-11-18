"""Studio tests: hermetic guard (fake schtasks, redirected per-user folders), see tools/tests/_hermetic.py."""
from __future__ import annotations

import sys
from pathlib import Path

import importlib.util as _ilu  # noqa: E402  (loaded by path: tools/tests must not join sys.path, its test_*.py names would shadow ours)

_spec = _ilu.spec_from_file_location("_amx_hermetic", Path(__file__).resolve().parents[3] / "tools" / "tests" / "_hermetic.py")
_mod = _ilu.module_from_spec(_spec)
sys.modules["_amx_hermetic"] = _mod
_spec.loader.exec_module(_mod)
hermetic_fixture = _mod.hermetic_fixture

_hermetic = hermetic_fixture()
