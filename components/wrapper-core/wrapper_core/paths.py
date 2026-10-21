"""Endpoint filesystem layout and platform configuration (SPEC §2)."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .util import read_json

import re

RINGS = ("dev", "pilot", "broad")
_NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def check_name(name: str) -> str:
    """Automation/slot names become file names and task names: reject anything that could escape a directory."""
    if not isinstance(name, str) or not _NAME_RX.match(name) or ".." in name:
        raise ValueError(f"invalid name {name!r}: use letters, digits, '.', '_', '-' (max 100)")
    return name

CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")


def home() -> Path:
    env = os.environ.get("AUTOMAGIX_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "AUTOMAGIX"
    return Path.home() / ".automagix"


class Paths:
    """All well-known directories under AUTOMAGIX_HOME. Cheap to construct; creates nothing until `ensure()`."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else home()
        self.config = self.root / "config"
        self.secrets = self.root / "secrets"
        self.wrapper_core = self.root / "wrapper-core"
        self.framework = self.root / "execution-framework"
        self.automations = self.root / "automations"
        self.state = self.root / "state"
        self.spool = self.root / "spool"
        self.runs = self.root / "runs"
        self.logs = self.root / "logs"
        self.bin = self.root / "bin"

    def ensure(self) -> "Paths":
        for d in (self.config, self.secrets, self.wrapper_core, self.framework, self.automations,
                  self.state, self.state / "runs", self.state / "locks", self.state / "toast",
                  self.spool, self.runs, self.logs, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # --- files -----------------------------------------------------------------------------------
    @property
    def platform_json(self) -> Path:
        return self.config / "platform.json"

    @property
    def assets_json(self) -> Path:
        return self.config / "assets.json"

    @property
    def installed_json(self) -> Path:
        return self.state / "installed.json"

    @property
    def manifest_cache(self) -> Path:
        return self.state / "manifest.cache.yaml"

    @property
    def manifest_sig_cache(self) -> Path:
        """The signature document that vouched for ``manifest_cache`` (re-verified every time the cache is read)."""
        return self.state / "manifest.cache.yaml.sig"

    @property
    def manifest_signed_at(self) -> Path:
        """Newest ``signed_at`` ever accepted: the anti-rollback high-water mark."""
        return self.state / "manifest.signed_at"

    @property
    def consumed_triggers(self) -> Path:
        return self.state / "consumed_triggers.json"

    @property
    def known_hosts(self) -> Path:
        return self.config / "known_hosts"

    def automation_dir(self, name: str) -> Path:
        return self.automations / check_name(name)

    def lock_file(self, name: str) -> Path:
        return self.state / "locks" / f"{check_name(name)}.lock"

    def last_run_file(self, name: str) -> Path:
        return self.state / "runs" / f"{check_name(name)}.json"

    def status_file(self, name: str) -> Path:
        return self.state / "toast" / f"{check_name(name)}.status.txt"


def platform_config(paths: Paths | None = None) -> dict:
    """Merged installer-written config with env overrides."""
    paths = paths or Paths()
    cfg = dict(read_json(paths.platform_json, {}) or {})
    env_map = {
        "AUTOMAGIX_RING": "ring",
        "AUTOMAGIX_REGISTRY_URL": "registry_url",
        "AUTOMAGIX_LOG_TRANSPORT": "log_transport",
        "AUTOMAGIX_LOG_REMOTE": "log_remote",
        "AUTOSTATION_LLM_ENDPOINT": "llm_endpoint",
        "AUTOSTATION_LLM_MODEL": "llm_model",
    }
    for env, key in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg.setdefault("ring", "broad")
    remote = cfg.get("log_remote", "")
    cfg.setdefault("log_transport", "git-ssh" if remote.startswith(("git@", "ssh://")) else
                   "github-api" if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", remote) and not remote.startswith(("/", ".")) else "dir")
    if cfg["ring"] not in RINGS:
        raise ValueError(f"invalid ring {cfg['ring']!r}; expected one of {RINGS}")
    return cfg


def get_logger(paths: Paths | None = None) -> logging.Logger:
    """wrapper-core's own diagnostic log (logs\\wrapper.log, rotating). Never raises."""
    log = logging.getLogger("wrapper_core")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        paths = paths or Paths()
        paths.logs.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(paths.logs / "wrapper.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    return log
