"""``python -m registry <command>``"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

from wrapper_core import miniyaml, versions
from wrapper_core.paths import RINGS

from . import __version__, ops, store
from .checks import validate_all
from .ops import OpError


def _actor() -> str | None:
    return os.environ.get("GITLAB_USER_LOGIN") or os.environ.get("USERNAME") or os.environ.get("USER")


def _allow_placeholders(args) -> bool:
    return bool(getattr(args, "allow_placeholders", False) or os.environ.get("REGISTRY_ALLOW_PLACEHOLDERS") in ("1", "true"))


def _finish(args, m: dict, res: ops.OpResult, doc: dict | None = None, *, history: bool = True, reason: str = "") -> int:
    """Persist (validated) and print the one-screen summary."""
    mpath = Path(args.manifest)
    print(f"[{res.action}] {res.target}")
    for line in res.summary:
        print("  " + line)
    if not (res.manifest_changed or res.triggers_changed):
        return 0
    if args.dry_run:
        print("  (dry run: nothing written)")
    else:
        if res.manifest_changed:
            store.save_manifest(mpath, m, allow_placeholders=_allow_placeholders(args))
        if res.triggers_changed and doc is not None:
            store.save_triggers(mpath.parent / store.TRIGGERS_FILE, doc)
        if history and res.manifest_changed:
            store.append_history(mpath, res.action, res.target, (reason or res.detail or res.action), _actor())
    if res.mr_title:
        print(f"\nSuggested MR title: {res.mr_title}")
    return 0


def _read_zip_meta(zip_path: Path) -> dict:
    meta: dict = {}
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if "automation.yaml" in names:
            meta["descriptor"] = miniyaml.loads(z.read("automation.yaml").decode("utf-8"))
        if "VERSION" in names:
            meta["version_file"] = z.read("VERSION").decode("utf-8").strip()
    return meta


def cmd_add_version(args) -> int:
    m = store.load_manifest(args.manifest)
    zp = Path(args.zip)
    if not zp.is_file():
        raise OpError(f"package not found: {zp}")
    try:
        meta = _read_zip_meta(zp)
    except zipfile.BadZipFile:
        raise OpError(f"{zp} is not a zip file")
    desc = meta.get("descriptor") or {}
    version = args.version or desc.get("version") or meta.get("version_file")
    if not version:
        raise OpError("cannot determine the version: pass --version (no automation.yaml/VERSION inside the zip)")
    version = str(version)
    if args.kind == "automation":
        if not desc:
            raise OpError("an automation package must contain automation.yaml at its root")
        if desc.get("name") != args.name:
            raise OpError(f"package automation.yaml name is {desc.get('name')!r}, not {args.name!r}")
        if str(desc.get("version")) != version:
            raise OpError(f"package automation.yaml version is {desc.get('version')}, not {version}")
    sha = hashlib.sha256(zp.read_bytes()).hexdigest()
    folder = "components" if args.kind == "component" else "automations"
    url = args.url or f"artifacts/{folder}/{args.name}-{version}.zip"
    rec: dict = {"url": url, "sha256": sha}
    requires = {}
    req = desc.get("requires") or {}
    if args.requires_wrapper or req.get("wrapper_core"):
        requires["wrapper_core"] = args.requires_wrapper or req.get("wrapper_core")
    if args.requires_framework or req.get("execution_framework"):
        requires["execution_framework"] = args.requires_framework or req.get("execution_framework")
    if requires and args.kind == "automation":
        rec["requires"] = requires
    res = ops.add_version(m, args.name, version, rec, kind=args.kind)
    if res.manifest_changed and not args.url and not args.no_copy and not args.dry_run:
        dest = Path(args.manifest).parent / url
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.resolve() != zp.resolve():
            shutil.copy2(zp, dest)
    return _finish(args, m, res, reason=args.reason)


def cmd_promote(args) -> int:
    m = store.load_manifest(args.manifest)
    return _finish(args, m, ops.promote(m, args.name, args.to, args.version), reason=args.reason)


def cmd_rollback(args) -> int:
    m = store.load_manifest(args.manifest)
    ops.clean_reason(args.reason, required=True)
    return _finish(args, m, ops.rollback(m, args.name, args.ring, args.to), reason=args.reason)


def cmd_pause(args) -> int:
    m = store.load_manifest(args.manifest)
    return _finish(args, m, ops.pause(m, args.name, args.reason), reason=args.reason)


def cmd_resume(args) -> int:
    m = store.load_manifest(args.manifest)
    return _finish(args, m, ops.resume(m, args.name, args.reason), reason=args.reason)


def cmd_fleet_pause(args) -> int:
    m = store.load_manifest(args.manifest)
    return _finish(args, m, ops.fleet_pause(m, args.reason), reason=args.reason)


def cmd_fleet_resume(args) -> int:
    m = store.load_manifest(args.manifest)
    return _finish(args, m, ops.fleet_resume(m, args.reason), reason=args.reason)


def cmd_decommission(args) -> int:
    m = store.load_manifest(args.manifest)
    res = ops.decommission(m, args.name, args.reason)
    code = _finish(args, m, res, reason=args.reason)
    if not args.dry_run:
        # keep the removed automation's final state for the audit trail
        note = Path(args.manifest).parent / store.HISTORY_DIR / "decommissioned" / f"{args.name}.md"
        store.atomic_write(note, f"# {args.name} decommissioned {store.today()}\n\nReason: {ops.clean_reason(args.reason)}\n\n"
                                 f"Requested by: {_actor() or 'unknown'}\n")
    return code


def cmd_trigger_add(args) -> int:
    m = store.load_manifest(args.manifest)
    path = Path(args.manifest).parent / store.TRIGGERS_FILE
    doc = store.load_triggers(path)
    params = {}
    for kv in args.param or []:
        if "=" not in kv:
            raise OpError(f"--param expects k=v, got {kv!r}")
        k, v = kv.split("=", 1)
        params[k] = v
    res = ops.add_trigger(doc, m, args.automation, args.slot, params, source=args.source, requested_by=_actor())
    return _finish(args, m, res, doc, history=False)


def cmd_new_automation(args) -> int:
    m = store.load_manifest(args.manifest)
    res = ops.new_automation(m, args.name, owner=args.owner, description=args.description, owner_team=args.owner_team or "",
                             systems=args.system or None, data_classification=args.classification, llm_allowed=args.llm_allowed,
                             time=args.time, timezone=args.timezone, tags=args.tag or None, process_owner=args.process_owner or "")
    return _finish(args, m, res, reason=args.reason or f"new automation owned by {args.owner}")


def cmd_validate(args) -> int:
    m = store.load_manifest(args.manifest)
    rep = validate_all(m, allow_placeholders=_allow_placeholders(args), artifacts_dir=args.artifacts, strict=args.strict,
                       schema_only=args.schema_only)
    print(rep.render())
    return 0 if rep.ok else 1


def cmd_compat(args) -> int:
    m = store.load_manifest(args.manifest)
    from wrapper_core import manifest as wcm
    comps = m.get("components") or {}
    rows, bad = [], 0
    for ring in wcm_rings(m):
        w = wcm.target_version(comps.get("wrapper-core", {}), ring)
        f = wcm.target_version(comps.get("execution-framework", {}), ring)
        ok = bool(w and f and wcm.compatible(m, str(w), str(f)))
        bad += not ok
        rows.append((ring, w, f, "ok" if ok else "NOT SUPPORTED"))
        for name, entry in (m.get("automations") or {}).items():
            av = wcm.target_version(entry or {}, ring)
            rec = ((entry or {}).get("versions") or {}).get(str(av)) if av else None
            req = (rec or {}).get("requires") or {}
            good = versions.satisfies(str(w), req.get("wrapper_core")) and versions.satisfies(str(f), req.get("execution_framework"))
            if av and not good:
                bad += 1
                rows.append((ring, f"{name} {av}", f"needs wc {req.get('wrapper_core')} / fw {req.get('execution_framework')}", "NOT SUPPORTED"))
    print(f"{'ring':<8}{'wrapper-core':<22}{'execution-framework':<42}status")
    for r in rows:
        print(f"{r[0]:<8}{str(r[1]):<22}{str(r[2]):<42}{r[3]}")
    return 1 if bad else 0


def wcm_rings(m: dict) -> list[str]:
    return [r for r in RINGS if r in (m.get("rings") or RINGS)]


def cmd_build_portal(args) -> int:
    from .portal import build
    out = build.build_portal(Path(args.manifest), Path(args.out), signals=args.signals, runs=args.runs,
                             config_path=Path(args.config) if args.config else None, governance=args.governance,
                             extra_triggers=args.extra_triggers, artifacts_dir=args.artifacts)
    print(f"portal written to {out['out']} ({len(out['files'])} files)")
    for f in out["files"][:12]:
        print("  " + f)
    return 0


def cmd_ops_action(args) -> int:
    from . import ci_ops
    return ci_ops.main(args)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest", default=None, help="path to manifest.yaml (default ./manifest.yaml)")
    common.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    common.add_argument("--allow-placeholders", action="store_true", help="accept all-zero sha256 placeholders (starter manifest only)")
    common.add_argument("--reason", default="", help="why (shown to users when pausing; kept in history/CHANGELOG.md)")

    p = argparse.ArgumentParser(prog="registry", description="AUTOSTATION registry operator tool")
    p.add_argument("--manifest", dest="manifest_top", default=None)
    p.add_argument("--version", action="version", version=f"registry {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", parents=[common], help="validate manifest (shared rules + schema + governance checks)")
    v.add_argument("--artifacts", default=None, help="directory holding the referenced zips; verifies existence + sha256")
    v.add_argument("--strict", action="store_true", help="treat warnings as errors")
    v.add_argument("--schema-only", action="store_true")
    v.set_defaults(fn=cmd_validate)

    a = sub.add_parser("add-version", parents=[common], help="register a built package; assigns it to ring dev only")
    a.add_argument("kind", choices=["component", "automation"])
    a.add_argument("name")
    a.add_argument("zip")
    a.add_argument("--version")
    a.add_argument("--url", help="absolute URL if the package is hosted elsewhere (no copy is made)")
    a.add_argument("--no-copy", action="store_true")
    a.add_argument("--requires-wrapper")
    a.add_argument("--requires-framework")
    a.set_defaults(fn=cmd_add_version)

    pr = sub.add_parser("promote", parents=[common], help="promote dev -> pilot -> broad")
    pr.add_argument("name")
    pr.add_argument("--to", required=True, choices=["pilot", "broad"])
    pr.add_argument("--version")
    pr.set_defaults(fn=cmd_promote)

    rb = sub.add_parser("rollback", parents=[common], help="roll a ring (and all later rings) back to an older version")
    rb.add_argument("name")
    rb.add_argument("--ring", required=True, choices=list(RINGS))
    rb.add_argument("--to", required=True)
    rb.set_defaults(fn=cmd_rollback)

    for nm, fn, hlp in (("pause", cmd_pause, "kill-switch one automation"), ("resume", cmd_resume, "lift a pause")):
        s = sub.add_parser(nm, parents=[common], help=hlp)
        s.add_argument("name")
        s.set_defaults(fn=fn)
    for nm, fn, hlp in (("fleet-pause", cmd_fleet_pause, "global kill-switch"), ("fleet-resume", cmd_fleet_resume, "lift the global pause")):
        s = sub.add_parser(nm, parents=[common], help=hlp)
        s.set_defaults(fn=fn)

    d = sub.add_parser("decommission", parents=[common], help="remove a paused automation from the manifest")
    d.add_argument("name")
    d.set_defaults(fn=cmd_decommission)

    t = sub.add_parser("trigger-add", parents=[common], help="queue an on-demand / event run (triggers.json)")
    t.add_argument("automation")
    t.add_argument("--slot")
    t.add_argument("--param", action="append", metavar="K=V")
    t.add_argument("--source", default="operator")
    t.set_defaults(fn=cmd_trigger_add)

    n = sub.add_parser("new-automation", parents=[common], help="add a catalog-complete, paused manifest skeleton")
    n.add_argument("name")
    n.add_argument("--owner", required=True, help="owner email (alert routing)")
    n.add_argument("--description", required=True)
    n.add_argument("--owner-team")
    n.add_argument("--process-owner")
    n.add_argument("--system", action="append", help="system touched (repeatable)")
    n.add_argument("--tag", action="append")
    n.add_argument("--classification", default="internal")
    n.add_argument("--llm-allowed", default="internal")
    n.add_argument("--time", default="09:00")
    n.add_argument("--timezone", default="UTC")
    n.set_defaults(fn=cmd_new_automation)

    c = sub.add_parser("compat", parents=[common], help="print the per-ring compatibility matrix; non-zero if unsupported")
    c.set_defaults(fn=cmd_compat)

    b = sub.add_parser("build-portal", parents=[common], help="generate the static portal + admin panel")
    b.add_argument("--out", default="public")
    b.add_argument("--signals", help="signals.json (or dir) from automation-logging")
    b.add_argument("--runs", help="runs.json or a directory of run logs from automation-logging")
    b.add_argument("--config", help="portal.config.json (default: next to manifest)")
    b.add_argument("--governance", help="governance markdown (default docs/GOVERNANCE.md found upwards)")
    b.add_argument("--extra-triggers", help="triggers.json computed by automation-logging (merged by id)")
    b.add_argument("--artifacts", help="copy this artifacts directory into the portal")
    b.set_defaults(fn=cmd_build_portal)

    o = sub.add_parser("ops-action", parents=[common], help="CI entry point: apply ACTION/TARGET/REASON/PARAMS from the environment")
    o.add_argument("--git", action="store_true", help="branch + commit + open MR (or push triggers branch) via the GitLab API")
    o.add_argument("--platform", choices=["auto", "gitlab", "github"], default="auto",
                   help="API flavour for --git (auto: GitHub when running in GitHub Actions)")
    o.add_argument("--result", default="ops_result.json")
    o.set_defaults(fn=cmd_ops_action)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.manifest = args.manifest or args.manifest_top or "manifest.yaml"
    try:
        return args.fn(args)
    except OpError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except store.ValidationFailed as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
