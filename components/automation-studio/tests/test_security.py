"""Studio security primitives (stdlib only, so these always run) and the real HTTP/WebSocket gate (needs fastapi + httpx)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from automation_studio import security as sec

PORT = 8765
TOKEN = "t" * 40


# --- primitives ------------------------------------------------------------------------------------------------------------------------------

def test_token_is_random_and_compared_safely():
    assert sec.new_token() != sec.new_token()
    assert sec.token_ok(TOKEN, TOKEN)
    assert not sec.token_ok(TOKEN, TOKEN + "x")
    assert not sec.token_ok(TOKEN, "")
    assert not sec.token_ok(TOKEN, None)
    assert not sec.token_ok("", "")            # an unconfigured server fails closed
    assert not sec.token_ok(None, "anything")


def test_bearer_and_subprotocol_parsing():
    assert sec.bearer_from_header("Bearer abc") == "abc"
    assert sec.bearer_from_header("bearer abc") == "abc"
    assert sec.bearer_from_header("Basic abc") is None
    assert sec.bearer_from_header("Bearer ") is None
    assert sec.bearer_from_header(None) is None
    assert sec.token_from_subprotocols("autostation.v1, token.abc") == "abc"
    assert sec.token_from_subprotocols("autostation.v1") is None
    assert sec.token_from_subprotocols("token.") is None


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:8765", True), ("localhost:8765", True), ("LOCALHOST:8765", True), ("[::1]:8765", True),
    ("127.0.0.1:9999", False), ("127.0.0.1", False), ("evil.example:8765", False), ("localhost.evil.example:8765", False),
    ("", False), (None, False),
])
def test_host_allow_list(host, ok):
    assert sec.host_allowed(host, PORT) is ok


@pytest.mark.parametrize("origin,ok", [
    (None, True),
    ("http://127.0.0.1:8765", True), ("http://localhost:8765", True), ("http://[::1]:8765", True),
    ("http://localhost:9999", False), ("https://localhost:8765", False), ("http://evil.example", False),
    ("http://localhost.evil.example:8765", False), ("null", False), ("file://", False),
])
def test_origin_allow_list(origin, ok):
    assert sec.origin_allowed(origin, PORT) is ok


# --- out_dir confinement ---------------------------------------------------------------------------------------------------------------------

def test_empty_out_dir_is_the_root(tmp_path):
    assert sec.resolve_out_dir(tmp_path, "") == tmp_path.resolve()
    assert sec.resolve_out_dir(tmp_path, None) == tmp_path.resolve()


def test_relative_subfolder_is_inside_root(tmp_path):
    got = sec.resolve_out_dir(tmp_path, "team-a/new work")
    assert got == (tmp_path / "team-a" / "new work").resolve()


@pytest.mark.parametrize("raw", [
    "..", "../x", "a/../../x", "a/..", "/etc", "\\Windows", "C:\\Windows", "C:/x", "c:x", "\\\\server\\share\\x", "//server/x",
    "a\x00b", "a\nb", "~/x", "a/b:c", "a/*", "bad|name", " ", ".hidden/..", "x" * 80, "ends-with-dot.",
])
def test_hostile_out_dir_is_refused(tmp_path, raw):
    if raw.strip() == "":
        assert sec.resolve_out_dir(tmp_path, raw) == tmp_path.resolve()
        return
    with pytest.raises(sec.PathRejected):
        sec.resolve_out_dir(tmp_path, raw)


def test_symlink_component_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        if sys.platform == "win32":
            import subprocess
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, text=True)
            if r.returncode != 0:
                pytest.skip("cannot create a symlink or junction here")
        else:
            pytest.skip("cannot create a symlink here")
    with pytest.raises(sec.PathRejected):
        sec.resolve_out_dir(root, "link/sub")


# --- sessions --------------------------------------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_sessions_expire_when_idle():
    clock = Clock()
    store = sec.SessionStore(idle_ttl_s=7200, clock=clock)
    store.add("a", object())
    clock.t += 7000
    assert store.get("a") is not None            # touching it restarts the idle timer
    clock.t += 7000
    assert store.get("a") is not None
    clock.t += 7201
    assert store.get("a") is None
    assert len(store) == 0


def test_session_cap_and_slot_reuse_after_expiry():
    clock = Clock()
    store = sec.SessionStore(idle_ttl_s=100, max_sessions=2, clock=clock)
    store.add("a", 1)
    store.add("b", 2)
    with pytest.raises(sec.SessionLimit):
        store.add("c", 3)
    clock.t += 101
    store.add("c", 3)                             # the two idle ones expired
    assert store.get("c") == 3


def test_step_budget_per_session():
    store = sec.SessionStore(max_steps=2)
    store.add("a", 1)
    store.add("b", 2)
    assert store.charge_step("a") == 1
    assert store.charge_step("a") == 2
    with pytest.raises(sec.StepBudgetExceeded):
        store.charge_step("a")
    assert store.charge_step("b") == 1            # another session has its own budget


def test_max_steps_env(monkeypatch):
    monkeypatch.setenv(sec.MAX_STEPS_ENV, "5")
    assert sec.max_steps_from_env() == 5
    monkeypatch.setenv(sec.MAX_STEPS_ENV, "nope")
    assert sec.max_steps_from_env() == sec.DEFAULT_MAX_STEPS
    monkeypatch.setenv(sec.MAX_STEPS_ENV, "0")
    assert sec.max_steps_from_env() == 1


# --- the real server -------------------------------------------------------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from automation_studio import server

    monkeypatch.setenv(sec.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(sec.PORT_ENV, str(PORT))
    monkeypatch.setenv(sec.ROOT_ENV, str(tmp_path / "projects"))
    server._sessions = sec.SessionStore()
    return TestClient(server.app, base_url=f"http://127.0.0.1:{PORT}")


AUTH = {"Authorization": f"Bearer {TOKEN}"}
WS_HOST = {"Host": f"127.0.0.1:{PORT}"}      # copy it per call: the test client mutates the dict it is given; it would otherwise send Host: testserver on a WebSocket


def _fake_session(server, contract=None):
    from automation_studio.chain import StepName

    class S:
        session_id = "abcd1234"
        goal = "g"
        clarifications: list = []
        steps: dict = {}

        def result(self, step):
            return contract if step == StepName.CONTRACT else None

        def completed_steps(self):
            return []

        def is_design_complete(self):
            return False

        def add_clarification(self, t):
            pass

    server._sessions.add("abcd1234", S())


def test_health_and_page_need_no_token(client):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200


def test_api_without_token_is_401(client):
    assert client.post("/api/sessions", json={"goal": "x"}).status_code == 401
    assert client.get("/api/sessions/abcd1234").status_code == 401
    assert client.post("/api/sessions", json={"goal": "x"}, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_api_with_token_passes_the_gate(client):
    assert client.get("/api/sessions/nope", headers=AUTH).status_code == 404


def test_foreign_host_is_403_even_with_token(client):
    r = client.get("/api/sessions/nope", headers={**AUTH, "Host": "evil.example:8765"})
    assert r.status_code == 403


def test_foreign_origin_is_403_even_with_token(client):
    r = client.get("/api/sessions/nope", headers={**AUTH, "Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.get("/api/sessions/nope", headers={**AUTH, "Origin": "http://localhost:" + str(PORT)})
    assert r.status_code == 404


def test_unconfigured_token_fails_closed(client, monkeypatch):
    monkeypatch.delenv(sec.TOKEN_ENV)
    assert client.get("/api/sessions/nope", headers=AUTH).status_code == 401


def test_scaffold_traversal_is_400(client):
    from automation_studio import server
    _fake_session(server, {"project_yaml": {"name": "automation-x", "description": "d", "owner": "a@b.co", "classification": "internal"}})
    for raw in ["../x", "/etc", "C:\\x", "a/../../b"]:
        r = client.post("/api/sessions/abcd1234/scaffold", json={"out_dir": raw}, headers=AUTH)
        assert r.status_code == 400, raw


def test_scaffold_defaults_to_verification_and_stays_in_root(client, tmp_path, monkeypatch):
    from automation_studio import server
    _fake_session(server, {"project_yaml": {"name": "automation-x", "description": "d", "owner": "a@b.co", "classification": "internal"}})
    seen = {}

    class R:
        success = True
        out_path = tmp_path / "projects" / "automation-x"
        output = "ok"
        error = ""

    def fake(project_yaml, out_base, *, no_verify):
        seen["out_base"] = Path(out_base)
        seen["no_verify"] = no_verify
        return R()

    monkeypatch.setattr(server, "scaffold_from_contract", fake)
    r = client.post("/api/sessions/abcd1234/scaffold", json={}, headers=AUTH)
    assert r.status_code == 200
    assert seen["no_verify"] is False
    assert seen["out_base"] == (tmp_path / "projects").resolve()
    # only a real JSON true skips verification
    client.post("/api/sessions/abcd1234/scaffold", json={"no_verify": "true"}, headers=AUTH)
    assert seen["no_verify"] is False
    client.post("/api/sessions/abcd1234/scaffold", json={"no_verify": True, "out_dir": "sub"}, headers=AUTH)
    assert seen["no_verify"] is True
    assert seen["out_base"] == (tmp_path / "projects" / "sub").resolve()


def test_session_cap_returns_429(client, monkeypatch):
    from automation_studio import server

    class FakeSession:
        session_id = "x"

    n = {"i": 0}

    def start(goal, ctx):
        n["i"] += 1
        s = FakeSession()
        s.session_id = f"s{n['i']:07d}"
        return s

    monkeypatch.setattr(server.DesignSession, "start", staticmethod(start))
    monkeypatch.setattr(server, "_get_ctx", lambda *a, **k: None)
    server._sessions = sec.SessionStore(max_sessions=2)
    assert client.post("/api/sessions", json={"goal": "a"}, headers=AUTH).status_code == 200
    assert client.post("/api/sessions", json={"goal": "b"}, headers=AUTH).status_code == 200
    assert client.post("/api/sessions", json={"goal": "c"}, headers=AUTH).status_code == 429


def test_expired_session_is_404(client):
    from automation_studio import server
    clock = Clock()
    server._sessions = sec.SessionStore(idle_ttl_s=60, clock=clock)
    _fake_session(server)
    assert client.get("/api/sessions/abcd1234", headers=AUTH).status_code == 200
    clock.t += 61
    assert client.get("/api/sessions/abcd1234", headers=AUTH).status_code == 404


def test_websocket_wrong_host_is_refused(client):
    from starlette.websockets import WebSocketDisconnect
    from automation_studio import server
    _fake_session(server)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/abcd1234/goal_refinement", subprotocols=["autostation.v1", f"token.{TOKEN}"],
                                      headers={"Host": "evil.example:8765"}):
            pass


def test_websocket_requires_token_and_origin(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    from automation_studio import server
    _fake_session(server)
    monkeypatch.setattr(server, "run_step", lambda s, step: {"ok": True})
    url = "/ws/abcd1234/goal_refinement"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, headers=dict(WS_HOST)):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, subprotocols=["autostation.v1", "token.wrong"], headers=dict(WS_HOST)):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, subprotocols=["autostation.v1", f"token.{TOKEN}"], headers={**WS_HOST, "Origin": "http://evil.example"}):
            pass
    with client.websocket_connect(url, subprotocols=["autostation.v1", f"token.{TOKEN}"], headers=dict(WS_HOST)) as ws:
        assert ws.accepted_subprotocol == "autostation.v1"


def test_websocket_step_budget(client, monkeypatch):
    from automation_studio import server
    _fake_session(server)
    server._sessions.max_steps = 1
    monkeypatch.setattr(server, "run_step", lambda s, step: {"ok": True})
    sub = ["autostation.v1", f"token.{TOKEN}"]
    with client.websocket_connect("/ws/abcd1234/goal_refinement", subprotocols=sub, headers=dict(WS_HOST)) as ws:
        assert ws.receive_json()["type"] == "start"
        assert ws.receive_json()["type"] == "result"
    with client.websocket_connect("/ws/abcd1234/goal_refinement", subprotocols=sub, headers=dict(WS_HOST)) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and "step runs" in msg["message"]
