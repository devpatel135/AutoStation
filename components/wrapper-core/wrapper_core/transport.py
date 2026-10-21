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


class GitSshTransport(Transport):
    def __init__(self, remote: str, ring: str, paths: Paths | None = None, timeout_s: int = 90):
        super().__init__(ring)
        self.remote, self.paths, self.timeout_s = remote, paths or Paths(), timeout_s

    def send(self, kind: str, name: str, art_id: str, payload: dict) -> None:
        git = shutil.which("git")
        if not git:
            raise TransportError("git not found on PATH")
        if self.remote.startswith("-"):
            raise TransportError("refusing remote that looks like an option")
        blob = secrets.logging_key_blob(self.paths)
        if blob is None:
            raise TransportError("logging key blob missing (secrets/logging_key.dpapi)")
        with tempfile.TemporaryDirectory(prefix="amx-log-") as tmp:
            repo = Path(tmp)
            f = repo / rel_path(kind, name, art_id)
            atomic_write_text(f, json.dumps(payload, indent=2, default=str) + "\n")
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            ident = ["-c", "user.name=automagix", "-c", "user.email=automagix@localhost", "-c", "commit.gpgsign=false"]
            self._git(git, repo, ["init", "-q"], env)
            self._git(git, repo, ["add", "-A"], env)
            self._git(git, repo, ident + ["commit", "-q", "-m", f"{kind} {name} {art_id}"], env)
            with secrets.TempPrivateKey(blob, self.paths.secrets / "tmp") as keyfile:
                hosts = self.paths.known_hosts
                ssh = (f'ssh -i "{keyfile}" -o IdentitiesOnly=yes -o BatchMode=yes '
                       f'-o StrictHostKeyChecking={"yes" if hosts.exists() else "accept-new"} '
                       + (f'-o UserKnownHostsFile="{hosts}" ' if hosts.exists() else ""))
                env["GIT_SSH_COMMAND"] = ssh.replace("\\", "/")
                self._git(git, repo, ["push", "-q", self.remote, f"HEAD:refs/heads/{ref_for(self.ring, kind, name, art_id)}"], env)

    def _git(self, git: str, cwd: Path, args: list[str], env: dict) -> None:
        try:
            r = subprocess.run([git] + args, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"git {args[0]} timed out") from exc
        if r.returncode != 0:
            raise TransportError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")


def get_transport(paths: Paths | None = None) -> Transport | None:
    paths = paths or Paths()
    cfg = platform_config(paths)
    remote = cfg.get("log_remote") or os.environ.get("AUTOMAGIX_LOG_REMOTE")
    if not remote:
        return None
    if cfg["log_transport"] == "git-ssh":
        return GitSshTransport(remote, cfg["ring"], paths)
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
