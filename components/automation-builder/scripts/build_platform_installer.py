#!/usr/bin/env python3
"""Build the AUTOSTATION platform installer (shared runtime, installed once per machine).

    build_platform_installer.py --config platform-build.json --out dist/ [--sign] [--render-only]

platform-build.json:
    {"version": "1.3.0",
     "ring": "broad",
     "registry_url": "https://registry.example.internal/",
     "log_remote": "git@gitlab.example.internal:platform/automation-logging.git",
     "llm_endpoint": "https://llm.example.internal/v1/chat/completions",
     "llm_model": "default",
     "publisher": "Automation Platform Team",
     "icon": "assets/app.ico",
     "wrapper_core_zip": "dist/components/wrapper-core-0.1.0.zip",
     "framework_zip": "vendor/execution-framework-0.5.0.zip",
     "python_embed_zip": "vendor/python-3.12-embed-amd64.zip",
     "git_zip": "vendor/mingit-2.44.0.zip",
     "known_hosts_file": "config/known_hosts"}

Secrets come ONLY from CI file variables (same pattern as build_installer.py):
    LOGGING_SSH_KEY_FILE, REGISTRY_READ_TOKEN_FILE (or REGISTRY_READ_TOKEN),
    LOGGING_TOKEN_FILE (or LOGGING_TOKEN),
    SIGNING_PFX_FILE + password, or SIGNING_THUMBPRINT.

Output: <out>/installers/autostation-platform-<version>-setup.exe and .sha256
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _wc import versions  # noqa: E402
import build_installer as bi   # noqa: E402 — reuse shared utils
import validate_nsi  # noqa: E402

TEMPLATE = HERE.parent / "installer" / "autostation-platform.nsi.in"
PLATFORM_SCRIPTS = ["install-check.ps1", "protect-secret.ps1", "restrict-acl.ps1",
                    "set-credential.ps1", "configure-python.ps1", "write-platform-config.ps1",
                    "first-run-platform.ps1"]


@dataclass
class PlatformConfig:
    version: str
    registry_url: str
    log_remote: str
    ring: str = "broad"
    llm_endpoint: str = ""
    llm_model: str = ""
    publisher: str = "AUTOMAGIX"
    icon: str = ""
    wrapper_core_zip: str = ""
    framework_zip: str = ""
    python_embed_zip: str = ""
    git_zip: str = ""
    registry_pubkeys: str = ""
    known_hosts_file: str = ""
    base_dir: Path = field(default_factory=Path.cwd)


def load_config(src: Path | dict, base_dir: Path | None = None) -> PlatformConfig:
    if isinstance(src, dict):
        raw, base = dict(src), (base_dir or Path.cwd())
    else:
        base = base_dir or Path(src).resolve().parent
        try:
            raw = json.loads(Path(src).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise bi.BuildError(f"cannot read platform build config {src}: {exc}") from exc

    known = set(PlatformConfig.__dataclass_fields__) | {"base_dir"}
    unknown = set(raw) - known
    if unknown:
        raise bi.BuildError(f"config: unknown keys {sorted(unknown)}")

    version = bi._clean("version", raw.get("version"))
    try:
        versions.parse(version)
    except ValueError as exc:
        raise bi.BuildError(f"config: {exc}") from exc

    ring = raw.get("ring", "broad")
    if ring not in bi.RINGS:
        raise bi.BuildError(f"config: ring must be one of {bi.RINGS}")

    registry = bi._clean("registry_url", raw.get("registry_url")).rstrip("\\")
    log_remote = bi._clean("log_remote", raw.get("log_remote"))
    dev_dir_remote = ring == "dev" and re.match(r"^([A-Za-z]:[\\/]|\\\\|/)", log_remote)
    if log_remote != "local" and not bi.LOG_REMOTE_RE.match(log_remote) and not dev_dir_remote:
        raise bi.BuildError("config: log_remote must be an ssh git remote or 'local'")

    llm = bi._clean("llm_endpoint", raw.get("llm_endpoint", ""), allow_empty=True)
    if llm and not bi.URL_RE.match(llm):
        raise bi.BuildError("config: llm_endpoint must be an http(s):// URL")

    return PlatformConfig(
        version=version, registry_url=registry, log_remote=log_remote, ring=ring,
        llm_endpoint=llm,
        llm_model=bi._clean("llm_model", raw.get("llm_model", ""), allow_empty=True),
        publisher=bi._clean("publisher", raw.get("publisher", "AUTOMAGIX")),
        icon=raw.get("icon", "") or "",
        wrapper_core_zip=raw.get("wrapper_core_zip", "") or "",
        framework_zip=raw.get("framework_zip", "") or "",
        python_embed_zip=raw.get("python_embed_zip", "") or "",
        git_zip=raw.get("git_zip", "") or "",
        registry_pubkeys=bi._pubkeys(raw.get("registry_pubkeys", "")),
        known_hosts_file=raw.get("known_hosts_file", "") or "",
        base_dir=base,
    )


def _resolve(cfg: PlatformConfig, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (cfg.base_dir / p)


def stage_platform_build(cfg: PlatformConfig, stage: Path, env: dict,
                          allow_missing_secrets: bool) -> tuple[dict, str]:
    """Stage the platform installer. Returns (flags, wrapper_version)."""
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    flags = {
        "HAVE_FRAMEWORK": False, "HAVE_GIT": False, "HAVE_PYTHON_EMBED": False,
        "HAVE_SSH_KEY": False, "HAVE_TOKEN": False, "HAVE_LOG_TOKEN": False,
        "HAVE_KNOWN_HOSTS": False, "HAVE_ICON": False,
    }

    # scripts
    ps_dir = HERE.parent / "installer" / "scripts"
    (stage / "scripts").mkdir()
    for name in PLATFORM_SCRIPTS:
        src = ps_dir / name
        if not src.exists():
            raise bi.BuildError(f"platform installer script missing: {src}")
        shutil.copyfile(src, stage / "scripts" / name)

    # wrapper-core
    if not cfg.wrapper_core_zip:
        raise bi.BuildError("config: wrapper_core_zip is required")
    wz = _resolve(cfg, cfg.wrapper_core_zip)
    if not wz.is_file():
        raise bi.BuildError(f"wrapper_core_zip not found: {wz}")
    bi.safe_extract(wz, stage / "wrapper-core")
    vfile = stage / "wrapper-core" / "VERSION"
    if not vfile.exists() or not (stage / "wrapper-core" / "wrapper_core" / "__init__.py").exists():
        raise bi.BuildError("wrapper_core_zip must contain VERSION and wrapper_core/__init__.py")
    wrapper_version = vfile.read_text(encoding="utf-8").strip()
    versions.parse(wrapper_version)

    # execution-framework (optional)
    if cfg.framework_zip:
        fz = _resolve(cfg, cfg.framework_zip)
        if not fz.is_file():
            raise bi.BuildError(f"framework_zip not found: {fz}")
        bi.safe_extract(fz, stage / "execution-framework")
        if not (stage / "execution-framework" / "VERSION").exists():
            raise bi.BuildError("framework_zip must contain VERSION")
        flags["HAVE_FRAMEWORK"] = True

    # MinGit (optional)
    if cfg.git_zip:
        gz = _resolve(cfg, cfg.git_zip)
        if not gz.is_file():
            raise bi.BuildError(f"git_zip not found: {gz}")
        bi.safe_extract(gz, stage / "git")
        if not (stage / "git" / "cmd" / "git.exe").exists():
            raise bi.BuildError("git_zip must be a MinGit archive (cmd/git.exe)")
        flags["HAVE_GIT"] = True

    # Python embed (optional but strongly recommended)
    if cfg.python_embed_zip:
        pz = _resolve(cfg, cfg.python_embed_zip)
        if not pz.is_file():
            raise bi.BuildError(f"python_embed_zip not found: {pz}")
        bi.safe_extract(pz, stage / "python")
        if not (stage / "python" / "python.exe").exists():
            raise bi.BuildError("python_embed_zip must contain python.exe")
        flags["HAVE_PYTHON_EMBED"] = True

    # known_hosts
    kh = env.get("KNOWN_HOSTS_FILE") or (str(_resolve(cfg, cfg.known_hosts_file)) if cfg.known_hosts_file else "")
    if kh:
        if not Path(kh).is_file():
            raise bi.BuildError("known_hosts file not found")
        (stage / "config").mkdir()
        shutil.copyfile(kh, stage / "config" / "known_hosts")
        flags["HAVE_KNOWN_HOSTS"] = True

    # icon
    if cfg.icon:
        ip = _resolve(cfg, cfg.icon)
        if not ip.is_file():
            raise bi.BuildError(f"icon not found: {ip}")
        shutil.copyfile(ip, stage / "icon.ico")
        flags["HAVE_ICON"] = True

    # secrets
    key = bi._read_env_file(env, "LOGGING_SSH_KEY_FILE")
    if key is not None:
        if "PRIVATE KEY" not in key:
            raise bi.BuildError("LOGGING_SSH_KEY_FILE does not look like a private key")
        bi._write_private(stage / "secrets" / "logging_key", key.rstrip("\n") + "\n")
        flags["HAVE_SSH_KEY"] = True

    log_token = bi._read_env_file(env, "LOGGING_TOKEN_FILE", "LOGGING_TOKEN")
    if log_token is not None:
        log_token = log_token.strip()
        if not log_token or "\n" in log_token:
            raise bi.BuildError("logging token must be a single non-empty line")
        bi._write_private(stage / "secrets" / "logging_token", log_token)
        flags["HAVE_LOG_TOKEN"] = True

    token = bi._read_env_file(env, "REGISTRY_READ_TOKEN_FILE", "REGISTRY_READ_TOKEN")
    if token is not None:
        token = token.strip()
        if not token or "\n" in token:
            raise bi.BuildError("registry token must be a single non-empty line")
        bi._write_private(stage / "secrets" / "registry_token", token)
        flags["HAVE_TOKEN"] = True

    if not allow_missing_secrets:
        missing = [n for n, ok in (("LOGGING_SSH_KEY_FILE", flags["HAVE_SSH_KEY"]),
                                   ("REGISTRY_READ_TOKEN_FILE", flags["HAVE_TOKEN"])) if not ok]
        if missing:
            raise bi.BuildError(f"required secrets not provided: {', '.join(missing)} "
                                 "(use --allow-missing-secrets for a dev build)")
    return flags, wrapper_version


def render_platform_nsi(cfg: PlatformConfig, wrapper_version: str, flags: dict[str, bool],
                          outfile: str, stage_dir: str = ".", icon: str = "") -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    defines = "\n".join(f"!define {k}" for k, v in sorted(flags.items()) if v)
    git_arg = "" if (cfg.log_remote.startswith(("git@", "ssh://")) and not flags.get("HAVE_GIT")) else "-SkipGit"
    values = {
        "DEFINES": defines,
        "PLATFORM_VERSION": cfg.version,
        "VIVERSION": bi.vi_version(cfg.version),
        "PUBLISHER": cfg.publisher,
        "DEFAULT_RING": cfg.ring,
        "REGISTRY_URL": cfg.registry_url,
        "LOG_REMOTE": cfg.log_remote,
        "LLM_ENDPOINT": cfg.llm_endpoint,
        "LLM_MODEL": cfg.llm_model,
        "WRAPPER_VERSION": bi.numeric_version(wrapper_version),
        "REGISTRY_PUBKEYS": cfg.registry_pubkeys,
        "GIT_ARG": git_arg,
        "STAGE_DIR": stage_dir,
        "OUTFILE": outfile,
        "ICON": icon,
    }
    out = tpl
    for key, val in values.items():
        raw = val if key == "DEFINES" else bi.nsis_escape(val)
        out = out.replace(f"@@{key}@@", raw)
    return out


def run_platform_build(cfg: PlatformConfig, out: Path, env: dict, *, sign: bool = False,
                        require_signing: bool = False, render_only: bool = False,
                        allow_missing_secrets: bool = False, makensis: str | None = None,
                        signer: str | None = None, keep_stage: bool = False,
                        runner=subprocess.run) -> dict:
    sign_cfg = bi.sign_config_from_env(env)
    if require_signing and not sign_cfg.configured:
        raise bi.BuildError("signing required but no certificate configured")
    do_sign = (sign or require_signing) and sign_cfg.configured
    if (sign or require_signing) and not sign_cfg.configured:
        print("WARNING: --sign requested but no signing certificate configured: building UNSIGNED.", file=sys.stderr)

    inst_dir = Path(out) / "installers"
    inst_dir.mkdir(parents=True, exist_ok=True)
    exe = (inst_dir / f"autostation-platform-{cfg.version}-setup.exe").resolve()
    stage = Path(tempfile.mkdtemp(prefix="amx-platform-stage-"))
    result: dict = {"version": cfg.version, "signed": False, "built": False}
    try:
        flags, wrapper_version = stage_platform_build(cfg, stage / "s", env, allow_missing_secrets)
        sdir = stage / "s"
        nsi_text = render_platform_nsi(cfg, wrapper_version, flags, str(exe), ".",
                                        icon="icon.ico" if flags["HAVE_ICON"] else "")
        nsi = sdir / "installer.nsi"
        nsi.write_text(nsi_text, encoding="utf-8")
        problems = validate_nsi.validate(nsi)
        if problems:
            raise bi.BuildError("rendered platform installer failed validation:\n  " + "\n  ".join(problems))
        result["nsi_sha256"] = hashlib.sha256(nsi_text.encode()).hexdigest()
        result["flags"] = {k: v for k, v in flags.items() if v}
        if keep_stage or render_only:
            kept = inst_dir / f"autostation-platform-{cfg.version}.nsi"
            kept.write_text(nsi_text, encoding="utf-8")
            result["nsi"] = str(kept)
        if render_only:
            return result
        mk = bi.find_makensis(makensis)
        if not mk:
            raise bi.BuildError("makensis not found; use --render-only to validate without compiling")
        r = runner([mk, "-V2", "-INPUTCHARSET", "UTF8", str(nsi)], cwd=str(sdir),
                   capture_output=True, text=True)
        if r.returncode != 0 or not exe.exists():
            raise bi.BuildError(f"makensis failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-800:]}")
        result["built"] = True
        if do_sign:
            bi.sign_installer(exe, sign_cfg, signer or bi.default_signer(), runner=runner)
            result["signed"] = True
        digest = hashlib.sha256(exe.read_bytes()).hexdigest()
        (exe.parent / (exe.name + ".sha256")).write_text(f"{digest}  {exe.name}\n", encoding="utf-8")
        result.update(path=str(exe), sha256=digest, size=exe.stat().st_size,
                      url=f"installers/{exe.name}")
        return result
    finally:
        bi.wipe_tree_secrets(stage / "s")
        if not keep_stage:
            shutil.rmtree(stage, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ring", choices=bi.RINGS, help="override default ring baked into installer")
    ap.add_argument("--sign", action="store_true")
    ap.add_argument("--require-signing", action="store_true")
    ap.add_argument("--signer", choices=["osslsigncode", "powershell"])
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--allow-missing-secrets", action="store_true")
    ap.add_argument("--makensis")
    ap.add_argument("--keep-stage", action="store_true")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(Path(args.config))
        if args.ring:
            cfg.ring = args.ring
        res = run_platform_build(cfg, Path(args.out), dict(os.environ), sign=args.sign,
                                  require_signing=args.require_signing, render_only=args.render_only,
                                  allow_missing_secrets=args.allow_missing_secrets,
                                  makensis=args.makensis, signer=args.signer,
                                  keep_stage=args.keep_stage)
    except bi.BuildError as exc:
        print(f"build_platform_installer.py: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
