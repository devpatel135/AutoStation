"""Proactive remediation: classify a failure, apply a SAFE, bounded, reversible fix, retry at most once, record everything.

    failure / pre-flight / login pass  ->  diagnose()  ->  playbooks (playbooks.py)  ->  events + audit log + debug bundle

What it will do automatically (risk ``safe``): switch an automation to native mode when managed Edge forbids DevTools, release a
browser profile left locked by a crashed Edge (only processes whose command line names THIS automation's profile folder), quarantine
a corrupt browser profile (moved, never deleted), re-fetch a damaged component from the verified registry, prune this tool's own old
data, re-register missing scheduled tasks, and ask a person - once, deduplicated - when a sign-in is needed.

What it never does: change a machine policy, touch ``secrets/``, delete outside the install folder, run elevated, kill a process it
cannot tie to the automation's own profile, loop (budget per playbook per 24 h + cool-down), or hide the original error.

Switch it off with ``"remediation": false`` in config/platform.json or ``AUTOMAGIX_NO_REMEDIATION=1``. ``--dry-run`` only reports.
Every attempt is a ``remediation_attempt`` event in the run record and a line in ``logs/remediation.jsonl`` (text is scrubbed of secrets).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import runctx
from .paths import Paths, check_name, get_logger, platform_config
from .util import iso, parse_iso, read_json, short_hash, utcnow, write_json

SAFE, MODERATE, MANUAL = "safe", "moderate", "manual"
OUTCOMES = ("fixed", "not_fixed", "skipped", "escalated", "recommended")
ACTIONS_PER_DAY_DEFAULT = 3
COOLDOWN_DEFAULT_S = 60
OVERRIDE_TTL_S = 30 * 86400
ENV_PASS_INTERVAL_S = 900
AUDIT_MAX_BYTES = 1_000_000
ALLOWED_OVERRIDES = {"mode": ("native",)}

# ---------------------------------------------------------------------------------------------------- scrubbing
_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED-KEY]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED-TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b"), "[REDACTED-JWT]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED-KEY]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED-TOKEN]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|set-cookie|cookie)\s*[:=]\s*[^\r\n]+"), r"\1: [REDACTED]"),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"(?i)((?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|client[_-]?secret|access[_-]?key|credential)s?[\"']?\s*[:=]\s*[\"']?)"
                r"([^\s\"',;&]{3,})"), r"\1[REDACTED]"),
]
_TB_LINE = re.compile(r"(?i)\bline \d+")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+\b")
_URL_SECRET_PARAM = re.compile(r"(?i)([?&](?:sig|signature|sv|se|sp|code|key|token|access_token|x-amz-signature|x-goog-signature)=)[^&\s\"'#]+")
_PATHLIKE = re.compile(r"^(?:/[A-Za-z0-9._-]{1,60}){2,}/?$")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{40,}")
_HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")
_USER_PATH = re.compile(r"(?i)([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s\"'<>|]+")
_HOME_PATH = re.compile(r"(/(?:home|Users)/)(?!<user>)[^/\s\"'<>|:]+")
_SECRET_ENV_SUFFIX =("_TOKEN", "_SECRET", "_KEY", "_PASSWORD", "_PASSWD")
_SECRET_ENV_PREFIX = ("AUTOSTATION_", "AUTOMAGIX_LOGGING_", "AUTOMAGIX_REGISTRY_TOKEN", "AUTOMAGIX_CRED_", "AMX_CRED_")


def _long_repl(m: "re.Match") -> str:
    s = m.group(0)
    if _HEX_ONLY.match(s) and len(s) in (40, 64):  # git / sha-256 digests are evidence, not secrets
        return s
    if _PATHLIKE.match(s):  # /usr/lib/python3/dist-packages style paths are evidence, not secrets (base64 has + and = and no leading slash)
        return s
    return "[REDACTED-LONG]"


def _env_secret_values() -> List[str]:
    out = []
    for k, v in os.environ.items():
        u = k.upper()
        if len(v) >= 8 and (u.endswith(_SECRET_ENV_SUFFIX) or u.startswith(_SECRET_ENV_PREFIX)):
            out.append(v)
    return sorted(set(out), key=len, reverse=True)


def scrub_text(text: Any, limit: Optional[int] = None) -> str:
    """Remove credentials, tokens, long opaque strings, URL passwords, known secret env values and user names from free text."""
    s = str(text if text is not None else "")
    for value in _env_secret_values():
        s = s.replace(value, "[REDACTED]")
    for rx, rep in _PATTERNS:
        s = rx.sub(rep, s)
    s = _URL_SECRET_PARAM.sub(r"\1[REDACTED]", s)
    s = _EMAIL.sub("[EMAIL]", s)
    s = _LONG_TOKEN.sub(_long_repl, s)
    s = _USER_PATH.sub(r"\1<user>", s)
    s = _HOME_PATH.sub(r"\1<user>", s)
    if limit is not None and len(s) > limit:
        s = s[:limit] + "...[truncated]"
    return s


# ---------------------------------------------------------------------------------------------------- diagnosis
@dataclass
class Diagnosis:
    category: str
    confidence: float = 0.5
    evidence: List[str] = field(default_factory=list)
    signature: str = ""
    automation: Optional[str] = None
    hint: str = ""

    def to_dict(self) -> dict:
        return {"category": self.category, "confidence": round(self.confidence, 2), "evidence": list(self.evidence),
                "signature": self.signature, "automation": self.automation, **({"hint": self.hint} if self.hint else {})}


def _norm(p: Any) -> str:
    return str(p).replace("/", "\\").lower().rstrip("\\")


def _blob(error: dict) -> str:
    parts = [str(error.get("message") or ""), str(error.get("type") or "")]
    tb = error.get("traceback_tail")
    tbtext = "\n".join(str(x) for x in tb) if isinstance(tb, (list, tuple)) else str(tb or "")
    parts.append(_TB_LINE.sub("line N", tbtext))  # "File x.py, line 401" is a source position, never an HTTP status
    parts.append(str(error.get("stderr_tail") or ""))
    parts.append(str(error.get("result_state") or ""))
    return "\n".join(parts)[-30000:]


def signature(category: str, error: dict) -> str:
    msg = re.sub(r"\d+", "#", str(error.get("message") or "")[:200])
    msg = _USER_PATH.sub(r"\1<user>", msg)
    return category + ":" + short_hash(category + "|" + str(error.get("type") or "") + "|" + msg, 12)


_RULES: List[Tuple[str, float, re.Pattern]] = [
    ("DEVTOOLS_BLOCKED", 0.98, re.compile(r"(?i)remote debugging is (?:disallowed|disabled|blocked)|disallowed by the system admin|RemoteDebuggingAllowed")),
    ("STALE_BROWSER_PROFILE_LOCK", 0.9, re.compile(r"(?i)SingletonLock|ProcessSingleton|profile (?:appears to be |is )?(?:already )?in use|user data directory is already in use|"
                                                   r"opening in existing browser session|Failed to create .{0,40}lock")),
    ("CORRUPT_BROWSER_PROFILE", 0.85, re.compile(r"(?i)profile[^\n]{0,60}(?:corrupt|could not be opened|cannot be read)|Preferences[^\n]{0,40}(?:corrupt|unreadable|malformed)|"
                                                 r"Failed to read (?:the )?profile|your profile can(?:'|no)t be used")),
    ("PYTHON_RUNTIME_BROKEN", 0.9, re.compile(r"(?i)python\d*\.dll|Failed to load Python|No module named '?encodings|Fatal Python error|api-ms-win-crt|vcruntime\d*\.dll")),
    ("NODE_MISSING", 0.9, re.compile(r"(?i)spawn [^\n]{0,80}node(?:\.exe)? ENOENT|'node(?:\.exe)?' is not recognized|node\.exe[^\n]{0,80}(?:not found|cannot find|no such file)")),
    ("EDGE_MISSING", 0.9, re.compile(r"(?i)(?:msedge|microsoft edge)[^\n]{0,120}(?:is not found|not found|doesn't exist|does not exist|not installed)|Executable doesn't exist")),
    ("GIT_MISSING", 0.9, re.compile(r"(?i)git not found|'git(?:\.exe)?' is not recognized|git executable (?:was )?not found")),
    ("DISK_LOW", 0.95, re.compile(r"(?i)no space left on device|(?-i:\bENOSPC\b)|not enough space on the disk|there is not enough space|disk (?:is )?full|\[Errno 28\]|WinError 112")),
    ("COMPONENT_CORRUPT", 0.85, re.compile(r"(?i)BadZipFile|File is not a zip file|unexpected end of (?:archive|data)|(?:sha-?256|hash|checksum) mismatch|corrupt(?:ed)? (?:package|component|archive)")),
    ("DEPENDENCY_VERSION_MISMATCH", 0.9, re.compile(r"(?i)does not satisfy|compatibility matrix|IncompatibleVersions")),
    ("SIGNIN_REQUIRED", 0.9, re.compile(r"(?i)manual_step_required|sign-?in (?:is )?required|interactive (?:login|sign-?in)|AADSTS\d{4,6}|needs a person|"
                                        r"not signed in|please sign in|mfa (?:is )?required|login\.microsoftonline\.com/[^\s]*(?:error|login)")),
    ("AUTH_EXPIRED", 0.75, re.compile(r"(?i)(?:\bHTTP[/ ]?[\d.]* |\bstatus(?: code)?[ :=]+|\berror[ :=]+)401\b|\b401 unauthori[sz]ed|token (?:has )?expired|session (?:has )?expired|credentials? (?:have |has )?expired|authentication failed|unauthori[sz]ed")),
    ("TLS_FAILURE", 0.9, re.compile(r"(?i)CERTIFICATE_VERIFY_FAILED|ERR_CERT_|SSL: ?[A-Z_]+|SSLError|certificate (?:is )?(?:not yet valid|has expired|verify failed)|"
                                    r"unable to get local issuer certificate")),
    ("DNS_FAILURE", 0.9, re.compile(r"(?i)getaddrinfo failed|name or service not known|ERR_NAME_NOT_RESOLVED|(?-i:\bENOTFOUND\b)|temporary failure in name resolution|"
                                    r"no address associated with hostname")),
    ("PROXY_FAILURE", 0.85, re.compile(r"(?i)ProxyError|ERR_PROXY_CONNECTION_FAILED|ERR_TUNNEL_CONNECTION_FAILED|(?:\bHTTP[/ ]?[\d.]* |\bstatus(?: code)?[ :=]+|\berror[ :=]+)407\b|\b407 proxy|proxy authentication required|tunnel connection failed")),
    ("NETWORK_UNREACHABLE", 0.7, re.compile(r"(?i)registry unreachable|connection (?:reset|refused|aborted)|network is unreachable|no route to host|"
                                            r"ERR_INTERNET_DISCONNECTED|ERR_CONNECTION_(?:RESET|REFUSED|TIMED_OUT)|\btime(?:d ?| ?)out\b|(?-i:\bECONNRESET\b|\bETIMEDOUT\b)")),
]
_MISSING_RX = re.compile(r"(?i)ENOENT|no such file or directory|cannot find (?:the )?(?:path|file)|FileNotFoundError|Cannot find module|is not installed|"
                         r"The system cannot find the (?:file|path) specified")
_OURS = re.compile(r"(?i)execution-framework|wrapper[-_]core|automagix|node_modules[\\/]playwright|automation payload")


def _missing_file(blob: str, root: Optional[str]) -> Optional[str]:
    r = _norm(root) if root else None
    for line in blob.splitlines():
        if not _MISSING_RX.search(line):
            continue
        if (r and r in _norm(line)) or _OURS.search(line):
            return line.strip()[:200]
    return None


def diagnose(error: Optional[dict], env: Optional[dict] = None) -> Diagnosis:
    """Deterministic classification of a failure. ``error`` keys (all optional): message, type, category, traceback_tail, stderr_tail,
    exit_code, status, result_state. ``env`` keys: root (install folder), automation, runtime. Never raises, never calls the network."""
    error = dict(error or {})
    env = dict(env or {})
    blob = _blob(error)
    runtime = str(env.get("runtime") or "")
    found: Optional[Diagnosis] = None
    for cat, conf, rx in _RULES:
        m = rx.search(blob)
        if m:
            found = Diagnosis(cat, conf, [scrub_text(m.group(0), 160)])
            break
    if found is None:
        line = _missing_file(blob, env.get("root"))
        if line:
            cat = "NODE_MISSING" if re.search(r"(?i)node(\.exe)?\b", line) and runtime == "node" and "node_modules" not in line.lower() else "MISSING_FILE"
            found = Diagnosis(cat, 0.8, [scrub_text(line, 160)])
    if found is None and runtime == "node" and re.search(r"(?i)WinError 2|cannot find the file specified|ENOENT", blob):
        found = Diagnosis("NODE_MISSING", 0.6, ["launching the node runtime failed: the file was not found"])
    if found is None:
        cat = str(error.get("category") or "")
        found = Diagnosis("UNKNOWN", 0.2, [scrub_text(str(error.get("message") or cat or "no error text"), 160)])
    found.automation = env.get("automation")
    found.signature = signature(found.category, error)
    return found


# ---------------------------------------------------------------------------------------------------- state + audit
def _state_path(paths: Paths) -> Path:
    return paths.state / "remediation.json"


def load_state(paths: Paths) -> dict:
    data = read_json(_state_path(paths), {}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("attempts", {})
    data.setdefault("notified", {})
    data.setdefault("bundles", {})
    return data


def save_state(paths: Paths, data: dict) -> None:
    write_json(_state_path(paths), data)


@contextlib.contextmanager
def state_lock(paths: Paths, timeout_s: float = 5.0, stale_s: float = 30.0):
    """Cross-process lock around remediation.json read-modify-write (concurrent runs of different automations share the file).
    Falls through after ``timeout_s`` rather than ever blocking a run: a lost update is better than a stuck run."""
    lock = paths.state / "remediation.json.lock"
    fd = None
    deadline = time.monotonic() + timeout_s
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_s:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)
        except OSError:
            break
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
                lock.unlink()
            except OSError:
                pass


def update_state(paths: Paths, fn: Callable[[dict], Any]) -> Any:
    """Atomically (under state_lock) load the state, let ``fn`` modify it, and save it. Returns fn's result."""
    with state_lock(paths):
        state = load_state(paths)
        result = fn(state)
        save_state(paths, state)
        return result


def audit(paths: Paths, record: dict) -> None:
    """Append one scrubbed line to logs/remediation.jsonl (bounded: the older half is dropped when it passes 1 MB)."""
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        f = paths.logs / "remediation.jsonl"
        if f.exists() and f.stat().st_size > AUDIT_MAX_BYTES:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            f.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
        with open(f, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")
    except OSError:
        pass


def config_enabled(cfg: Optional[dict]) -> Tuple[bool, str]:
    if os.environ.get("AUTOMAGIX_NO_REMEDIATION") == "1":
        return False, "disabled by AUTOMAGIX_NO_REMEDIATION=1"
    c = (cfg or {}).get("remediation")
    if c is False or (isinstance(c, str) and c.lower() in ("false", "off", "0", "no")):
        return False, "disabled by platform.json remediation=false"
    return True, ""


def disabled_playbooks(cfg: Optional[dict]) -> set:
    c = (cfg or {}).get("remediation")
    if isinstance(c, dict):
        d = c.get("disable") or c.get("disabled_playbooks") or []
        return {str(x) for x in d} if isinstance(d, (list, tuple)) else set()
    return set()


# ---------------------------------------------------------------------------------------------------- overrides (native mode)
def overrides_dir(paths: Paths) -> Path:
    return paths.state / "overrides"


def write_override(paths: Paths, automation: str, values: dict, reason: str, ttl_s: int = OVERRIDE_TTL_S) -> Path:
    check_name(automation)
    clean = {}
    for k, v in values.items():
        if k in ALLOWED_OVERRIDES and v in ALLOWED_OVERRIDES[k]:
            clean[k] = v
    if not clean:
        raise ValueError("no allowed override values")
    f = overrides_dir(paths) / f"{automation}.json"
    now = utcnow()
    write_json(f, {"values": clean, "reason": scrub_text(reason, 200), "created_at": iso(now), "expires_at": iso(now + timedelta(seconds=ttl_s))})
    return f


def load_overrides(paths: Paths, automation: str) -> dict:
    """Parameter overrides remediation has written for this automation (whitelisted keys/values only, unexpired)."""
    try:
        check_name(automation)
    except ValueError:
        return {}
    data = read_json(overrides_dir(paths) / f"{automation}.json", None)
    if not isinstance(data, dict):
        return {}
    try:
        if parse_iso(str(data.get("expires_at"))) < utcnow():
            return {}
    except (ValueError, TypeError):
        return {}
    vals = data.get("values") if isinstance(data.get("values"), dict) else {}
    return {k: v for k, v in vals.items() if k in ALLOWED_OVERRIDES and v in ALLOWED_OVERRIDES[k]}


def clear_override(paths: Paths, automation: str) -> bool:
    try:
        check_name(automation)
        f = overrides_dir(paths) / f"{automation}.json"
        if f.exists():
            f.unlink()
            return True
    except (OSError, ValueError):
        pass
    return False


def inside_root(paths: Paths, p: Path, allow_link: bool = False) -> bool:
    """True only for a path strictly inside the install folder, never ``secrets/``, and never reached through a symlink/junction.
    ``allow_link=True`` accepts a final component that IS a symlink (Chromium's SingletonLock on Linux): the parent chain is still
    checked and the caller must unlink the link itself, never follow it."""
    try:
        root = paths.root.resolve()
        pp = Path(p)
        rp = (pp.parent.resolve() / pp.name) if allow_link else pp.resolve()
        if root not in rp.parents:
            return False
        if root / "secrets" == rp or (root / "secrets") in rp.parents:
            return False
        cur = Path(os.path.abspath(str(p)))
        if allow_link:
            cur = cur.parent
        base = Path(os.path.abspath(str(paths.root)))
        while cur != base and base in cur.parents:
            if cur.is_symlink():
                return False
            cur = cur.parent
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------------------------------- playbook base
@dataclass
class Context:
    paths: Paths
    automation: Optional[str]
    cfg: dict
    diagnosis: Diagnosis
    dry_run: bool = False
    lister: Optional[Callable[[], list]] = None          # () -> [(pid, name, cmdline)]
    killer: Optional[Callable[[int], bool]] = None
    scheduler_runner: Optional[Callable] = None
    http_head: Optional[Callable[[str], dict]] = None    # url -> response headers
    now: Callable[[], datetime] = utcnow
    scratch: Dict[str, Any] = field(default_factory=dict)  # rollback tokens, details
    run_id: Optional[str] = None

    def inside_root(self, p: Path) -> bool:
        return inside_root(self.paths, p)

    def inside_root_link(self, p: Path) -> bool:
        return inside_root(self.paths, p, allow_link=True)


class Playbook:
    name = ""
    categories: Tuple[str, ...] = ()
    risk = SAFE
    max_attempts = ACTIONS_PER_DAY_DEFAULT
    cooldown_s = COOLDOWN_DEFAULT_S
    retry_after_fix = False
    description = ""

    def check(self, ctx: Context) -> bool:
        return True

    def fix(self, ctx: Context):  # -> str | (str, outcome)
        raise NotImplementedError

    def verify(self, ctx: Context) -> bool:
        return True

    def rollback(self, ctx: Context) -> None:
        return None

    def recommend(self, ctx: Context) -> str:
        return self.description


@dataclass
class Report:
    category: str
    signature: str = ""
    attempts: List[dict] = field(default_factory=list)
    retry: bool = False
    fixed: bool = False
    escalated: bool = False
    recommendations: List[str] = field(default_factory=list)
    disabled_reason: str = ""
    debug_bundle: Optional[str] = None
    diagnosis: Optional[Diagnosis] = None

    def summary(self) -> dict:
        s = {"category": self.category, "fixed": self.fixed, "playbooks": [a["playbook"] for a in self.attempts if a["outcome"] != "skipped"]}
        if self.escalated:
            s["needs_person"] = True
        if self.debug_bundle:
            s["debug_bundle"] = self.debug_bundle
        return s

    def to_dict(self) -> dict:
        return {"category": self.category, "signature": self.signature, "fixed": self.fixed, "retry": self.retry, "escalated": self.escalated,
                "attempts": self.attempts, "recommendations": self.recommendations, "disabled_reason": self.disabled_reason,
                "debug_bundle": self.debug_bundle, "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None}


def registry() -> List[Playbook]:
    from . import playbooks
    return playbooks.all_playbooks()


# ---------------------------------------------------------------------------------------------------- engine
class Remediator:
    def __init__(self, paths: Optional[Paths] = None, automation: Optional[str] = None, cfg: Optional[dict] = None, dry_run: bool = False,
                 lister: Optional[Callable] = None, killer: Optional[Callable] = None, scheduler_runner: Optional[Callable] = None,
                 http_head: Optional[Callable] = None, now: Optional[Callable[[], datetime]] = None, force: bool = False):
        self.paths = paths or Paths()
        self.automation = automation
        self.cfg = cfg if cfg is not None else _safe_cfg(self.paths)
        self.dry_run = dry_run
        self.force = force  # explicit operator request (CLI): ignores the cool-down but never the per-day budget or the kill switch
        self.lister, self.killer, self.scheduler_runner, self.http_head = lister, killer, scheduler_runner, http_head
        self.now = now or utcnow
        self.log = get_logger(self.paths)

    # -- gates ----------------------------------------------------------------------------------
    def enabled(self) -> Tuple[bool, str]:
        ok, why = config_enabled(self.cfg)
        if not ok:
            return ok, why
        if self.automation and self.paths.status_file(self.automation).exists():
            return False, "automation is paused by an operator"  # kill-switch notice present: nothing runs automatically
        return True, ""

    def _budget(self, state: dict, pb: Playbook) -> Tuple[bool, str]:
        key = f"{pb.name}:{self.automation or '*'}"
        now = self.now()
        recent = []
        for t in state["attempts"].get(key, []):
            try:
                if now - parse_iso(t) < timedelta(hours=24):
                    recent.append(t)
            except (ValueError, TypeError):
                continue
        state["attempts"][key] = recent
        if len(recent) >= pb.max_attempts:
            return False, f"attempt budget exhausted ({len(recent)}/{pb.max_attempts} in 24h)"
        if recent and not self.force and now - parse_iso(recent[-1]) < timedelta(seconds=pb.cooldown_s):
            return False, f"cool-down ({pb.cooldown_s}s) after the previous attempt"
        return True, ""

    def _record(self, state: dict, pb: Playbook) -> None:
        key, stamp = f"{pb.name}:{self.automation or '*'}", iso(self.now())
        state["attempts"].setdefault(key, []).append(stamp)
        try:  # persist now, under the lock, merging into whatever concurrent runs of other automations wrote meanwhile
            update_state(self.paths, lambda st: st["attempts"].setdefault(key, []).append(stamp))
        except Exception:  # noqa: BLE001 - bookkeeping must never break a run
            pass

    def _emit(self, data: dict, run_id: Optional[str]) -> None:
        clean = {"playbook": data["playbook"], "category": data["category"], "action": scrub_text(data["action"], 80), "outcome": data["outcome"],
                 "detail": scrub_text(data.get("detail", ""), 300), "duration_ms": int(data.get("duration_ms", 0)), "attempt": int(data.get("attempt", 1))}
        if not self.dry_run:
            audit(self.paths, {"ts": iso(self.now()), "automation": self.automation, "run_id": run_id, **clean})
            if getattr(runctx, "_current", None) is not None:
                try:
                    from .logging import log_event
                    log_event("remediation_attempt", clean)
                except Exception:  # noqa: BLE001 - telemetry never breaks remediation
                    pass
        data.update(clean)

    # -- main entry -----------------------------------------------------------------------------
    def handle(self, diag: Diagnosis, run_id: Optional[str] = None, allow_retry: bool = True) -> Report:
        rep = Report(category=diag.category, signature=diag.signature, diagnosis=diag)
        ok, why = self.enabled()
        if not ok:
            rep.disabled_reason = why
            return rep
        off = disabled_playbooks(self.cfg)
        pbs = [p for p in registry() if diag.category in p.categories and p.name not in off]
        state = load_state(self.paths)
        ctx = Context(self.paths, self.automation, self.cfg, diag, self.dry_run, self.lister, self.killer, self.scheduler_runner, self.http_head, self.now, run_id=run_id)
        dirty = False
        for pb in pbs:
            t0 = time.monotonic()
            n = len(state["attempts"].get(f"{pb.name}:{self.automation or '*'}", [])) + 1
            rec = {"playbook": pb.name, "category": diag.category, "action": "check", "outcome": "skipped", "detail": "", "duration_ms": 0, "attempt": n}
            try:
                if pb.risk != SAFE:
                    rec.update(action="recommend", outcome="recommended", detail=pb.recommend(ctx))
                    rep.recommendations.append(rec["detail"])
                elif not pb.check(ctx):
                    rec.update(action="check", outcome="skipped", detail="not applicable (nothing to fix)")
                else:
                    allowed, reason = self._budget(state, pb)
                    if not allowed:
                        rec.update(action="budget", outcome="skipped", detail=reason)
                    elif self.dry_run:
                        rec.update(action="dry-run", outcome="recommended", detail="would run: " + pb.description)
                        rep.recommendations.append(pb.description)
                    else:
                        self._record(state, pb)
                        dirty = True
                        res = pb.fix(ctx)
                        detail, outcome = res if isinstance(res, tuple) else (res, None)
                        verified = pb.verify(ctx)
                        outcome = outcome or ("fixed" if verified else "not_fixed")
                        if outcome == "fixed" and not verified:
                            outcome = "not_fixed"
                        if outcome == "not_fixed":
                            try:
                                pb.rollback(ctx)
                                detail = f"{detail} (verification failed; rolled back)"
                            except Exception as exc:  # noqa: BLE001
                                detail = f"{detail} (verification failed; rollback error: {type(exc).__name__})"
                        rec.update(action=pb.name, outcome=outcome, detail=str(detail))
                        if outcome == "fixed":
                            rep.fixed = True
                            if pb.retry_after_fix:
                                rep.retry = allow_retry
                        elif outcome == "escalated":
                            rep.escalated = True
                        elif outcome == "recommended":
                            rep.recommendations.append(str(detail))
            except Exception as exc:  # noqa: BLE001 - a broken playbook must never take the run down
                rec.update(outcome="not_fixed", detail=f"playbook error: {type(exc).__name__}: {str(exc)[:160]}")
                try:
                    pb.rollback(ctx)
                except Exception:  # noqa: BLE001
                    pass
            rec["duration_ms"] = int((time.monotonic() - t0) * 1000)
            self._emit(rec, run_id)
            rep.attempts.append(rec)
        rep.debug_bundle = ctx.scratch.get("bundle") or rep.debug_bundle
        return rep


def _safe_cfg(paths: Paths) -> dict:
    try:
        return platform_config(paths)
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------------------------------- notifications
def needs_person(paths: Paths, automation: Optional[str], key: str, title: str, body: str, now: Optional[datetime] = None) -> str:
    """Tell a human, once per 24 h per key: attention file + toast + (if available) one pending approval task. Returns what was done."""
    now = now or utcnow()

    def claim(state: dict) -> bool:
        last = state["notified"].get(key)
        try:
            if last and now - parse_iso(last) < timedelta(hours=24):
                return False
        except (ValueError, TypeError):
            pass
        state["notified"][key] = iso(now)  # claimed before delivery so two concurrent runs notify once
        return True

    if not update_state(paths, claim):
        return "already notified within 24h"
    done = []
    name = automation or "platform"
    try:
        from .util import atomic_write_text
        check_name(name)
        atomic_write_text(paths.state / "toast" / f"{name}.needs-attention.txt", f"{iso(now)}\n{scrub_text(title, 120)}\n{scrub_text(body, 600)}\n")
        done.append("attention file")
    except (OSError, ValueError):
        pass
    try:
        from . import notify
        if notify.toast(scrub_text(title, 80), scrub_text(body, 160)):
            done.append("toast")
    except Exception:  # noqa: BLE001
        pass
    try:
        from .activities import approvals  # present on installs that ship the approvals module
        a_title, a_body = scrub_text(title)[:120], scrub_text(body)[:600]  # hard cut: scrub_text(limit) appends a suffix that can exceed the limit
        pending = [t for t in approvals.list_tasks("pending", paths) if t.title == a_title and t.automation == name]
        if not pending:
            approvals.create(a_title, a_body, automation=name, expires_in_s=7 * 86400, notify=False, paths=paths)
            done.append("approval task")
    except Exception:  # noqa: BLE001
        pass
    return "notified: " + (", ".join(done) or "nothing deliverable")


# ---------------------------------------------------------------------------------------------------- environment checks
PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")
MIN_FREE_MB = 200


def profile_dir(paths: Paths, automation: str) -> Path:
    return paths.root / "browser-profile" / check_name(automation)


def profile_lock_files(paths: Paths, automation: str) -> List[Path]:
    d = profile_dir(paths, automation)
    out = []
    for n in PROFILE_LOCK_NAMES:
        p = d / n
        try:
            if p.exists() or p.is_symlink():
                out.append(p)
        except OSError:
            continue
    return out


def lock_state(paths: Paths, automation: str) -> str:
    """'none' | 'live' | 'stale' for the automation's run lock (never signals the owner)."""
    f = paths.lock_file(automation)
    if not f.exists():
        return "none"
    try:
        owner = int(f.read_text(encoding="ascii", errors="replace").split()[0])
    except (OSError, ValueError, IndexError):
        owner = 0
    try:
        age = time.time() - f.stat().st_mtime
    except OSError:
        return "none"
    from .runner import _pid_alive
    if owner and owner != os.getpid() and not _pid_alive(owner):
        return "stale"
    if age > 4 * 3600 + 600:
        return "stale"
    return "live"


def other_run_live(paths: Paths, automation: str) -> bool:
    """True when ANOTHER live process holds this automation's run lock (the runner's own lock does not count: it is the one remediating)."""
    if lock_state(paths, automation) != "live":
        return False
    try:
        owner = int(paths.lock_file(automation).read_text(encoding="ascii", errors="replace").split()[0])
    except (OSError, ValueError, IndexError):
        return True  # a live lock we cannot attribute: assume it is someone else's
    return owner != os.getpid()


def required_files(paths: Paths, automation: Optional[str], runtime: Optional[str] = None) -> List[Tuple[str, Path]]:
    """(component, path) pairs that must exist for the automation to start."""
    req: List[Tuple[str, Path]] = []
    try:  # only a populated wrapper-core folder can be "damaged"; an absent one means running from source (and this code is running)
        if paths.wrapper_core.is_dir() and any(paths.wrapper_core.iterdir()):
            req.append(("wrapper-core", paths.wrapper_core / "wrapper_core" / "__init__.py"))
    except OSError:
        pass
    if automation:
        adir = paths.automation_dir(automation)
        req.append((f"automation:{automation}", adir / "automation.yaml"))
        entry = None
        try:
            from . import miniyaml
            d = miniyaml.load(adir / "automation.yaml") if (adir / "automation.yaml").exists() else {}
            if isinstance(d, dict):
                entry = d.get("entrypoint")
                runtime = runtime or d.get("runtime")
        except Exception:  # noqa: BLE001
            pass
        if isinstance(entry, str) and entry and ".." not in entry and not os.path.isabs(entry):
            req.append((f"automation:{automation}", adir / entry))
        if runtime == "node":
            req.append(("execution-framework", paths.framework / "index.js"))
            req.append(("execution-framework", paths.framework / "node_modules" / "playwright-core" / "package.json"))
            if os.environ.get("AUTOMAGIX_NODE") is None and not shutil.which("node"):
                req.append(("execution-framework", paths.framework / "node" / "node.exe"))
    return req


def missing_components(paths: Paths, automation: Optional[str], runtime: Optional[str] = None) -> List[str]:
    bad = []
    for comp, p in required_files(paths, automation, runtime):
        if not p.exists() and comp not in bad:
            bad.append(comp)
    return bad


def free_mb(paths: Paths) -> Optional[int]:
    try:
        probe = paths.root if paths.root.exists() else paths.root.parent
        return int(shutil.disk_usage(str(probe)).free // (1024 * 1024))
    except OSError:
        return None


def diagnose_environment(paths: Paths, automation: Optional[str], runtime: Optional[str] = None, include_tasks: bool = False,
                         tasks_check: Optional[Callable[[str], str]] = None) -> List[Diagnosis]:
    """Cheap, read-only checks for conditions that will make the next run fail. Stat-based except ``include_tasks``."""
    out: List[Diagnosis] = []

    def add(cat: str, ev: str, conf: float = 0.9) -> None:
        out.append(Diagnosis(cat, conf, [ev], signature=f"{cat}:env", automation=automation))

    if automation:
        ls = lock_state(paths, automation)
        if ls == "stale":
            add("STALE_LOCK", "run lock belongs to a process that is gone (or is older than the run timeout)")
        if profile_lock_files(paths, automation):
            add("STALE_BROWSER_PROFILE_LOCK", "browser profile lock files are present: " + ", ".join(p.name for p in profile_lock_files(paths, automation)))
    bad = missing_components(paths, automation, runtime)
    if bad:
        add("MISSING_FILE", "required files missing for: " + ", ".join(bad))
    f = free_mb(paths)
    if f is not None and f < MIN_FREE_MB:
        add("DISK_LOW", f"{f} MB free (< {MIN_FREE_MB} MB)", 0.95)
    if include_tasks and automation and tasks_check:
        st = tasks_check(automation)
        if st in ("missing", "disabled"):
            add("SCHEDULED_TASK_MISSING" if st == "missing" else "SCHEDULED_TASK_DISABLED", f"scheduled task is {st}")
    return out


# ---------------------------------------------------------------------------------------------------- integration points
def runtime_of(paths: Paths, automation: str) -> Optional[str]:
    try:
        from . import miniyaml
        f = paths.automation_dir(automation) / "automation.yaml"
        d = miniyaml.load(f) if f.exists() else {}
        return str(d.get("runtime")) if isinstance(d, dict) and d.get("runtime") else None
    except Exception:  # noqa: BLE001
        return None


def preflight(paths: Paths, automation: str, cfg: Optional[dict] = None, runtime: Optional[str] = None, run_id: Optional[str] = None,
              **kw) -> List[dict]:
    """Run start (the run lock is held, so no other run of this automation is active): fix what is about to fail the run.
    Stat-only detection (< 200 ms when healthy); actions run only when something is actually wrong. Never raises."""
    try:
        if (paths.state / "repair-pending").is_dir():  # a repair that died half way must never leave the install record wiped
            from . import playbooks
            playbooks.recover_pending_repairs(paths)
        cfg = cfg if cfg is not None else _safe_cfg(paths)
        ok, _ = config_enabled(cfg)
        if not ok:
            return []
        diags = [d for d in diagnose_environment(paths, automation, runtime) if d.category != "STALE_LOCK"]  # RunLock reclaims its own stale lock
        if not diags:
            return []
        rem = Remediator(paths, automation, cfg, False, **kw)
        attempts: List[dict] = []
        for d in diags:
            rep = rem.handle(d, run_id, allow_retry=False)
            attempts.extend(rep.attempts)
        return attempts
    except Exception as exc:  # noqa: BLE001
        get_logger(paths).warning("remediation preflight failed: %s", exc)
        return []


def handle_failure(paths: Paths, cfg: Optional[dict], automation: str, error: dict, run_id: Optional[str] = None, run_dir: Optional[Path] = None,
                   allow_retry: bool = True, make_bundle: bool = True, runtime: Optional[str] = None, **kw) -> Optional[Report]:
    """A run failed: diagnose, apply safe playbooks, optionally create the debug bundle. Returns the Report (or None if disabled/errored)."""
    try:
        cfg = cfg if cfg is not None else _safe_cfg(paths)
        ok, _ = config_enabled(cfg)
        if not ok:
            return None
        diag = diagnose(error, {"root": str(paths.root), "automation": automation, "runtime": runtime or runtime_of(paths, automation)})
        rem = Remediator(paths, automation, cfg, False, **kw)
        rep = rem.handle(diag, run_id, allow_retry)
        if rep.disabled_reason:
            return None
        if make_bundle and not rep.debug_bundle:
            rep.debug_bundle = bundle_once(paths, diag, run_id)
        return rep
    except Exception as exc:  # noqa: BLE001
        get_logger(paths).warning("remediation failed: %s", exc)
        return None


def bundle_once(paths: Paths, diag: Diagnosis, run_id: Optional[str], automation: Optional[str] = None) -> Optional[str]:
    """Create a debug bundle for this failure signature once per 24 h (max 10 a day). Returns its path relative to the install folder."""
    try:
        now = utcnow()

        def claim(state: dict) -> bool:
            recent = {s: t for s, t in state["bundles"].items() if _within(t, now, 86400)}
            state["bundles"] = recent
            if diag.signature in recent or len(recent) >= 10:
                return False
            recent[diag.signature] = iso(now)
            return True

        if not update_state(paths, claim):
            return None
        from . import debugbundle
        try:
            out = debugbundle.create_bundle(paths, run_id=run_id, reason=diag.category, automation=automation or diag.automation)
        except Exception:
            update_state(paths, lambda st: st["bundles"].pop(diag.signature, None))  # a failed bundle must not suppress the next attempt
            raise
        return str(out.relative_to(paths.root)).replace("\\", "/")
    except Exception as exc:  # noqa: BLE001
        get_logger(paths).warning("debug bundle failed: %s", exc)
        return None


def _within(ts: str, now: datetime, seconds: int) -> bool:
    try:
        return now - parse_iso(ts) < timedelta(seconds=seconds)
    except (ValueError, TypeError):
        return False


_in_pass = False


def proactive_pass(paths: Paths, cfg: Optional[dict] = None, force: bool = False, **kw) -> List[dict]:
    """Login / idle heartbeat hook: fix environment problems for installed automations that are not running right now.
    At most every 15 minutes. Excludes scheduled tasks (``maintain`` already re-asserts those at every login)."""
    global _in_pass
    if _in_pass:
        return []
    _in_pass = True
    try:
        cfg = cfg if cfg is not None else _safe_cfg(paths)
        if not config_enabled(cfg)[0]:
            return []
        def due(state: dict) -> bool:
            if not force and _within(str(state.get("env_pass", "")), utcnow(), ENV_PASS_INTERVAL_S):
                return False
            state["env_pass"] = iso()
            return True

        if not update_state(paths, due):
            return []
        from . import selfheal
        out: List[dict] = []
        for name in sorted(selfheal.read_installed(paths)["automations"]):
            try:
                if lock_state(paths, name) == "live":
                    continue
                rem = Remediator(paths, name, cfg, False, **kw)
                for d in diagnose_environment(paths, name, runtime_of(paths, name)):
                    out.extend(rem.handle(d, None, allow_retry=False).attempts)
            except Exception as exc:  # noqa: BLE001
                get_logger(paths).warning("environment pass for %s failed: %s", name, exc)
        return out
    except Exception as exc:  # noqa: BLE001
        get_logger(paths).warning("environment pass failed: %s", exc)
        return []
    finally:
        _in_pass = False


# ---------------------------------------------------------------------------------------------------- doctor --fix
_DOCTOR_TASK = re.compile(r"^scheduled task: (.+)$")


def fix_from_doctor(results: List[dict], paths: Optional[Paths] = None, dry_run: bool = False, **kw) -> List[Report]:
    """Apply the safe playbooks for FAIL/WARN doctor checks that have one, plus the environment checks. Returns the reports."""
    paths = paths or Paths()
    cfg = _safe_cfg(paths)
    reports: List[Report] = []
    seen = set()
    for r in results:
        if r.get("status") == "PASS":
            continue
        m = _DOCTOR_TASK.match(str(r.get("check", "")))
        diag = None
        if m:
            diag = Diagnosis("SCHEDULED_TASK_MISSING", 0.9, [str(r.get("detail"))], "SCHEDULED_TASK_MISSING:doctor", m.group(1))
        elif r.get("check") == "login maintenance script":
            diag = Diagnosis("SCHEDULED_TASK_MISSING", 0.8, [str(r.get("detail"))], "STARTUP_SCRIPT:doctor", None)
        elif str(r.get("check", "")).startswith("node") and r.get("status") != "PASS":
            diag = Diagnosis("NODE_MISSING", 0.6, [str(r.get("detail"))], "NODE_MISSING:doctor", None)
        if diag is None:
            continue
        key = (diag.category, diag.automation, diag.signature)
        if key in seen:
            continue
        seen.add(key)
        reports.append(Remediator(paths, diag.automation, cfg, dry_run, force=True, **kw).handle(diag, None, allow_retry=False))
    from . import selfheal
    for name in sorted(selfheal.read_installed(paths)["automations"]):
        rem = Remediator(paths, name, cfg, dry_run, force=True, **kw)
        for d in diagnose_environment(paths, name, runtime_of(paths, name)):
            reports.append(rem.handle(d, None, allow_retry=False))
    return reports


# ---------------------------------------------------------------------------------------------------- CLI: remediate
def _latest_error(paths: Paths, name: str) -> Optional[dict]:
    last = read_json(paths.last_run_file(name), None)
    if not isinstance(last, dict) or last.get("status") in (None, "success", "partial", "skipped_maintenance", "halted_paused"):
        return None
    rid = str(last.get("run_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9T.-]{8,60}", rid):
        return None
    d = paths.runs / rid
    err = read_json(d / "error.json", None) or {}
    try:
        tail = (d / "stderr.log").read_bytes()[-4000:].decode("utf-8", "replace")
    except OSError:
        tail = ""
    err = dict(err) if isinstance(err, dict) else {}
    err["stderr_tail"] = tail
    err["status"] = last.get("status")
    err["run_id"] = rid
    return err


def main(argv: List[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="wrapper_core remediate", description="diagnose the latest failure and the install, and apply SAFE fixes")
    ap.add_argument("--automation", help="limit to one installed automation")
    ap.add_argument("--dry-run", action="store_true", help="report what would be done; change nothing")
    ap.add_argument("--category", help="only act on this diagnosis category (e.g. DEVTOOLS_BLOCKED)")
    ap.add_argument("--include-tasks", action="store_true", help="also check/repair scheduled tasks")
    ap.add_argument("--clear-overrides", action="store_true", help="remove remediation parameter overrides (e.g. native mode)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    paths = Paths().ensure()
    cfg = _safe_cfg(paths)
    from . import selfheal
    names = [a.automation] if a.automation else sorted(selfheal.read_installed(paths)["automations"])
    out: List[dict] = []
    unresolved = 0
    for name in names:
        try:
            check_name(name)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if a.clear_overrides:
            out.append({"automation": name, "override_cleared": clear_override(paths, name)})
            continue
        rem = Remediator(paths, name, cfg, a.dry_run, force=True)
        diags: List[Diagnosis] = []
        err = _latest_error(paths, name)
        if err:
            diags.append(diagnose(err, {"root": str(paths.root), "automation": name, "runtime": runtime_of(paths, name)}))
        tasks_check = None
        if a.include_tasks:
            from . import playbooks
            tasks_check = lambda n: playbooks.task_status(paths, n, rem.scheduler_runner)  # noqa: E731
        diags += diagnose_environment(paths, name, runtime_of(paths, name), include_tasks=a.include_tasks, tasks_check=tasks_check)
        for d in diags:
            if a.category and d.category != a.category:
                continue
            rep = rem.handle(d, err.get("run_id") if err else None, allow_retry=False)
            out.append({"automation": name, **rep.to_dict()})
            if not rep.fixed and not a.dry_run and d.category != "UNKNOWN":
                unresolved += 1
    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        if not out:
            print("nothing to remediate: no failed run and no environment problems found")
        for r in out:
            if "override_cleared" in r:
                print(f"{r['automation']}: override cleared" if r["override_cleared"] else f"{r['automation']}: no override")
                continue
            print(f"{r['automation']}: {r['category']}  (fixed={r['fixed']})")
            for at in r["attempts"]:
                print(f"   [{at['outcome']}] {at['playbook']}: {at['detail']}")
            for rec in r["recommendations"]:
                print(f"   -> {rec}")
    return 1 if unresolved else 0
