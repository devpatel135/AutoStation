"""HTTP / REST activities (UiPath: HTTP Request, Deserialize JSON, Download File) over plain urllib.

Errors are raised as the platform's classified exceptions so ``wrapper.run_item`` and the retry engine need no extra code:

    401/403                     -> AuthExpired        (OAuth2 clients refresh the token once on 401 before raising)
    408/429/5xx                 -> SystemUnavailable  (retried here with backoff for idempotent methods, honouring Retry-After)
    other 4xx / bad JSON        -> DataError          (never retried)
    DNS / connect / timeout/TLS -> NetworkError       (retried here for idempotent methods)

Tokens, passwords and query strings are never logged (only method, host, path, status, duration).
"""
from __future__ import annotations

import hashlib
import json as _json
import os
import re
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from ..retry import AuthExpired, DataError, NetworkError, SystemUnavailable
from ._common import emit

IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
MAX_BODY = 50 * 1024 * 1024


class Response:
    def __init__(self, status: int, headers: Dict[str, str], content: bytes, url: str):
        self.status, self.headers, self.content, self.url = status, headers, content, url

    @property
    def text(self) -> str:
        m = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.I)
        return self.content.decode(m.group(1) if m else "utf-8", "replace")

    def json(self) -> Any:
        try:
            return _json.loads(self.content.decode("utf-8-sig"))
        except ValueError as exc:
            raise DataError(f"response is not valid JSON (HTTP {self.status}, {len(self.content)} bytes)") from exc

    def __repr__(self) -> str:
        return f"<Response {self.status} {len(self.content)} bytes>"


def classify_status(status: int, detail: str = "") -> Exception:
    msg = f"HTTP {status}" + (f": {detail[:200]}" if detail else "")
    if status in (401, 403):
        exc: Exception = AuthExpired(msg)
    elif status in (408, 425, 429) or status >= 500:
        exc = SystemUnavailable(msg)
    else:
        exc = DataError(msg)
    exc.status = status  # type: ignore[attr-defined]
    return exc


# --- auth ------------------------------------------------------------------------------------------------
class BearerToken:
    def __init__(self, token: str):
        self._token = token

    def apply(self, headers: Dict[str, str]) -> None:
        headers["Authorization"] = "Bearer " + self._token

    def invalidate(self) -> None:  # nothing to refresh
        pass

    def __repr__(self) -> str:
        return "BearerToken(***)"


class BasicAuth:
    def __init__(self, username: str, password: str):
        import base64
        self._v = "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")

    def apply(self, headers: Dict[str, str]) -> None:
        headers["Authorization"] = self._v

    def invalidate(self) -> None:
        pass

    def __repr__(self) -> str:
        return "BasicAuth(***)"


class OAuth2ClientCredentials:
    """OAuth2 client-credentials grant with an in-memory token cache (refreshed 60 s early; thread-safe)."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, scope: Optional[str] = None,
                 audience: Optional[str] = None, timeout: float = 30, clock: Callable[[], float] = time.time):
        self.token_url, self.client_id, self._secret = token_url, client_id, client_secret
        self.scope, self.audience, self.timeout, self._clock = scope, audience, timeout, clock
        self._token: Optional[str] = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires - 60:
                return self._token
            form = {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self._secret}
            if self.scope:
                form["scope"] = self.scope
            if self.audience:
                form["audience"] = self.audience
            _check_scheme(self.token_url)
            req = urllib.request.Request(self.token_url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
            try:
                with _urlopen(req, self.timeout) as r:  # noqa: S310 - scheme checked above
                    body = _json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raise AuthExpired(f"token endpoint returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, OSError, socket.timeout) as exc:
                raise NetworkError(f"token endpoint unreachable: {type(exc).__name__}") from exc
            except ValueError as exc:
                raise DataError("token endpoint did not return JSON") from exc
            tok = body.get("access_token")
            if not tok:
                raise AuthExpired("token endpoint response has no access_token")
            self._token = tok
            self._expires = self._clock() + float(body.get("expires_in", 300))
            return tok

    def apply(self, headers: Dict[str, str]) -> None:
        headers["Authorization"] = "Bearer " + self.token()

    def invalidate(self) -> None:
        with self._lock:
            self._token, self._expires = None, 0.0

    def __repr__(self) -> str:
        return f"OAuth2ClientCredentials(token_url={self.token_url!r}, client_id={self.client_id!r}, secret=***)"


def _origin(url: str) -> Tuple[str, str]:
    p = urllib.parse.urlsplit(url)
    return p.scheme.lower(), p.netloc.lower()


def _is_loopback(host: Optional[str]) -> bool:
    return (host or "").lower() in ("localhost", "127.0.0.1", "::1")


def _check_scheme(url: str) -> str:
    """Only http(s) URLs may be requested: urllib would otherwise happily open file:// and ftp:// (local file read / SSRF)."""
    p = urllib.parse.urlsplit(url)
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        raise DataError(f"refusing URL with scheme {p.scheme or '(none)'!r}: only http and https are allowed")
    return url


_KEEP_ON_CROSS_ORIGIN = ("accept", "accept-language", "user-agent")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects stay on http(s), never downgrade https -> http, and cross-origin hops lose every credential-bearing header
    (Authorization, Cookie, API-key style custom headers - everything except a small harmless allow-list)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_scheme(newurl)
        old, new_p = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if old.scheme.lower() == "https" and new_p.scheme.lower() != "https" and not _is_loopback(new_p.hostname):
            raise DataError("refusing a redirect from https to http")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for store in (new.headers, new.unredirected_hdrs):
                for k in list(store):
                    if k.lower() not in _KEEP_ON_CROSS_ORIGIN:
                        del store[k]
        return new


def _urlopen(req: "urllib.request.Request", timeout: float, context: Any = None):
    """urlopen with the safe redirect policy (and an optional TLS context)."""
    handlers: List[Any] = [_SafeRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


def _dig(obj: Any, dotted: Optional[str]) -> Any:
    if not dotted:
        return obj
    for part in dotted.split("."):
        obj = obj[part] if isinstance(obj, dict) else obj[int(part)]
    return obj


class Client:
    def __init__(self, base_url: str = "", headers: Optional[Dict[str, str]] = None, auth: Any = None, retries: int = 3,
                 timeout: float = 30, backoff: float = 0.5, retry_post: bool = False, ca_file: Optional[str] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        if isinstance(auth, tuple) and len(auth) == 2:
            auth = BasicAuth(*auth)
        self.auth = auth
        self.retries, self.timeout, self.backoff, self.retry_post, self._sleep = retries, timeout, backoff, retry_post, sleep
        self._ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None

    # -- url building
    def _url(self, path: str, params: Optional[Dict[str, Any]]) -> str:
        if re.match(r"^[a-z][a-z0-9+.-]*://", path, re.I):
            if self.base_url and _origin(path) != _origin(self.base_url):
                raise DataError("refusing an absolute URL on a different origin than base_url (credentials would leak)")
            url = path
        else:
            url = f"{self.base_url}/{path.lstrip('/')}" if self.base_url else path
        _check_scheme(url)
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        return url

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json: Any = None, data: Any = None,
                headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None) -> Response:
        method = method.upper()
        url = self._url(path, params)
        body: Optional[bytes] = None
        hdrs = {"Accept": "application/json", "User-Agent": "wrapper-core-activities", **self.headers, **(headers or {})}
        if json is not None:
            body = _json.dumps(json).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif data is not None:
            if isinstance(data, dict):
                body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
            else:
                body = data if isinstance(data, bytes) else str(data).encode("utf-8")
        retryable_method = method in IDEMPOTENT or self.retry_post
        auth_refreshed = False
        attempt = 0
        host = urllib.parse.urlsplit(url).netloc
        log_path = urllib.parse.urlsplit(url).path  # never the query string
        while True:
            h = dict(hdrs)
            if self.auth is not None:
                self.auth.apply(h)
            t0 = time.monotonic()
            try:
                req = urllib.request.Request(url, data=body, headers=h, method=method)
                with _urlopen(req, timeout or self.timeout, self._ctx) as r:  # noqa: S310 - scheme checked in _url
                    content = r.read(MAX_BODY + 1)
                    if len(content) > MAX_BODY:
                        raise DataError("response body exceeds the 50 MB limit; use download()")
                    resp = Response(r.status, {k.lower(): v for k, v in r.headers.items()}, content, url)
                emit("http.request", method=method, host=host, path=log_path, status=resp.status,
                     duration_ms=int((time.monotonic() - t0) * 1000), attempt=attempt + 1)
                return resp
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read(300).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    pass
                err = classify_status(exc.code, detail)
                emit("http.request", method=method, host=host, path=log_path, status=exc.code,
                     duration_ms=int((time.monotonic() - t0) * 1000), attempt=attempt + 1)
                if exc.code == 401 and self.auth is not None and not auth_refreshed:
                    self.auth.invalidate()
                    auth_refreshed = True
                    continue
                if isinstance(err, SystemUnavailable) and retryable_method and attempt < self.retries:
                    ra = self._retry_after(exc)
                    self._sleep(ra if ra is not None else self.backoff * (2 ** attempt))  # Retry-After: 0 is a valid answer
                    attempt += 1
                    continue
                raise err from exc
            except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError, TimeoutError) as exc:
                if retryable_method and attempt < self.retries:
                    self._sleep(self.backoff * (2 ** attempt))
                    attempt += 1
                    continue
                reason = getattr(exc, "reason", exc)
                raise NetworkError(f"{method} {host}{log_path}: {type(reason).__name__}: {reason}"[:300]) from exc

    @staticmethod
    def _retry_after(exc: "urllib.error.HTTPError") -> Optional[float]:
        v = exc.headers.get("Retry-After") if exc.headers else None
        try:
            return min(float(v), 60.0) if v else None
        except ValueError:
            return None

    def get(self, path, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path, **kw) -> Response:
        return self.request("POST", path, **kw)

    def put(self, path, **kw) -> Response:
        return self.request("PUT", path, **kw)

    def patch(self, path, **kw) -> Response:
        return self.request("PATCH", path, **kw)

    def delete(self, path, **kw) -> Response:
        return self.request("DELETE", path, **kw)

    def get_json(self, path, **kw) -> Any:
        return self.get(path, **kw).json()

    # -- pagination
    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None, style: str = "page", items_key: Optional[str] = None,
                 next_key: str = "next", cursor_param: Optional[str] = None, page_param: str = "page",
                 per_page_param: Optional[str] = None, per_page: int = 100, start_page: int = 1, offset_param: str = "offset",
                 limit_param: str = "limit", max_pages: int = 1000) -> Iterator[Any]:
        """Yield items across pages. style: ``link`` (RFC 5988 Link rel=next), ``next_key`` (a URL or cursor in the JSON body),
        ``page`` (page=1,2,... until an empty page), ``offset`` (offset/limit until a short page)."""
        p = dict(params or {})
        url: Optional[str] = path
        page, offset = start_page, 0
        origin0 = _origin(self._url(path, None))
        seen = set()  # type: set

        def follow(link: str, current: str) -> str:
            nxt_url = urllib.parse.urljoin(current, link)
            _check_scheme(nxt_url)
            if _origin(nxt_url) != origin0:
                raise DataError("paginate: refusing a next link on a different origin (credentials would leak)")
            if nxt_url in seen:
                raise DataError("paginate: next link loops back to a page already fetched")
            seen.add(nxt_url)
            return nxt_url

        for _ in range(max_pages):
            q = dict(p)
            if style == "page":
                q[page_param] = page
                if per_page_param:
                    q[per_page_param] = per_page
            elif style == "offset":
                q[offset_param], q[limit_param] = offset, per_page
            resp = self.get(url or path, params=q if (style in ("page", "offset") or url == path) else None)
            body = resp.json()
            items = _dig(body, items_key) if items_key else body
            if not isinstance(items, list):
                raise DataError("paginate: could not find a list of items (set items_key)")
            for it in items:
                yield it
            if style == "page":
                if not items:
                    return
                page += 1
            elif style == "offset":
                offset += len(items)
                if len(items) < per_page or not items:
                    return
            elif style == "link":
                m = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', resp.headers.get("link", ""))
                if not m:
                    return
                url = follow(m.group(1), resp.url)
            elif style == "next_key":
                nxt = _dig(body, next_key) if isinstance(body, dict) else None
                if not nxt:
                    return
                if cursor_param:
                    p[cursor_param] = nxt
                    url = path
                else:
                    url = follow(str(nxt), resp.url)
            else:
                raise ValueError("style must be page|offset|link|next_key")
        raise DataError(f"paginate: stopped after max_pages={max_pages}")

    def download(self, path: str, dest: "str | os.PathLike", sha256: Optional[str] = None, params: Optional[Dict[str, Any]] = None,
                 timeout: float = 60, max_bytes: int = 2 * 1024 ** 3) -> str:
        url = self._url(path, params)
        h = {"User-Agent": "wrapper-core-activities", **self.headers}
        if self.auth is not None:
            self.auth.apply(h)
        return _download(url, dest, sha256, h, timeout, max_bytes, self._ctx)


def client(base_url: str = "", headers: Optional[Dict[str, str]] = None, auth: Any = None, retries: int = 3, timeout: float = 30, **kw) -> Client:
    """``auth`` may be ``(user, password)``, ``BearerToken(...)``, ``OAuth2ClientCredentials(...)`` or any object with ``apply(headers)``."""
    return Client(base_url, headers, auth, retries, timeout, **kw)


def _download(url: str, dest, sha256: Optional[str], headers: Dict[str, str], timeout: float, max_bytes: int, ctx) -> str:
    _check_scheme(url)
    dest_s = os.fspath(dest)
    d = os.path.dirname(os.path.abspath(dest_s))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".part")
    h = hashlib.sha256()
    total = 0
    try:
        req = urllib.request.Request(url, headers=headers)
        try:
            with _urlopen(req, timeout, ctx) as r, os.fdopen(fd, "wb") as f:  # noqa: S310 - scheme checked above
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    total += len(chunk)
                    if total > max_bytes:
                        raise DataError(f"download exceeds {max_bytes} bytes")
                    h.update(chunk)
                    f.write(chunk)
        except urllib.error.HTTPError as exc:
            raise classify_status(exc.code) from exc
        except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError, TimeoutError) as exc:
            raise NetworkError(f"download failed: {type(getattr(exc, 'reason', exc)).__name__}") from exc
        if sha256 and h.hexdigest().lower() != sha256.lower():
            raise DataError("download sha256 mismatch")
        os.replace(tmp, dest_s)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    emit("http.download", bytes=total, host=urllib.parse.urlsplit(url).netloc)
    return dest_s


def download(url: str, dest: "str | os.PathLike", sha256: Optional[str] = None, headers: Optional[Dict[str, str]] = None,
             timeout: float = 60, max_bytes: int = 2 * 1024 ** 3) -> str:
    """Stream `url` to `dest` atomically; verify `sha256` when given (mismatch => DataError, nothing left behind)."""
    return _download(url, dest, sha256, {"User-Agent": "wrapper-core-activities", **(headers or {})}, timeout, max_bytes, None)
