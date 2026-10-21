from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WRAPPER_CORE = ROOT.parent / "wrapper-core"
for p in (ROOT / "scripts", ROOT / "prompts", WRAPPER_CORE):
    sys.path.insert(0, str(p))


# Windows PowerShell 5.1 launched from a PowerShell 7 session inherits PS7's PSModulePath and then cannot load
# Microsoft.PowerShell.Security (Get-Acl, Set-Authenticode...). Drop it so powershell.exe uses its own defaults
# (a Task Scheduler / installer launch has a clean environment, so production is unaffected).
os.environ.pop("PSModulePath", None)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("SIGNING_", "LOGGING_SSH", "REGISTRY_READ", "KNOWN_HOSTS", "AUTOMAGIX_")):
            monkeypatch.delenv(k, raising=False)


def make_wrapper_zip(path: Path, version: str = "0.1.0") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("VERSION", version + "\n")
        z.writestr("wrapper_core/__init__.py", "# stub\n")
        z.writestr("wrapper_core/__main__.py", "# stub\n")
    return path


def make_python_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("python.exe", "MZ-stub")
        z.writestr("python312.zip", "stub")
        z.writestr("python312._pth", "python312.zip\n.\n")
    return path


PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRETKEYMATERIAL0123456789abcdefSECRET\n-----END OPENSSH PRIVATE KEY-----\n"
TOKEN = "glpat-SUPERSECRETTOKEN123456789"


@pytest.fixture
def build_dir(tmp_path):
    """A complete sample build: config dict, secret files and the env that points at them."""
    d = tmp_path / "build"
    d.mkdir()
    make_wrapper_zip(d / "wrapper-core-0.1.0.zip")
    make_python_zip(d / "python-embed.zip")
    (d / "known_hosts").write_text("gitlab.example.internal ssh-ed25519 AAAAC3Nza\n")
    (d / "app.ico").write_bytes(b"\0\0\1\0")
    key = d / "id_logging"
    key.write_text(PRIVATE_KEY)
    tok = d / "token.txt"
    tok.write_text(TOKEN)
    cfg = {
        "automation": "automation-example-hello", "version": "1.2.3", "ring": "pilot",
        "registry_url": "https://registry.example.internal/", "log_remote": "git@gitlab.example.internal:platform/automation-logging.git",
        "llm_endpoint": "https://llm.example.internal/v1/chat/completions", "llm_model": "default",
        "publisher": "Automation Platform Team", "icon": "app.ico",
        "wrapper_core_zip": "wrapper-core-0.1.0.zip", "python_embed_zip": "python-embed.zip", "known_hosts_file": "known_hosts",
    }
    env = {"LOGGING_SSH_KEY_FILE": str(key), "REGISTRY_READ_TOKEN_FILE": str(tok)}
    return d, cfg, env
