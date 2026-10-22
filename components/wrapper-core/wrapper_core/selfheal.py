"""Self-heal + update engine (SPEC §4, §12).

Runs at the start of every scheduled run. Reads the registry manifest with the read-only token, enforces the
kill-switch, installs anything missing/stale for this machine's ring (sha256-verified, zip-slip-safe, atomic swap),
refreshes dynamic config assets, and collects pending event triggers. A registry outage never blocks a run that is
already installed: the last cached manifest is used (and the run log records it).
"""
from __future__ import annotations

import hashlib
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
from . import miniyaml, secrets, trust, versions
from ._version import __version__ as WRAPPER_VERSION
from .paths import Paths, check_name, get_logger, platform_config
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
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 600 * 1024 * 1024
MAX_EXTRACT_BYTES = 500 * 1024 * 1024  # total uncompressed size of one package
MAX_EXTRACT_MEMBERS = 20_000
_AUTH_HEADERS = ("Private-token", "Authorization", "Cookie")


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


def _is_loopback(host: str | None) -> bool:
    return (host or "").lower() in ("localhost", "127.0.0.1", "::1", "[::1]")


def check_url(url: str, allow_http_origin: str | None = None) -> str:
    """URL policy for everything self-heal downloads: http(s) only, no embedded credentials, and plain http only for
    loopback or for the operator-configured registry origin itself. Returns the URL or raises HealError."""
    from urllib.parse import urlsplit
    try:
        sp = urlsplit(url)
        host, _ = sp.hostname, sp.port
    except ValueError as exc:
        raise HealError(f"malformed URL {url[:80]!r}") from exc
    if sp.scheme not in ("http", "https") or not host:
        raise HealError(f"refusing URL with scheme {sp.scheme or '(none)'!r}: {url[:80]}")
    if sp.username or sp.password:
        raise HealError("refusing URL with embedded credentials")
    if sp.scheme == "http" and not _is_loopback(host) and not (allow_http_origin and _same_origin(url, allow_http_origin)):
        raise HealError(f"refusing non-https URL {url[:80]}")
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to URLs that pass ``check_url``, and never carry credentials to a different origin."""

    def __init__(self, allow_http_origin: str | None = None):
        self.allow_http_origin = allow_http_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self.allow_http_origin)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _same_origin(newurl, req.full_url):
            for h in _AUTH_HEADERS:
                new.headers.pop(h, None)
                new.unredirected_hdrs.pop(h, None)
                new.headers.pop(h.lower(), None)
        return new


def _open(req: urllib.request.Request, timeout: int, allow_http_origin: str | None = None):
    return urllib.request.build_opener(_SafeRedirect(allow_http_origin)).open(req, timeout=timeout)


def _read_capped(resp, max_bytes: int) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = resp.read(1 << 20)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise HealError(f"response larger than {max_bytes} bytes")
        chunks.append(chunk)


def fetch_bytes(location: str, token: str | None = None, retries: int = 3, timeout_s: int = 60,
                trusted_base: str | None = None, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """Read a registry file from an http(s) URL (GitLab read token) or a local path/UNC share.

    The token is only sent to the registry's own origin (``trusted_base``), also across redirects: a manifest entry
    pointing at another host must never receive it. Non-https URLs and non-http(s) schemes are refused (``check_url``).
    """
    if not _is_url(location):
        if "://" in location and not location.lower().startswith("file://"):
            raise HealError(f"refusing URL with unsupported scheme: {location[:80]}")
        try:
            path = Path(location.replace("file://", ""))
            if path.stat().st_size > max_bytes:
                raise HealError(f"{location} larger than {max_bytes} bytes")
            return path.read_bytes()
        except OSError as exc:
            raise HealError(f"cannot read {location}: {exc}") from exc
    http_origin = trusted_base if trusted_base and trusted_base.startswith("http://") else None
    check_url(location, http_origin)
    headers = {"User-Agent": f"wrapper-core/{WRAPPER_VERSION}"}
    if token and _same_origin(location, trusted_base):
        headers["PRIVATE-TOKEN"] = token
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(location, headers=headers)
            with _open(req, timeout_s, http_origin) as resp:  # noqa: S310 - scheme/host policy enforced by check_url
                return _read_capped(resp, max_bytes)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308, 401, 403, 404):  # redirects land here only when refused; not retryable
                raise HealError(f"HTTP {exc.code} fetching {location}") from exc
            last = exc
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(min(2 ** attempt, 5))
    raise HealError(f"cannot fetch {location}: {last}")


def _member_is_unsafe(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name or "\x00" in name:
        return True
    if any(part == ".." for part in name.split("/")):
        return True
    mode = (info.external_attr >> 16) & 0xFFFF
    return (mode & 0o170000) == 0o120000  # symlink


def safe_extract(zip_path: Path, dest: Path, max_total: int = MAX_EXTRACT_BYTES, max_members: int = MAX_EXTRACT_MEMBERS) -> None:
    """Extract a package: zip-slip, symlink and drive/ADS names rejected; member count and the *actual* decompressed byte
    count capped (the declared sizes in the zip header are attacker-controlled, so they are not trusted). On any failure
    the partial extraction is removed."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = z.infolist()
            if len(infos) > max_members:
                raise HealError(f"archive has more than {max_members} members")
            for info in infos:
                target = (dest / info.filename).resolve()
                if _member_is_unsafe(info) or (root != target and root not in target.parents):
                    raise HealError(f"unsafe path in archive: {info.filename}")
            if sum(i.file_size for i in infos) > max_total * 8:  # cheap pre-check; the streaming count below is authoritative
                raise HealError("archive declares an implausible uncompressed size")
            total = 0
            for info in infos:
                target = dest / info.filename
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_total:
                            raise HealError(f"archive expands beyond {max_total} bytes (zip bomb?)")
                        out.write(chunk)
    except (HealError, zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        shutil.rmtree(dest, ignore_errors=True)
        raise


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
    if base and _is_url(base) and not _is_url(location):
        raise HealError(f"manifest points a URL registry at a local path: {location[:80]}")  # a manifest must not read local files
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
class _Untrusted(Exception):
    """The fetched manifest failed signature policy (reason in ``str(exc)``)."""


def _read_cached_manifest(paths: Paths, keys: list) -> tuple[bytes, dict]:
    """The cached manifest bytes + parsed manifest, re-verified against the pinned keys when any are configured
    (a tampered cache, or one saved before a key was pinned, is ignored: fail closed)."""
    raw = paths.manifest_cache.read_bytes()
    if keys:
        try:
            trust.verify_manifest(raw, paths.manifest_sig_cache.read_bytes(), keys)
        except OSError as exc:
            raise HealError("cached manifest has no signature") from exc
        except trust.TrustError as exc:
            raise HealError(f"cached manifest signature invalid: {exc}") from exc
    try:
        return raw, mf.parse(raw.decode("utf-8"))
    except ValueError as exc:  # ManifestError and UnicodeDecodeError
        raise HealError(f"cached manifest unusable: {exc}") from exc


def _check_rollback(paths: Paths, signed_at: str, sha: str) -> None:
    try:
        last = paths.manifest_signed_at.read_text(encoding="utf-8").strip()
    except OSError:
        return
    try:
        newer = trust.parse_iso(signed_at) > trust.parse_iso(last)
        older = trust.parse_iso(signed_at) < trust.parse_iso(last)
    except trust.TrustError:
        return  # corrupt high-water mark: do not lock the fleet out; the signature itself still has to verify
    if older:
        raise _Untrusted(f"rollback refused: signed_at {signed_at} is older than the newest accepted {last}")
    if not newer:  # same timestamp: only the exact manifest we already accepted
        try:
            cached = json.loads(paths.manifest_sig_cache.read_text(encoding="utf-8"))
            if str(cached.get("manifest_sha256", "")).lower() != sha.lower():
                raise _Untrusted(f"a different manifest carries the already-accepted signed_at {signed_at}")
        except (OSError, ValueError, AttributeError):
            pass


def _check_seq(paths: Paths, v: "trust.Verified") -> None:
    """Reject a validly signed manifest whose ``seq`` is LOWER than the highest accepted for the same key (replay of an older
    release). Equal is fine (re-runs, re-fetches). No stored mark, a corrupt mark, or an unsequenced (legacy) document passes:
    signed_at still guards those."""
    if v.seq is None:
        return
    marks = read_json(paths.manifest_seq, {})
    last = marks.get(v.key_id) if isinstance(marks, dict) else None
    if isinstance(last, int) and not isinstance(last, bool) and v.seq < last:
        raise _Untrusted(f"rollback refused: seq {v.seq} is lower than the newest accepted {last} for key {v.key_id}")


def _remember_seq(paths: Paths, v: "trust.Verified") -> None:
    if v.seq is None:
        return
    marks = read_json(paths.manifest_seq, {})
    if not isinstance(marks, dict):
        marks = {}
    last = marks.get(v.key_id)
    if not (isinstance(last, int) and not isinstance(last, bool)) or v.seq > last:
        marks[v.key_id] = v.seq
        write_json(paths.manifest_seq, marks)


def fetch_manifest(paths: Paths, base: str, token: str | None, notes: list | None = None, cfg: dict | None = None) -> tuple[dict, bool]:
    """Return (manifest, offline). Falls back to the cached copy when the registry is unreachable or its manifest is
    rejected (bad YAML, bad/missing signature, rollback). With a pinned key (``trust``) an unverified manifest is NEVER
    used and never replaces the cache; rejection reasons are appended to ``notes``."""
    log = get_logger(paths)
    keys = trust.configured_keys(cfg)
    try:
        raw = fetch_bytes(resolve_location("manifest.yaml", base), token, trusted_base=base, max_bytes=MAX_MANIFEST_BYTES)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise mf.ManifestError("manifest is not UTF-8") from exc
        m = mf.parse(text)
        sig_raw = None
        signed_at = ""
        verified = None
        if keys:
            try:
                sig_raw = fetch_bytes(resolve_location("manifest.yaml.sig", base), token, retries=1, trusted_base=base,
                                      max_bytes=64 * 1024)
            except HealError as exc:
                raise _Untrusted(f"signature file unavailable ({exc})") from exc
            try:
                verified = trust.verify_manifest_doc(raw, sig_raw, keys)
                signed_at = verified.signed_at
            except trust.TrustError as exc:
                raise _Untrusted(str(exc)) from exc
            _check_rollback(paths, signed_at, hashlib.sha256(raw).hexdigest())
            _check_seq(paths, verified)
        else:
            log.warning("registry manifest is unsigned and no registry key is pinned (set registry_pubkeys in platform.json)")
        try:
            if sig_raw is not None:
                atomic_write_text(paths.manifest_sig_cache, sig_raw.decode("utf-8"))
            else:
                paths.manifest_sig_cache.unlink(missing_ok=True)
            atomic_write_text(paths.manifest_cache, text)
            if keys:
                atomic_write_text(paths.manifest_signed_at, signed_at + "\n")
                if verified is not None:
                    _remember_seq(paths, verified)
        except OSError as exc:
            log.warning("cannot cache manifest: %s", exc)
        return m, False
    except _Untrusted as exc:
        log.error("manifest signature invalid: %s", exc)
        if notes is not None:
            notes.append(f"manifest signature invalid: {exc}")
        failure: Exception = exc
    except (HealError, mf.ManifestError) as exc:
        log.warning("manifest fetch failed: %s", exc)
        failure = exc
    if paths.manifest_cache.exists():
        try:
            return _read_cached_manifest(paths, keys)[1], True
        except HealError as exc:
            log.error("cached manifest rejected: %s", exc)
            if notes is not None:
                notes.append(str(exc))
            failure = exc
    raise HealError(f"registry unreachable or untrusted and no usable cached manifest: {failure}") from failure


def run(paths: Paths, cfg: dict, automation: str | None) -> HealResult:
    """The self-heal pass for one automation (or just the shared components when `automation` is None).
    Never raises: a hostile/garbled registry degrades to an error note and the installed payload keeps running."""
    res = HealResult(ring=str(cfg.get("ring", "")))
    try:
        return _run(paths, cfg, automation, res)
    except Exception as exc:  # noqa: BLE001
        get_logger(paths).error("self-heal crashed: %s: %s", type(exc).__name__, exc)
        res.errors.append(f"self-heal crashed: {type(exc).__name__}: {str(exc)[:200]}")
        try:
            res.versions = _versions_snapshot(paths)
        except Exception:  # noqa: BLE001
            res.versions = {}
        return res


def _run(paths: Paths, cfg: dict, automation: str | None, res: HealResult) -> HealResult:
    ring = cfg["ring"]
    log = get_logger(paths)
    if automation:
        try:
            check_name(automation)
        except ValueError as exc:
            res.errors.append(str(exc))
            res.versions = _versions_snapshot(paths)
            return res
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
        m, offline = fetch_manifest(paths, base, token, notes=res.errors, cfg=cfg)
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


def _signed_extra_sha(paths: Paths, name: str, keys: list) -> str | None:
    """The sha256 the registry operator signed for ``name`` (from the cached, re-verified manifest + signature document)."""
    try:
        v = trust.verify_manifest_doc(paths.manifest_cache.read_bytes(), paths.manifest_sig_cache.read_bytes(), keys)
    except (OSError, trust.TrustError):
        return None
    return v.extras.get(name)


def pending_triggers(paths: Paths, base: str, token: str | None, automation: str) -> list[dict]:
    log = get_logger(paths)
    try:
        raw = fetch_bytes(resolve_location("triggers.json", base), token, retries=1, trusted_base=base)
    except HealError:
        return []
    keys = trust.configured_keys(platform_config(paths))
    if keys:
        # a key is pinned: triggers queue runs on endpoints, so they must be the exact bytes the operator signed (fail closed)
        want = _signed_extra_sha(paths, "triggers.json", keys)
        if want is None:
            log.warning("triggers.json ignored: the signed registry manifest does not vouch for it")
            return []
        if hashlib.sha256(raw).hexdigest() != want:
            log.warning("triggers.json ignored: it does not match the digest the registry operator signed (tampered or stale)")
            return []
    else:
        log.warning("triggers.json is unsigned and no registry key is pinned (set registry_pubkeys in platform.json)")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError:
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
