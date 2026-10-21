"""Shared plumbing for activities: telemetry, error types, and the (careful) way we launch Windows PowerShell."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..logging import log_event
from ..retry import AutomationError, LogicError

IS_WINDOWS = sys.platform == "win32"


class NotAvailable(LogicError):
    """The capability needs something this host does not have (Windows, Outlook, an optional library...).

    Classified as a LOGIC error: retrying will not install Outlook.
    """


class ActivityError(AutomationError):
    """An activity failed in a way that has no better classification (UNKNOWN: the retry engine asks the LLM or escalates)."""


def emit(activity: str, **data: Any) -> None:
    """Record a telemetry event for an activity. Telemetry must never break the activity itself."""
    try:
        log_event("activity", {"activity": activity, **data})
    except Exception:  # noqa: BLE001
        pass


def require_windows(what: str) -> None:
    if not IS_WINDOWS:
        raise NotAvailable(f"{what} is only available on Windows")


def ps_env(extra: "dict[str, str] | None" = None) -> "dict[str, str]":
    """Environment for a Windows PowerShell 5.1 child.

    A PowerShell 7 parent (or a CI runner) leaks its PSModulePath into the environment, after which Windows PowerShell
    cannot load Microsoft.PowerShell.Security / UIAutomation helpers. Always pin the built-in module path.
    """
    env = dict(os.environ)
    pf = env.get("ProgramFiles", r"C:\Program Files")
    win = env.get("windir", r"C:\Windows")
    env["PSModulePath"] = ";".join([
        os.path.join(pf, "WindowsPowerShell", "Modules"),
        os.path.join(win, "System32", "WindowsPowerShell", "v1.0", "Modules"),
    ])
    env.update(extra or {})
    return env


def powershell_exe() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


PS_PREAMBLE = (
    "$ErrorActionPreference = 'Stop'\n"
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
    "$A = $null\n"
    "if ($env:AMX_ARGS_FILE) { $A = Get-Content -Raw -Encoding UTF8 -LiteralPath $env:AMX_ARGS_FILE | ConvertFrom-Json }\n"
    "elseif ($env:AMX_ARGS) { $A = $env:AMX_ARGS | ConvertFrom-Json }\n"
)


def write_script(script: str, directory: Path) -> Path:
    """Write a .ps1 as UTF-8 with BOM (Windows PowerShell 5.1 needs the BOM for non-ASCII)."""
    fd, name = tempfile.mkstemp(suffix=".ps1", dir=str(directory))
    with os.fdopen(fd, "wb") as f:
        f.write(b"\xef\xbb\xbf" + script.encode("utf-8"))
    return Path(name)


def run_powershell(script: str, args: Any = None, timeout: float = 60, env_extra: "dict[str, str] | None" = None,
                   what: str = "PowerShell") -> subprocess.CompletedProcess:
    """Run `script` (with PS_PREAMBLE) in a fresh powershell.exe. `args` is passed as JSON in $A (never on the command line)."""
    require_windows(what)
    with tempfile.TemporaryDirectory(prefix="amx-ps-") as tmp:
        tmpdir = Path(tmp)
        extra = dict(env_extra or {})
        if args is not None:
            payload = json.dumps(args)
            if len(payload) > 20000:
                af = tmpdir / "args.json"
                af.write_text(payload, encoding="utf-8")
                extra["AMX_ARGS_FILE"] = str(af)
            else:
                extra["AMX_ARGS"] = payload
        ps1 = write_script(PS_PREAMBLE + script, tmpdir)
        try:
            return subprocess.run([powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                                  capture_output=True, timeout=timeout, env=ps_env(extra), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise ActivityError(f"{what} timed out after {timeout}s") from exc


def _decode(b: bytes) -> str:
    return b.decode("utf-8", "replace").lstrip("\ufeff")


def powershell_json(script: str, args: Any = None, timeout: float = 60, env_extra: "dict[str, str] | None" = None,
                    what: str = "PowerShell") -> Any:
    """Run a script that prints one JSON document; return it parsed. Non-zero exit => ActivityError with a short stderr."""
    r = run_powershell(script, args, timeout, env_extra, what)
    out, err = _decode(r.stdout).strip(), _decode(r.stderr).strip()
    if r.returncode != 0:
        raise ActivityError(f"{what} failed ({r.returncode}): {(err or out)[:400]}")
    if not out:
        return None
    try:
        return json.loads(out.splitlines()[-1] if "\n" in out and not out.startswith(("[", "{")) else out)
    except ValueError as exc:
        raise ActivityError(f"{what} returned non-JSON output: {out[:200]}") from exc


def data_dir(*parts: str) -> Path:
    """AUTOMAGIX_HOME/data/<parts...> (created)."""
    from ..paths import Paths
    d = Paths().root.joinpath("data", *parts)
    d.mkdir(parents=True, exist_ok=True)
    return d
