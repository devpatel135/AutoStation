"""Endpoint feedback (assessment item 30): status file always, Windows toast when enabled.

Toast uses the built-in ``Windows.UI.Notifications`` API through PowerShell (no external package, Windows 10+).
Suppress with ``AUTOMAGIX_NO_TOAST=1``. Notification failures are swallowed - feedback must never break a run.
"""
from __future__ import annotations

import subprocess
import sys

from .paths import Paths, get_logger
from .util import atomic_write_text, iso

_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' + $env:AMX_TITLE + '</text><text>' + $env:AMX_BODY + '</text></binding></visual></toast>')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def _xml_safe(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:200]


def toast(title: str, body: str) -> bool:
    import os
    if sys.platform != "win32" or os.environ.get("AUTOMAGIX_NO_TOAST") == "1":
        return False
    try:
        env = dict(os.environ, AMX_TITLE=_xml_safe(title), AMX_BODY=_xml_safe(body))
        env.pop("PSModulePath", None)  # a PowerShell 7 parent's module path breaks Windows PowerShell 5.1
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _PS],
                           env=env, capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception as exc:  # noqa: BLE001
        get_logger().warning("toast failed: %s", exc)
        return False


def write_status(paths: Paths, automation: str, text: str) -> None:
    """Human-readable status users can open: ``state\\toast\\<automation>.status.txt``."""
    try:
        atomic_write_text(paths.status_file(automation), f"{iso()}\n{text}\n")
    except OSError:
        pass


def clear_status(paths: Paths, automation: str) -> None:
    try:
        paths.status_file(automation).unlink()
    except OSError:
        pass


def paused_notice(paths: Paths, automation: str, reason: str) -> None:
    msg = f"{automation} is intentionally paused by the platform operators ({reason}). It will resume automatically."
    write_status(paths, automation, msg)
    toast("Automation paused", msg)


def version_bump_notice(automation: str, changes: list[str]) -> None:
    toast("Automation updated", f"{automation}: " + ", ".join(changes))


def run_complete_notice(automation: str, status: str, synopsis: str) -> None:
    toast(f"{automation}: {status}", synopsis)
