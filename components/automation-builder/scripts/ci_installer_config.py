#!/usr/bin/env python3
"""CI glue: fetch the bootstrap wrapper-core from the registry and generate build.json for build_installer.py.

    ci_installer_config.py wrapper-core --out dist/bootstrap [--ring dev]
    ci_installer_config.py config --out dist/build.json

All inputs are CI variables (never argv): REGISTRY_URL, REGISTRY_READ_TOKEN_FILE (or REGISTRY_READ_TOKEN),
BUILD_RING (default dev), AUTOMATION_NAME, AUTOMATION_VERSION, LOG_REMOTE, LLM_ENDPOINT, LLM_MODEL, PUBLISHER,
ICON_FILE, PYTHON_EMBED_ZIP, KNOWN_HOSTS_FILE.

The wrapper-core bundled into an installer is whatever version the registry assigns to the installer's ring; self-heal keeps
it current afterwards, so the bundled copy only needs to be good enough to bootstrap.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wc import miniyaml  # noqa: E402
from wrapper_core import selfheal  # noqa: E402


class ConfigError(RuntimeError):
    pass


def read_token(env: dict) -> str | None:
    f = env.get("REGISTRY_READ_TOKEN_FILE")
    if f:
        return Path(f).read_text(encoding="utf-8").strip()
    return (env.get("REGISTRY_READ_TOKEN") or "").strip() or None


def fetch_wrapper_core(registry_url: str, ring: str, token: str | None, out_dir: Path) -> Path:
    try:
        text = selfheal.fetch_bytes(selfheal.resolve_location("manifest.yaml", registry_url), token).decode("utf-8")
    except selfheal.HealError as exc:
        raise ConfigError(f"cannot read registry manifest: {exc}") from exc
    manifest = miniyaml.loads(text)
    entry = ((manifest or {}).get("components") or {}).get("wrapper-core")
    if not entry:
        raise ConfigError("registry manifest has no wrapper-core component")
    version = (entry.get("rings") or {}).get(ring)
    rec = (entry.get("versions") or {}).get(str(version)) if version else None
    if not rec:
        raise ConfigError(f"wrapper-core has no version assigned for ring {ring!r}")
    data = selfheal.fetch_bytes(selfheal.resolve_location(rec["url"], registry_url), token)
    if hashlib.sha256(data).hexdigest() != str(rec["sha256"]).lower():
        raise ConfigError("wrapper-core sha256 mismatch (registry artefact does not match its manifest entry)")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"wrapper-core-{version}.zip"
    target.write_bytes(data)
    return target


def make_config(env: dict, out: Path, wrapper_zip: Path | None = None) -> dict:
    def need(k: str) -> str:
        if not env.get(k):
            raise ConfigError(f"CI variable {k} is required")
        return env[k]

    ring = env.get("BUILD_RING", "dev")
    registry = need("REGISTRY_URL")
    if wrapper_zip is None:
        wrapper_zip = fetch_wrapper_core(registry, ring, read_token(env), Path(out).parent / "bootstrap")
    cfg = {
        "automation": need("AUTOMATION_NAME"), "version": need("AUTOMATION_VERSION"), "ring": ring,
        "registry_url": registry, "log_remote": need("LOG_REMOTE"), "llm_endpoint": env.get("LLM_ENDPOINT", ""),
        "llm_model": env.get("LLM_MODEL", ""), "publisher": env.get("PUBLISHER", "AUTOMAGIX"),
        "wrapper_core_zip": str(Path(wrapper_zip).resolve()),
    }
    for key, var in (("icon", "ICON_FILE"), ("python_embed_zip", "PYTHON_EMBED_ZIP")):
        if env.get(var):
            cfg[key] = env[var]
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wrapper-core")
    w.add_argument("--out", required=True)
    w.add_argument("--ring")
    c = sub.add_parser("config")
    c.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    env = dict(os.environ)
    try:
        if args.cmd == "wrapper-core":
            p = fetch_wrapper_core(env["REGISTRY_URL"], args.ring or env.get("BUILD_RING", "dev"), read_token(env), Path(args.out))
            print(p)
        else:
            make_config(env, Path(args.out))
            print(args.out)
    except (ConfigError, KeyError) as exc:
        print(f"ci_installer_config.py: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
