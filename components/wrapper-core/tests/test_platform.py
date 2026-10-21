from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from wrapper_core import secrets, transport
from wrapper_core.paths import Paths
from wrapper_core.scheduler import Scheduler, launcher_cmd, launcher_vbs, maintain_cmd, task_name, task_xml

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class FakeSchtasks:
    returncode, stdout, stderr = 0, "", ""

    def __init__(self):
        self.calls, self.existing = [], set()

    def __call__(self, args, input_text=None):
        self.calls.append(args)
        if args[1] == "/Create":
            self.existing.add(args[3])
        if args[1] == "/Delete":
            self.existing.discard(args[3])
        r = type("R", (), {"stdout": "", "stderr": ""})()
        r.returncode = 1 if (args[1] == "/Query" and args[3] not in self.existing) else 0
        return r


SCHED = {"timezone": "UTC", "slots": [
    {"id": "daily", "type": "daily", "time": "09:00", "params": {"d": "yesterday"}},
    {"id": "weekly", "type": "weekly", "days": ["Monday", "Friday"], "time": "08:30"},
    {"id": "hourly", "type": "hourly", "every": 2},
    {"id": "poll", "type": "poll", "every_minutes": 5}]}


@pytest.mark.parametrize("slot", SCHED["slots"], ids=lambda s: s["id"])
def test_task_xml_is_wellformed_and_least_privilege(slot):
    xml = task_xml(Paths(), "auto-a", slot, "UTC")
    root = ET.fromstring(xml.replace('encoding="UTF-16"', 'encoding="UTF-8"'))
    assert root.find("t:Principals/t:Principal/t:RunLevel", NS).text == "LeastPrivilege"      # no admin needed
    assert root.find("t:Principals/t:Principal/t:LogonType", NS).text == "InteractiveToken"
    assert root.find("t:Settings/t:StartWhenAvailable", NS).text == "true"                    # catch up missed runs
    assert root.find("t:Settings/t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert slot["id"] in root.find("t:Actions/t:Exec/t:Arguments", NS).text


def test_weekly_and_repetition_content():
    w = task_xml(Paths(), "a", SCHED["slots"][1], "UTC")
    assert "<Monday />" in w and "<Friday />" in w and "<Tuesday />" not in w
    assert "PT2H" in task_xml(Paths(), "a", SCHED["slots"][2], "UTC")
    assert "PT5M" in task_xml(Paths(), "a", SCHED["slots"][3], "UTC")


def test_register_is_idempotent_and_prunes_removed_slots(monkeypatch):
    monkeypatch.setattr("wrapper_core.scheduler.IS_WINDOWS", True)
    fake = FakeSchtasks()
    sch = Scheduler(Paths().ensure(), runner=fake, python_exe="C:/py/python.exe")
    first = sch.register("auto-a", SCHED)
    assert sorted(first) == sorted(task_name("auto-a", s["id"]) for s in SCHED["slots"])
    assert sch.register("auto-a", SCHED) == []                       # nothing changed -> no schtasks /Create
    # a user (or VDI reset) deleted a task: maintain re-asserts it
    fake.existing.discard(task_name("auto-a", "daily"))
    assert sch.register("auto-a", SCHED) == [task_name("auto-a", "daily")]
    # slot removed from the manifest: its task is deleted
    reduced = {"timezone": "UTC", "slots": SCHED["slots"][:1]}
    touched = sch.register("auto-a", reduced)
    assert any(t.startswith("-") for t in touched) and not any("weekly" in t for t in fake.existing)


def test_launchers_apply_pending_before_python_and_hide_window():
    p = Paths()
    cmd = launcher_cmd(p, "auto-a", "C:/py/python.exe")
    assert cmd.index("wrapper-core.pending") < cmd.index("-m wrapper_core run")
    assert "PYTHONPATH" in cmd and '--slot "%~1"' in cmd
    vbs = launcher_vbs(p, "auto-a")
    assert ", 0, True" in vbs                                         # window style 0 = hidden
    assert "maintain" in maintain_cmd(p, "C:/py/python.exe")


def test_startup_script_written(tmp_path):
    sch = Scheduler(Paths().ensure())
    f = sch.write_startup_script()
    assert f.name == "AUTOMAGIX-maintain.cmd" and "wrapper_core maintain" in f.read_text()


# ---- secrets -------------------------------------------------------------------------------------
def test_protect_roundtrip_and_acl(tmp_path):
    src = tmp_path / "key"
    src.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----")  # secret-scan: allow (fake fixture)
    dst = tmp_path / "secrets" / "logging_key.dpapi"
    secrets.protect_file(src, dst, delete_input=True)
    assert not src.exists()                                           # plaintext wiped
    assert b"OPENSSH" not in dst.read_bytes()                         # ciphertext
    assert secrets.unprotect_file(dst).startswith(b"-----BEGIN OPENSSH")


def test_temp_private_key_is_wiped(tmp_path):
    src = tmp_path / "key"
    src.write_bytes(b"KEYDATA")
    blob = tmp_path / "k.dpapi"
    secrets.protect_file(src, blob)
    with secrets.TempPrivateKey(blob, tmp_path / "tmp") as kf:
        assert kf.read_bytes() == b"KEYDATA\n"
        seen = kf
    assert not seen.exists()


def test_insecure_fallback_is_opt_in(monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("real DPAPI on Windows")
    monkeypatch.delenv("AUTOMAGIX_ALLOW_INSECURE_SECRETS")
    with pytest.raises(secrets.SecretError):
        secrets.dpapi_protect(b"x")


def test_registry_token_resolution_order(monkeypatch, tmp_path):
    p = Paths().ensure()
    assert secrets.get_registry_token(p) is None
    f = tmp_path / "t"
    f.write_text("file-token")
    secrets.protect_file(f, p.secrets / "registry_token.dpapi")
    assert secrets.get_registry_token(p) == "file-token"
    monkeypatch.setenv("AUTOMAGIX_REGISTRY_TOKEN", "env-token")
    assert secrets.get_registry_token(p) == "env-token"


@pytest.mark.skipif(sys.platform != "win32", reason="Credential Manager is Windows-only")
def test_credential_manager_roundtrip():
    target = f"AUTOMAGIX-TEST/{os.getpid()}"
    try:
        secrets.credman_write(target, "s3cr3t-value")
        assert secrets.credman_read(target) == "s3cr3t-value"
    finally:
        secrets.credman_delete(target)
    assert secrets.credman_read(target) is None


# ---- git transport (write-only by construction) -----------------------------------------------------
def test_git_transport_pushes_orphan_refs_without_fetching(tmp_path, monkeypatch):
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("git not available")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    p = Paths().ensure()
    key = tmp_path / "k"
    key.write_bytes(b"fake-key")
    secrets.protect_file(key, p.secrets / "logging_key.dpapi")
    monkeypatch.setenv("AUTOMAGIX_LOG_TRANSPORT", "git-ssh")
    monkeypatch.setenv("AUTOMAGIX_LOG_REMOTE", str(bare))   # local path works with the same push command
    monkeypatch.setenv("AUTOMAGIX_LOG_GIT_MODE", "refs")
    assert transport.emit("run", "auto-a", "RID1", {"hello": "world"}, p) is True
    assert transport.emit("run", "auto-a", "RID2", {"hello": "again"}, p) is True
    refs = subprocess.run(["git", "--git-dir", str(bare), "for-each-ref", "--format=%(refname)"], capture_output=True, text=True).stdout.split()
    assert sorted(refs) == ["refs/heads/ingest/pilot/run/auto-a/RID1", "refs/heads/ingest/pilot/run/auto-a/RID2"]
    blob = subprocess.run(["git", "--git-dir", str(bare), "show", "ingest/pilot/run/auto-a/RID1:run/auto-a/RID1.json"], capture_output=True, text=True).stdout
    assert json.loads(blob) == {"hello": "world"}
    assert not list((p.secrets / "tmp").glob(".k*"))          # temp key wiped


def test_git_transport_branch_mode_appends_to_one_branch(tmp_path, monkeypatch):
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("git not available")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    p = Paths().ensure()
    key = tmp_path / "k"
    key.write_bytes(b"fake-key")
    secrets.protect_file(key, p.secrets / "logging_key.dpapi")
    monkeypatch.setenv("AUTOMAGIX_LOG_TRANSPORT", "git-ssh")
    monkeypatch.setenv("AUTOMAGIX_LOG_REMOTE", str(bare))
    assert transport.emit("run", "auto-a", "RID1", {"n": 1}, p) is True
    assert transport.emit("run", "auto-a", "RID2", {"n": 2}, p) is True
    assert transport.emit("run", "auto-a", "RID2", {"n": 2}, p) is True   # idempotent resend
    refs = subprocess.run(["git", "--git-dir", str(bare), "for-each-ref", "--format=%(refname)"], capture_output=True, text=True).stdout.split()
    assert refs == ["refs/heads/ingest"]
    blob = subprocess.run(["git", "--git-dir", str(bare), "show", "ingest:ingest/pilot/run/auto-a/RID2.json"], capture_output=True, text=True).stdout
    assert json.loads(blob) == {"n": 2}
    assert subprocess.run(["git", "--git-dir", str(bare), "rev-list", "--count", "ingest"], capture_output=True, text=True).stdout.strip() == "2"


def test_git_transport_failure_spools(tmp_path, monkeypatch):
    import shutil
    if not shutil.which("git"):
        pytest.skip("git not available")
    p = Paths().ensure()
    key = tmp_path / "k"
    key.write_bytes(b"fake-key")
    secrets.protect_file(key, p.secrets / "logging_key.dpapi")
    monkeypatch.setenv("AUTOMAGIX_LOG_TRANSPORT", "git-ssh")
    monkeypatch.setenv("AUTOMAGIX_LOG_REMOTE", str(tmp_path / "does-not-exist.git"))
    assert transport.emit("run", "a", "R1", {"x": 1}, p) is False
    assert len(list(p.spool.glob("*.json"))) == 1


def test_doctor_reports_without_crashing_and_never_leaks_secrets(monkeypatch, tmp_path):
    from wrapper_core import doctor
    monkeypatch.setenv("AUTOMAGIX_REGISTRY_TOKEN", "tok-should-never-print")
    p = Paths().ensure()
    res = doctor.run_checks(p, online=False)
    names = {r["check"] for r in res}
    assert {"python", "wrapper-core", "ring", "installed automations", "run-log destination", "unsent log spool"} <= names
    assert "tok-should-never-print" not in str(res)
    assert all(r["status"] in ("PASS", "WARN", "FAIL") for r in res)
    assert doctor.main(["--offline", "--json"]) in (0, 1)


def _fake_github(state):
    """Minimal fake of the GitHub REST endpoints the log transport uses (branch bootstrap + contents PUT)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _auth(self):
            state.setdefault("auth", []).append(self.headers.get("Authorization"))

        def do_GET(self):
            self._auth()
            if state.get("empty") and "/git/ref/heads/" in self.path:
                return self._send(409, {"message": "Git Repository is empty."})
            if self.path.endswith("/git/ref/heads/ingest"):
                return self._send(200 if state.get("branch") else 404, {"object": {"sha": "abc"}})
            if self.path.endswith("/git/ref/heads/main"):
                return self._send(200, {"object": {"sha": "abc"}})
            if self.path.endswith("/repos/o/logs"):
                return self._send(200, {"default_branch": "main"})
            self._send(404, {})

        def do_POST(self):
            self._auth()
            self.rfile.read(int(self.headers["Content-Length"]))
            state["branch"] = True
            self._send(201, {})

        def do_PUT(self):
            self._auth()
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            path = self.path.split("/contents/", 1)[1]
            if path == "README.md" and state.get("empty"):
                state["empty"] = False
                state["readme"] = True
                return self._send(201, {})
            if path in state.setdefault("files", {}):
                return self._send(422, {"message": "Invalid request. \"sha\" wasn't supplied."})
            if state.get("fail_once"):
                state["fail_once"] = False
                return self._send(409, {"message": "conflict"})
            state["files"][path] = body
            self._send(201, {})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_github_api_transport_uploads_artefacts_idempotently(monkeypatch):
    import base64
    state = {"fail_once": True}
    srv = _fake_github(state)
    try:
        monkeypatch.setenv("AUTOMAGIX_GITHUB_API", f"http://127.0.0.1:{srv.server_port}")
        monkeypatch.setenv("AUTOMAGIX_LOGGING_TOKEN", "ghp_test_token")
        monkeypatch.setenv("AUTOMAGIX_LOG_TRANSPORT", "github-api")
        monkeypatch.setenv("AUTOMAGIX_LOG_REMOTE", "o/logs")
        p = Paths().ensure()
        assert transport.emit("run", "auto-a", "RID1", {"hello": "world"}, p) is True      # branch created, 409 retried
        assert transport.emit("run", "auto-a", "RID1", {"hello": "world"}, p) is True      # duplicate -> idempotent
        files = state["files"]
        assert list(files) == ["ingest/pilot/run/auto-a/RID1.json"]
        assert json.loads(base64.b64decode(files["ingest/pilot/run/auto-a/RID1.json"]["content"])) == {"hello": "world"}
        assert files["ingest/pilot/run/auto-a/RID1.json"]["branch"] == "ingest"
        assert set(state["auth"]) == {"Bearer ghp_test_token"}
    finally:
        srv.shutdown()


def test_github_api_transport_initialises_a_brand_new_empty_logs_repo(monkeypatch):
    state = {"empty": True}
    srv = _fake_github(state)
    try:
        monkeypatch.setenv("AUTOMAGIX_GITHUB_API", f"http://127.0.0.1:{srv.server_port}")
        monkeypatch.setenv("AUTOMAGIX_LOGGING_TOKEN", "ghp_test_token")
        monkeypatch.setenv("AUTOMAGIX_LOG_TRANSPORT", "github-api")
        monkeypatch.setenv("AUTOMAGIX_LOG_REMOTE", "o/logs")
        p = Paths().ensure()
        assert transport.emit("run", "auto-a", "RID9", {"first": True}, p) is True
        assert state.get("readme") and state.get("branch")
        assert list(state["files"]) == ["ingest/pilot/run/auto-a/RID9.json"]
    finally:
        srv.shutdown()


def test_github_api_transport_without_token_spools(monkeypatch):
    monkeypatch.setenv("AUTOMAGIX_LOG_TRANSPORT", "github-api")
    monkeypatch.setenv("AUTOMAGIX_LOG_REMOTE", "o/logs")
    monkeypatch.setenv("AUTOMAGIX_GITHUB_API", "http://127.0.0.1:9")
    p = Paths().ensure()
    assert transport.emit("run", "a", "R1", {"x": 1}, p) is False
    assert len(list(p.spool.glob("*.json"))) == 1


def test_transport_is_inferred_from_the_remote_shape():
    from wrapper_core.paths import platform_config
    import os
    for remote, want in [("git@github.com:o/r.git", "git-ssh"), ("o/logs", "github-api"), ("C:/logs", "dir"), ("/var/logs", "dir"), ("local", "dir")]:
        os.environ["AUTOMAGIX_LOG_REMOTE"] = remote
        os.environ.pop("AUTOMAGIX_LOG_TRANSPORT", None)
        assert platform_config(Paths())["log_transport"] == want, remote
