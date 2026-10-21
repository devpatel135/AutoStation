"""Registry manifest model: load, validate, resolve per ring, maintenance windows (SPEC §4)."""
from __future__ import annotations

import re
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import miniyaml, schedule as _schedule, versions
from .paths import CLASSIFICATIONS, RINGS

SUPPORTED_SCHEMA = 1
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WINDOW = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")
_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_SLOT_TYPES = ("daily", "weekly", "hourly", "poll", "monthly", "weekdays", "interval", "cron")


class ManifestError(ValueError):
    pass


def parse(text: str) -> dict:
    """Parse + sanity-check a manifest. Anything that is not a well-formed manifest raises ManifestError (never anything else)."""
    try:
        data = miniyaml.loads(text)
    except miniyaml.YamlError as exc:
        raise ManifestError(f"manifest is not valid YAML (supported subset): {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - a hostile document must never crash the caller
        raise ManifestError(f"manifest cannot be parsed: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be a mapping")
    major = data.get("schema_version")
    if major != SUPPORTED_SCHEMA:
        raise ManifestError(f"unsupported manifest schema_version {major!r} (this wrapper supports {SUPPORTED_SCHEMA})")
    for key in ("fleet", "components", "automations", "assets"):
        if data.get(key) is not None and not isinstance(data[key], dict):
            raise ManifestError(f"manifest {key!r} must be a mapping")
    if data.get("compatibility") is not None and not isinstance(data["compatibility"], list):
        raise ManifestError("manifest 'compatibility' must be a list")
    return data


def load(path: Path | str) -> dict:
    return parse(Path(path).read_text(encoding="utf-8"))


# --- resolution ------------------------------------------------------------------------------------
def fleet_paused(m: dict) -> tuple[bool, str]:
    f = m.get("fleet") or {}
    return bool(f.get("paused")), str(f.get("paused_reason") or "")


def automation(m: dict, name: str) -> dict | None:
    a = (m.get("automations") or {}).get(name)
    return a if isinstance(a, dict) else None


def paused(m: dict, name: str) -> tuple[bool, str]:
    """Kill-switch: fleet-wide or per-automation. Returns (halt?, reason)."""
    fp, reason = fleet_paused(m)
    if fp:
        return True, reason or "fleet paused by operator"
    a = automation(m, name)
    if a is None:
        return True, "automation is not registered in the manifest (decommissioned?)"
    if str(a.get("status", "active")) == "paused":
        return True, str(a.get("paused_reason") or "paused by operator")
    return False, ""


def target_version(entry: dict, ring: str) -> str | None:
    return (entry.get("rings") or {}).get(ring)


def version_record(entry: dict, version: str) -> dict:
    rec = (entry.get("versions") or {}).get(str(version))
    if not rec:
        raise ManifestError(f"version {version} is not listed in versions")
    return rec


def assets_for(m: dict, ring: str) -> dict:
    assets = m.get("assets") or {}
    out = dict(assets.get("default") or {})
    out.update(assets.get(ring) or {})
    return out


def compatible(m: dict, wrapper_version: str, framework_version: str) -> bool:
    matrix = m.get("compatibility") or []
    if not matrix:
        return True
    return any(versions.satisfies(wrapper_version, row.get("wrapper_core")) and
               versions.satisfies(framework_version, row.get("execution_framework"))
               for row in matrix if isinstance(row, dict))


# --- schedule / maintenance windows -------------------------------------------------------------
def _tz(name: str | None):
    if not name:
        return None
    if name in ("UTC", "Etc/UTC", "GMT", "Z"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - no tzdata on stock Windows Python: fall back to local time
        return None


def in_maintenance(schedule: dict | None, now: datetime | None = None) -> dict | None:
    """Return the matching maintenance window dict if `now` falls inside one, else None."""
    if not schedule:
        return None
    now = now or datetime.now(timezone.utc)
    tz = _tz(schedule.get("timezone"))
    local = now.astimezone(tz) if tz else now.astimezone()
    from . import schedule as _sched
    hit = _sched.in_blackout(schedule.get("blackouts"), local.date())  # calendar blackout dates (holidays, freezes) skip the run too
    if hit:
        return hit
    for w in schedule.get("maintenance_windows") or []:
        days = [str(d).capitalize() for d in (w.get("days") or [])]
        daily = w.get("daily")
        if days and not daily:
            if _DAYS[local.weekday()] in days:
                return w
        elif daily:
            m = _WINDOW.match(str(daily))
            if not m:
                continue
            start = dtime(int(m.group(1)), int(m.group(2)))
            end = dtime(int(m.group(3)), int(m.group(4)))
            t = local.time()
            if days and _DAYS[local.weekday()] not in days:
                continue
            inside = (start <= t < end) if start <= end else (t >= start or t < end)
            if inside:
                return w
    return None


# --- validation (mirrors automation-registry/schema/manifest.schema.json) ------------------------
def validate(m: dict) -> list[str]:
    """Return a list of human-readable problems (empty = valid)."""
    errs: list[str] = []
    if m.get("schema_version") != SUPPORTED_SCHEMA:
        errs.append(f"schema_version must be {SUPPORTED_SCHEMA}")
    rings = m.get("rings") or list(RINGS)
    for r in rings:
        if r not in RINGS:
            errs.append(f"unknown ring {r!r}")

    def check_entry(label: str, entry: Any, *, is_automation: bool):
        if not isinstance(entry, dict):
            errs.append(f"{label}: must be a mapping")
            return
        vers = entry.get("versions") or {}
        if not vers and not (is_automation and str(entry.get("status", "active")) == "paused"):
            errs.append(f"{label}: no versions listed")
        for v, rec in vers.items():
            try:
                versions.parse(v)
            except ValueError:
                errs.append(f"{label}: version key {v!r} is not semver")
            if not isinstance(rec, dict) or not rec.get("url"):
                errs.append(f"{label} {v}: missing url")
            elif not _SHA.match(str(rec.get("sha256", ""))):
                errs.append(f"{label} {v}: sha256 must be 64 hex chars")
            for key, spec in ((rec or {}).get("requires") or {}).items():
                try:
                    versions.validate_spec(spec)
                except ValueError:
                    errs.append(f"{label} {v}: bad requires.{key} constraint {spec!r}")
        for ring in rings:
            tv = (entry.get("rings") or {}).get(ring)
            if tv is None:
                if not is_automation:  # shared components must exist in every ring; an automation may simply not be released there yet
                    errs.append(f"{label}: ring {ring!r} has no version assigned")
            elif str(tv) not in vers:
                errs.append(f"{label}: ring {ring!r} points at {tv} which is not in versions")
        # promotion order: broad must not be newer than pilot, pilot not newer than dev
        rv = entry.get("rings") or {}
        try:
            if "broad" in rv and "pilot" in rv and versions.compare(str(rv["broad"]), str(rv["pilot"])) > 0:
                errs.append(f"{label}: broad ({rv['broad']}) is newer than pilot ({rv['pilot']}) - promote through pilot first")
            if "pilot" in rv and "dev" in rv and versions.compare(str(rv["pilot"]), str(rv["dev"])) > 0:
                errs.append(f"{label}: pilot ({rv['pilot']}) is newer than dev ({rv['dev']}) - promote through dev first")
        except ValueError:
            pass
        if not is_automation:
            return
        if entry.get("data_classification") and entry["data_classification"] not in CLASSIFICATIONS:
            errs.append(f"{label}: data_classification must be one of {CLASSIFICATIONS}")
        if entry.get("llm_allowed_classification") and entry["llm_allowed_classification"] not in CLASSIFICATIONS:
            errs.append(f"{label}: llm_allowed_classification must be one of {CLASSIFICATIONS}")
        if entry.get("status", "active") not in ("active", "paused"):
            errs.append(f"{label}: status must be active|paused")
        if entry.get("status") == "paused" and not entry.get("paused_reason"):
            errs.append(f"{label}: paused automations need a paused_reason")
        for field in ("description", "owner"):
            if not entry.get(field):
                errs.append(f"{label}: missing {field}")
        sch = entry.get("schedule") or {}
        for slot in sch.get("slots") or []:
            if slot.get("type") not in _SLOT_TYPES:
                errs.append(f"{label}: slot {slot.get('id')!r} has invalid type {slot.get('type')!r}")
            if not slot.get("id"):
                errs.append(f"{label}: slot without id")
            if slot.get("type") in ("daily", "weekly") and not _HHMM.match(str(slot.get("time", ""))):
                errs.append(f"{label}: slot {slot.get('id')!r} needs time \"HH:MM\" (quote it)")
            if slot.get("type") == "weekly":
                for d in slot.get("days") or []:
                    if str(d).capitalize() not in _DAYS:
                        errs.append(f"{label}: slot {slot.get('id')!r} bad day {d!r}")
            if slot.get("type") == "hourly" and int(slot.get("every", 0) or 0) < 1:
                errs.append(f"{label}: hourly slot {slot.get('id')!r} needs every >= 1")
            if slot.get("type") == "poll" and int(slot.get("every_minutes", 0) or 0) < 1:
                errs.append(f"{label}: poll slot {slot.get('id')!r} needs every_minutes >= 1")
            if slot.get("type") in _schedule.NEW_TYPES:
                errs.extend(_schedule.validate_slot(slot, label + ": slot"))
        errs.extend(_schedule.validate_blackouts(sch.get("blackouts"), f"{label}: schedule"))
        for w in sch.get("maintenance_windows") or []:
            if w.get("daily") and not _WINDOW.match(str(w["daily"])):
                errs.append(f"{label}: bad maintenance window {w['daily']!r}")
            for d in w.get("days") or []:
                if str(d).capitalize() not in _DAYS:
                    errs.append(f"{label}: bad maintenance day {d!r}")
        for trig in entry.get("triggers") or []:
            if not trig.get("on_automation"):
                errs.append(f"{label}: trigger without on_automation")

    for name, entry in (m.get("components") or {}).items():
        check_entry(f"component {name}", entry, is_automation=False)
    autos = m.get("automations") or {}
    for name, entry in autos.items():
        check_entry(f"automation {name}", entry, is_automation=True)
        for trig in (entry or {}).get("triggers") or []:
            if trig.get("on_automation") and trig["on_automation"] not in autos:
                errs.append(f"automation {name}: trigger refers to unknown automation {trig['on_automation']!r}")

    # compatibility: every ring's wrapper/framework pair must be allowed, and every automation's requires must hold
    comps = m.get("components") or {}
    for ring in rings:
        w = target_version(comps.get("wrapper-core", {}), ring)
        f = target_version(comps.get("execution-framework", {}), ring)
        if w and f and not compatible(m, str(w), str(f)):
            errs.append(f"ring {ring}: wrapper-core {w} + execution-framework {f} is not in the compatibility matrix")
        for name, entry in autos.items():
            av = target_version(entry or {}, ring)
            rec = ((entry or {}).get("versions") or {}).get(str(av)) if av else None
            req = (rec or {}).get("requires") or {}
            if w and not versions.satisfies(str(w), req.get("wrapper_core")):
                errs.append(f"ring {ring}: automation {name} {av} requires wrapper_core {req.get('wrapper_core')} but ring has {w}")
            if f and not versions.satisfies(str(f), req.get("execution_framework")):
                errs.append(f"ring {ring}: automation {name} {av} requires execution_framework {req.get('execution_framework')} but ring has {f}")
    return errs
