"""Log transports (SPEC §7).

``git-ssh``: write-only *by construction*. The endpoint never fetches or reads the logging repo; it creates a
throwaway repo, commits one artefact and pushes the orphan commit to a brand-new unique ref
``refs/heads/ingest/<ring>/<kind>/<name>/<id>``. GitLab push rules allow only that pattern, so a stolen key can only
create junk refs.

``dir``: same relative layout on a local/shared directory (dev ring, tests, offline).

Failures never fail a run: artefacts are spooled and flushed on the next run/login.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import secrets
from .paths import Paths, get_logger, platform_config
from .util import atomic_write_text, hostname, iso, parse_iso, safe_name, utcnow


class TransportError(RuntimeError):
    pass


def ref_for(ring: str, kind: str, name: str, art_id: str) -> str:
    return f"ingest/{safe_name(ring)}/{safe_name(kind)}/{safe_name(name)}/{safe_name(art_id)}"


def rel_path(kind: str, name: str, art_id: str) -> str:
    return f"{safe_name(kind)}/{safe_name(name)}/{safe_name(art_id)}.json"


class Transport:
    def __init__(self, ring: str):
        self.ring = ring

    def send(self, kind: str, name: str, art_id: str, payload: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class DirTransport(Transport):
    def __init__(self, root: str | Path, ring: str):
        super().__init__(ring)
        self.root = Path(root)

    def send(self, kind: str, name: str, art_id: str, payload: dict) -> None:
        target = self.root / "ingest" / safe_name(self.ring) / rel_path(kind, name, art_id)
        try:
            atomic_write_text(target, json.dumps(payload, indent=2, default=str) + "\n")
        except OSError as exc:
            raise TransportError(f"cannot write {target}: {exc}") from exc


def find_git(paths: Paths) -> str | None:
    """Git bundled by the installer (MinGit) wins, then PATH."""
    bundled = paths.root / "git" / "cmd" / "git.exe"
    return str(bundled) if bundled.exists() else shutil.which("git")


def find_ssh(paths: Paths) -> str | None:
    """Windows' built-in OpenSSH, then the ssh.exe inside the bundled MinGit, then PATH."""
    win = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
    for cand in (win, paths.root / "git" / "usr" / "bin" / "ssh.exe"):
        if cand.exists():
            return str(cand)
    return shutil.which("ssh")


class GitSshTransport(Transport):
    """git over SSH with a deploy key (DPAPI-protected at rest, written to a private temp file for one push, then wiped).

    mode ``branch`` (default): every artefact is a new file on ONE branch (``ingest``, override with env
    ``AUTOMAGIX_LOG_GIT_BRANCH``): shallow clone, add, commit, push; a lost race (non-fast-forward) is retried. This is what the
    site workflow reads (``clone --branch ingest``).  mode ``refs`` (env ``AUTOMAGIX_LOG_GIT_MODE=refs``): the original
    write-only design - an orphan commit pushed to a brand-new unique ref, no fetch.
    """

    def __init__(self, remote: str, ring: str, paths: Paths | None = None, timeout_s: int = 90):
        super().__init__(ring)
        self.remote, self.paths, self.timeout_s = remote, paths or Paths(), timeout_s
        self.mode = os.environ.get("AUTOMAGIX_LOG_GIT_MODE", "branch")
        self.branch = os.environ.get("AUTOMAGIX_LOG_GIT_BRANCH", "ingest")
        if not all(c.isalnum() or c in "-_/." for c in self.branch) or self.branch.startswith(("-", "/")) or ".." in self.branch:
            raise TransportError("invalid AUTOMAGIX_LOG_GIT_BRANCH")

    def _ssh_env(self, keyfile: Path) -> dict:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        hosts = self.paths.known_hosts
        ssh = find_ssh(self.paths) or "ssh"
        cmd = (f'"{ssh}" -i "{keyfile}" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20 '
               f'-o StrictHostKeyChecking={"yes" if hosts.exists() else "accept-new"} '
               + (f'-o UserKnownHostsFile="{hosts}" ' if hosts.exists() else ""))
        env["GIT_SSH_COMMAND"] = cmd.replace("\\", "/")
        return env

    def send(self, kind: str, name: str, art_id: str, payload: dict) -> None:
        git = find_git(self.paths)
        if not git:
            raise TransportError("git not found (not bundled and not on PATH)")
        if self.remote.startswith("-"):
            raise TransportError("refusing remote that looks like an option")
        blob = secrets.logging_key_blob(self.paths)
        if blob is None:
            raise TransportError("logging key blob missing (secrets/logging_key.dpapi)")
        body = json.dumps(payload, indent=2, default=str) + "\n"
        with tempfile.TemporaryDirectory(prefix="amx-log-") as tmp:
            with secrets.TempPrivateKey(blob, self.paths.secrets / "tmp") as keyfile:
                env = self._ssh_env(keyfile)
                if self.mode == "refs":
                    self._send_ref(git, Path(tmp), env, kind, name, art_id, body)
                else:
                    self._send_branch(git, Path(tmp), env, kind, name, art_id, body)

    # -- unique-ref mode (no fetch) ------------------------------------------------------------------------------------------
    def _send_ref(self, git, tmp: Path, env: dict, kind, name, art_id, body) -> None:
        repo = tmp / "r"
        repo.mkdir()
        atomic_write_text(repo / rel_path(kind, name, art_id), body)
        ident = ["-c", "user.name=automagix", "-c", "user.email=automagix@localhost", "-c", "commit.gpgsign=false"]
        self._git(git, repo, ["init", "-q"], env)
        self._git(git, repo, ["add", "-A"], env)
        self._git(git, repo, ident + ["commit", "-q", "-m", f"{kind} {name} {art_id}"], env)
        self._git(git, repo, ["push", "-q", self.remote, f"HEAD:refs/heads/{ref_for(self.ring, kind, name, art_id)}"], env)

    # -- single-branch mode ----------------------------------------------------------------------------------------------------
    def _send_branch(self, git, tmp: Path, env: dict, kind, name, art_id, body) -> None:
        ident = ["-c", "user.name=automagix", "-c", "user.email=automagix@localhost", "-c", "commit.gpgsign=false"]
        rel = f"ingest/{safe_name(self.ring)}/{rel_path(kind, name, art_id)}"
        last = ""
        for attempt in range(4):
            repo = tmp / f"r{attempt}"
            try:
                self._git(git, tmp, ["clone", "-q", "--depth", "1", "--branch", self.branch, "--single-branch", self.remote, str(repo)], env)
            except TransportError as exc:
                if "not found" not in str(exc).lower() and "could not find remote branch" not in str(exc).lower():
                    raise
                repo.mkdir(exist_ok=True)       # first writer: create the branch from an orphan commit
                self._git(git, repo, ["init", "-q", "-b", self.branch], env)
                self._git(git, repo, ["remote", "add", "origin", self.remote], env)
            target = repo / rel
            if target.exists():
                return  # already delivered (idempotent re-send)
            atomic_write_text(target, body)
            self._git(git, repo, ["add", "-A"], env)
            self._git(git, repo, ident + ["commit", "-q", "-m", f"{kind} {name} {art_id}"], env)
            try:
                self._git(git, repo, ["push", "-q", "origin", f"HEAD:refs/heads/{self.branch}"], env)
                return
            except TransportError as exc:
                last = str(exc)
                if not any(t in last.lower() for t in ("non-fast-forward", "fetch first", "rejected", "failed to update ref")):
                    raise
                time.sleep(min(1 + attempt, 3))  # lost a race with another machine: re-clone and retry
        raise TransportError(f"push kept losing races: {last[:200]}")

    def _git(self, git: str, cwd: Path, args: list[str], env: dict) -> None:
        try:
            r = subprocess.run([git] + args, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"git {args[0]} timed out") from exc
        if r.returncode != 0:
            raise TransportError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")


class GitHubApiTransport(Transport):
    """Push artefacts to a (private) GitHub logs repo over HTTPS with the Contents API - no git, no ssh, stdlib only.

    Layout ``v2`` (default): date folders and one branch per month, so no directory or branch grows without bound and a job only
    needs the last months:  ``ingest-YYYY-MM:ingest/<ring>/<kind>/<name>/YYYY/MM/DD/<id>.json``. A run's artefacts (run record,
    heartbeats, lifecycle events) travel as ONE ``bundle`` file per flush (``send_many``), i.e. one commit instead of 4-6.
    Layout ``legacy`` (env ``AUTOMAGIX_LOG_LAYOUT=legacy``): the original flat layout on the single branch ``ingest``.

    The token is a fine-grained PAT limited to that one repo (contents: write), DPAPI-protected on the endpoint. It can also READ
    the repo (see SECURITY.md); protect the branches with a ruleset so it can only ever add logs. Re-sending an existing artefact is
    a no-op (HTTP 422 'sha wasn't supplied' = it is already there; bundle ids are derived from their contents).
    """

    MAX_BUNDLE_BYTES = 600_000
    MAX_BUNDLE_ITEMS = 150

    def __init__(self, repo: str, ring: str, paths: Paths | None = None, branch: str = "ingest", timeout_s: int = 30,
                 layout: str | None = None):
        super().__init__(ring)
        self.repo, self.paths, self.branch, self.timeout_s = repo, paths or Paths(), branch, timeout_s
        self.api = os.environ.get("AUTOMAGIX_GITHUB_API", "https://api.github.com").rstrip("/")
        self.layout = layout or os.environ.get("AUTOMAGIX_LOG_LAYOUT", "v2")
        self.retry_budget_s = float(os.environ.get("AUTOMAGIX_LOG_RETRY_BUDGET_S", "90"))
        self._ready: set = set()
        self._headers: dict = {}

    # ---- low level -------------------------------------------------------------------------------------------------------
    def _call(self, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        import urllib.error
        import urllib.request
        token = secrets.get_logging_token(self.paths)
        if not token:
            status, detail = secrets.logging_token_status(self.paths)
            note_auth_failure(self.paths, 0, status)
            if status == "undecryptable":
                raise TransportError("logging token cannot be decrypted by Windows (profile rebuilt / password reset): reinstall with the connected installer")
            raise TransportError("logging token missing (secrets/logging_token.dpapi or AUTOMAGIX_LOGGING_TOKEN)")
        req = urllib.request.Request(self.api + url, method=method, data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "autostation-wrapper-core",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:  # noqa: S310 - fixed GitHub API host
                raw = r.read().decode("utf-8") or "{}"
                self._headers = {k.lower(): v for k, v in r.headers.items()}
                return r.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                detail = {}
            self._headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            if exc.code == 401 or (exc.code == 403 and not self._is_throttle(exc.code, detail)):
                note_auth_failure(self.paths, exc.code, "rejected")
            return exc.code, detail
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"GitHub API unreachable: {exc}") from exc

    def _is_throttle(self, code: int, detail: dict) -> bool:
        msg = str(detail.get("message", "")).lower()
        return (code == 429 or "retry-after" in self._headers or self._headers.get("x-ratelimit-remaining") == "0"
                or (code == 403 and any(w in msg for w in ("rate limit", "secondary", "abuse"))))

    def _wait_s(self, attempt: int) -> float:
        """Exponential backoff that honours Retry-After, with jitter so a fleet does not retry in lockstep."""
        try:
            retry_after = float(self._headers.get("retry-after", 0) or 0)
        except ValueError:
            retry_after = 0.0
        base = min(2 ** attempt, 30)
        wait = max(retry_after, base) + random.uniform(0, 1 + attempt)
        return min(wait, 120.0) * _retry_scale()

    # ---- branches --------------------------------------------------------------------------------------------------------
    def _branch_for(self, dt: datetime) -> str:
        return self.branch if self.layout == "legacy" else f"{self.branch}-{dt.strftime('%Y-%m')}"

    def _cache_path(self) -> Path:
        return self.paths.state / "log-branches.json"

    def _cached_ready(self, branch: str) -> bool:
        try:
            data = json.loads(self._cache_path().read_text(encoding="utf-8"))
            return branch in (data.get(self.repo) or [])
        except (OSError, ValueError, AttributeError):
            return False

    def _remember_ready(self, branch: str, ready: bool = True) -> None:
        try:
            try:
                data = json.loads(self._cache_path().read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            names = [b for b in (data.get(self.repo) or []) if b != branch]
            if ready:
                names.append(branch)
            data[self.repo] = names[-24:]
            atomic_write_text(self._cache_path(), json.dumps(data))
        except OSError:
            pass

    def _bootstrap_empty_repo(self) -> None:
        """Create the first commit (a README on the default branch) so branches can exist. Idempotent: 422 = another machine won the race."""
        import base64
        text = "# AutoStation run logs" + chr(10) * 2 + "Written by installed automations (branches `ingest-YYYY-MM`). Private; do not edit by hand." + chr(10)
        code, detail = self._call("PUT", f"/repos/{self.repo}/contents/README.md",
                                  {"message": "initialise logs repo", "content": base64.b64encode(text.encode("utf-8")).decode("ascii")})
        if code not in (200, 201, 409, 422):
            raise TransportError(f"cannot initialise empty logs repo {self.repo}: HTTP {code} {str(detail.get('message', ''))[:100]}")

    def _ensure_branch(self, branch: str) -> None:
        if branch in self._ready:
            return
        if self._cached_ready(branch):          # a run must not spend a request per artefact re-checking a branch it already knows
            self._ready.add(branch)
            return
        code, detail = self._call("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")
        if code == 409 and "empty" in str(detail.get("message", "")).lower():
            self._bootstrap_empty_repo()          # a brand-new logs repo has no commits at all: the API answers 409 until one exists
            code, detail = self._call("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")
        if code == 404:
            code, repo = self._call("GET", f"/repos/{self.repo}")
            if code != 200:
                raise TransportError(f"cannot read repo {self.repo}: HTTP {code}")
            default = repo.get("default_branch", "main")
            code, ref = self._call("GET", f"/repos/{self.repo}/git/ref/heads/{default}")
            if code != 200:
                raise TransportError(f"logs repo {self.repo} has no commits yet: HTTP {code}")
            code, _ = self._call("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": ref["object"]["sha"]})
            if code not in (201, 422):  # 422 = created concurrently by another machine
                raise TransportError(f"cannot create branch {branch}: HTTP {code}")
        elif code != 200:
            raise TransportError(f"cannot read branch {branch}: HTTP {code}")
        self._ready.add(branch)
        self._remember_ready(branch)

    def _forget_branch(self, branch: str) -> None:
        self._ready.discard(branch)
        self._remember_ready(branch, ready=False)

    # ---- writing ---------------------------------------------------------------------------------------------------------
    def _path_for(self, kind: str, name: str, art_id: str, dt: datetime) -> str:
        if self.layout == "legacy":
            return f"ingest/{safe_name(self.ring)}/{rel_path(kind, name, art_id)}"
        return (f"ingest/{safe_name(self.ring)}/{safe_name(kind)}/{safe_name(name)}/"
                f"{dt.strftime('%Y')}/{dt.strftime('%m')}/{dt.strftime('%d')}/{safe_name(art_id)}.json")

    def _put_file(self, branch: str, path: str, text: str, message: str) -> None:
        import base64
        body = {"message": message[:200], "branch": branch, "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        deadline = time.monotonic() + self.retry_budget_s
        attempt, last, reensured = 0, "", False
        while True:
            code, detail = self._call("PUT", f"/repos/{self.repo}/contents/{path}", body)
            if code in (200, 201):
                clear_auth_failure(self.paths)
                return
            if code == 422 and "sha" in str(detail.get("message", "")):
                return  # already uploaded (idempotent re-send)
            if code == 404 and not reensured:
                self._forget_branch(branch)      # the branch was deleted or the cache was stale: create it again, once
                self._ensure_branch(branch)
                reensured = True
                continue
            last = f"HTTP {code} {str(detail.get('message', ''))[:120]}"
            retryable = code in (409, 429, 500, 502, 503, 504) or self._is_throttle(code, detail)
            if not retryable or attempt >= 7 or time.monotonic() >= deadline:
                break
            time.sleep(self._wait_s(attempt))      # concurrent commit on the same branch / throttling: back off, then retry
            attempt += 1
        raise TransportError(f"GitHub upload failed: {last}")

    def send(self, kind: str, name: str, art_id: str, payload: dict) -> None:
        dt = art_time(kind, payload, art_id)
        branch = self._branch_for(dt)
        self._ensure_branch(branch)
        self._put_file(branch, self._path_for(kind, name, art_id, dt), json.dumps(payload, indent=2, default=str) + chr(10),
                       f"{kind} {name} {art_id}")

    def send_many(self, items: list[dict]) -> None:
        """Send artefacts as few commits as possible: one ``bundle`` file per month-branch chunk (<= ~600 KB, <= 150 items)."""
        if self.layout == "legacy":
            for it in items:
                self.send(it["kind"], it["name"], it["id"], it["payload"])
            return
        groups: dict[str, list] = {}
        for it in items:
            dt = art_time(it["kind"], it["payload"], it["id"])
            if len(json.dumps(it["payload"], default=str)) > self.MAX_BUNDLE_BYTES:
                self.send(it["kind"], it["name"], it["id"], it["payload"])      # big (evidence): its own file, not bundled
                continue
            groups.setdefault(self._branch_for(dt), []).append((dt, it))
        host = safe_name(hostname())
        for branch, entries in groups.items():
            chunk: list = []
            size = 0
            for dt, it in entries + [(None, None)]:
                item_size = len(json.dumps(it["payload"], default=str)) if it else 0
                if chunk and (it is None or size + item_size > self.MAX_BUNDLE_BYTES or len(chunk) >= self.MAX_BUNDLE_ITEMS):
                    self._send_bundle(branch, host, chunk)
                    chunk, size = [], 0
                if it is not None:
                    chunk.append((dt, it))
                    size += item_size

    def _send_bundle(self, branch: str, host: str, chunk: list) -> None:
        first = min(dt for dt, _ in chunk)
        ids = sorted(f"{it['kind']}:{it['name']}:{it['id']}" for _, it in chunk)
        bundle_id = "b" + hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:20]     # same contents -> same file (idempotent)
        doc = {"schema_version": 1, "kind": "bundle", "host": host, "created_at": iso(utcnow()),
               "items": [{"kind": it["kind"], "name": it["name"], "id": it["id"], "payload": it["payload"]} for _, it in chunk]}
        self._ensure_branch(branch)
        self._put_file(branch, self._path_for("bundle", host, bundle_id, first), json.dumps(doc, separators=(",", ":"), default=str) + chr(10),
                       f"bundle {host} {len(chunk)} artefact(s)")


def get_transport(paths: Paths | None = None) -> Transport | None:
    paths = paths or Paths()
    cfg = platform_config(paths)
    remote = cfg.get("log_remote") or os.environ.get("AUTOMAGIX_LOG_REMOTE")
    if not remote:
        return None
    if cfg["log_transport"] == "git-ssh":
        return GitSshTransport(remote, cfg["ring"], paths)
    if cfg["log_transport"] == "github-api":
        return GitHubApiTransport(remote, cfg["ring"], paths)
    return DirTransport(remote, cfg["ring"])


# ---- batching: collect a process's artefacts and send them in ONE commit at the end --------------------------------------------
_BATCH = {"on": False}


def enable_batching() -> None:
    """Called by ``python -m wrapper_core`` (never by tests or library users): artefacts are then written to the durable spool and
    sent together by ``flush_batch`` when the process ends, as one commit per run instead of one per artefact."""
    _BATCH["on"] = True


def batching_enabled() -> bool:
    return bool(_BATCH["on"])


def flush_batch(paths: Paths | None = None) -> int:
    """Send everything pending (this run's artefacts plus any older spool) with as few commits as possible. Never raises."""
    if not _BATCH["on"]:
        return 0
    try:
        return flush_spool(paths, max_items=400, jitter=True)
    except Exception:  # noqa: BLE001 - logging must never break a run
        return 0


# ---- state: authentication failures, blocking, spool ----------------------------------------------------------------------------
BLOCK_SECONDS = {"rejected": 1800, "undecryptable": 6 * 3600, "missing": 6 * 3600}
SPOOL_MAX_AGE_S = 14 * 86400
SPOOL_MAX_BYTES = 100 * 1024 * 1024
MAX_RUN_RECORD_BYTES = 900_000       # the ingest limit for a run record is 1,000,000 bytes
MAX_RUN_EVENTS = 4000                # the ingest limit is 5,000 events


def _retry_scale() -> float:
    try:
        return max(0.0, float(os.environ.get("AUTOMAGIX_RETRY_SCALE", "1")))
    except ValueError:
        return 1.0


def _state_file(paths: Paths, name: str) -> Path:
    return paths.state / name


def note_auth_failure(paths: Paths, http: int, reason: str) -> None:
    """Remember that the logging credential does not work (doctor and the heartbeat surface this) and pause network attempts for a while."""
    try:
        paths.state.mkdir(parents=True, exist_ok=True)
        now = utcnow()
        atomic_write_text(_state_file(paths, "logging_auth_failed.json"), json.dumps({"ts": iso(now), "http": int(http), "reason": reason}))
        until = now + timedelta(seconds=BLOCK_SECONDS.get(reason, 1800))
        atomic_write_text(_state_file(paths, "logging_blocked.json"), json.dumps({"until": iso(until), "reason": reason}))
    except OSError:
        pass


def clear_auth_failure(paths: Paths) -> None:
    for name in ("logging_auth_failed.json", "logging_blocked.json"):
        try:
            _state_file(paths, name).unlink()
        except OSError:
            pass


def auth_failure(paths: Paths) -> dict | None:
    try:
        data = json.loads(_state_file(paths, "logging_auth_failed.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def blocked(paths: Paths) -> bool:
    """True while network attempts are paused after an authentication failure (spool only; retried once the pause ends)."""
    try:
        data = json.loads(_state_file(paths, "logging_blocked.json").read_text(encoding="utf-8"))
        return parse_iso(data["until"]) > utcnow()
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _spool_file(paths: Paths, kind: str, art_id: str) -> Path:
    return paths.spool / f"{safe_name(kind)}-{safe_name(art_id)}.json"


def spool_stats(paths: Paths) -> dict:
    count, size, oldest = 0, 0, None
    try:
        for f in paths.spool.glob("*.json"):
            st = f.stat()
            count += 1
            size += st.st_size
            oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
    except OSError:
        pass
    return {"count": count, "bytes": size, "oldest_age_s": int(max(0, time.time() - oldest)) if oldest else 0}


def cap_spool(paths: Paths, max_age_s: int = SPOOL_MAX_AGE_S, max_bytes: int = SPOOL_MAX_BYTES) -> dict:
    """A machine that cannot deliver (token expired, offline for weeks) must not fill its disk: drop what is older than
    ``max_age_s``, then oldest-first - evidence before everything else - until the spool fits in ``max_bytes``."""
    dropped = {"age": 0, "size": 0}
    try:
        files = [(f, f.stat()) for f in paths.spool.glob("*.json")]
    except OSError:
        return dropped
    now = time.time()
    keep = []
    for f, st in files:
        if now - st.st_mtime > max_age_s:
            try:
                f.unlink()
                dropped["age"] += 1
            except OSError:
                keep.append((f, st))
        else:
            keep.append((f, st))
    total = sum(st.st_size for _, st in keep)
    if total > max_bytes:
        order = sorted(keep, key=lambda x: (not x[0].name.startswith("evidence-"), x[1].st_mtime))
        for f, st in order:
            if total <= max_bytes:
                break
            try:
                f.unlink()
                total -= st.st_size
                dropped["size"] += 1
            except OSError:
                pass
    return dropped


def _maybe_cap_spool(paths: Paths, every_s: int = 600) -> None:
    stamp = paths.spool / ".capped"
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < every_s:
            return
        cap_spool(paths)
        stamp.write_text("", encoding="utf-8")
    except OSError:
        pass


def art_time(kind: str, payload: dict, art_id: str = "") -> datetime:
    """When the artefact happened (UTC): its own timestamp, else the timestamp prefix of its id, else now."""
    raw = payload.get("started_at") if kind == "run" else payload.get("ts")
    for text in (raw, art_id):
        if isinstance(text, str):
            try:
                return parse_iso(text).astimezone(timezone.utc)
            except (ValueError, TypeError, OverflowError, OSError):
                pass
            m = _ID_TS.match(text)
            if m:
                try:
                    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
                except ValueError:
                    pass
    return utcnow()


_ID_TS = re.compile(r"^([0-9]{4})([0-9]{2})([0-9]{2})T")


def cap_run_record(rec: dict, limit: int = MAX_RUN_RECORD_BYTES) -> dict:
    """Keep a run record inside the ingest limits (1 MB, 5,000 events): the biggest runs are the ones that matter most, so they must
    arrive with their status, error and synopsis intact; the middle of a long event list is what is dropped (``truncated`` is set)."""
    try:
        events = list(rec.get("events") or [])
        size = len(json.dumps(rec, default=str))
    except (TypeError, ValueError):
        return rec
    if size <= limit and len(events) <= MAX_RUN_EVENTS:
        return rec
    out = dict(rec)
    keep = min(len(events), MAX_RUN_EVENTS)
    while True:
        head = keep // 8
        out["events"] = events[:head] + (events[len(events) - (keep - head):] if keep - head > 0 else [])
        if keep <= 20 or len(json.dumps(out, default=str)) <= limit:
            break
        keep = max(20, keep // 2)
    if len(json.dumps(out, default=str)) > limit:                     # still too large: shrink the free-text fields
        err = out.get("error")
        if isinstance(err, dict):
            err = dict(err)
            if isinstance(err.get("message"), str):
                err["message"] = err["message"][:2000]
            if isinstance(err.get("traceback_tail"), list):
                err["traceback_tail"] = [str(x)[:300] for x in err["traceback_tail"][-20:]]
            out["error"] = err
        if isinstance(out.get("synopsis"), str):
            out["synopsis"] = out["synopsis"][:2000]
    out["truncated"] = True
    out["events_dropped"] = max(0, len(events) - len(out["events"]))
    return out


def _spool_write(paths: Paths, kind: str, name: str, art_id: str, payload: dict) -> None:
    paths.spool.mkdir(parents=True, exist_ok=True)
    atomic_write_text(_spool_file(paths, kind, art_id), json.dumps({"kind": kind, "name": name, "id": art_id, "payload": payload}, default=str))
    _maybe_cap_spool(paths)


def emit(kind: str, name: str, art_id: str, payload: dict, paths: Paths | None = None) -> bool:
    """Send an artefact; on any failure spool it. Returns True if it reached (or, when batching, was queued for) the transport.
    Never raises."""
    paths = paths or Paths()
    log = get_logger(paths)
    try:
        if kind == "run" and isinstance(payload, dict):
            payload = cap_run_record(payload)
        t = get_transport(paths)
        if t is None:
            raise TransportError("no log transport configured (platform.json log_remote)")
        if _BATCH["on"] and kind != "evidence" and hasattr(t, "send_many"):
            _spool_write(paths, kind, name, art_id, payload)    # durable now, delivered with the rest of this run's artefacts
            return True
        if isinstance(t, GitHubApiTransport) and blocked(paths):
            raise TransportError("log sending is paused after an authentication failure (see: wrapper_core doctor); retrying later")
        t.send(kind, name, art_id, payload)
        return True
    except Exception as exc:  # noqa: BLE001 - logging must never break a run
        log.warning("log send failed (%s %s): %s - spooling", kind, art_id, exc)
        try:
            _spool_write(paths, kind, name, art_id, payload)
        except OSError as spool_exc:
            log.error("spool failed: %s", spool_exc)
        return False


def _jitter() -> None:
    """A random pause before a background send so that a fleet that wakes at the same minute does not hit GitHub in the same second."""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("AUTOMAGIX_NO_JITTER"):
        return
    try:
        top = float(os.environ.get("AUTOMAGIX_SEND_JITTER_S", "20"))
    except ValueError:
        top = 20.0
    if top > 0:
        time.sleep(random.uniform(0, top))


def flush_spool(paths: Paths | None = None, max_items: int = 50, jitter: bool = False, force: bool = False) -> int:
    """Retry spooled artefacts (oldest first), bundled when the transport supports it. Returns the number sent."""
    paths = paths or Paths()
    sent = 0
    try:
        t = get_transport(paths)
    except Exception:  # noqa: BLE001
        return 0
    if t is None or not paths.spool.exists():
        return 0
    if isinstance(t, GitHubApiTransport) and not force and blocked(paths):
        return 0                                  # paused after an auth failure; emit()/doctor already say so
    cap_spool(paths)
    files = sorted(paths.spool.glob("*.json"), key=lambda p: p.stat().st_mtime)[:max_items]
    if not files:
        return 0
    if jitter:
        _jitter()
    loaded: list[tuple[Path, dict]] = []
    for f in files:
        try:
            loaded.append((f, json.loads(f.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            try:
                f.unlink()                         # unreadable spool file: nothing can ever deliver it
            except OSError:
                pass
    try:
        if hasattr(t, "send_many") and len(loaded) > 1:
            t.send_many([rec for _, rec in loaded])
            for f, _ in loaded:
                f.unlink()
                sent += 1
        else:
            for f, rec in loaded:
                t.send(rec["kind"], rec["name"], rec["id"], rec["payload"])
                f.unlink()
                sent += 1
    except Exception as exc:  # noqa: BLE001
        get_logger(paths).warning("spool flush stopped: %s", exc)    # transport is down: stop hammering it, keep the files
    return sent
