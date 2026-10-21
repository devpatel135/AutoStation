from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wrapper_core import runctx
from wrapper_core.activities import connectors as cn
from wrapper_core.activities import http
from wrapper_core.retry import AuthExpired, DataError, SystemUnavailable


class Fake:
    """A scriptable local HTTP server. ``routes[(method, path)]`` is a list of (status, headers, body) answered in order (the last one repeats)."""

    def __init__(self):
        self.requests = []
        self.routes = {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.requests.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                key = (self.command, self.path.split("?")[0])
                seq = fake.routes.get(key) or fake.routes.get(("*", "*")) or [(404, {}, b"{}")]
                i = sum(1 for r in fake.requests[:-1] if (r["method"], r["path"].split("?")[0]) == key)
                status, headers, payload = seq[min(i, len(seq) - 1)]
                if callable(payload):
                    payload = payload(fake)
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = _do

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.th = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.th.start()

    def on(self, method, path, *responses):
        self.routes[(method, path)] = list(responses)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fake():
    f = Fake()
    cn.clear_token_cache()
    yield f
    f.close()


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("AMX_CRED_GRAPH_APP", json.dumps({"username": "client-id", "password": "client-secret-VALUE"}))
    monkeypatch.setenv("AMX_CRED_JIRA_BOT", "bot@example.com:jira-token-VALUE")


OK_TOKEN = (200, {}, {"access_token": "tok-123", "expires_in": 3600})


def graph(fake, **kw):
    fake.on("POST", "/token", OK_TOKEN)
    return cn.Graph("graph-app", tenant="contoso.onmicrosoft.com", base_url=fake.base + "/v1.0", token_url=fake.base + "/token", allow_test_hosts=True, **kw)


# --- URL / host guards ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("url,ok", [
    ("https://graph.microsoft.com/v1.0", True),
    ("http://graph.microsoft.com/v1.0", False),
    ("https://graph.microsoft.com.evil.example/", False),
    ("https://evil.example/https://graph.microsoft.com", False),
    ("https://user:pw@graph.microsoft.com/", False),
    ("https://graph.microsoft.com:8443/", False),
    ("https://graph.microsoft.com/x" + chr(92) + "y", False),
    ("ftp://graph.microsoft.com/", False),
    ("file:///c:/windows/win.ini", False),
    ("", False),
])
def test_check_url_is_strict(url, ok):
    if ok:
        assert cn.check_url(url, cn.GRAPH_HOSTS) == url
    else:
        with pytest.raises(DataError):
            cn.check_url(url, cn.GRAPH_HOSTS)


def test_wildcard_hosts_need_a_real_subdomain():
    assert cn.check_url("https://contoso.atlassian.net", cn.JIRA_HOSTS)
    for bad in ("https://atlassian.net", "https://evilatlassian.net", "https://contoso.atlassian.net.evil.example"):
        with pytest.raises(DataError):
            cn.check_url(bad, cn.JIRA_HOSTS)


def test_plain_http_loopback_only_in_test_mode(monkeypatch):
    with pytest.raises(DataError):
        cn.check_url("http://127.0.0.1:9/x", cn.GRAPH_HOSTS)
    assert cn.check_url("http://127.0.0.1:9/x", cn.GRAPH_HOSTS, allow_test_hosts=True)
    with pytest.raises(DataError):
        cn.check_url("http://evil.example/x", cn.GRAPH_HOSTS, allow_test_hosts=True)       # test mode never opens other hosts
    monkeypatch.setenv("AMX_ALLOW_TEST_HOSTS", "1")
    assert cn.check_url("http://localhost:9/x", cn.GRAPH_HOSTS)


# --- OAuth2 -------------------------------------------------------------------------------------------------------
def test_token_is_fetched_once_and_cached(fake):
    fake.on("POST", "/token", OK_TOKEN)
    args = (fake.base + "/token", "graph-app")
    kw = {"scope": "s", "allowed_hosts": ["127.0.0.1"], "allow_test_hosts": True}
    assert cn.oauth2_token(*args, **kw) == "tok-123" and cn.oauth2_token(*args, **kw) == "tok-123"
    assert len(fake.requests) == 1
    form = fake.requests[0]["body"].decode()
    assert "grant_type=client_credentials" in form and "client_id=client-id" in form and "client-secret-VALUE" in form


def test_client_secret_only_goes_to_allow_listed_token_hosts(fake):
    with pytest.raises(ValueError):
        cn.oauth2_token(fake.base + "/token", "graph-app", allowed_hosts=[])
    with pytest.raises(DataError):
        cn.oauth2_token("https://evil.example/token", "graph-app", allowed_hosts=["login.microsoftonline.com"])
    assert fake.requests == []


def test_token_endpoint_failure_is_classified(fake):
    fake.on("POST", "/token", (401, {}, {"error": "invalid_client"}))
    with pytest.raises(AuthExpired):
        cn.oauth2_token(fake.base + "/token", "graph-app", allowed_hosts=["127.0.0.1"], allow_test_hosts=True)


def test_secrets_never_appear_in_telemetry_or_reprs(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMAGIX_RUN_DIR", str(tmp_path / "run"))
    runctx.set_current(None)
    g = graph(fake)
    fake.on("POST", "/v1.0/users/robot@contoso.com/sendMail", (202, {}, b""))
    g.send_mail("robot@contoso.com", "ap@contoso.com", "hi", "body")
    events = json.dumps(runctx.current().read_events(), default=str)
    text = events + repr(g.client.auth) + repr(g.client)
    for secret in ("client-secret-VALUE", "tok-123"):
        assert secret not in text
    assert "client-id" not in events                                        # the client id is not a secret, but telemetry has no business with it


def test_no_parameter_accepts_a_raw_secret():
    import inspect
    for fn in (cn.Graph.__init__, cn.Jira.__init__, cn.teams_post, cn.slack_post, cn.oauth2_auth):
        names = set(inspect.signature(fn).parameters)
        assert not names & {"token", "password", "secret", "client_secret", "webhook_url", "api_key", "access_token"}, fn


# --- Graph --------------------------------------------------------------------------------------------------------
def test_send_mail_builds_the_right_request(fake):
    g = graph(fake)
    fake.on("POST", "/v1.0/users/robot@contoso.com/sendMail", (202, {}, b""))
    g.send_mail("robot@contoso.com", ["ap@contoso.com", "x@contoso.com"], "Report\r\nBcc: evil@x.io", "<b>hi</b>", html=True, cc="boss@contoso.com")
    req = fake.requests[-1]
    assert req["headers"]["authorization"] == "Bearer tok-123"
    msg = json.loads(req["body"])["message"]
    assert msg["subject"] == "Report Bcc: evil@x.io" and msg["body"]["contentType"] == "HTML"
    assert [r["emailAddress"]["address"] for r in msg["toRecipients"]] == ["ap@contoso.com", "x@contoso.com"] and msg["ccRecipients"][0]["emailAddress"]["address"] == "boss@contoso.com"


@pytest.mark.parametrize("to", ["not-an-address", "a@b.co\nBcc: evil@x.io", ["ok@x.io", "bad"], [], "a@b", ["a@b.co"] * 101])
def test_send_mail_rejects_bad_recipients(fake, to):
    g = graph(fake)
    with pytest.raises(ValueError):
        g.send_mail("robot@contoso.com", to, "s", "b")
    assert not [r for r in fake.requests if r["path"].endswith("sendMail")]


def test_429_is_retried_for_post_but_503_is_not(fake):
    g = graph(fake)
    path = "/v1.0/users/robot@contoso.com/sendMail"
    fake.on("POST", path, (429, {"Retry-After": "0"}, b"{}"), (429, {"Retry-After": "0"}, b"{}"), (202, {}, b""))
    g.send_mail("robot@contoso.com", "ap@contoso.com", "s", "b")
    assert len([r for r in fake.requests if r["path"] == path]) == 3
    fake.requests.clear()
    fake.on("POST", path, (503, {}, b"{}"), (202, {}, b""))
    with pytest.raises(SystemUnavailable):
        g.send_mail("robot@contoso.com", "ap@contoso.com", "s", "b")
    assert len([r for r in fake.requests if r["path"] == path]) == 1                  # never double-send after a server error


def test_expired_token_is_refreshed_once_on_401(fake):
    g = graph(fake)
    path = "/v1.0/users/robot@contoso.com/sendMail"
    fake.on("POST", path, (401, {}, b"{}"), (202, {}, b""))
    g.send_mail("robot@contoso.com", "ap@contoso.com", "s", "b")
    assert len([r for r in fake.requests if r["path"] == "/token"]) == 2


def test_list_files_follows_next_links_but_only_on_the_same_host(fake):
    g = graph(fake)
    page1 = {"value": [{"id": "1", "name": "a.txt", "size": 5}], "@odata.nextLink": fake.base + "/v1.0/next"}
    fake.on("GET", "/v1.0/users/robot@contoso.com/drive/root:/Reports/2026:/children", (200, {}, page1))
    fake.on("GET", "/v1.0/next", (200, {}, {"value": [{"id": "2", "name": "sub", "folder": {}}]}))
    files = g.list_files("Reports/2026", user="robot@contoso.com")
    assert [f["name"] for f in files] == ["a.txt", "sub"] and files[1]["folder"] is True
    fake.on("GET", "/v1.0/users/robot@contoso.com/drive/root/children", (200, {}, {"value": [], "@odata.nextLink": "https://evil.example/steal"}))
    with pytest.raises(DataError):
        g.list_files("", user="robot@contoso.com")


@pytest.mark.parametrize("remote", ["../secret", "a/../../b", "a" + chr(92) + "b", "x/./y", "a" * 401, "bad" + chr(0) + "name"])
def test_remote_paths_are_validated(fake, remote):
    g = graph(fake)
    with pytest.raises(DataError):
        g.list_files(remote, user="robot@contoso.com")
    assert not [r for r in fake.requests if r["path"] != "/token"]


def test_owner_must_be_exactly_one_of_user_or_drive(fake):
    g = graph(fake)
    with pytest.raises(ValueError):
        g.list_files("", user="robot@contoso.com", drive_id="b!abc")
    with pytest.raises(ValueError):
        g.list_files("")
    with pytest.raises(ValueError):
        g.list_files("", drive_id="b!abc/../x")


def test_upload_and_download_round_trip(fake, tmp_path):
    g = graph(fake)
    src = tmp_path / "report.csv"
    src.write_bytes(b"a,b\n1,2\n")
    up = "/v1.0/drives/b%21abc/root:/Reports/my%20file.csv:/content"
    fake.on("PUT", up, (201, {}, {"id": "9", "name": "my file.csv", "size": 8}))
    meta = g.upload_file(src, "Reports/my file.csv", drive_id="b!abc", conflict="rename")
    put = [r for r in fake.requests if r["method"] == "PUT"][0]
    assert meta["id"] == "9" and put["body"] == b"a,b\n1,2\n" and "conflictBehavior=rename" in put["path"]
    # download: Graph redirects to a pre-authenticated storage URL on ANOTHER origin - the bearer token must not follow
    other = Fake()
    try:
        other.on("GET", "/blob", (200, {}, b"file-bytes"))
        fake.on("GET", up, (302, {"Location": other.base + "/blob"}, b""))
        dest = tmp_path / "out" / "got.bin"
        g.get_file("Reports/my file.csv", dest, drive_id="b!abc")
        assert dest.read_bytes() == b"file-bytes"
        assert "authorization" not in other.requests[0]["headers"]
    finally:
        other.close()


def test_upload_limits_and_conflict_validation(fake, tmp_path):
    g = graph(fake)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (cn.MAX_SIMPLE_UPLOAD + 1))
    with pytest.raises(DataError, match="upload session"):
        g.upload_file(big, "big.bin", drive_id="b!abc")
    with pytest.raises(DataError):
        g.upload_file(tmp_path / "missing.bin", "x", drive_id="b!abc")
    with pytest.raises(ValueError):
        g.upload_file(big, "x", drive_id="b!abc", conflict="overwrite-everything")
    assert not [r for r in fake.requests if r["method"] == "PUT"]


def test_graph_constructor_validates(fake):
    with pytest.raises(ValueError):
        cn.Graph("graph-app", tenant="bad tenant!", allow_test_hosts=True)
    with pytest.raises(DataError):
        cn.Graph("graph-app", tenant="contoso.onmicrosoft.com", base_url="https://evil.example/v1.0")
    with pytest.raises(DataError):
        cn.Graph("graph-app", tenant="contoso.onmicrosoft.com", token_url="https://evil.example/token")


# --- webhooks -----------------------------------------------------------------------------------------------------
def test_slack_post_hides_the_secret_webhook_path(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMAGIX_RUN_DIR", str(tmp_path / "run"))
    runctx.set_current(None)
    monkeypatch.setenv("AMX_CRED_SLACK_HOOK", fake.base + "/services/T000/B000/SUPERSECRETPART")
    fake.on("POST", "/services/T000/B000/SUPERSECRETPART", (200, {}, b"ok"))
    cn.slack_post("slack-hook", "Nightly run finished", allow_test_hosts=True)
    assert json.loads(fake.requests[0]["body"]) == {"text": "Nightly run finished", "link_names": 0}
    assert "SUPERSECRETPART" not in json.dumps(runctx.current().read_events(), default=str)
    fake.on("POST", "/services/T000/B000/SUPERSECRETPART", (500, {}, b"boom"))
    with pytest.raises(SystemUnavailable) as ei:
        cn.slack_post("slack-hook", "again", allow_test_hosts=True)
    assert "SUPERSECRETPART" not in str(ei.value)


def test_teams_post_card_and_host_allow_list(fake, monkeypatch):
    monkeypatch.setenv("AMX_CRED_TEAMS_HOOK", fake.base + "/webhookb2/abc")
    fake.on("POST", "/webhookb2/abc", (200, {}, b"1"))
    cn.teams_post("teams-hook", "Deploy done", title="Release", allow_test_hosts=True)
    card = json.loads(fake.requests[0]["body"])
    assert card["text"] == "Deploy done" and card["title"] == "Release"
    monkeypatch.setenv("AMX_CRED_BAD_HOOK", "https://evil.example/hook")
    with pytest.raises(DataError):
        cn.teams_post("bad-hook", "x")
    with pytest.raises(DataError):
        cn.slack_post("teams-hook", "x")                                  # a Teams-looking URL is not a Slack webhook host
    n = len(fake.requests)
    with pytest.raises(ValueError):
        cn.slack_post("teams-hook", "x" * 20001, allow_test_hosts=True)
    with pytest.raises(ValueError):
        cn.slack_post("teams-hook", "  ", allow_test_hosts=True)
    assert len(fake.requests) == n


# --- Jira ----------------------------------------------------------------------------------------------------------
def jira(fake):
    return cn.Jira(fake.base, "jira-bot", allow_test_hosts=True)


def test_create_issue(fake):
    fake.on("POST", "/rest/api/2/issue", (201, {}, {"id": "10001", "key": "OPS-7"}))
    out = jira(fake).create_issue("OPS", "Portal down\nnow", "Login page returns 503", labels=["robot", "p1"])
    req = fake.requests[0]
    body = json.loads(req["body"])["fields"]
    assert out == {"key": "OPS-7", "id": "10001"} and body["summary"] == "Portal down now" and body["labels"] == ["robot", "p1"] and body["project"] == {"key": "OPS"}
    assert req["headers"]["authorization"] == "Basic " + base64.b64encode(b"bot@example.com:jira-token-VALUE").decode()


@pytest.mark.parametrize("kw", [{"project": "ops"}, {"project": "OPS;DROP"}, {"project": "O"}, {"project": "OPS", "labels": ["has space"]},
                                {"project": "OPS", "labels": ["x"] * 21}, {"project": "OPS", "summary": " "}, {"project": "OPS", "summary": "s" * 256}])
def test_create_issue_validates(fake, kw):
    args = {"project": "OPS", "summary": "s", **kw}
    with pytest.raises(ValueError):
        jira(fake).create_issue(**args)
    assert fake.requests == []


def test_transition_by_name_and_unknown_transition(fake):
    fake.on("GET", "/rest/api/2/issue/OPS-7/transitions", (200, {}, {"transitions": [{"id": "31", "name": "Done"}, {"id": "21", "name": "In Progress"}]}))
    fake.on("POST", "/rest/api/2/issue/OPS-7/transitions", (204, {}, b""))
    j = jira(fake)
    j.transition_issue("OPS-7", "done", comment="fixed")
    post = [r for r in fake.requests if r["method"] == "POST"][0]
    body = json.loads(post["body"])
    assert body["transition"] == {"id": "31"} and body["update"]["comment"][0]["add"]["body"] == "fixed"
    with pytest.raises(DataError, match="available: Done, In Progress"):
        j.transition_issue("OPS-7", "Reopen")
    with pytest.raises(ValueError):
        j.transition_issue("ops-7/../x", "Done")


def test_jira_host_must_be_atlassian_or_explicitly_allowed():
    with pytest.raises(DataError):
        cn.Jira("https://evil.example", "jira-bot")
    with pytest.raises(DataError):
        cn.Jira("http://contoso.atlassian.net", "jira-bot")
    assert cn.Jira("https://jira.corp.example", "jira-bot", allowed_hosts=["jira.corp.example"]).base_url == "https://jira.corp.example"


def test_http_client_redact_path_hides_paths_in_errors(fake):
    fake.on("GET", "/very/secret/path", (500, {}, b"x"))
    c = http.Client(fake.base, redact_path=True, retries=0)
    with pytest.raises(SystemUnavailable):
        c.get("/very/secret/path")
