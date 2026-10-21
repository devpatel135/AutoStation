"""Run context: the per-run directory and the append-only ``events.jsonl`` that SDKs in any language write.

The runner creates ``AUTOMAGIX_RUN_DIR`` and exports it to the child. Inside the child (or in tests / ad-hoc
scripts where the variable is missing) ``current()`` lazily creates a context so the public API never fails.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .paths import Paths
from .util import iso, new_run_id

_lock = threading.Lock()
_current: "RunContext | None" = None


class RunContext:
    def __init__(self, run_dir: Path, run_id: str, automation: str = "adhoc", slot: str = "adhoc"):
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.automation = automation
        self.slot = slot
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self._write_lock = threading.Lock()

    def append(self, event: str, data: dict | None = None, sensitive_keys: list[str] | None = None) -> dict:
        rec: dict = {"ts": iso(), "event": event, "data": data or {}}
        if sensitive_keys:
            rec["sensitive_keys"] = sorted(set(sensitive_keys))
        line = json.dumps(rec, default=str, separators=(",", ":")) + "\n"
        with self._write_lock:
            with open(self.events_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)
        return rec

    def read_events(self) -> list[dict]:
        out = []
        try:
            with open(self.events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        out.append({"ts": iso(), "event": "corrupt_event_line", "data": {"line": line[:200]}})
        except OSError:
            pass
        return out

    # --- results the payload can hand back to the runner --------------------------------------
    def write_result(self, obj: dict) -> None:
        (self.run_dir / "result.json").write_text(json.dumps(obj, default=str), encoding="utf-8")

    def read_result(self) -> dict:
        try:
            return json.loads((self.run_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def write_error(self, obj: dict) -> None:
        (self.run_dir / "error.json").write_text(json.dumps(obj, default=str), encoding="utf-8")

    def read_error(self) -> dict | None:
        try:
            return json.loads((self.run_dir / "error.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def from_env() -> "RunContext | None":
    run_dir = os.environ.get("AUTOMAGIX_RUN_DIR")
    if not run_dir:
        return None
    return RunContext(Path(run_dir), os.environ.get("AUTOMAGIX_RUN_ID", "unknown"),
                      os.environ.get("AUTOMAGIX_AUTOMATION", "adhoc"), os.environ.get("AUTOMAGIX_SLOT", "adhoc"))


def current() -> RunContext:
    """The active run context (env-provided, or an ad-hoc one under ``runs/adhoc-*``)."""
    global _current
    with _lock:
        env_dir = os.environ.get("AUTOMAGIX_RUN_DIR")
        if _current is not None and (not env_dir or Path(env_dir) == _current.run_dir):
            return _current
        _current = from_env()
        if _current is None:
            rid = "adhoc-" + new_run_id()
            _current = RunContext(Paths().runs / rid, rid)
        return _current


def set_current(ctx: "RunContext | None") -> None:
    global _current
    with _lock:
        _current = ctx
