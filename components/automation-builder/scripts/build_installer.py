#!/usr/bin/env python3
"""Render, build and (optionally) sign the per-automation NSIS installer.

    build_installer.py --config build.json --out dist/ [--sign] [--require-signing] [--render-only]

build.json (all values are data, never code):

    {"automation": "automation-example-hello", "version": "1.0.0",
     "ring": "broad",                          # default ring baked in; /RING=pilot overrides at install time
     "registry_url": "https://registry.example.internal/",
     "log_remote": "git@gitlab.example.internal:platform/automation-logging.git",
     "llm_endpoint": "https://llm.example.internal/v1/chat/completions", "llm_model": "default",
     "publisher": "Automation Platform Team", "icon": "assets/app.ico",
     "wrapper_core_zip": "dist/components/wrapper-core-0.1.0.zip",
     "python_embed_zip": "vendor/python-3.12-embed-amd64.zip",     # optional; else a system Python is required
     "known_hosts_file": "config/known_hosts"}                     # optional (public host keys; may be committed)

Secrets come ONLY from CI file variables, never from build.json or argv, and are staged as separate files that
`makensis` embeds - they never appear in the rendered .nsi text:

    LOGGING_SSH_KEY_FILE      write-only logging deploy key (private key file)
    REGISTRY_READ_TOKEN_FILE  read-only registry token (file) - or REGISTRY_READ_TOKEN (masked variable)
    KNOWN_HOSTS_FILE          overrides known_hosts_file
    SIGNING_PFX_FILE + SIGNING_PFX_PASSWORD_FILE|SIGNING_PFX_PASSWORD  | SIGNING_THUMBPRINT   (+ SIGNING_TIMESTAMP_URL)

Output: <out>/installers/<automation>-<version>-setup.exe and .sha256 (the registry's path convention).
Staged secrets are wiped in a finally block.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _wc import versions  # noqa: E402
import validate_nsi  # noqa: E402

TEMPLATE = HERE.parent / "installer" / "autostation.nsi.in"
PS_SCRIPTS = HERE.parent / "installer" / "scripts"
STAGE_SCRIPTS = ["install-check.ps1", "protect-secret.ps1", "restrict-acl.ps1", "set-credential.ps1",
                 "configure-python.ps1", "write-platform-config.ps1", "first-run.ps1"]
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LOG_REMOTE_RE = re.compile(r"^(git@[A-Za-z0-9.-]+:[\w./~-]+|ssh://[\w.@:/~-]+|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$")  # ssh remote, or owner/repo (github-api)
URL_RE = re.compile(r"^(https?://|mock://)\S+$")
RINGS = ("dev", "pilot", "broad")


class BuildError(RuntimeError):
    pass


# --- config ---------------------------------------------------------------------------------------
@dataclass
class Config:
    automation: str
    version: str
    registry_url: str
    log_remote: str
    ring: str = "broad"
    llm_endpoint: str = ""
    llm_model: str = ""
    publisher: str = "AUTOMAGIX"
    icon: str = ""
    wrapper_core_zip: str = ""
    framework_zip: str = ""       # optional: execution-framework (Node + node_modules) bundled in the installer
    automation_zip: str = ""      # optional: payload bundled INSIDE the installer (offline-capable first install)
    python_embed_zip: str = ""
    known_hosts_file: str = ""
    base_dir: Path = field(default_factory=Path.cwd)


def _clean(label: str, value: str, allow_empty: bool = False) -> str:
    value = "" if value is None else str(value)
    if not value and not allow_empty:
        raise BuildError(f"config: {label} is required")
    if re.search(r"[\x00-\x1f\"`]", value):
        raise BuildError(f"config: {label} contains control characters, quotes or backticks")
    return value


def load_config(src: Path | dict, base_dir: Path | None = None) -> Config:
    if isinstance(src, dict):
        raw, base = dict(src), (base_dir or Path.cwd())
    else:
        base = base_dir or Path(src).resolve().parent
        try:
            raw = json.loads(Path(src).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BuildError(f"cannot read build config {src}: {exc}") from exc
    unknown = set(raw) - set(Config.__dataclass_fields__) - {"base_dir"}
    if unknown:
        raise BuildError(f"config: unknown keys {sorted(unknown)}")
    name = _clean("automation", raw.get("automation"))
    if not NAME_RE.match(name):
        raise BuildError(f"config: invalid automation name {name!r}")
    version = _clean("version", raw.get("version"))
    try:
        versions.parse(version)
    except ValueError as exc:
        raise BuildError(f"config: {exc}") from exc
    ring = raw.get("ring", "broad")
    if ring not in RINGS:
        raise BuildError(f"config: ring must be one of {RINGS}")
    registry = _clean("registry_url", raw.get("registry_url")).rstrip("\\")
    log_remote = _clean("log_remote", raw.get("log_remote"))
    dev_dir_remote = ring == "dev" and re.match(r"^([A-Za-z]:[\\/]|\\\\|/)", log_remote)  # dev ring may log to a share
    if log_remote != "local" and not LOG_REMOTE_RE.match(log_remote) and not dev_dir_remote:
        raise BuildError("config: log_remote must be an ssh git remote (git@host:path.git or ssh://...), or 'local' "
                         "(logs stay in <install dir>/local-logs: a standalone install with no logging server)")
    llm = _clean("llm_endpoint", raw.get("llm_endpoint", ""), allow_empty=True)
    if llm and not URL_RE.match(llm):
        raise BuildError("config: llm_endpoint must be an http(s):// URL")
    return Config(automation=name, version=version, registry_url=registry, log_remote=log_remote, ring=ring,
                  llm_endpoint=llm, llm_model=_clean("llm_model", raw.get("llm_model", ""), allow_empty=True),
                  publisher=_clean("publisher", raw.get("publisher", "AUTOMAGIX")), icon=raw.get("icon", "") or "",
                  wrapper_core_zip=raw.get("wrapper_core_zip", "") or "", automation_zip=raw.get("automation_zip", "") or "", framework_zip=raw.get("framework_zip", "") or "", python_embed_zip=raw.get("python_embed_zip", "") or "",
                  known_hosts_file=raw.get("known_hosts_file", "") or "", base_dir=base)


# --- NSIS rendering -----------------------------------------------------------------------------------
def nsis_escape(value: str) -> str:
    """Escape for use inside a double-quoted NSIS string."""
    return value.replace("$", "$$").replace('"', '$\\"').replace("\r", "$\\r").replace("\n", "$\\n")


def vi_version(version: str) -> str:
    """NSIS VIProductVersion must be numeric a.b.c.d (prerelease/build metadata dropped)."""
    major, minor, patch = versions.parse(version)[:3]
    return f"{major}.{minor}.{patch}.0"


def numeric_version(version: str) -> str:
    major, minor, patch = versions.parse(version)[:3]
    return f"{major}.{minor}.{patch}"


def render_nsi(cfg: Config, wrapper_version: str, flags: dict[str, bool], outfile: str, stage_dir: str = ".",
               icon: str = "") -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    defines = "\n".join(f"!define {k}" for k, v in sorted(flags.items()) if v)
    values = {
        "DEFINES": defines, "AUTOMATION": cfg.automation, "VERSION": cfg.version, "VIVERSION": vi_version(cfg.version),
        "PUBLISHER": cfg.publisher, "DEFAULT_RING": cfg.ring, "REGISTRY_URL": cfg.registry_url, "LOG_REMOTE": cfg.log_remote,
        "LLM_ENDPOINT": cfg.llm_endpoint, "LLM_MODEL": cfg.llm_model, "WRAPPER_VERSION": numeric_version(wrapper_version),
        "STAGE_DIR": stage_dir, "OUTFILE": outfile, "ICON": icon,
        "GIT_ARG": "" if cfg.log_remote.startswith(("git@", "ssh://")) else "-SkipGit",
    }
    out = tpl
    for key, val in values.items():
        raw = val if key == "DEFINES" else nsis_escape(val)
        out = out.replace(f"@@{key}@@", raw)
    return out


# --- staging ----------------------------------------------------------------------------------------
def safe_extract(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if target != root and root not in target.parents:
                raise BuildError(f"unsafe path in archive {zip_path.name}: {info.filename}")
        z.extractall(dest)


def _resolve(cfg: Config, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (cfg.base_dir / p)


def _read_env_file(env: dict, file_var: str, value_var: str | None = None) -> str | None:
    """Return the secret text from a CI file variable (preferred) or a masked value variable."""
    f = env.get(file_var)
    if f:
        p = Path(f)
        if not p.is_file():
            raise BuildError(f"{file_var} points at a missing file")
        return p.read_text(encoding="utf-8", errors="strict")
    if value_var and env.get(value_var):
        return env[value_var]
    return None


def _write_private(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    b = data.encode("utf-8") if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(b)


def wipe_tree_secrets(stage: Path) -> None:
    sec = stage / "secrets"
    if sec.exists():
        for f in sec.rglob("*"):
            if f.is_file():
                try:
                    f.chmod(stat.S_IWRITE | stat.S_IREAD)
                    size = f.stat().st_size
                    with open(f, "r+b") as h:
                        h.write(b"\0" * size)
                except OSError:
                    pass
        shutil.rmtree(sec, ignore_errors=True)


def stage_build(cfg: Config, stage: Path, env: dict, allow_missing_secrets: bool) -> tuple[dict, str]:
    """Create the staging dir NSIS embeds from. Returns (flags, wrapper_version)."""
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    flags = {"HAVE_FRAMEWORK": False, "HAVE_LOG_TOKEN": False, "HAVE_PAYLOAD": False, "HAVE_ICON": False, "HAVE_PYTHON_EMBED": False, "HAVE_SSH_KEY": False, "HAVE_TOKEN": False, "HAVE_KNOWN_HOSTS": False}

    # scripts
    (stage / "scripts").mkdir()
    for name in STAGE_SCRIPTS:
        src = PS_SCRIPTS / name
        if not src.exists():
            raise BuildError(f"installer script missing: {src}")
        shutil.copyfile(src, stage / "scripts" / name)

    # wrapper-core bootstrap payload
    if not cfg.wrapper_core_zip:
        raise BuildError("config: wrapper_core_zip is required (build it with package.py component wrapper-core ...)")
    wz = _resolve(cfg, cfg.wrapper_core_zip)
    if not wz.is_file():
        raise BuildError(f"wrapper_core_zip not found: {wz}")
    safe_extract(wz, stage / "wrapper-core")
    vfile = stage / "wrapper-core" / "VERSION"
    if not vfile.exists() or not (stage / "wrapper-core" / "wrapper_core" / "__init__.py").exists():
        raise BuildError("wrapper_core_zip must contain VERSION and wrapper_core/__init__.py (use package.py)")
    wrapper_version = vfile.read_text(encoding="utf-8").strip()
    versions.parse(wrapper_version)

    # bundled execution-framework (Node runtime + playwright-core): lets browser automations run with nothing pre-installed
    if cfg.framework_zip:
        fz = _resolve(cfg, cfg.framework_zip)
        if not fz.is_file():
            raise BuildError(f"framework_zip not found: {fz}")
        safe_extract(fz, stage / "execution-framework")
        if not (stage / "execution-framework" / "VERSION").exists() or not (stage / "execution-framework" / "index.js").exists():
            raise BuildError("framework_zip must contain VERSION and index.js (use package.py component execution-framework ...)")
        flags["HAVE_FRAMEWORK"] = True

    # bundled automation payload: the installer itself is the trust root, so a first install works with no registry
    if cfg.automation_zip:
        az = _resolve(cfg, cfg.automation_zip)
        if not az.is_file():
            raise BuildError(f"automation_zip not found: {az}")
        with zipfile.ZipFile(az) as zf:
            if "automation.yaml" not in zf.namelist():
                raise BuildError("automation_zip must contain automation.yaml at its root (use package.py automation ...)")
        (stage / "bundled").mkdir()
        shutil.copyfile(az, stage / "bundled" / f"{cfg.automation}.zip")
        meta = {"name": cfg.automation, "version": cfg.version, "sha256": hashlib.sha256(az.read_bytes()).hexdigest()}
        (stage / "bundled" / f"{cfg.automation}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        flags["HAVE_PAYLOAD"] = True

    # interpreter
    if cfg.python_embed_zip:
        pz = _resolve(cfg, cfg.python_embed_zip)
        if not pz.is_file():
            raise BuildError(f"python_embed_zip not found: {pz}")
        safe_extract(pz, stage / "python")
        if not (stage / "python" / "python.exe").exists():
            raise BuildError("python_embed_zip must contain python.exe at its root (python.org 'embeddable package')")
        flags["HAVE_PYTHON_EMBED"] = True

    # known_hosts (public)
    kh = env.get("KNOWN_HOSTS_FILE") or (str(_resolve(cfg, cfg.known_hosts_file)) if cfg.known_hosts_file else "")
    if kh:
        if not Path(kh).is_file():
            raise BuildError("known_hosts file not found")
        (stage / "config").mkdir()
        shutil.copyfile(kh, stage / "config" / "known_hosts")
        flags["HAVE_KNOWN_HOSTS"] = True

    # icon
    if cfg.icon:
        ip = _resolve(cfg, cfg.icon)
        if not ip.is_file():
            raise BuildError(f"icon not found: {ip}")
        shutil.copyfile(ip, stage / "icon.ico")
        flags["HAVE_ICON"] = True

    # secrets
    key = _read_env_file(env, "LOGGING_SSH_KEY_FILE")
    token = _read_env_file(env, "REGISTRY_READ_TOKEN_FILE", "REGISTRY_READ_TOKEN")
    if key is not None:
        if "PRIVATE KEY" not in key:
            raise BuildError("LOGGING_SSH_KEY_FILE does not look like a private key")
        _write_private(stage / "secrets" / "logging_key", key.rstrip("\n") + "\n")
        flags["HAVE_SSH_KEY"] = True
    log_token = _read_env_file(env, "LOGGING_TOKEN_FILE", "LOGGING_TOKEN")
    if log_token is not None:
        log_token = log_token.strip()
        if not log_token or "\n" in log_token:
            raise BuildError("logging token must be a single non-empty line")
        _write_private(stage / "secrets" / "logging_token", log_token)
        flags["HAVE_LOG_TOKEN"] = True
    if token is not None:
        token = token.strip()
        if not token or "\n" in token:
            raise BuildError("registry token must be a single non-empty line")
        _write_private(stage / "secrets" / "registry_token", token)
        flags["HAVE_TOKEN"] = True
    if not allow_missing_secrets:
        missing = [n for n, ok in (("LOGGING_SSH_KEY_FILE", flags["HAVE_SSH_KEY"]), ("REGISTRY_READ_TOKEN_FILE", flags["HAVE_TOKEN"])) if not ok]
        if missing:
            raise BuildError(f"required secrets not provided: {', '.join(missing)} (use --allow-missing-secrets for a dev build "
                             "that cannot ship logs / read the registry)")
    return flags, wrapper_version


# --- signing ---------------------------------------------------------------------------------------
@dataclass
class SignConfig:
    pfx_file: str = ""
    pfx_password: str = ""
    pfx_password_file: str = ""
    thumbprint: str = ""
    timestamp_url: str = ""
    timestamp_kind: str = "rfc3161"

    @property
    def configured(self) -> bool:
        return bool(self.thumbprint or (self.pfx_file and (self.pfx_password or self.pfx_password_file)))


@dataclass
class SignPlan:
    argv: list[str]
    env: dict
    cleanup: list[Path] = field(default_factory=list)
    output: Path | None = None   # osslsigncode writes a new file that replaces the installer


def sign_config_from_env(env: dict) -> SignConfig:
    return SignConfig(pfx_file=env.get("SIGNING_PFX_FILE", ""), pfx_password=env.get("SIGNING_PFX_PASSWORD", ""),
                      pfx_password_file=env.get("SIGNING_PFX_PASSWORD_FILE", ""), thumbprint=env.get("SIGNING_THUMBPRINT", ""),
                      timestamp_url=env.get("SIGNING_TIMESTAMP_URL", ""), timestamp_kind=env.get("SIGNING_TIMESTAMP_KIND", "rfc3161"))


def build_sign_command(exe: Path, cfg: SignConfig, signer: str, tmpdir: Path, display_name: str = "AUTOMAGIX") -> SignPlan:
    """Construct the signing command. Passwords go via a file (osslsigncode -readpass) or the child's environment
    (sign.ps1 -PfxPasswordEnv) - NEVER argv, so process listings and CI logs cannot leak them."""
    if signer == "osslsigncode":
        if not cfg.pfx_file:
            raise BuildError("osslsigncode signing needs SIGNING_PFX_FILE (thumbprint signing is Windows-only)")
        cleanup: list[Path] = []
        pw_file = cfg.pfx_password_file
        if not pw_file:
            if not cfg.pfx_password:
                raise BuildError("osslsigncode signing needs SIGNING_PFX_PASSWORD_FILE or SIGNING_PFX_PASSWORD")
            pwp = tmpdir / "pfx.pass"
            _write_private(pwp, cfg.pfx_password)
            pw_file, cleanup = str(pwp), [pwp]
        signed = exe.with_suffix(".signed.exe")
        argv = ["osslsigncode", "sign", "-pkcs12", cfg.pfx_file, "-readpass", pw_file, "-h", "sha256", "-n", display_name]
        if cfg.timestamp_url:
            argv += ["-ts" if cfg.timestamp_kind == "rfc3161" else "-t", cfg.timestamp_url]
        argv += ["-in", str(exe), "-out", str(signed)]
        return SignPlan(argv, {}, cleanup, signed)
    if signer == "powershell":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                str(PS_SCRIPTS / "sign.ps1"), "-File", str(exe)]
        env: dict = {}
        if cfg.thumbprint:
            argv += ["-Thumbprint", cfg.thumbprint]
        else:
            pw = cfg.pfx_password
            if not pw and cfg.pfx_password_file:
                pw = Path(cfg.pfx_password_file).read_text(encoding="utf-8").strip()
            argv += ["-PfxFile", cfg.pfx_file, "-PfxPasswordEnv", "AMX_SIGN_PFX_PASSWORD"]
            env["AMX_SIGN_PFX_PASSWORD"] = pw
        if cfg.timestamp_url:
            argv += ["-TimestampUrl", cfg.timestamp_url]
        return SignPlan(argv, env)
    raise BuildError(f"unknown signer {signer!r}")


def default_signer() -> str:
    return "powershell" if sys.platform == "win32" else "osslsigncode"


def sign_installer(exe: Path, cfg: SignConfig, signer: str, runner=subprocess.run) -> None:
    with tempfile.TemporaryDirectory(prefix="amx-sign-") as td:
        plan = build_sign_command(exe, cfg, signer, Path(td))
        print("signing: " + " ".join(plan.argv), flush=True)  # argv contains no secrets by construction
        try:
            r = runner(plan.argv, env={**os.environ, **plan.env}, capture_output=True, text=True)
        finally:
            for f in plan.cleanup:
                f.unlink(missing_ok=True)
        if r.returncode != 0:
            raise BuildError(f"signing failed ({signer}): {(r.stderr or r.stdout).strip()[:400]}")
        if plan.output is not None:
            if not plan.output.exists():
                raise BuildError("signer reported success but produced no output")
            os.replace(plan.output, exe)


# --- build ---------------------------------------------------------------------------------------------
def find_makensis(explicit: str | None) -> str | None:
    if explicit:
        return explicit if (Path(explicit).exists() or shutil.which(explicit)) else None
    found = shutil.which("makensis") or shutil.which("makensis.exe")
    if found:
        return found
    for cand in (r"C:\Program Files (x86)\NSIS\makensis.exe", r"C:\Program Files\NSIS\makensis.exe"):
        if Path(cand).exists():
            return cand
    return None


def run_build(cfg: Config, out: Path, env: dict, *, sign: bool = False, require_signing: bool = False, render_only: bool = False,
              allow_missing_secrets: bool = False, makensis: str | None = None, signer: str | None = None,
              keep_stage: bool = False, runner=subprocess.run) -> dict:
    sign_cfg = sign_config_from_env(env)
    if require_signing and not sign_cfg.configured:
        raise BuildError("signing is required (--require-signing) but no signing certificate is configured "
                         "(SIGNING_PFX_FILE + SIGNING_PFX_PASSWORD_FILE, or SIGNING_THUMBPRINT)")
    do_sign = sign or require_signing
    if do_sign and not sign_cfg.configured:
        print("WARNING: --sign requested but no signing certificate is configured: building an UNSIGNED installer. "
              "AV/EDR and SmartScreen will flag it. Never distribute this build.", file=sys.stderr)
        do_sign = False

    inst_dir = Path(out) / "installers"
    inst_dir.mkdir(parents=True, exist_ok=True)
    exe = (inst_dir / f"{cfg.automation}-{cfg.version}-setup.exe").resolve()
    stage = Path(tempfile.mkdtemp(prefix=f"amx-stage-{cfg.automation}-"))
    result: dict = {"automation": cfg.automation, "version": cfg.version, "signed": False, "built": False}
    try:
        flags, wrapper_version = stage_build(cfg, stage / "s", env, allow_missing_secrets)
        sdir = stage / "s"
        nsi_text = render_nsi(cfg, wrapper_version, flags, str(exe), ".", icon="icon.ico" if flags["HAVE_ICON"] else "")
        nsi = sdir / "installer.nsi"
        nsi.write_text(nsi_text, encoding="utf-8")
        problems = validate_nsi.validate(nsi)
        if problems:
            raise BuildError("rendered installer failed validation:\n  " + "\n  ".join(problems))
        result["nsi_sha256"] = hashlib.sha256(nsi_text.encode()).hexdigest()
        result["flags"] = {k: v for k, v in flags.items() if v}
        if keep_stage or render_only:
            kept = Path(out) / "installers" / f"{cfg.automation}-{cfg.version}.nsi"
            kept.write_text(nsi_text, encoding="utf-8")   # safe to keep: contains no secrets by construction
            result["nsi"] = str(kept)
        if render_only:
            return result
        mk = find_makensis(makensis)
        if not mk:
            raise BuildError("makensis not found (install NSIS or use the automation-builder Docker image); "
                             "use --render-only to render + validate without compiling")
        r = runner([mk, "-V2", "-INPUTCHARSET", "UTF8", str(nsi)], cwd=str(sdir), capture_output=True, text=True)
        if r.returncode != 0 or not exe.exists():
            raise BuildError(f"makensis failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-800:]}")
        result["built"] = True
        if do_sign:
            sign_installer(exe, sign_cfg, signer or default_signer(), runner=runner)
            result["signed"] = True
        digest = hashlib.sha256(exe.read_bytes()).hexdigest()
        (exe.parent / (exe.name + ".sha256")).write_text(f"{digest}  {exe.name}\n", encoding="utf-8")
        result.update(path=str(exe), sha256=digest, size=exe.stat().st_size,
                      url=f"installers/{exe.name}")
        return result
    finally:
        wipe_tree_secrets(stage / "s")
        if not keep_stage:
            shutil.rmtree(stage, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ring", choices=RINGS, help="override the default ring baked into the installer")
    ap.add_argument("--sign", action="store_true", help="sign the installer if a certificate is configured (warn otherwise)")
    ap.add_argument("--require-signing", action="store_true", help="release mode: fail closed if signing is not configured")
    ap.add_argument("--signer", choices=["osslsigncode", "powershell"])
    ap.add_argument("--render-only", action="store_true", help="render + validate the .nsi, do not compile")
    ap.add_argument("--allow-missing-secrets", action="store_true", help="dev builds only")
    ap.add_argument("--makensis")
    ap.add_argument("--keep-stage", action="store_true")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(Path(args.config))
        if args.ring:
            cfg.ring = args.ring
        res = run_build(cfg, Path(args.out), dict(os.environ), sign=args.sign, require_signing=args.require_signing,
                        render_only=args.render_only, allow_missing_secrets=args.allow_missing_secrets, makensis=args.makensis,
                        signer=args.signer, keep_stage=args.keep_stage)
    except BuildError as exc:
        print(f"build_installer.py: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
