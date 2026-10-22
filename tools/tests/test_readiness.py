from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import readiness as rd  # noqa: E402

ART = b"zip-bytes"
SHA = hashlib.sha256(ART).hexdigest()
MANIFEST = f"""
components:
  wrapper-core:
    rings: {{dev: "1.0.0", pilot: "1.0.0", broad: "1.0.0"}}
    versions:
      "1.0.0": {{url: artifacts/c.zip, sha256: {SHA}}}
automations:
  auto-a:
    status: active
    rings: {{dev: "1.0.0", pilot: "1.0.0", broad: "1.0.0"}}
    versions:
      "1.0.0": {{url: artifacts/a.zip, sha256: {SHA}}}
"""


def fake(overrides=None, bad_sha=False):
    base = "https://o.github.io/r/"
    exe = b"installer"
    files = {base + p: (200, b"<html>") for p in rd.PAGES}
    files[base + "registry/manifest.yaml"] = (200, MANIFEST.encode())
    files[base + "registry/artifacts/c.zip"] = (200, b"tampered" if bad_sha else ART)
    files[base + "registry/artifacts/a.zip"] = (200, ART)
    files["https://api.github.com/repos/o/r/releases/tags/installers-latest"] = (200, json.dumps({"assets": [
        {"name": "auto-a-setup.exe", "size": 9, "updated_at": "t", "browser_download_url": "https://dl/exe"},
        {"name": "auto-a-setup.exe.sha256", "size": 1, "updated_at": "t", "browser_download_url": "https://dl/sha"}]}).encode())
    files["https://dl/exe"] = (200, exe)
    files["https://dl/sha"] = (200, (hashlib.sha256(exe).hexdigest() + "  auto-a-setup.exe").encode())
    runs = [{"name": n, "status": "completed", "conclusion": "success", "head_sha": "abcdef1234", "event": "push"} for n in rd.WORKFLOWS]
    files["https://api.github.com/repos/o/r/actions/runs?branch=main&per_page=60"] = (200, json.dumps({"workflow_runs": runs}).encode())
    files["https://api.github.com/repos/o/r/rulesets"] = (200, json.dumps([{"name": "main: x"}, {"name": "registry-artifacts: x"}]).encode())
    files.update(overrides or {})
    return lambda url, token=None: files.get(url, (404, b""))


def test_healthy_platform_has_no_failures_and_lists_manual_todos():
    rep = rd.run("o", "r", fake(), deep=True)
    assert rep.failed == 0
    todos = [r["check"] for r in rep.rows if r["status"] == rd.TODO]
    assert "manifest unsigned" in todos and "ruleset: triggers" in todos


def test_tampered_artifact_fails_the_audit():
    rep = rd.run("o", "r", fake(bad_sha=True))
    assert rep.failed >= 1 and any("sha256" in r["check"] and r["status"] == rd.FAIL for r in rep.rows)


def test_missing_page_and_failed_pipeline_are_reported():
    runs = [{"name": "ci", "status": "completed", "conclusion": "failure", "head_sha": "abcdef1234", "event": "push"}]
    rep = rd.run("o", "r", fake({"https://o.github.io/r/jobs/": (404, b""),
                                 "https://api.github.com/repos/o/r/actions/runs?branch=main&per_page=60": (200, json.dumps({"workflow_runs": runs}).encode())}))
    bad = {r["check"] for r in rep.rows if r["status"] == rd.FAIL}
    assert "/jobs/" in bad and "ci" in bad


def test_unreachable_network_fails_closed():
    rep = rd.run("o", "r", lambda url, token=None: (0, b""))
    assert rep.failed >= 1


def test_a_rejected_token_is_reported_as_unchecked_not_as_disabled_features():
    base = fake()

    def f(url, token=None):
        if token and url.startswith("https://api.github.com/repos/o/"):
            return 401, b""
        return base(url, token)
    rep = rd.run("o", "r", f, token="bad")
    assert rep.failed == 0
    rows = [r for r in rep.rows if r["area"] == "settings" and "token-only" in r["check"]]
    assert rows and "rejected" in rows[0]["detail"]
    assert not any(r["check"] == "secret_scanning" for r in rep.rows)


def _sig_rows(rep):
    return {r["check"]: r for r in rep.rows if r["area"] == "signing"}


def test_signature_with_seq_and_signed_triggers_is_reported_ok():
    from wrapper_core import trust
    seed = bytes(range(32))
    trig = b'{"triggers": []}'
    doc = trust.make_sig_doc(MANIFEST.encode(), seed, "2026-01-01T00:00:00Z", seq=12, extras={"triggers.json": hashlib.sha256(trig).hexdigest()})
    rep = rd.run("o", "r", fake({"https://o.github.io/r/registry/manifest.yaml.sig": (200, json.dumps(doc).encode()),
                                 "https://o.github.io/r/registry/triggers.json": (200, trig)}))
    rows = _sig_rows(rep)
    assert rows["manifest signed"]["status"] == rd.OK and rows["anti-rollback sequence"]["status"] == rd.OK
    assert rows["triggers.json is signed"]["status"] == rd.OK and rep.failed == 0


def test_tampered_triggers_or_missing_seq_are_flagged():
    from wrapper_core import trust
    doc = trust.make_sig_doc(MANIFEST.encode(), bytes(range(32)), "2026-01-01T00:00:00Z", seq=12,
                             extras={"triggers.json": hashlib.sha256(b"original").hexdigest()})
    rep = rd.run("o", "r", fake({"https://o.github.io/r/registry/manifest.yaml.sig": (200, json.dumps(doc).encode()),
                                 "https://o.github.io/r/registry/triggers.json": (200, b"altered")}))
    assert _sig_rows(rep)["triggers.json is signed"]["status"] == rd.FAIL
    legacy = trust.make_sig_doc(MANIFEST.encode(), bytes(range(32)), "2026-01-01T00:00:00Z")
    rep = rd.run("o", "r", fake({"https://o.github.io/r/registry/manifest.yaml.sig": (200, json.dumps(legacy).encode())}))
    assert _sig_rows(rep)["anti-rollback sequence"]["status"] == rd.WARN
    bad = rd.run("o", "r", fake({"https://o.github.io/r/registry/manifest.yaml.sig": (200, b"not json")}))
    assert _sig_rows(bad)["manifest signature"]["status"] == rd.FAIL
