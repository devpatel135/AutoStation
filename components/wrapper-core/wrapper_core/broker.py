"""Loopback LLM broker (SPEC §8): the runner holds the key; child automations in ANY language call this."""
from __future__ import annotations

import hmac
import json
import secrets as pysecrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import llm


class Broker:
    def __init__(self):
        self.token = pysecrets.token_urlsafe(24)
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._srv.serve_forever, name="amx-broker", daemon=True)

    def start(self) -> "Broker":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def env(self) -> dict:
        return {"AUTOMAGIX_BROKER_URL": self.url, "AUTOMAGIX_BROKER_TOKEN": self.token}

    def _make_handler(self):
        broker = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, code: int, obj: dict):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self) -> bool:
                got = self.headers.get("Authorization", "")
                return hmac.compare_digest(got, f"Bearer {broker.token}")

            def do_GET(self):
                if self.path == "/health":
                    return self._send(200, {"ok": True})
                self._send(404, {"ok": False})

            def do_POST(self):
                if self.path != "/llm":
                    return self._send(404, {"ok": False})
                if not self._authed():
                    return self._send(401, {"ok": False, "error": "unauthorized"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if n < 0 or n > 2_000_000:
                        return self._send(413, {"ok": False, "error": "too large"})
                    req = json.loads(self.rfile.read(n).decode("utf-8"))
                    res = llm.llm_call_ex(
                        req.get("prompt", ""), req.get("context", ""), req.get("schema"),
                        int(req.get("timeout_s") or 30), req.get("caller") or "broker-client",
                        template=req.get("template"), classification=req.get("classification"),
                        inject_context=req.get("inject_context"), decision_log=bool(req.get("decision_log")),
                        _force_direct=True)
                    self._send(200, {"ok": res.status == "success", "status": res.status, "result": res.value})
                except Exception as exc:  # noqa: BLE001
                    self._send(500, {"ok": False, "status": "error", "result": None, "error": type(exc).__name__})

        return H
