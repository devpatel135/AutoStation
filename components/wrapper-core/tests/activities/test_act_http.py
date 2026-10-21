from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from wrapper_core import retry, runctx
from wrapper_core.activities import http
from wrapper_core.retry import AuthExpired, DataError, NetworkError, SystemUnavailable


class State:
    def __init__(self):
        self.hits = {}
        self.token_calls = 0
        self.seen_auth = []
        self.token_valid = "tok-1"


@pytest.fixture
def server():
    st = State()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _j(self, code, obj, extra=None):
            self._send(code, json.dumps(obj).encode(), extra=extra)

        def do_GET(self):
            u = urlsplit(self.path)
            q = parse_qs(u.query)
            st.hits[u.path] = st.hits.get(u.path, 0) + 1
            st.seen_auth.append(self.headers.get("Authorization"))
            if u.path == "/ok":
                self._j(200, {"hello": "world", "q": q})
            elif u.path == "/flaky":
                if st.hits[u.path] <= 2:
                    self._send(503, b"busy", "text/plain", {"Retry-After": "0"})
                else:
                    self._j(200, {"ok": True})
            elif u.path == "/always503":
                self._send(503, b"down", "text/plain")
            elif u.path == "/401":
                self._send(401, b"nope", "text/plain")
            elif u.path == "/404":
                self._send(404, b"gone", "text/plain")
            elif u.path == "/429":
                self._send(429, b"slow down", "text/plain", {"Retry-After": "7"})
            elif u.path == "/badjson":
                self._send(200, b"<html>not json</html>", "text/html")
            elif u.path == "/protected":
                if self.headers.get("Authorization") == "Bearer " + st.token_valid:
                    self._j(200, {"secret": "data"})
                else:
                    self._send(401, b"", "text/plain")
            elif u.path == "/pages":
                page = int(q.get("page", ["1"])[0])
                self._j(200, {"items": list(range((page - 1) * 3, page * 3)) if page <= 3 else []})
            elif u.path == "/offset":
                off, lim = int(q["offset"][0]), int(q["limit"][0])
                self._j(200, [i for i in range(7)][off:off + lim])
            elif u.path == "/link":
                page = int(q.get("p", ["1"])[0])
                extra = {"Link": f'<http://127.0.0.1:{self.server.server_port}/link?p={page + 1}>; rel="next"'} if page < 3 else {}
                self._j(200, [page * 10 + i for i in range(2)], extra)
            elif u.path == "/cursor":
                cur = q.get("cursor", ["0"])[0]
                nxt = {"0": "a", "a": "b", "b": None}[cur]
                self._j(200, {"data": {"rows": [cur]}, "meta": {"next": nxt}})
            elif u.path == "/file":
                self._send(200, b"file-bytes" * 1000, "application/octet-stream")
            else:
                self._send(404, b"?", "text/plain")

        def do_POST(self):
            u = urlsplit(self.path)
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n)
            st.hits[u.path] = st.hits.get(u.path, 0) + 1
            if u.path == "/echo":
                self._j(200, {"json": json.loads(body or b"null"), "ctype": self.headers.get("Content-Type")})
            elif u.path == "/token":
                st.token_calls += 1
                form = parse_qs(body.decode())
                if form.get("client_secret") != ["s3cret"]:
                    self._send(401, b"bad client", "text/plain")
                else:
                    st.token_valid = f"tok-{st.token_calls}"
                    self._j(200, {"access_token": st.token_valid, "expires_in": 3600})
            elif u.path == "/post503":
                self._send(503, b"down", "text/plain")
            else:
                self._send(404, b"?", "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    st.base = f"http://127.0.0.1:{srv.server_port}"
    yield st
    srv.shutdown()
    srv.server_close()


def cli(server, sleeps=None, **kw):
    return http.client(server.base, sleep=(sleeps.append if sleeps is not None else (lambda s: None)), **kw)


def test_get_json_and_params_and_post_json(server):
    c = cli(server)
    assert c.get_json("/ok", params={"a": [1, 2], "b": "x y"})["q"] == {"a": ["1", "2"], "b": ["x y"]}
    r = c.post("/echo", json={"n": 1})
    assert r.json() == {"json": {"n": 1}, "ctype": "application/json"} and r.status == 200


def test_retries_5xx_with_backoff_and_retry_after(server):
    sleeps = []
    c = cli(server, sleeps)
    assert c.get_json("/flaky") == {"ok": True}
    assert len(sleeps) == 2 and sleeps[0] == 0.0                      # honoured Retry-After: 0
    sleeps.clear()
    with pytest.raises(SystemUnavailable):
        c.get("/always503")
    assert sleeps == [0.5, 1.0, 2.0]                                   # exponential backoff, then give up (retries=3)


def test_retry_after_is_honoured_and_capped(server):
    sleeps = []
    c = cli(server, sleeps, retries=1)
    with pytest.raises(SystemUnavailable):
        c.get("/429")
    assert sleeps == [7.0]


def test_post_is_not_retried_unless_asked(server):
    sleeps = []
    c = cli(server, sleeps)
    with pytest.raises(SystemUnavailable):
        c.post("/post503", json={})
    assert sleeps == [] and server.hits["/post503"] == 1
    c2 = cli(server, sleeps, retry_post=True, retries=2)
    with pytest.raises(SystemUnavailable):
        c2.post("/post503", json={})
    assert server.hits["/post503"] == 1 + 3


def test_status_classification_feeds_the_retry_engine(server):
    c = cli(server)
    with pytest.raises(AuthExpired) as e1:
        c.get("/401")
    assert e1.value.status == 401 and retry.classify_exception(e1.value) is retry.Category.AUTH_EXPIRED
    with pytest.raises(DataError) as e2:
        c.get("/404")
    assert retry.classify_exception(e2.value) is retry.Category.DATA_ERROR and server.hits["/404"] == 1   # 4xx never retried
    with pytest.raises(DataError):
        c.get("/badjson").json()


def test_network_error_and_timeout_are_retried_then_classified(server):
    sleeps = []
    c = http.client("http://127.0.0.1:1", sleep=sleeps.append, retries=2, timeout=2)
    with pytest.raises(NetworkError) as ei:
        c.get("/x")
    assert retry.classify_exception(ei.value) is retry.Category.NETWORK_ERROR and len(sleeps) == 2


def test_basic_auth_and_bearer_headers(server):
    c = cli(server, auth=("user", "pw"))
    c.get("/ok")
    assert server.seen_auth[-1].startswith("Basic ")
    cli(server, auth=http.BearerToken("abc")).get("/ok")
    assert server.seen_auth[-1] == "Bearer abc"
    assert "abc" not in repr(http.BearerToken("abc")) and "pw" not in repr(http.BasicAuth("user", "pw"))


def test_oauth2_client_credentials_caches_and_refreshes_on_401(server):
    now = [1000.0]
    auth = http.OAuth2ClientCredentials(server.base + "/token", "cid", "s3cret", scope="read", clock=lambda: now[0])
    c = cli(server, auth=auth)
    assert c.get_json("/protected") == {"secret": "data"}
    assert c.get_json("/protected") == {"secret": "data"}
    assert server.token_calls == 1                                      # cached
    server.token_valid = "revoked-server-side"                         # token revoked: 401 -> refresh once -> success
    assert c.get_json("/protected") == {"secret": "data"} and server.token_calls == 2
    now[0] += 3600                                                      # expiry (minus the 60 s margin) forces a refresh
    c.get_json("/protected")
    assert server.token_calls == 3
    bad = http.OAuth2ClientCredentials(server.base + "/token", "cid", "WRONG")
    with pytest.raises(AuthExpired):
        cli(server, auth=bad).get("/protected")
    assert "s3cret" not in repr(auth)


def test_secrets_and_query_strings_are_never_logged(server):
    auth = http.OAuth2ClientCredentials(server.base + "/token", "cid", "s3cret")
    cli(server, auth=auth).get("/protected", params={"api_key": "QUERY-SECRET"})
    blob = json.dumps(runctx.current().read_events())
    assert "QUERY-SECRET" not in blob and "tok-1" not in blob and "s3cret" not in blob
    ev = [e for e in runctx.current().read_events() if e["data"].get("activity") == "http.request"][0]["data"]
    assert ev["path"] == "/protected" and ev["status"] == 200 and "duration_ms" in ev


def test_absolute_url_on_other_origin_is_refused(server):
    c = cli(server, auth=http.BearerToken("abc"))
    with pytest.raises(DataError):
        c.get("http://evil.example.com/steal")
    assert c.get_json(server.base + "/ok")["hello"] == "world"          # same origin absolute URL is fine


def test_pagination_styles(server):
    c = cli(server)
    assert list(c.paginate("/pages", style="page", items_key="items")) == list(range(9))
    assert list(c.paginate("/offset", style="offset", per_page=3)) == list(range(7))
    assert list(c.paginate("/link", params={"p": 1}, style="link")) == [10, 11, 20, 21, 30, 31]
    assert list(c.paginate("/cursor", style="next_key", items_key="data.rows", next_key="meta.next", cursor_param="cursor")) == ["0", "a", "b"]
    with pytest.raises(DataError):
        list(c.paginate("/ok", style="page"))                            # body is a dict without items_key
    with pytest.raises(DataError):
        list(c.paginate("/pages", style="page", items_key="items", max_pages=2))


def test_download_verifies_sha256_and_leaves_nothing_on_failure(server, tmp_path):
    data = b"file-bytes" * 1000
    dest = tmp_path / "d" / "f.bin"
    assert http.download(server.base + "/file", dest, sha256=hashlib.sha256(data).hexdigest()) == str(dest)
    assert dest.read_bytes() == data
    bad = tmp_path / "bad.bin"
    with pytest.raises(DataError):
        http.download(server.base + "/file", bad, sha256="0" * 64)
    assert not bad.exists() and not list(tmp_path.glob("*.part"))
    with pytest.raises(DataError):
        http.download(server.base + "/404", tmp_path / "n.bin")
    assert cli(server).download("/file", tmp_path / "c.bin") and (tmp_path / "c.bin").stat().st_size == len(data)
    with pytest.raises(DataError):
        http.download(server.base + "/file", tmp_path / "big.bin", max_bytes=100)
