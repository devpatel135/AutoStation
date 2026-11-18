from __future__ import annotations

import functools
import hashlib
import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

import verify_live as vl  # noqa: E402
from wrapper_core import ed25519, trust  # noqa: E402

SEED = bytes(range(32))
PUB = ed25519.keypair_from_seed(SEED)[1].hex()
OTHER = ed25519.keypair_from_seed(bytes(range(1, 33)))[1].hex()


def write_registry(root: Path, seq=1_800_000_000, corrupt=False, missing=False, seed=SEED):
    (root / "artifacts" / "components").mkdir(parents=True, exist_ok=True)
    zbytes = b"PK-fake-zip-content"
    (root / "artifacts" / "components" / "wrapper-core-0.7.0.zip").write_bytes(b"tampered" if corrupt else zbytes)
    sha = hashlib.sha256(zbytes).hexdigest()
    gone = "artifacts/components/wrapper-core-0.6.0.zip"
    manifest = f"""schema_version: 1
components:
  wrapper-core:
    rings: {{dev: "0.7.0", pilot: "0.7.0", broad: "0.7.0"}}
    versions:
      "0.7.0":
        url: artifacts/components/wrapper-core-0.7.0.zip
        sha256: {sha}
""" + (f"""      "0.6.0":
        url: {gone}
        sha256: {sha}
""" if missing else "") + "automations: {}\n"
    mb = manifest.encode()
    (root / "manifest.yaml").write_bytes(mb)
    doc = trust.make_sig_doc(mb, seed, "2026-11-11T10:00:00Z", seq=seq, extras={})
    (root / "manifest.yaml.sig").write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def serve(tmp_path):
    root = tmp_path / "site"
    root.mkdir()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=str(root))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def go(base, keys=(PUB,), **kw):
    kw.setdefault("attempts", 1)
    kw.setdefault("wait_s", 0)
    return vl.verify(base, list(keys), **kw)


def test_a_good_registry_verifies_and_records_the_seq(serve, tmp_path):
    root, base = serve
    write_registry(root)
    seqf = tmp_path / "state" / "seq.json"
    res = go(base, expect_dir=root, seq_file=seqf)
    assert res["ok"], res["errors"]
    assert res["checked"] == 1 and res["seq"] == 1_800_000_000
    assert json.loads(seqf.read_text())["seq"] == 1_800_000_000


def test_wrong_key_fails(serve):
    root, base = serve
    write_registry(root)
    res = go(base, keys=(OTHER,))
    assert not res["ok"] and "pinned key" in res["errors"][0]


def test_tampered_zip_fails_on_sha256(serve):
    root, base = serve
    write_registry(root, corrupt=True)
    res = go(base)
    assert not res["ok"] and "sha256" in res["errors"][0] and "wrapper-core 0.7.0" in res["errors"][0]


def test_a_referenced_zip_that_is_not_published_fails_on_head(serve):
    root, base = serve
    write_registry(root, missing=True)
    res = go(base)
    assert not res["ok"] and any("0.6.0" in e and "404" in e for e in res["errors"])


def test_lower_seq_than_last_verified_is_a_rollback(serve, tmp_path):
    root, base = serve
    seqf = tmp_path / "seq.json"
    write_registry(root, seq=1_800_000_500)
    assert go(base, seq_file=seqf)["ok"]
    write_registry(root, seq=1_800_000_100)
    res = go(base, seq_file=seqf)
    assert not res["ok"] and "lower than the last verified" in res["errors"][0]
    assert json.loads(seqf.read_text())["seq"] == 1_800_000_500          # the floor never goes down


def test_a_stale_live_manifest_is_not_what_was_deployed(serve, tmp_path):
    root, base = serve
    write_registry(root, seq=1_800_000_000)
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    (deployed / "manifest.yaml").write_text("schema_version: 1\ncomponents: {}\nautomations: {}\n", encoding="utf-8")
    res = go(base, expect_dir=deployed)
    assert not res["ok"] and "stale" in res["errors"][0]


def test_unsigned_registry_fails_once_a_key_is_pinned(serve):
    root, base = serve
    write_registry(root)
    (root / "manifest.yaml.sig").unlink()
    res = go(base)
    assert not res["ok"] and "must be signed" in res["errors"][0]


def test_unreachable_registry_fails(tmp_path):
    res = go("http://127.0.0.1:9/")
    assert not res["ok"] and "cannot download" in res["errors"][0]


def test_unsafe_artifact_urls_are_refused():
    assert "unsafe" in vl.check_artifact("http://127.0.0.1:1/", "x 1", "../../etc/passwd", "0" * 64)
    assert "unsafe" in vl.check_artifact("http://127.0.0.1:1/", "x 1", "https://evil.example/x.zip", "0" * 64)
    assert "sha256" in vl.check_artifact("http://127.0.0.1:1/", "x 1", "a.zip", "nothex")


def test_plain_http_is_refused_except_loopback():
    with pytest.raises(ValueError):
        vl._check_url("http://example.com/registry/")
    assert vl._check_url("http://127.0.0.1:8000/") and vl._check_url("https://example.com/")


def test_cli_skips_without_a_pinned_key_and_rejects_bad_keys(capsys, monkeypatch):
    monkeypatch.delenv("AUTOSTATION_REGISTRY_PUBKEY", raising=False)
    assert vl.main(["--base", "https://example.com/registry/", "--pubkey", ""]) == 0
    assert "skipped" in capsys.readouterr().out
    assert vl.main(["--base", "https://example.com/registry/", "--pubkey", "nothex"]) == 2
    assert vl.main(["--base", "http://example.com/registry/", "--pubkey", PUB]) == 2


def test_cli_end_to_end(serve, tmp_path, capsys):
    root, base = serve
    write_registry(root)
    assert vl.main(["--base", base, "--pubkey", PUB, "--expect-dir", str(root), "--attempts", "1", "--seq-file", str(tmp_path / "s.json")]) == 0
    assert "live registry verified" in capsys.readouterr().out
    write_registry(root, corrupt=True)
    assert vl.main(["--base", base, "--pubkey", PUB, "--attempts", "1"]) == 1
