from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from wrapper_core.transport import DirTransport

from automation_logging import ingest
from factories import T, heartbeat_record, lifecycle_record, run_record

GIT = shutil.which("git")


def arts():
    r = run_record("auto-a", T(0, 9), host="VDI-1")
    h = heartbeat_record("VDI-1", T(0, 9, 5), phase="run_end")
    l = lifecycle_record("installed", "auto-a", "VDI-1", T(0, 8))
    return r, h, l


def send_via_dir_transport(root, ring="pilot"):
    t = DirTransport(root, ring)
    r, h, l = arts()
    t.send("run", "auto-a", r["run_id"], r)
    t.send("heartbeat", "VDI-1", "VDI-1-hb1", h)
    t.send("lifecycle", "auto-a", "evt-1", l)
    return r


def test_dir_ingest_archives_with_date_layout_and_ring(tmp_path):
    src, arch = tmp_path / "logs", tmp_path / "archive"
    r = send_via_dir_transport(src)
    stats = ingest.ingest_dir(src, arch)
    assert stats["archived"] == 3 and stats["by_kind"] == {"run": 1, "heartbeat": 1, "lifecycle": 1}
    f = arch / "run" / "auto-a" / "2026" / "10" / "14" / f"{r['run_id']}.json"
    assert f.exists() and json.loads(f.read_text())["ring"] == "pilot"
    assert (arch / "heartbeat" / "VDI-1" / "2026" / "10" / "14" / "VDI-1-hb1.json").exists()
    assert not list((src / "ingest").rglob("*.json"))  # sources consumed


def test_dir_ingest_is_idempotent(tmp_path):
    src, arch = tmp_path / "logs", tmp_path / "archive"
    send_via_dir_transport(src)
    ingest.ingest_dir(src, arch)
    assert ingest.ingest_dir(src, arch)["archived"] == 0           # nothing left
    send_via_dir_transport(src)                                    # same artefacts re-sent (e.g. spool retry)
    stats = ingest.ingest_dir(src, arch)
    assert stats["archived"] == 0 and stats["duplicates"] == 3
    assert len(list(arch.rglob("*.json"))) == 3


def test_dir_ingest_quarantines_bad_artefacts(tmp_path):
    src, arch = tmp_path / "logs", tmp_path / "archive"
    d = src / "ingest" / "pilot" / "run" / "auto-a"
    d.mkdir(parents=True)
    (d / "broken.json").write_text("{not json")
    (d / "wrongkind.json").write_text(json.dumps({"schema_version": 1, "kind": "heartbeat", "ts": "2026-10-14T00:00:00Z"}))
    (d / "future.json").write_text(json.dumps({"schema_version": 2, "kind": "run"}))
    stats = ingest.ingest_dir(src, arch)
    assert stats["rejected"] == 3 and stats["archived"] == 0
    assert len(list((arch / "_rejected").glob("*.reason"))) == 3


# ---------------------------------------------------------------------------------------------- git mode
def git(cwd, *args, check=True):
    r = subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError(r.stderr)
    return r


def push_like_endpoint(bare, tmp_path, ring, kind, name, art_id, payload, path_in_commit=None):
    """Exactly what wrapper_core.transport.GitSshTransport does: throwaway repo, one orphan commit, push to a new ref."""
    repo = tmp_path / f"ep-{art_id}"
    repo.mkdir()
    f = repo / (path_in_commit or f"{kind}/{name}/{art_id}.json")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"{kind} {name} {art_id}")
    git(repo, "push", "-q", str(bare), f"HEAD:refs/heads/ingest/{ring}/{kind}/{name}/{art_id}")


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_git_ingest_archives_then_deletes_refs(tmp_path):
    bare = tmp_path / "logging.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare")
    r, h, l = arts()
    l.pop("ring")  # real lifecycle artefacts carry no ring; ingest takes it from the ref path
    push_like_endpoint(bare, tmp_path, "pilot", "run", "auto-a", r["run_id"], r)
    push_like_endpoint(bare, tmp_path, "pilot", "heartbeat", "VDI-1", "VDI-1-hb1", h)
    push_like_endpoint(bare, tmp_path, "broad", "lifecycle", "auto-a", "evt-1", l)
    # unrelated branch must survive
    main = tmp_path / "main-repo"
    main.mkdir()
    git(main, "init", "-q")
    (main / "README.md").write_text("x")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "init")
    git(main, "push", "-q", str(bare), "HEAD:refs/heads/main")

    arch = tmp_path / "archive"
    stats = ingest.ingest_git(str(bare), arch)
    assert stats["archived"] == 3 and stats["deleted_refs"] == 3 and stats["errors"] == []
    assert json.loads((arch / "lifecycle" / "auto-a" / "2026" / "10" / "14" / "evt-1.json").read_text())["ring"] == "broad"
    refs = git(bare, "for-each-ref", "--format=%(refname)").stdout.split()
    assert refs == ["refs/heads/main"]
    # idempotent: nothing left to fetch is not an error
    again = ingest.ingest_git(str(bare), arch)
    assert again["archived"] == 0 and again["errors"] == []


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_git_ingest_quarantines_bad_layout_and_keep_refs(tmp_path):
    bare = tmp_path / "logging.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare")
    r, _, _ = arts()
    push_like_endpoint(bare, tmp_path, "pilot", "run", "auto-a", "weird", r, path_in_commit="somewhere/else.json")
    push_like_endpoint(bare, tmp_path, "pilot", "run", "auto-a", r["run_id"], r)
    arch = tmp_path / "archive"
    stats = ingest.ingest_git(str(bare), arch, delete=False)
    assert stats["archived"] == 1 and stats["rejected"] == 1 and stats["deleted_refs"] == 0
    assert len(git(bare, "for-each-ref", "--format=%(refname)").stdout.split()) == 2   # --keep-refs honoured


def test_prune_removes_only_old_day_folders(tmp_path):
    from automation_logging.synthetic import write_archive
    write_archive(tmp_path, [run_record("auto-a", T(100, 9)), run_record("auto-a", T(1, 9)), heartbeat_record("VDI-1", T(120, 6))])
    from factories import NOW
    out = ingest.prune(tmp_path, 90, NOW)
    assert out["removed_day_folders"] == 2
    assert len(list(tmp_path.rglob("*.json"))) == 1 and not (tmp_path / "heartbeat" / "VDI-1").exists()   # empty parents cleaned


def test_repo_alerting_yaml_loads():
    from pathlib import Path
    from automation_logging import alerting
    cfg = alerting.load_config(Path(__file__).resolve().parent.parent / "alerting.yaml")
    assert {c["type"] for c in cfg["channels"]} == {"file", "webhook", "jira", "email"} and cfg["routing"]["critical"] == ["outbox"]
    assert cfg["quiet_hours"]["window"] == "22:00-06:00" and cfg["owners"] == {}
