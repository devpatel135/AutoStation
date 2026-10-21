"""Operator operations as pure functions over the manifest dict.

Each mutates the dict in place and returns an ``OpResult`` (summary + suggested MR title + history record). They never
touch the filesystem; ``cli`` validates and persists, and the admin panel generator applies them to a deep copy to
precompute exact diffs. Governance note: every one of these is meant to land through a merge request.
"""
from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass, field

from wrapper_core import versions
from wrapper_core.paths import CLASSIFICATIONS, RINGS

from .store import now_iso

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
MAX_REASON = 300
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OpError(Exception):
    """A refused operation (bad input or governance rule). The message is shown to the operator verbatim."""


@dataclass
class OpResult:
    action: str
    target: str
    summary: list[str]
    mr_title: str
    detail: str = ""
    manifest_changed: bool = True
    triggers_changed: bool = False
    extra: dict = field(default_factory=dict)


def clean_reason(reason: str | None, required: bool = False) -> str:
    r = re.sub(r"\s+", " ", (reason or "").strip())
    if required and not r:
        raise OpError("a --reason is required for this action (it is shown to users and kept in the history)")
    if len(r) > MAX_REASON:
        raise OpError(f"reason is too long ({len(r)} > {MAX_REASON} characters)")
    return r


def resolve(m: dict, name: str) -> tuple[str, dict]:
    comp = (m.get("components") or {}).get(name)
    auto = (m.get("automations") or {}).get(name)
    if comp is not None and auto is not None:
        raise OpError(f"{name!r} is both a component and an automation - rename one")
    if comp is not None:
        return "component", comp
    if auto is not None:
        return "automation", auto
    known = sorted(list(m.get("components") or {}) + list(m.get("automations") or {}))
    raise OpError(f"unknown component/automation {name!r}. Known: {', '.join(known) or '(none)'}")


def _need_automation(m: dict, name: str) -> dict:
    kind, entry = resolve(m, name)
    if kind != "automation":
        raise OpError(f"{name!r} is a component; this action applies to automations")
    return entry


def ring_order(m: dict) -> list[str]:
    rings = [r for r in RINGS if r in (m.get("rings") or RINGS)]
    return rings or list(RINGS)


def _sort_versions(entry: dict) -> None:
    vs = entry.get("versions") or {}
    entry["versions"] = {k: vs[k] for k in sorted(vs, key=versions.parse)}


# ---- add-version -----------------------------------------------------------------------------------
def add_version(m: dict, name: str, version: str, rec: dict, *, kind: str | None = None) -> OpResult:
    try:
        versions.parse(version)
    except ValueError:
        raise OpError(f"{version!r} is not a semantic version (expected e.g. 1.2.0)")
    k, entry = resolve(m, name) if kind is None else (kind, ((m.get("components") if kind == "component" else m.get("automations")) or {}).get(name))
    if entry is None:
        hint = " Run `new-automation` first so the catalog entry exists." if kind == "automation" else ""
        raise OpError(f"unknown {kind} {name!r}.{hint}")
    vs = entry.setdefault("versions", {})
    if version in vs:
        if vs[version].get("sha256") == rec.get("sha256"):
            return OpResult("add-version", name, [f"{name} {version} is already registered with the same sha256 - nothing to do"],
                            "", manifest_changed=False)
        raise OpError(f"{name} {version} already exists with a different sha256 - released versions are immutable; "
                      "publish a new version number")
    vs[version] = rec
    _sort_versions(entry)
    rings = entry.setdefault("rings", {})
    rings["dev"] = version
    entry["rings"] = {r: rings[r] for r in RINGS if r in rings}
    return OpResult("add-version", name,
                    [f"added {k} {name} {version} (sha256 {str(rec['sha256'])[:12]}...)", f"assigned to ring dev only",
                     f"promote with: python -m registry promote {name} --to pilot"],
                    f"registry: add {name} {version} to dev", f"{version} -> dev", extra={"kind": k})


# ---- promote / rollback ----------------------------------------------------------------------------
def promote(m: dict, name: str, to: str, version: str | None = None) -> OpResult:
    kind, entry = resolve(m, name)
    order = ring_order(m)
    if to not in order or to == order[0]:
        raise OpError(f"--to must be one of {order[1:]} (dev is only assigned via add-version)")
    prev = order[order.index(to) - 1]
    rings = entry.setdefault("rings", {})
    src = rings.get(prev)
    if not src:
        raise OpError(f"{name} has no version in ring {prev} yet - nothing to promote to {to}")
    v = version or src
    if v not in (entry.get("versions") or {}):
        raise OpError(f"{name} {v} is not a registered version")
    if versions.compare(v, src) > 0:
        raise OpError(f"{name} {v} is newer than what ring {prev} runs ({src}): promote through {prev} first")
    if rings.get(to) == v:
        return OpResult("promote", name, [f"{name} {v} already runs in {to} - nothing to do"], "", manifest_changed=False)
    old = rings.get(to)
    if old and versions.compare(v, old) < 0:
        raise OpError(f"{name} {v} is older than {old} already in {to}; use `rollback` for downgrades")
    rings[to] = v
    entry["rings"] = {r: rings[r] for r in RINGS if r in rings}
    lines = [f"{name}: ring {to} {old or '(none)'} -> {v}"]
    if kind == "automation" and entry.get("status") == "paused":
        lines.append(f"warning: {name} is paused ({entry.get('paused_reason')}); resume separately")
    return OpResult("promote", name, lines, f"registry: promote {name} {v} to {to}", f"{old or '-'} -> {v} in {to}")


def rollback(m: dict, name: str, ring: str, to: str) -> OpResult:
    kind, entry = resolve(m, name)
    order = ring_order(m)
    if ring not in order:
        raise OpError(f"--ring must be one of {order}")
    if to not in (entry.get("versions") or {}):
        raise OpError(f"{name} {to} is not a registered version (known: {', '.join(entry.get('versions') or {})})")
    rings = entry.setdefault("rings", {})
    changed = []
    for r in order[order.index(ring):]:
        cur = rings.get(r)
        if cur is None:
            continue
        if versions.compare(cur, to) > 0:
            rings[r] = to
            changed.append(f"{r}: {cur} -> {to}")
    if not changed:
        raise OpError(f"{name} is already at or below {to} in {ring} and later rings - nothing to roll back")
    entry["rings"] = {r: rings[r] for r in RINGS if r in rings}
    return OpResult("rollback", name, [f"{name} rolled back"] + changed + ["endpoints heal to this version on their next scheduled run"],
                    f"registry: ROLLBACK {name} to {to} ({ring}+)", "; ".join(changed))


# ---- kill-switch -----------------------------------------------------------------------------------
def pause(m: dict, name: str, reason: str) -> OpResult:
    entry = _need_automation(m, name)
    reason = clean_reason(reason, required=True)
    if entry.get("status") == "paused" and entry.get("paused_reason") == reason:
        return OpResult("pause", name, [f"{name} is already paused with that reason"], "", manifest_changed=False)
    entry["status"], entry["paused_reason"] = "paused", reason
    return OpResult("pause", name, [f"{name}: status active -> paused ({reason})",
                                    "wrapper-core halts it at the next self-heal check; no payload runs"],
                    f"registry: PAUSE {name} - {reason[:60]}", reason)


def resume(m: dict, name: str, reason: str) -> OpResult:
    entry = _need_automation(m, name)
    reason = clean_reason(reason, required=True)
    if entry.get("status") != "paused":
        return OpResult("resume", name, [f"{name} is not paused - nothing to do"], "", manifest_changed=False)
    entry["status"], entry["paused_reason"] = "active", ""
    return OpResult("resume", name, [f"{name}: status paused -> active"], f"registry: resume {name} - {reason[:60]}", reason)


def fleet_pause(m: dict, reason: str) -> OpResult:
    reason = clean_reason(reason, required=True)
    m["fleet"] = {"paused": True, "paused_reason": reason}
    return OpResult("fleet-pause", "fleet", [f"FLEET PAUSED: {reason}", "every automation halts at its next run"],
                    f"registry: FLEET PAUSE - {reason[:60]}", reason)


def fleet_resume(m: dict, reason: str) -> OpResult:
    reason = clean_reason(reason, required=True)
    if not (m.get("fleet") or {}).get("paused"):
        return OpResult("fleet-resume", "fleet", ["fleet is not paused - nothing to do"], "", manifest_changed=False)
    m["fleet"] = {"paused": False, "paused_reason": ""}
    return OpResult("fleet-resume", "fleet", ["fleet resumed"], f"registry: fleet resume - {reason[:60]}", reason)


# ---- decommission ----------------------------------------------------------------------------------
def decommission(m: dict, name: str, reason: str) -> OpResult:
    entry = _need_automation(m, name)
    reason = clean_reason(reason, required=True)
    if entry.get("status") != "paused":
        raise OpError(f"{name} must be paused first (so machines have stopped it) - run `pause` and let it merge")
    removed_rules = []
    for other, oe in (m.get("automations") or {}).items():
        if other == name:
            continue
        rules = (oe or {}).get("triggers") or []
        keep = [t for t in rules if t.get("on_automation") != name]
        if len(keep) != len(rules):
            removed_rules.append(other)
            if keep:
                oe["triggers"] = keep
            else:
                oe.pop("triggers", None)
    del m["automations"][name]
    lines = [f"removed {name} from the manifest", "installed copies halt as 'not registered'; users uninstall via the NSIS uninstaller"]
    if removed_rules:
        lines.append("also removed trigger rules in: " + ", ".join(removed_rules))
    return OpResult("decommission", name, lines, f"registry: DECOMMISSION {name}", reason, extra={"removed_rules_in": removed_rules})


# ---- new-automation --------------------------------------------------------------------------------
def new_automation(m: dict, name: str, *, owner: str, description: str, owner_team: str = "", systems: list[str] | None = None,
                   data_classification: str = "internal", llm_allowed: str = "internal", time: str = "09:00",
                   timezone: str = "UTC", tags: list[str] | None = None, process_owner: str = "") -> OpResult:
    if not NAME_RE.match(name):
        raise OpError("name must be lower-case letters, digits and dashes (e.g. automation-invoice-router)")
    if name in (m.get("automations") or {}) or name in (m.get("components") or {}):
        raise OpError(f"{name!r} already exists")
    if not EMAIL_RE.match(owner):
        raise OpError("--owner must be an email address (failure alerts are routed to it)")
    if data_classification not in CLASSIFICATIONS or llm_allowed not in CLASSIFICATIONS:
        raise OpError(f"classification must be one of {CLASSIFICATIONS}")
    if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", time):
        raise OpError("--time must look like 09:00")
    systems = systems or ["none"]
    stanza = {
        "description": description,
        "owner": owner,
        "owner_team": owner_team or "TODO owner team",
        "systems": systems,
        "data_classification": data_classification,
        "status": "paused",
        "paused_reason": "awaiting first release (new automation skeleton)",
        "llm_allowed_classification": llm_allowed,
        "rings": {},
        "versions": {},
        "schedule": {
            "timezone": timezone,
            "expected_duration_ms": {"baseline": 120000, "warn_threshold": 2.0},
            "maintenance_windows": [{"days": ["Saturday", "Sunday"]}],
            "slots": [{"id": "daily", "type": "daily", "time": time, "params": {}}],
        },
        "error_handling": {"max_retries": 3, "network_retry_wait_s": 60, "backoff_multiplier": 2.0,
                           "skip_on_data_error": True, "escalate_after_retries": True},
        "notify_on_complete": False,
        "catalog": {
            "summary": description[:200],
            "systems": systems,
            "owner_team": owner_team or "TODO owner team",
            "data_classification": data_classification,
            "process_owner": process_owner or owner,
            "links": [],
            "minutes_per_item": 0,
            "tags": tags or ["new"],
        },
    }
    m.setdefault("automations", {})[name] = stanza
    return OpResult("new-automation", name,
                    [f"added skeleton for {name} (paused until its first release)",
                     "next: package + `add-version`, test in dev, then `promote` and `resume`"],
                    f"registry: new automation {name}", f"owner {owner}")


# ---- triggers.json ---------------------------------------------------------------------------------
def add_trigger(doc: dict, m: dict, automation: str, slot: str | None, params: dict | None, source: str = "operator",
                requested_by: str | None = None, prune_days: int = 30) -> OpResult:
    entry = _need_automation(m, automation)
    poll = [s for s in ((entry.get("schedule") or {}).get("slots") or []) if s.get("type") == "poll"]
    if not poll:
        raise OpError(f"{automation} has no `poll` slot, so a trigger would never be consumed. Add a poll slot to its schedule "
                      "(type: poll, every_minutes: N) via a normal MR first.")
    ids = [s["id"] for s in poll]
    slot = slot or ids[0]
    if slot not in ids:
        raise OpError(f"slot {slot!r} is not a poll slot of {automation} (poll slots: {ids})")
    if entry.get("status") == "paused":
        raise OpError(f"{automation} is paused - a trigger would be ignored. Resume it first.")
    stamp = now_iso()
    trig = {"id": f"t-{stamp.replace('-', '').replace(':', '')}-{uuid.uuid4().hex[:8]}", "automation": automation, "slot": slot,
            "params": {str(k): str(v) for k, v in (params or {}).items()}, "created_at": stamp, "source": source}
    if requested_by:
        trig["requested_by"] = requested_by
    kept = [t for t in doc.get("triggers", []) if str(t.get("created_at", "9999")) >= _days_before(stamp, prune_days)]
    kept.append(trig)
    doc["triggers"] = kept
    return OpResult("trigger-add", automation,
                    [f"queued trigger {trig['id']} for {automation} (slot {slot}, params {trig['params'] or '{}'})",
                     "machines pick it up at their next poll run after the registry is republished"],
                    f"registry: trigger {automation}", f"{trig['id']} params={trig['params']}",
                    manifest_changed=False, triggers_changed=True, extra={"trigger": trig})


def _days_before(stamp: str, days: int) -> str:
    from datetime import datetime, timedelta, timezone
    base = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (base - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def preview(m: dict, fn, *args, **kwargs) -> tuple[OpResult, str, str]:
    """Apply an op to a deep copy; return (result, before_yaml, after_yaml)."""
    from .store import dump_manifest
    work = copy.deepcopy(m)
    res = fn(work, *args, **kwargs)
    return res, dump_manifest(m), dump_manifest(work)


# ---- dispatcher shared by the CLI `ops-action` command, the CI job and the admin panel precompute -------------
ACTION_PARAMS = {
    "pause": set(), "resume": set(), "fleet-pause": set(), "fleet-resume": set(), "decommission": set(),
    "promote": {"to", "version"}, "rollback": {"ring", "to"}, "trigger-add": {"slot", "params"},
}
MANIFEST_ACTIONS = {"pause", "resume", "fleet-pause", "fleet-resume", "decommission", "promote", "rollback"}


def apply_action(m: dict, action: str, target: str, reason: str = "", params: dict | None = None,
                 triggers_doc: dict | None = None, actor: str | None = None) -> OpResult:
    params = params or {}
    if action not in ACTION_PARAMS:
        raise OpError(f"unknown action {action!r}; allowed: {', '.join(sorted(ACTION_PARAMS))}")
    extra = set(params) - ACTION_PARAMS[action]
    if extra:
        raise OpError(f"{action} does not accept parameter(s): {', '.join(sorted(extra))}")
    if action in ("fleet-pause", "fleet-resume"):
        return (fleet_pause if action == "fleet-pause" else fleet_resume)(m, reason)
    if not NAME_RE.match(target or ""):
        raise OpError(f"invalid target {target!r}")
    if action == "pause":
        return pause(m, target, reason)
    if action == "resume":
        return resume(m, target, reason)
    if action == "decommission":
        return decommission(m, target, reason)
    if action == "promote":
        if "to" not in params:
            raise OpError("promote needs PARAMS {\"to\": \"pilot|broad\"}")
        return promote(m, target, str(params["to"]), params.get("version"))
    if action == "rollback":
        if not {"ring", "to"} <= set(params):
            raise OpError("rollback needs PARAMS {\"ring\": ..., \"to\": version}")
        clean_reason(reason, required=True)
        return rollback(m, target, str(params["ring"]), str(params["to"]))
    if action == "trigger-add":
        p = params.get("params") or {}
        if not isinstance(p, dict):
            raise OpError("trigger-add PARAMS.params must be an object")
        return add_trigger(triggers_doc if triggers_doc is not None else {"triggers": []}, m, target, params.get("slot"), p,
                           source="admin-panel", requested_by=actor)
    raise OpError(f"unhandled action {action}")  # pragma: no cover
