"""The public view: hashed machine ids, no free text, no evidence, opt-in detail only for public/internal data."""
from __future__ import annotations

import json

from automation_logging import analytics, public
from factories import NOW, T, heartbeat_record, lifecycle_record, manifest, run_record, write_archive

HOST = "VDI-0204.corp.example"
MSG = "Timeout opening https://intranet.corp/x for bob@corp.example in C:\\Users\\bob\\file.txt"


def failed():
    return run_record("auto-a", T(0, 9), host=HOST, status="failed", params={"account": "bob@corp.example", "token": "s3cr3t"},
                      result={"state": "oops", "path": "C:\\data"}, synopsis="Bob's invoice 4711 failed",
                      error={"category": "NETWORK_ERROR", "type": "Timeout", "message": MSG, "traceback": "Traceback (most recent call last): boom", "attempts": 2},
                      extra_events=[{"ts": T(0, 9).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "event": "note", "data": {"message": "secret", "count": 3, "outcome": "failed"}}])


def test_machine_id_is_stable_salted_and_not_the_host():
    a, b = public.machine_id(HOST, "salt-1"), public.machine_id(HOST.upper(), "salt-1")
    assert a == b and a.startswith("machine-") and len(a) == 14 and "VDI" not in a
    assert public.machine_id(HOST, "salt-2") != a


def test_salt_prefers_env(monkeypatch):
    monkeypatch.setenv("SITE_ID_SALT", "abc")
    assert public.site_salt("o/r") == "abc"
    monkeypatch.delenv("SITE_ID_SALT")
    assert "o/r" in public.site_salt("o/r")


def test_run_loses_free_text_but_keeps_category_signature_and_counts():
    out = public.sanitize_run(failed(), "s")
    blob = json.dumps(out)
    for bad in (HOST, "bob", "s3cr3t", "oops", "4711", "Traceback", "intranet", "C:\\\\Users", "secret"):
        assert bad not in blob, bad
    assert out["error"]["category"] == "NETWORK_ERROR" and out["error"]["signature"]
    assert out["error"]["message"] == public.CATEGORY_LABELS["NETWORK_ERROR"]
    assert out["host"] == public.machine_id(HOST, "s") and out["events"][-1]["data"] == {"count": 3, "outcome": "failed"}


def test_public_detail_needs_the_flag_and_a_public_or_internal_classification():
    def entry(cls, flag=True):
        return {"data_classification": cls, "catalog": {"public_detail": flag}}
    assert public.public_detail_allowed(entry("public")) and public.public_detail_allowed(entry("internal"))
    assert not public.public_detail_allowed(entry("confidential")) and not public.public_detail_allowed(entry("restricted"))
    assert not public.public_detail_allowed(entry("internal", False)) and not public.public_detail_allowed({"data_classification": "internal"})
    on = public.sanitize_run(failed(), "s", True)["error"]["message"]
    assert "Timeout opening" in on and "intranet" not in on and "bob@" not in on and "Users" not in on
    assert public.sanitize_run(failed(), "s", False)["error"]["message"] == public.CATEGORY_LABELS["NETWORK_ERROR"]


def test_sanitize_archive_is_valid_withholds_evidence_and_respects_the_opt_in(tmp_path):
    src, dst = tmp_path / "raw", tmp_path / "pub"
    ev = {"schema_version": 1, "kind": "evidence", "run_id": failed()["run_id"], "automation": "auto-a", "host": HOST, "ts": failed()["started_at"], "files": []}
    write_archive(src, [failed(), heartbeat_record(HOST, T(0, 11), automations={"auto-a": "1.0.0"}),
                        lifecycle_record("installed", None, HOST, T(1, 8))])
    evp = src / "evidence" / "auto-a" / "2026" / "10" / "14" / "e1.json"
    evp.parent.mkdir(parents=True)
    evp.write_text(json.dumps(ev), encoding="utf-8")
    mf = manifest(classification="confidential", catalog={"public_detail": True})
    stats = public.sanitize_archive(src, dst, mf, "s")
    assert stats["copied"] == 3 and stats["evidence_withheld"] == 1 and not (dst / "evidence").exists()
    data = analytics.load_archive(dst, NOW)
    assert data.skipped_malformed == 0 and len(data.runs) == 1 and len(data.heartbeats) == 1 and len(data.lifecycle) == 1
    assert data.runs[0]["error"]["message"] == public.CATEGORY_LABELS["NETWORK_ERROR"]      # confidential: the opt-in is ignored
    text = "".join(p.read_text(encoding="utf-8") for p in dst.rglob("*.json"))
    assert HOST not in text and not public.find_leaks(dst, stats["raw_hosts"])


def test_find_leaks_catches_a_raw_host_and_an_evidence_folder(tmp_path):
    (tmp_path / "dashboard" / "evidence").mkdir(parents=True)
    (tmp_path / "dashboard" / "evidence" / "x.png").write_bytes(b"x")
    (tmp_path / "a.html").write_text("<p>seen on vdi-0204.corp.example</p>", encoding="utf-8")
    problems = public.find_leaks(tmp_path, [HOST])
    assert any("evidence folder" in p for p in problems) and any("raw host" in p for p in problems)
