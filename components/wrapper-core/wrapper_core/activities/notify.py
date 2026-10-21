"""Notifications and desktop helpers: Teams webhook, clipboard, screen capture.

* ``teams_webhook(url_or_credential, title, text, facts=None)``: post a MessageCard to a Teams/Office 365 incoming webhook. The URL
  is a secret: pass the Credential Manager *name* (``AUTOMAGIX/cred/<name>``, the password field - or the whole value - holds the URL)
  rather than the URL itself. The URL is never logged.
* ``clipboard.get() / clipboard.set(text)`` and ``screenshot_screen(path)``: Windows only (PowerShell + .NET), need an interactive desktop.
"""
from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from typing import Dict, Optional

from ..retry import AuthExpired, DataError, NetworkError, SystemUnavailable
from ._common import ActivityError, NotAvailable, emit, powershell_json, require_windows, run_powershell, _decode


_URL_OK = re.compile(r"^(https://|http://(127\.0\.0\.1|localhost)[:/])")  # plain http only for loopback (local tests / relays)


def _webhook_url(url_or_credential: str) -> str:
    if _URL_OK.match(url_or_credential):
        return url_or_credential
    from .credentials import get_raw, parse_credential
    raw = get_raw(url_or_credential).strip()  # raises CredentialNotFound with a helpful message
    if _URL_OK.match(raw):
        return raw
    cred = parse_credential(raw)  # user:password or JSON form: the URL is the password field
    if _URL_OK.match(str(cred.password)):
        return str(cred.password)
    raise DataError("webhook credential does not contain an https URL")


def build_teams_card(title: str, text: str, facts: Optional[Dict[str, str]] = None) -> dict:
    card = {"@type": "MessageCard", "@context": "https://schema.org/extensions", "summary": title[:200], "title": title[:250],
            "text": text[:10000].replace("\n", "<br>")}
    if facts:
        card["sections"] = [{"facts": [{"name": str(k)[:100], "value": str(v)[:500]} for k, v in facts.items()]}]
    return card


def teams_webhook(url_or_credential: str, title: str, text: str = "", facts: Optional[Dict[str, str]] = None, timeout: float = 20) -> None:
    url = _webhook_url(url_or_credential)
    req = urllib.request.Request(url, data=json.dumps(build_teams_card(title, text, facts)).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https URL from secure storage
            body = r.read(200)
            if r.status >= 300:
                raise SystemUnavailable(f"webhook returned HTTP {r.status}")
            if body and body.strip() not in (b"1", b"ok", b"OK") and b"error" in body.lower():
                raise DataError("webhook rejected the message")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise AuthExpired(f"webhook rejected the request (HTTP {exc.code}); the URL may have been revoked") from exc
        if exc.code == 429 or exc.code >= 500:
            raise SystemUnavailable(f"webhook temporarily unavailable (HTTP {exc.code})") from exc
        raise DataError(f"webhook error (HTTP {exc.code})") from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise NetworkError(f"webhook unreachable: {type(getattr(exc, 'reason', exc)).__name__}") from exc
    emit("notify.teams_webhook", title_chars=len(title))  # the URL is never logged


# --- clipboard / screen (Windows) ---------------------------------------------------------------------------
class _Clipboard:
    def get(self) -> str:
        require_windows("the clipboard")
        r = run_powershell("$t = Get-Clipboard -Raw; if ($null -eq $t) { $t = '' }; [Console]::Out.Write([string]$t)", timeout=30, what="clipboard")
        if r.returncode != 0:
            raise ActivityError("clipboard read failed (needs an interactive desktop)")
        return _decode(r.stdout)

    def set(self, text: str) -> None:
        require_windows("the clipboard")
        r = run_powershell("Set-Clipboard -Value ([string]$A.text)", {"text": text}, timeout=30, what="clipboard")
        if r.returncode != 0:
            raise ActivityError("clipboard write failed (needs an interactive desktop)")
        emit("notify.clipboard_set", chars=len(text))


clipboard = _Clipboard()

SCREENSHOT_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
if ($b.Width -le 0 -or $b.Height -le 0) { throw 'no interactive desktop' }
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$g.Dispose()
$bmp.Save([string]$A.path, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
ConvertTo-Json -Compress -InputObject @{ ok = $true; width = $b.Width; height = $b.Height }
"""


def screenshot_screen(path: "str | os.PathLike") -> str:
    """Capture the whole (virtual) screen to a PNG. Windows only; needs an interactive desktop."""
    require_windows("screen capture")
    dest = os.path.abspath(os.fspath(path))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        info = powershell_json(SCREENSHOT_SCRIPT, {"path": dest}, timeout=60, what="screenshot")
    except ActivityError as exc:
        raise NotAvailable("screen capture failed (no interactive desktop?)") from exc
    emit("notify.screenshot_screen", width=info.get("width"), height=info.get("height"))
    return dest
