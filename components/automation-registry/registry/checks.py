"""Validation: wrapper_core's shared rules + JSON-Schema + registry-only governance checks."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from wrapper_core import manifest as wc_manifest
from wrapper_core import miniyaml, versions
from wrapper_core.paths import CLASSIFICATIONS

from . import jsonschema_lite
from .store import PLACEHOLDER_SHA

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "manifest.schema.json"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TZ = re.compile(r"^(UTC|GMT|[A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2})$")
_KNOWN_AUTOMATION_KEYS = {"description", "owner", "owner_team", "systems", "data_classification", "status",
                          "paused_reason", "llm_allowed_classification", "rings", "versions", "schedule",
                          "error_handling", "notify_on_complete", "triggers", "catalog"}
_TRIGGER_STATUSES = ("success", "failed", "partial", "any")


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = []
        for e in self.errors:
            lines.append(f"ERROR   {e}")
        for w in self.warnings:
            lines.append(f"warning {w}")
        lines.append(f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)")
        return "\n".join(lines)


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def schema_errors(m: dict) -> list[str]:
    return jsonschema_lite.validate(m, load_schema())


def is_placeholder(sha: str | None) -> bool:
    return str(sha or "").lower() == PLACEHOLDER_SHA


def _automation_filter(errors: list[str], m: dict) -> list[str]:
    """The shared validator insists every ring has a version. For *automations* an absent ring means
    "not released there yet" (that is how promotion works), and a paused skeleton may have no versions yet."""
    kept = []
    autos = m.get("automations") or {}
    for e in errors:
        mt = re.match(r"^automation ([^:]+): (.*)$", e)
        if mt:
            name, msg = mt.group(1), mt.group(2)
            entry = autos.get(name) or {}
            if re.match(r"ring '[a-z]+' has no version assigned$", msg):
                continue
            if msg == "no versions listed" and entry.get("status") == "paused":
                continue
        kept.append(e)
    return kept


def validate_all(m: dict, *, allow_placeholders: bool = False, artifacts_dir: Path | str | None = None,
                 strict: bool = False, schema_only: bool = False) -> Report:
    rep = Report()
    rep.errors.extend(schema_errors(m))
    if schema_only:
        return rep
    try:
        rep.errors.extend(_automation_filter(wc_manifest.validate(m), m))
    except Exception as exc:  # noqa: BLE001 - malformed shapes the shared validator cannot walk
        rep.errors.append(f"structure problem prevented semantic validation: {type(exc).__name__}: {exc}")
        return rep
    _extra(m, rep, allow_placeholders)
    if artifacts_dir is not None:
        _check_artifacts(m, Path(artifacts_dir), rep)
    if strict:
        rep.errors.extend(f"(strict) {w}" for w in rep.warnings)
        rep.warnings = []
    # de-duplicate while keeping order (schema + semantic checks often flag the same defect)
    rep.errors = list(dict.fromkeys(rep.errors))
    return rep


def _extra(m: dict, rep: Report, allow_placeholders: bool) -> None:
    fleet = m.get("fleet") or {}
    if fleet.get("paused") and not fleet.get("paused_reason"):
        rep.errors.append("fleet: paused without a paused_reason")
    for i, row in enumerate(m.get("compatibility") or []):
        for key in ("wrapper_core", "execution_framework"):
            if not row.get(key):
                rep.errors.append(f"compatibility[{i}]: missing {key}")

    for kind, group in (("component", m.get("components") or {}), ("automation", m.get("automations") or {})):
        for name, entry in group.items():
            label = f"{kind} {name}"
            for v, rec in ((entry or {}).get("versions") or {}).items():
                if is_placeholder((rec or {}).get("sha256")) and not allow_placeholders:
                    rep.errors.append(f"{label} {v}: placeholder sha256 (all zeros) - publish a real artifact "
                                      "(use --allow-placeholders only for the starter/demo manifest)")
                for key in ((rec or {}).get("requires") or {}):
                    if key not in ("wrapper_core", "execution_framework"):
                        rep.errors.append(f"{label} {v}: unknown requires key {key!r}")

    autos = m.get("automations") or {}
    for name, entry in autos.items():
        entry = entry or {}
        label = f"automation {name}"
        owner = str(entry.get("owner") or "")
        if owner and not _EMAIL.match(owner):
            rep.errors.append(f"{label}: owner {owner!r} is not an email address (alerts are routed by it)")
        for key in entry:
            if key not in _KNOWN_AUTOMATION_KEYS:
                rep.warnings.append(f"{label}: unknown key {key!r} (ignored by wrapper-core)")
        sched = entry.get("schedule") or {}
        tz = sched.get("timezone")
        if tz and not _TZ.match(str(tz)):
            rep.errors.append(f"{label}: timezone {tz!r} is not an IANA name like America/New_York")
        seen = set()
        for slot in sched.get("slots") or []:
            sid = slot.get("id")
            if sid in seen:
                rep.errors.append(f"{label}: duplicate slot id {sid!r}")
            seen.add(sid)
        eh = entry.get("error_handling") or {}
        for key in eh:
            if key not in ("max_retries", "network_retry_wait_s", "backoff_multiplier", "vpn_tool",
                           "skip_on_data_error", "escalate_after_retries"):
                rep.errors.append(f"{label}: unknown error_handling key {key!r}")
        dc, la = entry.get("data_classification"), entry.get("llm_allowed_classification")
        if dc in CLASSIFICATIONS and la in CLASSIFICATIONS and CLASSIFICATIONS.index(dc) > CLASSIFICATIONS.index(la):
            rep.warnings.append(f"{label}: data is {dc} but the LLM endpoint is only approved for {la}; "
                                "all llm_call()s will be disabled (deterministic fallback only)")
        poll_slots = {s.get("id") for s in sched.get("slots") or [] if s.get("type") == "poll"}
        for t in entry.get("triggers") or []:
            if t.get("on_status", "any") not in _TRIGGER_STATUSES:
                rep.errors.append(f"{label}: trigger on_status must be one of {_TRIGGER_STATUSES}")
            slot = t.get("slot")
            if not poll_slots:
                rep.errors.append(f"{label}: has triggers but no poll slot - triggered runs would never start")
            elif slot and slot not in poll_slots:
                rep.errors.append(f"{label}: trigger slot {slot!r} is not a poll slot ({sorted(poll_slots)})")
        _catalog(label, entry, rep)


def _catalog(label: str, entry: dict, rep: Report) -> None:
    cat = entry.get("catalog")
    if cat is None:
        rep.warnings.append(f"{label}: no catalog block (portal shows description only)")
        return
    for field_ in ("summary", "owner_team", "process_owner"):
        if not cat.get(field_):
            rep.warnings.append(f"{label}: catalog.{field_} is empty")
    if not cat.get("systems"):
        rep.warnings.append(f"{label}: catalog.systems is empty (list the systems it touches, or [none])")
    cdc = cat.get("data_classification")
    if cdc and entry.get("data_classification") and cdc != entry["data_classification"]:
        rep.errors.append(f"{label}: catalog.data_classification {cdc!r} disagrees with data_classification "
                          f"{entry['data_classification']!r}")
    for link in cat.get("links") or []:
        url = link if isinstance(link, str) else link.get("url", "")
        if not re.match(r"^(https?://|mailto:)", str(url)):
            rep.errors.append(f"{label}: catalog link {url!r} must be http(s) or mailto")


def _check_artifacts(m: dict, root: Path, rep: Report) -> None:
    for kind, group in (("component", m.get("components") or {}), ("automation", m.get("automations") or {})):
        for name, entry in group.items():
            for v, rec in ((entry or {}).get("versions") or {}).items():
                url = str((rec or {}).get("url", ""))
                if re.match(r"^https?://", url):
                    continue
                path = root / url
                label = f"{kind} {name} {v}"
                if not path.is_file():
                    rep.errors.append(f"{label}: artifact {url} not found under {root}")
                    continue
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                if sha != str((rec or {}).get("sha256", "")).lower():
                    rep.errors.append(f"{label}: sha256 mismatch for {url} (manifest {str(rec.get('sha256'))[:12]}..., file {sha[:12]}...)")
                    continue
                if kind == "automation":
                    try:
                        with zipfile.ZipFile(path) as z:
                            desc = miniyaml.loads(z.read("automation.yaml").decode("utf-8"))
                    except (KeyError, zipfile.BadZipFile, miniyaml.YamlError) as exc:
                        rep.errors.append(f"{label}: {url} is not a valid automation package ({exc})")
                        continue
                    if desc.get("name") != name or str(desc.get("version")) != v:
                        rep.errors.append(f"{label}: package automation.yaml says {desc.get('name')} {desc.get('version')}")
