"""Security primitives for the Studio server (stdlib only, so they are testable without FastAPI).

The Studio is a local developer tool that can write files and spend LLM budget, so any web page open in the same browser must not be able
to drive it (DNS rebinding, cross-site WebSocket, plain CSRF). Rules, all enforced by server.py through this module:

* a random bearer token generated per server start; every /api request and every WebSocket must present it
* Host and Origin must be the loopback names with the server's own port
* sessions expire after an idle period and there is a cap on how many exist
* each session may run a limited number of LLM steps (a spend cap)
* ``out_dir`` is resolved inside ONE root directory (default ~/AutoStation-projects); no traversal, absolute path, drive, UNC, symlink or junction
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

TOKEN_ENV = "AUTOSTATION_STUDIO_TOKEN"
PORT_ENV = "AUTOSTATION_STUDIO_PORT"
ROOT_ENV = "AUTOSTATION_STUDIO_ROOT"
MAX_STEPS_ENV = "AUTOSTATION_STUDIO_MAX_STEPS"

WS_SUBPROTOCOL = "autostation.v1"
WS_TOKEN_PREFIX = "token."
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")

DEFAULT_IDLE_TTL_S = 2 * 60 * 60
DEFAULT_MAX_SESSIONS = 20
DEFAULT_MAX_STEPS = 30

_COMPONENT_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def default_root() -> Path:
    return Path.home() / "AutoStation-projects"


def token_ok(expected: Optional[str], presented: Optional[str]) -> bool:
    """Constant-time comparison. An unconfigured (empty) expected token never matches: the server fails closed."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


def bearer_from_header(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    scheme, _, rest = value.partition(" ")
    if scheme.lower() != "bearer" or not rest.strip():
        return None
    return rest.strip()


def token_from_subprotocols(header: Optional[str]) -> Optional[str]:
    """The browser WebSocket API cannot set headers, so the token travels as a subprotocol ``token.<value>`` next to ``autostation.v1``."""
    if not header:
        return None
    for part in header.split(","):
        part = part.strip()
        if part.startswith(WS_TOKEN_PREFIX) and len(part) > len(WS_TOKEN_PREFIX):
            return part[len(WS_TOKEN_PREFIX):]
    return None


def host_allowed(host_header: Optional[str], port: int) -> bool:
    """Host must be a loopback name with the server's port (a rebinding page presents its own hostname)."""
    if not host_header:
        return False
    return host_header.strip().lower() in {f"{n}:{port}" for n in LOOPBACK_NAMES}


def origin_allowed(origin: Optional[str], port: int) -> bool:
    """A browser always sends Origin on a WebSocket and on a cross-origin fetch. An absent Origin (curl, tests, same-origin GET) is allowed:
    the bearer token is the real gate, the Origin check only shuts out other web pages."""
    if origin is None:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme != "http" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    netloc_host = f"[{host}]" if ":" in host else host
    try:
        origin_port = parts.port
    except ValueError:
        return False
    return netloc_host in LOOPBACK_NAMES and origin_port == port


# --- out_dir confinement ---------------------------------------------------------------------------------------------------------------------

class PathRejected(ValueError):
    """The requested output directory is not allowed."""


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if hasattr(path, "is_junction"):          # Python 3.12+
            return bool(path.is_junction())
        return bool(getattr(os.path, "isjunction", lambda _p: False)(str(path)))
    except OSError:
        return True


def resolve_out_dir(root: Path, raw: Optional[str]) -> Path:
    """Return the directory projects are written under: ``root`` itself for an empty request, else ``root/<relative path>``.

    Each component must match a conservative charset; ``..``, absolute paths, drive letters, UNC paths, NUL and control characters are refused;
    no existing component below the root may be a symlink or junction, and the resolved result must stay inside the resolved root."""
    root_resolved = root.resolve()
    text = (raw or "").strip()
    if not text:
        return root_resolved
    if any(ord(c) < 0x20 for c in text):
        raise PathRejected("out_dir contains control characters")
    if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", text):
        raise PathRejected("out_dir must be relative to the Studio projects folder (no absolute, drive or UNC paths)")
    parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return root_resolved
    for part in parts:
        if part == "..":
            raise PathRejected("out_dir must not contain '..'")
        if not _COMPONENT_RX.match(part) or part.endswith((" ", ".")):
            raise PathRejected(f"out_dir component {part[:20]!r} is not allowed (letters, digits, . _ - and spaces only)")
    current = root_resolved
    for part in parts:
        current = current / part
        if current.exists() and _is_link_or_junction(current):
            raise PathRejected("out_dir must not pass through a symlink or junction")
    final = current.resolve()
    try:
        final.relative_to(root_resolved)
    except ValueError:
        raise PathRejected("out_dir resolves outside the Studio projects folder") from None
    return final


# --- sessions --------------------------------------------------------------------------------------------------------------------------------

class SessionLimit(Exception):
    """Too many live sessions."""


class StepBudgetExceeded(Exception):
    """This session has used its LLM step budget."""


@dataclass
class SessionStore:
    """Thread-safe session map with idle expiry, a hard cap and a per-session step budget. ``clock`` is injectable for tests."""
    idle_ttl_s: float = DEFAULT_IDLE_TTL_S
    max_sessions: int = DEFAULT_MAX_SESSIONS
    max_steps: int = DEFAULT_MAX_STEPS
    clock: Callable[[], float] = time.monotonic
    _items: dict = field(default_factory=dict)
    _last: dict = field(default_factory=dict)
    _steps: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _expire(self) -> None:
        now = self.clock()
        for sid in [k for k, t in self._last.items() if now - t > self.idle_ttl_s]:
            self._drop(sid)

    def _drop(self, sid: str) -> None:
        self._items.pop(sid, None)
        self._last.pop(sid, None)
        self._steps.pop(sid, None)

    def add(self, sid: str, session: Any) -> None:
        with self._lock:
            self._expire()
            if sid not in self._items and len(self._items) >= self.max_sessions:
                raise SessionLimit(f"too many open sessions (limit {self.max_sessions}); delete one or wait for it to expire")
            self._items[sid] = session
            self._last[sid] = self.clock()
            self._steps.setdefault(sid, 0)

    def get(self, sid: str) -> Any:
        """The session (and its idle timer is refreshed), or None when unknown or expired."""
        with self._lock:
            self._expire()
            session = self._items.get(sid)
            if session is not None:
                self._last[sid] = self.clock()
            return session

    def remove(self, sid: str) -> None:
        with self._lock:
            self._drop(sid)

    def charge_step(self, sid: str) -> int:
        """Count one LLM step against the session; raises StepBudgetExceeded past the budget. Returns the steps used."""
        with self._lock:
            used = self._steps.get(sid, 0)
            if used >= self.max_steps:
                raise StepBudgetExceeded(f"this session has used its {self.max_steps} step runs; start a new session")
            self._steps[sid] = used + 1
            return used + 1

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._items)


def max_steps_from_env() -> int:
    try:
        value = int(os.environ.get(MAX_STEPS_ENV, DEFAULT_MAX_STEPS))
    except ValueError:
        return DEFAULT_MAX_STEPS
    return max(1, min(value, 1000))
