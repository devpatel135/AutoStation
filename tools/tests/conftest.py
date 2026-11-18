"""tools/ tests: hermetic guard (fake schtasks, redirected per-user folders), see _hermetic.py in this folder."""
from __future__ import annotations

import sys
from pathlib import Path

import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("_amx_hermetic", Path(__file__).resolve().parent / "_hermetic.py")
_mod = _ilu.module_from_spec(_spec)
sys.modules["_amx_hermetic"] = _mod
_spec.loader.exec_module(_mod)
hermetic_fixture = _mod.hermetic_fixture

_hermetic = hermetic_fixture()
