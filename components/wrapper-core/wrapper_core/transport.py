"""Log transports (SPEC §7).

``git-ssh``: write-only *by construction*. The endpoint never fetches or reads the logging repo; it creates a
throwaway repo, commits one artefact and pushes the orphan commit to a brand-new unique ref
``refs/heads/ingest/<ring>/<kind>/<name>/<id>``. GitLab push rules allow only that pattern, so a stolen key can only
create junk refs.

``dir``: same relative layout on a local/shared directory (dev ring, tests, offline).

Failures never fail a run: artefacts are spooled and flushed on the next run/login.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import secrets
from .paths import Paths, get_logger, platform_config
from .util import atomic_write_text, safe_name


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

    One new file per artefact at ``ingest/<ring>/<kind>/<name>/<id>.json`` on the branch ``ingest`` (created on first use).
    The token is a fine-grained PAT limited to that one repo (contents: write), DPAPI-protected on the endpoint. Protect the
    logs repo's default branch with a ruleset so the token can only ever add logs. Re-sending an existing artefact is a
    no-op (HTTP 422 'sha wasn't supplied' = it is already there).
    """

    def __init__(self, repo: str, ring: str, paths: Paths | None = None, branch: str = "ingest", timeout_s: int = 30):
        super().__init__(ring)
        self.repo, self.paths, self.branch, self.timeout_s = repo, paths or Paths(), branch, timeout_s
        self.api = os.environ.get("AUTOMAGIX_GITHUB_API", "https://api.github.com").rstrip("/")
        self._branch_ready = False

    def _call(self, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        import urllib.error
        import urllib.request
        token = secrets.get_logging_token(self.paths)
        if not token:
            raise TransportError("logging token missing (secrets/logging_token.dpapi or AUTOMAGIX_LOGGING_TOKEN)")
        req = urllib.request.Request(self.api + url, method=method, data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "autostation-wrapper-core",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:  # noqa: S310 - fixed GitHub API host
                raw = r.read().decode("utf-8") or "{}"
                return r.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                detail = {}
            return exc.code, detail
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"GitHub API unreachable: {exc}") from exc

    def _bootstrap_empty_repo(self) -> None:
        """Create the first commit (a README on the default branch) so branches can exist. Idempotent: 422 = another machine won the race."""
        import base64
        text = "# AutoStation run logs\n\nWritten by installed automations (branch `ingest`). Private; do not edit by hand.\n"
        code, detail = self._call("PUT", f"/repos/{self.repo}/contents/README.md",
                                  {"message": "initialise logs repo", "content": base64.b64encode(text.encode("utf-8")).decode("ascii")})
        if code not in (200, 201, 409, 422):
            raise TransportError(f"cannot initialise empty logs repo {self.repo}: HTTP {code} {str(detail.get('message', ''))[:100]}")

    def _ensure_branch(self) -> None:
        if self._branch_ready:
            return
        code, detail = self._call("GET", f"/repos/{self.repo}/git/ref/heads/{self.branch}")
        if code == 409 and "empty" in str(detail.get("message", "")).lower():
            self._bootstrap_empty_repo()          # a brand-new logs repo has no commits at all: the API answers 409 until one exists
            code, detail = self._call("GET", f"/repos/{self.repo}/git/ref/heads/{self.branch}")
        if code == 404:
            code, repo = self._call("GET", f"/repos/{self.repo}")
            if code != 200:
                raise TransportError(f"cannot read repo {self.repo}: HTTP {code}")
            default = repo.get("default_branch", "main")
            code, ref = self._call("GET", f"/repos/{self.repo}/git/ref/heads/{default}")
            if code != 200:
                raise TransportError(f"logs repo {self.repo} has no commits yet: HTTP {code}")
            code, _ = self._call("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{self.branch}", "sha": ref["object"]["sha"]})
            if code not in (201, 422):  # 422 = created concurrently by another machine
                raise TransportError(f"cannot create branch {self.branch}: HTTP {code}")
        elif code != 200:
            raise TransportError(f"cannot read branch {self.branch}: HTTP {code}")
        self._branch_ready = True

    def send(self, kind: str, name: str, art_id: str, payload: dict) -> None:
        import base64
        import time
        self._ensure_branch()
        path = f"ingest/{safe_name(self.ring)}/{rel_path(kind, name, art_id)}"
        body = {"message": f"{kind} {name} {art_id}", "branch": self.branch,
                "content": base64.b64encode((json.dumps(payload, indent=2, default=str) + "\n").encode("utf-8")).decode("ascii")}
        last = ""
        for attempt in range(4):
            code, detail = self._call("PUT", f"/repos/{self.repo}/contents/{path}", body)
            if code in (200, 201):
                return
            if code == 422 and "sha" in str(detail.get("message", "")):
                return  # already uploaded (idempotent re-send)
            last = f"HTTP {code} {str(detail.get('message', ''))[:120]}"
            if code in (409, 429, 500, 502, 503, 504) or (code == 403 and "rate" in last.lower()):
                time.sleep(min(2 ** attempt, 8))  # concurrent commit on the same branch / throttling: retry
                continue
            break
        raise TransportError(f"GitHub upload failed: {last}")


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


def _spool_file(paths: Paths, kind: str, art_id: str) -> Path:
    return paths.spool / f"{safe_name(kind)}-{safe_name(art_id)}.json"


def emit(kind: str, name: str, art_id: str, payload: dict, paths: Paths | None = None) -> bool:
    """Send an artefact; on any failure spool it. Returns True if it reached the transport. Never raises."""
    paths = paths or Paths()
    log = get_logger(paths)
    try:
        t = get_transport(paths)
        if t is None:
            raise TransportError("no log transport configured (platform.json log_remote)")
        t.send(kind, name, art_id, payload)
        return True
    except Exception as exc:  # noqa: BLE001 - logging must never break a run
        log.warning("log send failed (%s %s): %s - spooling", kind, art_id, exc)
        try:
            paths.spool.mkdir(parents=True, exist_ok=True)
            atomic_write_text(_spool_file(paths, kind, art_id),
                              json.dumps({"kind": kind, "name": name, "id": art_id, "payload": payload}, default=str))
        except OSError as spool_exc:
            log.error("spool failed: %s", spool_exc)
        return False


def flush_spool(paths: Paths | None = None, max_items: int = 50) -> int:
    """Retry spooled artefacts (oldest first). Returns the number sent."""
    paths = paths or Paths()
    sent = 0
    try:
        t = get_transport(paths)
    except Exception:  # noqa: BLE001
        return 0
    if t is None or not paths.spool.exists():
        return 0
    for f in sorted(paths.spool.glob("*.json"), key=lambda p: p.stat().st_mtime)[:max_items]:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            t.send(rec["kind"], rec["name"], rec["id"], rec["payload"])
            f.unlink()
            sent += 1
        except Exception as exc:  # noqa: BLE001
            get_logger(paths).warning("spool flush stopped at %s: %s", f.name, exc)
            break  # transport is down; stop hammering it
    return sent
