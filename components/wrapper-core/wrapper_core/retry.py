"""Intelligent retry: deterministic classifier first, LLM only for unknown errors, fail-safe ESCALATE (SPEC §10)."""
from __future__ import annotations

import enum
import json
import re
import socket
import subprocess
import time
import traceback
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Callable

from . import runctx
from .llm import llm_call_ex, log_llm_decision
from .logging import log_event, log_item
from .redact import redacted_json
from .util import short_hash


class Category(str, enum.Enum):
    NETWORK_ERROR = "NETWORK_ERROR"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    SYSTEM_UNAVAILABLE = "SYSTEM_UNAVAILABLE"
    DATA_ERROR = "DATA_ERROR"
    LOGIC_ERROR = "LOGIC_ERROR"
    UNKNOWN = "UNKNOWN"


# Explicit exception types automations can raise to steer the handler ---------------------------------
class AutomationError(Exception):
    category = Category.UNKNOWN


class NetworkError(AutomationError):
    category = Category.NETWORK_ERROR


class AuthExpired(AutomationError):
    category = Category.AUTH_EXPIRED


class SystemUnavailable(AutomationError):
    category = Category.SYSTEM_UNAVAILABLE


class DataError(AutomationError):
    category = Category.DATA_ERROR


class LogicError(AutomationError):
    category = Category.LOGIC_ERROR


class Escalation(Exception):
    """Raised when the handler gave up. Carries the final category and attempt history; never retried again."""

    def __init__(self, message: str, category: Category = Category.UNKNOWN, attempts: int = 0, cause: BaseException | None = None):
        super().__init__(message)
        self.category, self.attempts, self.cause = category, attempts, cause


_TEXT_RULES: list[tuple[re.Pattern, Category]] = [
    (re.compile(r"\b(401|403)\b|unauthori[sz]ed|forbidden|token (has )?expired|session (has )?expired|certificate (has )?expired|login required|authentication failed", re.I), Category.AUTH_EXPIRED),
    (re.compile(r"\b(502|503|504)\b|service unavailable|bad gateway|gateway time-?out|under maintenance|maintenance page|temporarily unavailable|server is busy", re.I), Category.SYSTEM_UNAVAILABLE),
    (re.compile(r"connection (reset|refused|aborted|error)|timed? ?out|name or service not known|getaddrinfo failed|network is unreachable|no route to host|vpn|proxy error|ERR_INTERNET_DISCONNECTED|ERR_NAME_NOT_RESOLVED|(?-i:\bE(?:CONNRESET|TIMEDOUT|NOTFOUND)\b)", re.I), Category.NETWORK_ERROR),
    (re.compile(r"KeyError|AttributeError|TypeError|NameError|ImportError|ModuleNotFoundError|IndexError|AssertionError|SyntaxError|UnboundLocalError|ZeroDivisionError|NotImplementedError", re.I), Category.LOGIC_ERROR),
    (re.compile(r"missing required field|invalid (format|value|date)|could not parse|JSONDecodeError|UnicodeDecodeError|ValueError|validation error|malformed|unexpected format", re.I), Category.DATA_ERROR),
]


_LAST_EXC = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Exit|Warning)):", re.M)
_NET_NAMES = {"ConnectionError", "ConnectionResetError", "ConnectionRefusedError", "ConnectionAbortedError", "TimeoutError",
              "timeout", "gaierror", "URLError", "ReadTimeout", "ConnectTimeout", "ProxyError", "ConnectError"}
_LOGIC_NAMES = {"KeyError", "AttributeError", "TypeError", "NameError", "ImportError", "ModuleNotFoundError", "IndexError",
                "AssertionError", "SyntaxError", "UnboundLocalError", "ZeroDivisionError", "NotImplementedError"}
_DATA_NAMES = {"ValueError", "UnicodeDecodeError", "UnicodeEncodeError", "JSONDecodeError", "DataError"}


def classify_text(text: str) -> Category:
    """Classify an error from free text (stderr tail, message). The final exception type name wins over keywords."""
    names = _LAST_EXC.findall(text or "")
    if names:
        last = names[-1].split(".")[-1]
        if last in _LOGIC_NAMES:
            return Category.LOGIC_ERROR
        if last in _NET_NAMES:
            return Category.NETWORK_ERROR
        if last in _DATA_NAMES:
            return Category.DATA_ERROR
    for rx, cat in _TEXT_RULES:
        if rx.search(text or ""):
            return cat
    return Category.UNKNOWN


def classify_exception(exc: BaseException) -> Category:
    if isinstance(exc, AutomationError):
        return exc.category
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return Category.AUTH_EXPIRED
        if exc.code in (502, 503, 504):
            return Category.SYSTEM_UNAVAILABLE
        if exc.code in (400, 404, 409, 422):
            return Category.DATA_ERROR
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror, urllib.error.URLError)):
        return Category.NETWORK_ERROR
    if isinstance(exc, (KeyError, AttributeError, TypeError, NameError, ImportError, AssertionError, IndexError,
                        NotImplementedError, UnboundLocalError, ZeroDivisionError)):
        return Category.LOGIC_ERROR
    if isinstance(exc, (ValueError, UnicodeError)):
        return Category.DATA_ERROR  # includes json.JSONDecodeError
    return classify_text(f"{type(exc).__name__}: {exc}")


@dataclass
class RetryPolicy:
    max_retries: int = 3
    network_retry_wait_s: float = 60.0
    backoff_multiplier: float = 2.0
    vpn_tool: str | None = None
    skip_on_data_error: bool = True
    escalate_after_retries: bool = True
    cwd: str | None = None  # base dir for relative vpn_tool

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        """Policy exported by the runner as AUTOMAGIX_ERROR_POLICY (JSON of the manifest's error_handling block)."""
        import os
        try:
            block = json.loads(os.environ.get("AUTOMAGIX_ERROR_POLICY", "") or "{}")
        except ValueError:
            block = {}
        return cls.from_manifest(block, cwd=os.getcwd())

    @classmethod
    def from_manifest(cls, block: dict | None, cwd: str | None = None) -> "RetryPolicy":
        block = block or {}
        fields = {k: block[k] for k in ("max_retries", "network_retry_wait_s", "backoff_multiplier", "vpn_tool",
                                        "skip_on_data_error", "escalate_after_retries") if k in block}
        return cls(cwd=cwd, **fields)


class Skipped:
    """Returned by run_with_retry when a DATA_ERROR was skipped; ``reason`` is the error message."""
    __slots__ = ("reason",)

    def __init__(self, reason: str = ""):
        self.reason = reason


SKIPPED = Skipped()  # kept for isinstance-free callers


def _run_vpn_tool(policy: RetryPolicy) -> bool:
    if not policy.vpn_tool:
        return False
    import os
    path = policy.vpn_tool if os.path.isabs(policy.vpn_tool) else os.path.join(policy.cwd or ".", policy.vpn_tool)
    cmd = (["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path]
           if path.lower().endswith(".ps1") else [path])
    try:
        return subprocess.run(cmd, capture_output=True, timeout=300).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def llm_classify(exc: BaseException, tb_tail: list[str], run_ctx_json: str, caller: str = "wrapper-core/retry-classifier") -> dict | None:
    """LLM fallback for UNKNOWN errors. Returns {category, retry, reasoning} or None (=> ESCALATE)."""
    context = json.dumps({"error_type": type(exc).__name__, "error_message": str(exc)[:500], "traceback_tail": tb_tail[-3:],
                          "run_context": run_ctx_json[:3000]})
    res = llm_call_ex(
        "Classify this automation error. Decide whether it is transient (worth retrying) or permanent.\n"
        'Return JSON: {"category": "transient|permanent|unknown", "retry": true|false, "reasoning": "one sentence"}',
        context, schema={"category": "transient|permanent|unknown", "retry": "boolean", "reasoning": "string"},
        caller=caller, template="retry-classifier@1", decision_log=False)
    return res.value if isinstance(res.value, dict) else None


def run_with_retry(fn: Callable[[], Any], policy: RetryPolicy | None = None, *, item_id: str | None = None,
                   auth_refresh: Callable[[], None] | None = None, use_llm: bool = True,
                   sleep: Callable[[float], None] = time.sleep) -> Any:
    """Run ``fn`` applying the retry policy. Returns its value, or ``SKIPPED``; raises ``Escalation`` when exhausted.

    Exactly one ``retry_event`` is logged per failed attempt (error, category, decision, source, reasoning, outcome).
    """
    policy = policy or RetryPolicy.from_env()
    attempt = 0
    auth_retried = False
    pending: dict | None = None  # the previous attempt's retry_event, logged once its outcome is known

    def flush(outcome: str) -> None:
        nonlocal pending
        if pending is not None:
            log_event("retry_event", {**pending, "outcome": outcome})
            pending = None

    while True:
        attempt += 1
        try:
            result = fn()
            flush("recovered")
            return result
        except Escalation:
            flush("escalated")
            raise
        except Exception as exc:  # noqa: BLE001 - classifier decides
            flush("failed_again")
            cat = classify_exception(exc)
            tb = traceback.format_exc().strip().splitlines()
            source, reasoning, wait_s = "deterministic", "", 0.0
            decision = "escalate"

            if cat is Category.LOGIC_ERROR:
                reasoning = "same code will fail again - not retrying"
            elif cat is Category.DATA_ERROR:
                if policy.skip_on_data_error:
                    decision, reasoning = "skip", "bad data - skipping item and continuing"
                else:
                    reasoning = "data error and skip_on_data_error is false"
            elif cat is Category.NETWORK_ERROR:
                if attempt <= policy.max_retries:
                    decision, wait_s, reasoning = "retry", policy.network_retry_wait_s, "network error - reconnect, wait, retry"
                else:
                    reasoning = "network error persisted after retries"
            elif cat is Category.AUTH_EXPIRED:
                if not auth_retried:
                    decision, reasoning = "retry", "auth expired - refresh once and retry"
                else:
                    reasoning = "auth still failing after refresh"
            elif cat is Category.SYSTEM_UNAVAILABLE:
                backoffs = min(3, policy.max_retries)
                if attempt <= backoffs:
                    decision = "retry"
                    wait_s = policy.network_retry_wait_s * (policy.backoff_multiplier ** (attempt - 1))
                    reasoning = "target system unavailable - exponential backoff"
                else:
                    reasoning = "system unavailable after backoff attempts (SLOW signal)"
            else:  # UNKNOWN
                verdict = None
                if use_llm:
                    ctx = runctx.current()
                    verdict = llm_classify(exc, tb, redacted_json(ctx.read_events()[-40:], max_chars=3000))
                if verdict and verdict.get("category") == "transient" and verdict.get("retry") and attempt <= policy.max_retries:
                    decision, source, wait_s = "retry", "llm", policy.network_retry_wait_s
                    reasoning = str(verdict.get("reasoning", ""))[:300]
                    log_llm_decision("wrapper-core/retry-classifier", "retry-classifier@1",
                                     short_hash(f"{type(exc).__name__}:{exc}"), "retry", reasoning, error_type=type(exc).__name__)
                elif verdict:
                    source, reasoning = "llm", str(verdict.get("reasoning", ""))[:300] or "LLM judged error permanent"
                    log_llm_decision("wrapper-core/retry-classifier", "retry-classifier@1",
                                     short_hash(f"{type(exc).__name__}:{exc}"), "escalate", reasoning, error_type=type(exc).__name__)
                else:
                    source, reasoning = "fallback", "unknown error and no LLM verdict - escalating (fail safe)"

            pending = {"attempt": attempt, "item_id": item_id, "error_type": type(exc).__name__,
                       "error_message": str(exc)[:300], "category": cat.value, "decision": decision,
                       "source": source, "reasoning": reasoning, "wait_s": wait_s}
            if decision == "skip":
                flush("skipped")
                return Skipped(str(exc)[:200])
            if decision == "escalate":
                flush("escalated")
                raise Escalation(f"{cat.value}: {exc}", cat, attempt, exc) from exc

            # --- remediation before the retry
            if cat is Category.NETWORK_ERROR:
                _run_vpn_tool(policy)
            elif cat is Category.AUTH_EXPIRED:
                auth_retried = True
                if auth_refresh:
                    try:
                        auth_refresh()
                    except Exception as refresh_exc:  # noqa: BLE001
                        raise Escalation(f"AUTH_EXPIRED: refresh failed: {refresh_exc}", cat, attempt, exc) from exc
            if wait_s:
                sleep(wait_s)


def run_item(item_id: Any, fn: Callable[..., Any], *args, policy: RetryPolicy | None = None, **kwargs) -> Any:
    """Process one item with retry + telemetry: logs ``item_processed`` (success/skipped/failed).

    DATA_ERROR → item skipped, run continues (returns ``None``). Anything unresolved raises ``Escalation`` after
    the item is logged as failed.
    """
    t0 = time.monotonic()
    try:
        out = run_with_retry(lambda: fn(*args, **kwargs), policy, item_id=str(item_id))
    except Escalation as esc:
        log_item(item_id, "failed", duration_ms=int((time.monotonic() - t0) * 1000), error_category=esc.category.value,
                 detail=str(esc)[:300])
        raise
    if isinstance(out, Skipped):
        log_item(item_id, "skipped", duration_ms=int((time.monotonic() - t0) * 1000), reason=out.reason or "data_error",
                 error_category="DATA_ERROR")
        return None
    log_item(item_id, "success", duration_ms=int((time.monotonic() - t0) * 1000))
    return out


def entrypoint(fn: Callable[[], Any]) -> Callable[[], int]:
    """Decorator for a Python automation's ``main``: classifies uncaught errors for the runner.

    Writes ``error.json`` (category, type, message, traceback tail, escalated) and returns a non-zero exit code.
    """
    import functools
    import sys

    @functools.wraps(fn)
    def wrapper(*a, **kw) -> int:
        try:
            fn(*a, **kw)
            return 0
        except SystemExit as se:
            return int(se.code or 0) if isinstance(se.code, int) or se.code is None else 1
        except BaseException as exc:  # noqa: BLE001
            esc = exc if isinstance(exc, Escalation) else None
            cause = esc.cause if esc and esc.cause else exc
            cat = esc.category if esc else classify_exception(exc)
            tb = traceback.format_exc().strip().splitlines()
            runctx.current().write_error({
                "category": cat.value, "type": type(cause).__name__, "message": str(cause)[:500],
                "traceback_tail": tb[-6:], "escalated": esc is not None, "attempts": esc.attempts if esc else 1})
            print("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), file=sys.stderr)
            return 1

    return wrapper
