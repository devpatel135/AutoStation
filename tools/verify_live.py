#!/usr/bin/env python3
"""Post-deploy verification of the LIVE registry (what installed machines actually download), run after Pages deploys.

    python tools/verify_live.py --base https://<owner>.github.io/<repo>/registry/ --pubkey <hex> [--expect-dir registry-out]
                                [--seq-file verify-state/seq.json] [--raise-issue]

Checks, in order (every failure is listed, then exit 1):
  1. manifest.yaml and manifest.yaml.sig download from the live URL (retried: Pages can take a minute to publish);
  2. the Ed25519 signature verifies against the pinned public key(s) with ``wrapper_core.trust`` - the same code an endpoint runs;
  3. with --expect-dir: the live manifest is byte-identical to the one this deployment published (a stale CDN copy is retried, then fails);
  4. ``seq`` is not lower than the last verified one (--seq-file keeps it between runs): a replayed older manifest fails;
  5. every zip the manifest references answers HEAD 200 and its sha256 equals the manifest's (downloaded, streamed, never kept).

No pinned key (--pubkey / AUTOSTATION_REGISTRY_PUBKEY empty) = a notice and exit 0: signing is not rolled out yet, there is nothing to verify against.
--raise-issue raises ONE ops issue ``pipe-site-verify`` on failure and resolves it on success (tools/ops_issues.py, needs GITHUB_TOKEN).
Exit codes: 0 verified (or skipped), 1 verification failed, 2 unusable arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlsplit

HERE = Path(__file__).resolve().parent
COMPONENTS = HERE.parent / "components"
sys.path.insert(0, str(COMPONENTS / "wrapper-core"))

from wrapper_core import miniyaml, trust  # noqa: E402

UA = "autostation-verify-live"
MAX_MANIFEST = 4 * 1024 * 1024
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _check_url(url: str) -> str:
    sp = urlsplit(url)
    loop = (sp.hostname or "").lower() in ("localhost", "127.0.0.1", "::1")
    if sp.scheme == "https" or (sp.scheme == "http" and loop):
        return url
    raise ValueError(f"refusing non-https URL {url[:80]}")


def _open(url: str, method: str = "GET", timeout: int = 60):
    req = urllib.request.Request(_check_url(url), method=method, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - scheme checked above


def fetch(url: str, attempts: int = 1, wait_s: float = 5.0, limit: int = MAX_MANIFEST) -> bytes:
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            with _open(url) as r:
                data = r.read(limit + 1)
            if len(data) > limit:
                raise ValueError(f"{url} is larger than {limit} bytes")
            return data
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(wait_s)
    raise RuntimeError(f"cannot download {url}: {last}")


def referenced_artifacts(manifest: dict) -> list[tuple[str, str, str, str]]:
    """(label, relative url, sha256, version) for every version record the manifest lists."""
    out = []
    for kind in ("components", "automations"):
        for name, entry in sorted((manifest.get(kind) or {}).items()):
            for ver, rec in sorted(((entry or {}).get("versions") or {}).items()):
                rec = rec or {}
                out.append((f"{name} {ver}", str(rec.get("url") or ""), str(rec.get("sha256") or "").lower(), str(ver)))
    return out


def check_artifact(base: str, label: str, rel: str, sha: str) -> str | None:
    if not rel or ".." in rel.split("/") or rel.startswith(("/", "\\")) or "://" in rel:
        return f"{label}: unsafe or missing url {rel!r}"
    if not HEX64.match(sha):
        return f"{label}: the manifest has no valid sha256"
    url = urljoin(base, rel)
    try:
        with _open(url, "HEAD") as r:
            if r.status != 200:
                return f"{label}: HEAD {url} answered {r.status}"
    except urllib.error.HTTPError as exc:
        return f"{label}: HEAD {url} answered {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"{label}: HEAD {url} failed ({exc})"
    h = hashlib.sha256()
    try:
        with _open(url, "GET", timeout=120) as r:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                h.update(chunk)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"{label}: download of {url} failed ({exc})"
    if h.hexdigest() != sha:
        return f"{label}: the live file has sha256 {h.hexdigest()[:12]}..., the manifest says {sha[:12]}..."
    return None


def read_seq(path: Path | None) -> int | None:
    if not path or not path.exists():
        return None
    try:
        v = json.loads(path.read_text(encoding="utf-8")).get("seq")
        return v if isinstance(v, int) and not isinstance(v, bool) else None
    except (OSError, ValueError, AttributeError):
        return None


def verify(base: str, keys: list[str], expect_dir: Path | None = None, seq_file: Path | None = None, attempts: int = 12, wait_s: float = 10.0,
           workers: int = 8) -> dict:
    """Pure-ish core (network only). Returns {"ok", "errors", "checked", "seq", "key_id"}."""
    errors: list[str] = []
    base = base if base.endswith("/") else base + "/"
    res = {"ok": False, "errors": errors, "checked": 0, "seq": None, "key_id": None}
    expected = None
    if expect_dir:
        try:
            expected = (Path(expect_dir) / "manifest.yaml").read_bytes()
        except OSError as exc:
            errors.append(f"cannot read the deployed manifest in {expect_dir}: {exc}")
            return res
    manifest_bytes = None
    last = ""
    for i in range(max(1, attempts)):
        try:
            manifest_bytes = fetch(base + "manifest.yaml")
            if expected is None or manifest_bytes == expected:
                break
            last = "the live manifest is not the one that was just deployed (stale copy)"
            manifest_bytes = None
        except RuntimeError as exc:
            last = str(exc)
            manifest_bytes = None
        if i + 1 < attempts:
            time.sleep(wait_s)
    if manifest_bytes is None:
        errors.append(last or "manifest.yaml could not be downloaded")
        return res
    try:
        sig_bytes = fetch(base + "manifest.yaml.sig", attempts=3, wait_s=min(wait_s, 5.0))
    except RuntimeError as exc:
        errors.append(f"{exc} (a pinned key is configured, so the registry must be signed)")
        return res
    try:
        v = trust.verify_manifest_doc(manifest_bytes, sig_bytes, keys)
    except trust.TrustError as exc:
        errors.append(f"the live manifest signature does not verify against the pinned key: {exc}")
        return res
    res["key_id"] = v.key_id if hasattr(v, "key_id") else None
    seq = getattr(v, "seq", None)
    res["seq"] = seq
    prev = read_seq(seq_file)
    if seq is not None and prev is not None and seq < prev:
        errors.append(f"the live manifest has seq {seq}, lower than the last verified {prev}: an older registry was published (rollback or replay)")
    try:
        manifest = miniyaml.loads(manifest_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        errors.append(f"the live manifest does not parse: {exc}")
        return res
    arts = referenced_artifacts(manifest)
    res["checked"] = len(arts)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for msg in ex.map(lambda a: check_artifact(base, a[0], a[1], a[2]), arts):
            if msg:
                errors.append(msg)
    res["ok"] = not errors
    if res["ok"] and seq_file and seq is not None:
        seq_file.parent.mkdir(parents=True, exist_ok=True)
        seq_file.write_text(json.dumps({"seq": max(seq, prev or 0)}) + "\n", encoding="utf-8")
    return res


def _issue(args: list[str]) -> None:
    try:
        subprocess.run([sys.executable, str(HERE / "ops_issues.py"), *args], check=False, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"::warning::could not update the ops issue: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True, help="live registry URL, e.g. https://owner.github.io/repo/registry/")
    ap.add_argument("--pubkey", default=os.environ.get("AUTOSTATION_REGISTRY_PUBKEY", ""), help="pinned public key(s), hex, comma separated")
    ap.add_argument("--expect-dir", default=None, help="the registry directory that was just deployed (the live manifest must equal it)")
    ap.add_argument("--seq-file", default=None)
    ap.add_argument("--attempts", type=int, default=12)
    ap.add_argument("--wait", type=float, default=10.0)
    ap.add_argument("--raise-issue", action="store_true")
    a = ap.parse_args(argv)
    keys = [k for k in re.split(r"[,\s]+", (a.pubkey or "").strip().lower()) if k]
    if not keys:
        print("::notice title=live verification skipped::AUTOSTATION_REGISTRY_PUBKEY is not set: there is no pinned key to verify the live signature against")
        return 0
    bad = [k for k in keys if not HEX64.match(k)]
    if bad:
        print("::error title=live verification::AUTOSTATION_REGISTRY_PUBKEY must be 64 hex characters per key")
        return 2
    try:
        _check_url(a.base)
    except ValueError as exc:
        print(f"::error title=live verification::{exc}")
        return 2
    res = verify(a.base, keys, Path(a.expect_dir) if a.expect_dir else None, Path(a.seq_file) if a.seq_file else None, a.attempts, a.wait)
    if res["ok"]:
        print(f"live registry verified: signature OK (key {res['key_id']}, seq {res['seq']}), {res['checked']} artifact(s) match their sha256")
        if a.raise_issue:
            _issue(["resolve", "--id", "pipe-site-verify", "--note", "The live registry verifies again."])
        return 0
    for e in res["errors"]:
        print(f"::error title=live registry verification failed::{e}")
    if a.raise_issue:
        _issue(["raise", "--id", "pipe-site-verify", "--type", "PIPELINE", "--severity", "high",
                "--title", "The live registry failed post-deploy verification",
                "--detail", "After the site deployed, the published registry did not verify: " + "; ".join(res["errors"])[:300]
                            + ". Machines may be refusing it. Check the site workflow run and docs/RUNBOOKS.md (registry/signing)."])
    return 1


if __name__ == "__main__":
    sys.exit(main())
