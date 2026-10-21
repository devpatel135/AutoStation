"""wrapper-core public API - the surface every automation payload codes against.

    import wrapper_core as wrapper

    @wrapper.entrypoint
    def main():
        for inv in fetch_invoices():
            wrapper.run_item(inv.id, process, inv)        # retry + item telemetry in one call
        wrapper.log_event("done", {"count": n})
        wrapper.set_result(processed=n)

    if __name__ == "__main__":
        raise SystemExit(main())
"""
from __future__ import annotations

import os

from ._version import __version__
from .llm import llm_call, llm_call_ex, log_llm_decision
from .logging import log_event, log_item, track_item
from .retry import (AuthExpired, AutomationError, Category, DataError, Escalation, LogicError, NetworkError,
                    RetryPolicy, SystemUnavailable, entrypoint, run_item, run_with_retry)
from .evidence import add_evidence
from .runctx import current as _current_run

__all__ = [
    "__version__", "log_event", "log_item", "track_item", "llm_call", "llm_call_ex", "log_llm_decision",
    "run_item", "run_with_retry", "entrypoint", "RetryPolicy", "Escalation", "Category", "AutomationError",
    "NetworkError", "AuthExpired", "SystemUnavailable", "DataError", "LogicError",
    "params", "param", "is_dry_run", "set_result", "emit_trigger", "run_dir", "assets", "add_evidence",
]


def params() -> dict:
    """Parameters of the schedule slot / trigger that started this run (``AUTOMATION_PARAM_*``)."""
    pre = "AUTOMATION_PARAM_"
    return {k[len(pre):].lower(): v for k, v in os.environ.items() if k.startswith(pre)}


def param(name: str, default=None):
    return params().get(name.lower(), default)


def is_dry_run() -> bool:
    return os.environ.get("AUTOMAGIX_DRY_RUN") == "1"


def set_result(**values) -> None:
    """Hand a small structured result back to the runner (stored in the run log as ``result``)."""
    _current_run().write_result(values)


def emit_trigger(target: str, params: dict | None = None, reason: str = "") -> None:
    """Ask the platform to start another automation (event-driven orchestration, SPEC §12)."""
    log_event("trigger", {"target": target, "params": params or {}, "reason": reason})


def run_dir():
    return _current_run().run_dir


def assets() -> dict:
    """Dynamic config assets (non-secret): ring defaults from the registry (refreshed by self-heal on every run), overridden per machine,
    per user and per schedule slot (``asset_<name>`` parameters). See :mod:`wrapper_core.assetconf` for the precedence and validation."""
    try:
        from .assetconf import effective_assets
        return effective_assets()
    except Exception:  # noqa: BLE001 - config trouble must never stop a run: fall back to the plain ring defaults
        from .paths import Paths
        from .util import read_json
        return read_json(Paths().assets_json, {}) or {}
