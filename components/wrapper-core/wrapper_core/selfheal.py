"""Self-heal + update engine (SPEC §4, §12).

Runs at the start of every scheduled run. Reads the registry manifest with the read-only token, enforces the
kill-switch, installs anything missing/stale for this machine's ring (sha256-verified, zip-slip-safe, atomic swap),
refreshes dynamic config assets, and collects pending event triggers. A registry outage never blocks a run that is
already installed: the last cached manifest is used (and the run log records it).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import manifest as mf
from . import miniyaml, secrets, versions
from ._version import __version__ as WRAPPER_VERSION
from .paths import Paths, get_logger
from .util import atomic_write_text, read_json, sha256_file, write_json

COMPONENTS = ("wrapper-core", "execution-framework")


class HealError(RuntimeError):
    pass


@dataclass
class HealResult:
    ring: str
    manifest: dict | None = None
    offline: bool = False
    halted: bool = False
    halt_reason: str = ""
    installed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    assets: dict = field(default_factory=dict)
    triggers: list[dict] = field(default_factory=list)
    versions: dict = field(default_factory=dict)


# --- transport ---------------------------------------------------------------------------------------
def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def resolve_location(location: str, base: str) -> str:
    if _is_url(location) or os.path.isabs(location):
        return location
    if _is_url(base):
        return base.rstrip("/") + "/" + location.lstrip("/")
    return str(Path(base) / location)


def _same_origin(a: str, b: str | None) -> bool:
    from urllib.parse import urlsplit
    if not b or not _is_url(b):
        return False
    x, y = urlsplit(a), urlsplit(b)
    return (x.scheme, x.netloc.lower()) == (y.scheme, y.netloc.lower())


def fetch_bytes(location: str, token: str | None = None, retries: int = 3, timeout_s: int = 60,
                trusted_base: str | None = None) -> bytes:
    """Read a registry file from an http(s) URL (GitLab read token) or a local path/UNC share.

    The token is only sent to the registry's own origin (``trusted_base``): a manifest entry pointing at another
    host must never receive it.
    """
    if not _is_url(location):
        try:
            return Path(location.replace("file://", "")).read_bytes()
        except OSError as exc:
            raise HealError(f"cannot read {location}: {exc}") from exc
    headers = {"User-Agent": f"wrapper-core/{WRAPPER_VERSION}"}
    if token and _same_origin(location, trusted_base):
        headers["PRIVATE-TOKEN"] = token
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(location, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - operator-configured registry
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):  # not retryable
                raise HealError(f"HTTP {exc.code} fetching {location}") from exc
            last = exc
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(min(2 ** attempt, 5))
    raise HealError(f"cannot fetch {location}: {last}")


def safe_extract(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if root != target and root not in target.parents:
                raise HealError(f"unsafe path in archive: {info.filename}")
        z.extractall(dest)


# --- installation ------------------------------------------------------------------------------------
def read_installed(paths: Paths) -> dict:
    data = read_json(paths.installed_json, {}) or {}
    data.setdefault("components", {})
    data.setdefault("automations", {})
    return data


def write_installed(paths: Paths, data: dict) -> None:
    write_json(paths.installed_json, data)


def installed_component_version(paths: Paths, name: str) -> str | None:
    """The VERSION marker inside the component dir is authoritative (the launcher swaps wrapper-core outside Python)."""
    marker = (paths.wrapper_core if name == "wrapper-core" else paths.framework) / "VERSION"
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip()
    v = read_installed(paths)["components"].get(name)
    if v:
        return v
    return WRAPPER_VERSION if name == "wrapper-core" else None


def _replace(src: Path, dst: Path, attempts: int = 6) -> None:
    """os.replace with short retries: EDR/AV/indexers briefly lock freshly written directories on Windows VDI."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.25 * (i + 1))


def _swap(staging: Path, target: Path) -> None:
    old = target.with_name(target.name + ".old")
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        _replace(target, old)  # still fails if payload files are genuinely in use -> caller records the error
    try:
        _replace(staging, target)
    except OSError:
        if old.exists() and not target.exists():
            _replace(old, target)  # roll back
        raise
    shutil.rmtree(old, ignore_errors=True)


def download_and_stage(location: str, expected_sha: str, staging: Path, token: str | None, base: str | None = None) -> None:
    data = fetch_bytes(location, token, trusted_base=base)
    with tempfile.TemporaryDirectory(prefix="amx-dl-") as tmp:
        zp = Path(tmp) / "pkg.zip"
        zp.write_bytes(data)
        actual = sha256_file(zp)
        if actual.lower() != expected_sha.lower():
            raise HealError(f"sha256 mismatch for {location}: expected {expected_sha[:12]}…, got {actual[:12]}…")
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        safe_extract(zp, staging)


def apply_pending(paths: Paths) -> list[str]:
    """Swap in a staged wrapper-core (``wrapper-core.pending``). Called by the launcher *before* Python starts
    the next run (the running package cannot replace itself on Windows)."""
    applied = []
    pending = paths.root / "wrapper-core.pending"
    if pending.exists():
        ver = (pending / "VERSION").read_text(encoding="utf-8").strip() if (pending / "VERSION").exists() else "unknown"
        _swap(pending, paths.wrapper_core)
        data = read_installed(paths)
        data["components"]["wrapper-core"] = ver
        write_installed(paths, data)
        applied.append(f"wrapper-core {ver}")
    return applied


def install_component(paths: Paths, name: str, version: str, rec: dict, base: str, token: str | None) -> str:
    target = {"wrapper-core": paths.wrapper_core, "execution-framework": paths.framework}[name]
    loc = resolve_location(rec["url"], base)
    if name == "wrapper-core":
        staging = paths.root / "wrapper-core.pending"
        download_and_stage(loc, rec["sha256"], staging, token, base)
        atomic_write_text(staging / "VERSION", version + "\n")
        return f"{name} staged {version} (applied at next launch)"
    staging = target.with_name(target.name + ".staging")
    download_and_stage(loc, rec["sha256"], staging, token, base)
    atomic_write_text(staging / "VERSION", version + "\n")
    _swap(staging, target)
    data = read_installed(paths)
    data["components"][name] = version
    write_installed(paths, data)
    return f"{name} -> {version}"


def install_automation(paths: Paths, name: str, version: str, rec: dict, base: str, token: str | None) -> str:
    target = paths.automation_dir(name)
    staging = target.with_name(name + ".staging")
    download_and_stage(resolve_location(rec["url"], base), rec["sha256"], staging, token, base)
    atomic_write_text(staging / "VERSION", version + "\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    _swap(staging, target)
    data = read_installed(paths)
    data["automations"][name] = version
    write_installed(paths, data)
    return f"{name} -> {version}"


def _needs_update(cur: str | None, target: str) -> bool:
    if cur is None:
        return True
    if str(cur) == str(target):
        return False
    try:
        return versions.compare(str(cur), str(target)) != 0
    except ValueError:  # non-semver (dev builds): any difference means update
        return True


def installed_automation_version(paths: Paths, name: str) -> str | None:
    marker = paths.automation_dir(name) / "VERSION"
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip()
    return read_installed(paths)["automations"].get(name)


PAYLOAD_DEFAULT_KEYS = ("schedule", "error_handling", "data_classification", "llm_allowed_classification", "notify_on_complete")


def payload_defaults(paths: Paths, name: str) -> dict:
    """Operational defaults a payload ships in its own ``automation.yaml`` (schedule, error_handling, ...). They apply only
    when the registry manifest has nothing to say (offline / standalone installs); a manifest entry always wins."""
    f = paths.automation_dir(name) / "automation.yaml"
    try:
        d = miniyaml.load(f) if f.exists() else {}
    except (OSError, miniyaml.YamlError):
        return {}
    return {k: d[k] for k in PAYLOAD_DEFAULT_KEYS if isinstance(d, dict) and k in d}


def install_bundled(paths: Paths, name: str) -> str | None:
    """Install the payload bundled inside the installer (``<home>/bundled/<name>.zip`` + ``<name>.json``) when the
    automation is not present yet. The installer is the trust root, so the sha256 in the sidecar (written at build time,
    inside the signed .exe) is checked, but no registry is needed: a first install works offline / before a registry
    exists. Self-heal later moves the payload to the ring's version when the registry is reachable."""
    zp, meta_p = paths.root / "bundled" / f"{name}.zip", paths.root / "bundled" / f"{name}.json"
    if paths.automation_dir(name).exists() or not zp.is_file() or not meta_p.is_file():
        return None
    meta = read_json(meta_p, {}) or {}
    if not meta.get("sha256") or sha256_file(zp).lower() != str(meta["sha256"]).lower():
        raise HealError(f"bundled payload for {name} failed its sha256 check")
    target = paths.automation_dir(name)
    staging = target.with_name(name + ".staging")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    safe_extract(zp, staging)
    atomic_write_text(staging / "VERSION", str(meta.get("version", "bundled")) + "\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    _swap(staging, target)
    data = read_installed(paths)
    data["automations"][name] = str(meta.get("version", "bundled"))
    write_installed(paths, data)
    return f"{name} -> {meta.get('version', 'bundled')} (bundled with the installer)"


# --- the pass ----------------------------------------------------------------------------------------
def fetch_manifest(paths: Paths, base: str, token: str | None) -> tuple[dict, bool]:
    """Return (manifest, offline). Falls back to the cached copy when the registry is unreachable."""
    log = get_logger(paths)
    try:
        text = fetch_bytes(resolve_location("manifest.yaml", base), token, trusted_base=base).decode("utf-8")
        m = mf.parse(text)
        atomic_write_text(paths.manifest_cache, text)
        return m, False
    except (HealError, mf.ManifestError) as exc:
        log.warning("manifest fetch failed: %s", exc)
        if paths.manifest_cache.exists():
            return mf.parse(paths.manifest_cache.read_text(encoding="utf-8")), True
        raise HealError(f"registry unreachable and no cached manifest: {exc}") from exc


def run(paths: Paths, cfg: dict, automation: str | None) -> HealResult:
    """The self-heal pass for one automation (or just the shared components when `automation` is None)."""
    ring = cfg["ring"]
    res = HealResult(ring=ring)
    log = get_logger(paths)
    if automation:
        try:
            note = install_bundled(paths, automation)
            if note:
                res.installed.append(note)
        except (HealError, OSError, zipfile.BadZipFile) as exc:
            log.error("bundled install of %s failed: %s", automation, exc)
            res.errors.append(f"bundled payload: {exc}")
    base = cfg.get("registry_url")
    if not base:
        res.errors.append("registry_url not configured")
        res.versions = _versions_snapshot(paths)
        return res
    token = secrets.get_registry_token(paths)
    try:
        m, offline = fetch_manifest(paths, base, token)
    except HealError as exc:
        res.errors.append(str(exc))
        res.versions = _versions_snapshot(paths)
        return res
    res.manifest, res.offline = m, offline
    if offline:
        res.errors.append("registry unreachable - using cached manifest")

    # kill-switch comes first: nothing else matters if the fleet/automation is paused
    if automation:
        halt, reason = mf.paused(m, automation)
        if halt:
            res.halted, res.halt_reason = True, reason
            res.versions = _versions_snapshot(paths)
            return res

    res.assets = mf.assets_for(m, ring)
    try:
        write_json(paths.assets_json, res.assets)
    except OSError as exc:
        res.errors.append(f"cannot write assets.json: {exc}")

    if offline:
        res.versions = _versions_snapshot(paths)
        return res

    # components
    for cname in COMPONENTS:
        entry = (m.get("components") or {}).get(cname)
        if not entry:
            continue
        tv = mf.target_version(entry, ring)
        cur = installed_component_version(paths, cname)
        if tv and _needs_update(cur, str(tv)):
            try:
                res.installed.append(install_component(paths, cname, str(tv), mf.version_record(entry, str(tv)), base, token))
            except (HealError, mf.ManifestError, OSError, zipfile.BadZipFile) as exc:
                log.error("component %s install failed: %s", cname, exc)
                res.errors.append(f"{cname} {tv}: {exc}")

    # the automation payload
    if automation:
        entry = mf.automation(m, automation) or {}
        tv = mf.target_version(entry, ring)
        cur = installed_automation_version(paths, automation)
        have_dir = paths.automation_dir(automation).exists()
        if tv and (not have_dir or _needs_update(cur, str(tv))):
            try:
                res.installed.append(install_automation(paths, automation, str(tv), mf.version_record(entry, str(tv)), base, token))
            except (HealError, mf.ManifestError, OSError, zipfile.BadZipFile) as exc:
                log.error("automation %s install failed: %s", automation, exc)
                res.errors.append(f"{automation} {tv}: {exc}")
        elif not tv:
            res.errors.append(f"{automation}: no version assigned for ring {ring}")

        # event triggers addressed to this automation
        if _wants_triggers(entry):
            res.triggers = pending_triggers(paths, base, token, automation)

    res.versions = _versions_snapshot(paths)
    return res


def _wants_triggers(entry: dict) -> bool:
    slots = (entry.get("schedule") or {}).get("slots") or []
    return any(s.get("type") == "poll" for s in slots) or bool(entry.get("triggers"))


def pending_triggers(paths: Paths, base: str, token: str | None, automation: str) -> list[dict]:
    try:
        doc = json.loads(fetch_bytes(resolve_location("triggers.json", base), token, retries=1, trusted_base=base).decode("utf-8"))
    except (HealError, ValueError):
        return []
    consumed = set(read_json(paths.consumed_triggers, []) or [])
    items = doc.get("triggers", doc) if isinstance(doc, (dict, list)) else []
    return [t for t in items if isinstance(t, dict) and t.get("automation") == automation and t.get("id") not in consumed]


def consume_trigger(paths: Paths, trigger_id: str) -> None:
    consumed = list(read_json(paths.consumed_triggers, []) or [])
    if trigger_id not in consumed:
        consumed.append(trigger_id)
    write_json(paths.consumed_triggers, consumed[-500:])


def _automation_dirs(paths: Paths) -> set:
    try:
        return {d.name for d in paths.automations.iterdir() if d.is_dir() and not d.name.endswith((".staging", ".old"))}
    except OSError:
        return set()


def _versions_snapshot(paths: Paths) -> dict:
    inst = read_installed(paths)
    return {
        "wrapper-core": installed_component_version(paths, "wrapper-core"),
        "execution-framework": installed_component_version(paths, "execution-framework"),
        "automations": {n: installed_automation_version(paths, n) or inst["automations"].get(n)
                        for n in sorted(set(inst["automations"]) | _automation_dirs(paths))},
    }
