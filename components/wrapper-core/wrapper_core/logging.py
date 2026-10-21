"""Public telemetry API: ``log_event``, ``log_item``, ``track_item``.

Named ``logging`` to match the architecture assessment (``from wrapper_core.logging import log_event``);
modules inside this package that need the stdlib module import it via absolute imports as usual.
"""
from __future__ import annotations

import contextlib
import time
from typing import Iterator

from . import runctx

__all__ = ["log_event", "log_item", "track_item"]


def log_event(event: str, data: dict | None = None, *, sensitive: bool = False,
              sensitive_keys: list[str] | None = None, **fields) -> None:
    """Record a structured event in the current run.

    ``data`` and ``**fields`` are merged. ``sensitive=True`` marks every key as sensitive; ``sensitive_keys``
    marks specific keys. Sensitive values stay in the raw run log but are replaced with ``[REDACTED]`` before
    any content is sent to an LLM (run synopsis, retry classification, CI diagnosis).
    """
    payload = dict(data or {})
    payload.update(fields)
    keys = list(sensitive_keys or [])
    if sensitive:
        keys.extend(payload.keys())
    runctx.current().append(event, payload, keys or None)


def log_item(item_id, outcome: str, *, duration_ms: int | None = None, sensitive: bool = False,
             sensitive_keys: list[str] | None = None, **fields) -> None:
    """Record the outcome of one processed item (``success`` | ``skipped`` | ``failed``)."""
    if outcome not in ("success", "skipped", "failed"):
        raise ValueError("outcome must be success|skipped|failed")
    data = {"item_id": str(item_id), "outcome": outcome}
    if duration_ms is not None:
        data["duration_ms"] = int(duration_ms)
    data.update(fields)
    keys = list(sensitive_keys or [])
    if sensitive:
        keys.extend(k for k in data if k not in ("outcome",))
    runctx.current().append("item_processed", data, keys or None)


@contextlib.contextmanager
def track_item(item_id, **fields) -> Iterator[dict]:
    """Time one item and log it automatically.

    >>> with track_item("INV-1", routed_to="AP") as it:   # doctest: +SKIP
    ...     process()                      # raise -> logged as failed (exception propagates)
    ...     it["outcome"] = "skipped"      # optional override
    ...     it["reason"] = "duplicate"
    """
    state: dict = {"outcome": "success"}
    start = time.monotonic()
    try:
        yield state
    except BaseException as exc:  # noqa: BLE001 - re-raised below
        extra = {k: v for k, v in state.items() if k != "outcome"}
        log_item(item_id, "failed", duration_ms=int((time.monotonic() - start) * 1000),
                 **{**fields, "error_category": type(exc).__name__, "detail": str(exc)[:300], **extra})
        raise
    else:
        extra = {k: v for k, v in state.items() if k != "outcome"}
        log_item(item_id, state["outcome"], duration_ms=int((time.monotonic() - start) * 1000), **{**fields, **extra})
