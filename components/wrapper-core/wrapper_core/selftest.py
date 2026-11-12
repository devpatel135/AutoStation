"""``python -m wrapper_core selftest``: can this copy of wrapper-core start and do its basic jobs?

The launcher runs it right after swapping in a staged wrapper-core and REVERTS to the previous copy when it fails, so a broken release
(import error, a bad Windows-only path, a syntax error) can never leave a machine unable to run, heal or report. It must therefore be
fast (well under a few seconds), touch nothing outside a throw-away temp home, never use the network and never raise: exit 0 = healthy,
exit 1 = broken (the reasons are printed, one per line).
"""
from __future__ import annotations

import importlib
import json
import pkgutil
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Tuple

# modules that legitimately cannot be imported off Windows / without a desktop (they are exercised on Windows by the launcher)
_PLATFORM_ONLY = {"wrapper_core._traywin"}


def _import_everything() -> List[str]:
    import wrapper_core
    problems: List[str] = []
    for mod in pkgutil.walk_packages(wrapper_core.__path__, "wrapper_core."):
        if mod.name in _PLATFORM_ONLY and sys.platform != "win32":
            continue
        if mod.name.endswith(("__main__",)) or ".tests" in mod.name:
            continue
        try:
            importlib.import_module(mod.name)
        except Exception as exc:  # noqa: BLE001 - any failure at all means a broken copy
            problems.append(f"import {mod.name}: {type(exc).__name__}: {str(exc)[:160]}")
    return problems


def _mini_lifecycle() -> List[str]:
    """Tiny in-memory lifecycle: paths, atomic json, manifest parse + target version, signature verify, schedule maths, task XML."""
    from datetime import datetime, timezone
    from . import ed25519, manifest as mf, schedule, scheduler, trust, versions
    from .paths import Paths
    from .util import read_json, write_json

    problems: List[str] = []
    with tempfile.TemporaryDirectory(prefix="amx-selftest-") as tmp:
        paths = Paths(Path(tmp)).ensure()
        write_json(paths.state / "selftest.json", {"ok": True})
        if read_json(paths.state / "selftest.json", {}) != {"ok": True}:
            problems.append("atomic JSON write/read round trip failed")
        text = ("schema_version: 1\ncomponents:\n  wrapper-core:\n    rings: {dev: '1.0.0', pilot: '1.0.0', broad: '1.0.0'}\n    versions:\n"
                "      '1.0.0': {url: x.zip, sha256: " + "0" * 64 + "}\nautomations: {}\n")
        try:
            m = mf.parse(text)
            if str(mf.target_version(m["components"]["wrapper-core"], "broad")) != "1.0.0":
                problems.append("manifest target_version is wrong")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"manifest parse: {type(exc).__name__}: {str(exc)[:160]}")
        if versions.compare("1.2.0", "1.10.0") >= 0:
            problems.append("version comparison is wrong")
        try:
            seed, pub = ed25519.keypair_from_seed(bytes(range(32)))
            doc = trust.make_sig_doc(b"x", seed, "2026-01-01T00:00:00Z", seq=1)
            if trust.verify_manifest_doc(b"x", doc, [pub.hex()]).seq != 1:
                problems.append("signature round trip failed")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"signature round trip: {type(exc).__name__}: {str(exc)[:160]}")
        try:
            runs = schedule.next_runs({"id": "d", "type": "daily", "time": "09:00"}, datetime(2026, 1, 1, tzinfo=timezone.utc), 2, "UTC")
            if len(runs) != 2:
                problems.append("schedule maths returned the wrong number of runs")
            scheduler.task_xml(paths, "selftest", {"id": "daily", "type": "daily", "time": "09:00"}, "UTC")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"schedule/task XML: {type(exc).__name__}: {str(exc)[:160]}")
    return problems


CHECKS: List[Tuple[str, Callable[[], List[str]]]] = [("imports", _import_everything), ("lifecycle", _mini_lifecycle)]


def run() -> List[str]:
    problems: List[str] = []
    for name, fn in CHECKS:
        try:
            problems += fn()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: {type(exc).__name__}: {str(exc)[:160]}")
    return problems


def main(argv: List[str]) -> int:
    problems = run()
    from . import __version__
    if problems:
        print(f"wrapper-core {__version__} SELFTEST FAILED")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"wrapper-core {__version__} selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
