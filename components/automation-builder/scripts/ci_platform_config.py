#!/usr/bin/env python3
"""CI glue: generate platform-build.json for build_platform_installer.py.

    ci_platform_config.py config --out dist/platform-build.json

All inputs are CI variables (never argv):

Required
--------
  PLATFORM_VERSION       semver (1.3.0) — the version being built
  REGISTRY_URL           base URL or share used by the installed runtime for manifest fetch
  LOG_REMOTE             git remote the shared write-only logging key can push to

Optional
--------
  BUILD_RING             dev | pilot | broad          [broad]
  PUBLISHER              human-readable publisher      [AUTOMAGIX]
  LLM_ENDPOINT           internal LLM proxy URL
  LLM_MODEL              model name / ID
  ICON_FILE              [File] custom .ico for the installer
  KNOWN_HOSTS_FILE       [File] known_hosts for LOG_REMOTE
  WRAPPER_CORE_ZIP       [File or path] pre-fetched wrapper-core zip
                         (if absent the zip is fetched from the registry at build time)
  FRAMEWORK_ZIP          [File or path] execution-framework zip
  PYTHON_EMBED_ZIP       [File or path] Python embeddable package (from the containeryard artifact mirror)
  GIT_ZIP                [File or path] MinGit zip (bundled for log transport)
  REGISTRY_PUBKEYS       comma-separated 64-hex Ed25519 keys pinned into platform.json (strongly recommended
                         for pilot/broad — without this the runtime cannot verify the registry manifest signature)
                   alt:  REGISTRY_PUBKEYS_FILE [File] — file holding the same content (preferred when set)
"""
from __future__ import annotations

import argparse
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


def make_config(env: dict, out: Path) -> dict:
    def need(k: str) -> str:
        if not env.get(k):
            raise ConfigError(f"CI variable {k} is required")
        return env[k]

    ring = env.get("BUILD_RING", "broad")
    cfg: dict = {
        "version": need("PLATFORM_VERSION"),
        "registry_url": need("REGISTRY_URL"),
        "log_remote": need("LOG_REMOTE"),
        "ring": ring,
        "publisher": env.get("PUBLISHER", "AUTOMAGIX"),
        "llm_endpoint": env.get("LLM_ENDPOINT", ""),
        "llm_model": env.get("LLM_MODEL", ""),
    }

    # optional file / path variables
    for key, var in (
        ("icon",             "ICON_FILE"),
        ("known_hosts_file", "KNOWN_HOSTS_FILE"),
        ("wrapper_core_zip", "WRAPPER_CORE_ZIP"),
        ("framework_zip",    "FRAMEWORK_ZIP"),
        ("python_embed_zip", "PYTHON_EMBED_ZIP"),
        ("git_zip",          "GIT_ZIP"),
    ):
        if env.get(var):
            cfg[key] = env[var]

    # registry signing keys — trust anchor for manifest verification
    pubkeys = ""
    if env.get("REGISTRY_PUBKEYS_FILE"):
        try:
            pubkeys = Path(env["REGISTRY_PUBKEYS_FILE"]).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"REGISTRY_PUBKEYS_FILE: {exc}") from exc
    elif env.get("REGISTRY_PUBKEYS"):
        pubkeys = env["REGISTRY_PUBKEYS"].strip()
    if pubkeys:
        cfg["registry_pubkeys"] = pubkeys

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("config")
    c.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    env = dict(os.environ)
    try:
        make_config(env, Path(args.out))
        print(args.out)
    except (ConfigError, KeyError) as exc:
        print(f"ci_platform_config.py: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
