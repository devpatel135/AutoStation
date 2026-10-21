"""Shared helpers for the UiPath migration kit: secret redaction, name normalisation, cron -> schedule-slot conversion."""
from __future__ import annotations

import re

REDACTED = "***REDACTED***"

_SECRET_NAME = re.compile(r"(?i)(pass(word|wd|phrase)?|pwd|secret|token|api[-_ ]?key|client[-_ ]?secret|credential|private[-_ ]?key|connection[-_ ]?string)")
# a name that mentions "password" but is clearly an asset/credential REFERENCE rather than a value
_REFERENCE_HINT = re.compile(r"(?i)(asset|orchestrator|credentialname|credentialtarget|assetname|queue)")

_KV = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[-_]?key|client[-_]?secret|accesskey|access[-_]?token)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^;\s&\"',]+)")
_URL_CREDS = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{10,}")


def looks_like_secret_name(name: str) -> bool:
    """True for names like Password / ClientSecret / smtpPassword, but not for asset/queue *references*."""
    return bool(_SECRET_NAME.search(name or "")) and not _REFERENCE_HINT.search(name or "")


def is_expression(value: str) -> bool:
    """UiPath expressions in XAML attributes are bracketed: ``[strPassword]`` / ``[in_Config("X")]``."""
    v = (value or "").strip()
    return v.startswith("[") and v.endswith("]")


def redact_text(text: str) -> str:
    """Strip anything that looks like an inline credential from free text (selectors, URLs, connection strings)."""
    if not text:
        return text
    out = _URL_CREDS.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
    out = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    return _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", out)


def contains_inline_secret(text: str) -> bool:
    return bool(text) and redact_text(text) != text


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def snake(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s).lower()
    return s or "workflow"


def kebab(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", snake(name).replace("_", "-")).strip("-") or "project"


# --- cron -> slot -----------------------------------------------------------------------------------
_DOW_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
_DOW_FULL = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday", "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"}


def _dow_list(field: str, quartz: bool) -> list[str] | None:
    out: list[str] = []
    for part in field.upper().split(","):
        part = part.strip()
        if not part:
            return None
        if "-" in part:
            a, b = part.split("-", 1)
            ia, ib = _dow_index(a, quartz), _dow_index(b, quartz)
            if ia is None or ib is None:
                return None
            i = ia
            while True:
                out.append(_DOW_NAMES[i])
                if i == ib:
                    break
                i = (i + 1) % 7
        else:
            i = _dow_index(part, quartz)
            if i is None:
                return None
            out.append(_DOW_NAMES[i])
    seen: list[str] = []
    for d in out:
        if d not in seen:
            seen.append(d)
    return seen


def _dow_index(tok: str, quartz: bool):
    tok = tok.strip().upper()
    if tok in _DOW_NAMES:
        return _DOW_NAMES.index(tok)
    if tok.isdigit():
        n = int(tok)
        if quartz:  # 1=SUN .. 7=SAT
            return {1: 6, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5}.get(n)
        return {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6}.get(n)  # unix: 0/7=SUN
    return None


def cron_to_slot(cron: str) -> tuple[dict | None, str]:
    """Convert a Quartz (UiPath) or unix cron string to an AutoStation schedule slot.

    Returns ``(slot, note)``; ``slot`` is None when the expression has no faithful equivalent (monthly, sub-hourly...).
    """
    parts = (cron or "").split()
    quartz = len(parts) in (6, 7)
    if len(parts) == 7:
        parts = parts[:6]
    if len(parts) == 6:
        parts = parts[1:]
    if len(parts) != 5:
        return None, f"unrecognised cron expression {cron!r}"
    minute, hour, dom, mon, dow = parts
    any_ = ("*", "?")
    if mon not in any_ or dom not in any_:
        return None, f"calendar-day/monthly cron {cron!r} has no direct slot; use a daily slot and gate on date inside the automation"
    if minute.isdigit() and hour.isdigit():
        time = f"{int(hour):02d}:{int(minute):02d}"
        if dow in any_:
            return {"type": "daily", "time": time}, ""
        days = _dow_list(dow, quartz)
        if days:
            return {"type": "weekly", "days": [_DOW_FULL[d] for d in days], "time": time}, ""
        return None, f"unrecognised day-of-week {dow!r}"
    m = re.match(r"^(?:\*|\d+)/(\d+)$", hour)
    if minute.isdigit() and (hour == "*" or m) and dow in any_:
        return {"type": "hourly", "every": int(m.group(1)) if m else 1}, ""
    return None, f"sub-hourly/complex cron {cron!r}: use a poll slot (event/queue driven) or an hourly slot"
