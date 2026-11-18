"""AUTOSTATION Studio — FastAPI server.

Hosts the design chain over HTTP + WebSocket so the wizard UI can drive
steps one at a time and stream LLM progress to the browser.

Launch via amx:
    python tools/amx.py studio [--port 8765] [--project <dir>]

Security: every /api request needs "Authorization: Bearer <token>" (the token is printed at start-up inside the URL fragment), Host and
Origin must be 127.0.0.1/localhost with this server's port, and project output stays under one root folder (see security.py).

Or directly:
    python -m automation_studio.server [--port 8765] [--project <dir>]

WebSocket protocol (per-step, one message per direction):
    Client → opens  ws://.../ws/{session_id}/{step_name}   with subprotocols ["autostation.v1", "token.<bearer token>"]
    Server → sends  {"type": "start",  "step": "<name>"}
    Server → sends  {"type": "result", "step": "<name>", "data": {...}}
                 or {"type": "error",  "step": "<name>", "message": "..."}
    Connection closes after result or error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    sys.exit(
        "automation-studio server requires optional deps.\n"
        "Install: pip install 'automation-studio[server]'\n"
        "or:      pip install fastapi uvicorn"
    )

from .chain import DesignSession, StepError, StepName, run_step
from .context import StudioContext, build_studio_context
from .scaffold import ScaffoldResult, scaffold_from_contract
from . import security as sec

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="AUTOSTATION Studio", version="0.1.0")

# Shared state — single-user dev tool; no persistence needed. Sessions expire when idle and are capped (security.SessionStore).
_sessions = sec.SessionStore(max_steps=sec.max_steps_from_env())
_ctx_cache: StudioContext | None = None
_ctx_lock = threading.Lock()


def _get_ctx(project: Path | None = None) -> StudioContext:
    global _ctx_cache
    with _ctx_lock:
        if _ctx_cache is None:
            _ctx_cache = build_studio_context()
    return _ctx_cache


# ── Request gate ──────────────────────────────────────────────────────────────

_OPEN_PATHS = ("/", "/health")          # the page itself and the liveness probe never carry data
_OPEN_PREFIXES = ("/static/",)


def _port() -> int:
    try:
        return int(os.environ.get(sec.PORT_ENV, "8765"))
    except ValueError:
        return 8765


def _token() -> str:
    return os.environ.get(sec.TOKEN_ENV, "")      # empty = not configured = every protected request is refused


def _root() -> Path:
    raw = os.environ.get(sec.ROOT_ENV)
    return Path(raw) if raw else sec.default_root()


@app.middleware("http")
async def _gate(request: Request, call_next):
    port = _port()
    if not sec.host_allowed(request.headers.get("host"), port):
        return JSONResponse({"detail": "host not allowed"}, status_code=403)
    if not sec.origin_allowed(request.headers.get("origin"), port):
        return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    path = request.url.path
    if path not in _OPEN_PATHS and not path.startswith(_OPEN_PREFIXES):
        if not sec.token_ok(_token(), sec.bearer_from_header(request.headers.get("authorization"))):
            return JSONResponse({"detail": "missing or invalid token"}, status_code=401)
    return await call_next(request)


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.post("/api/sessions")
async def create_session(body: dict[str, Any]) -> dict[str, str]:
    goal = (body.get("goal") or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="goal is required")
    if len(goal) > 4000:
        raise HTTPException(status_code=422, detail="goal is too long (4000 characters at most)")
    ctx = _get_ctx()
    session = DesignSession.start(goal, ctx)
    try:
        _sessions.add(session.session_id, session)
    except sec.SessionLimit as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    return {"session_id": session.session_id}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    s = _sessions.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session_id": s.session_id,
        "goal": s.goal,
        "completed_steps": [st.value for st in s.completed_steps()],
        "is_design_complete": s.is_design_complete(),
        "clarifications": s.clarifications,
        "steps": s.steps,
    }


@app.post("/api/sessions/{session_id}/clarify")
async def add_clarification(session_id: str, body: dict[str, Any]) -> dict[str, bool]:
    s = _sessions.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    text = (body.get("text") or "").strip()
    if len(text) > 4000:
        raise HTTPException(status_code=422, detail="clarification is too long (4000 characters at most)")
    if text:
        s.add_clarification(text)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, bool]:
    _sessions.remove(session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/scaffold")
async def scaffold_session(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    s = _sessions.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")

    contract = s.result(StepName.CONTRACT)
    if contract is None:
        raise HTTPException(status_code=422, detail="contract step has not been completed")

    project_yaml = contract.get("project_yaml")
    if not project_yaml:
        raise HTTPException(status_code=422, detail="contract has no project_yaml")

    try:
        out_base = sec.resolve_out_dir(_root(), str(body.get("out_dir") or ""))
    except sec.PathRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    out_base.mkdir(parents=True, exist_ok=True)
    no_verify = body.get("no_verify", False) is True      # verification is the default; skipping it needs an explicit JSON true

    result: ScaffoldResult = await asyncio.to_thread(
        scaffold_from_contract, project_yaml, out_base, no_verify=no_verify
    )
    return {
        "success": result.success,
        "out_path": str(result.out_path),
        "output": result.output,
        "error": result.error,
    }


# ── WebSocket step runner ─────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}/{step_name}")
async def run_step_ws(websocket: WebSocket, session_id: str, step_name: str) -> None:
    port = _port()
    presented = sec.token_from_subprotocols(websocket.headers.get("sec-websocket-protocol"))
    if (not sec.host_allowed(websocket.headers.get("host"), port)
            or not sec.origin_allowed(websocket.headers.get("origin"), port)
            or not sec.token_ok(_token(), presented)):
        await websocket.close(code=1008)          # policy violation: refused before accept, nothing is sent
        return
    await websocket.accept(subprotocol=sec.WS_SUBPROTOCOL)

    s = _sessions.get(session_id)
    if s is None:
        await websocket.send_text(json.dumps({"type": "error", "message": "session not found"}))
        await websocket.close()
        return

    try:
        step = StepName(step_name)
    except ValueError:
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": f"unknown step {step_name!r}; valid: {[st.value for st in StepName]}",
        }))
        await websocket.close()
        return

    try:
        _sessions.charge_step(session_id)
    except sec.StepBudgetExceeded as exc:
        await websocket.send_text(json.dumps({"type": "error", "step": step.value, "message": str(exc)}))
        await websocket.close()
        return

    await websocket.send_text(json.dumps({"type": "start", "step": step.value}))

    try:
        result = await asyncio.to_thread(run_step, s, step)
        await websocket.send_text(json.dumps({
            "type": "result",
            "step": step.value,
            "data": result,
        }))
    except StepError as exc:
        await websocket.send_text(json.dumps({
            "type": "error",
            "step": step.value,
            "message": str(exc),
        }))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        await websocket.send_text(json.dumps({
            "type": "error",
            "step": step.value,
            "message": f"unexpected error: {exc}",
        }))
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ── Static files (UI) ─────────────────────────────────────────────────────────

_STATIC = Path(__file__).parent / "static"

if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(str(_STATIC / "index.html"))
else:
    @app.get("/")
    async def root_no_ui() -> JSONResponse:
        return JSONResponse({
            "service": "AUTOSTATION Studio API",
            "version": "0.1.0",
            "docs": "/docs",
            "note": "UI not built yet — use the REST API or WebSocket directly.",
        })


# ── Entry point ───────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AUTOSTATION Studio server")
    p.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p.add_argument("--project", type=Path, default=None,
                   help="Path to an automation project dir for extra context")
    p.add_argument("--root", type=Path, default=None,
                   help="The only folder projects may be written under (default: ~/AutoStation-projects)")
    p.add_argument("--unsafe-bind", action="store_true", help="Allow a non-loopback --host (not recommended)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.unsafe_bind:
        sys.exit("refusing to bind the Studio to a non-loopback address (it can write files and spend LLM budget); "
                 "pass --unsafe-bind only if you understand that")
    if args.project:
        # Warm the cache with project-specific context before accepting connections.
        global _ctx_cache
        _ctx_cache = build_studio_context()

    token = sec.new_token()
    root = args.root or sec.default_root()
    os.environ[sec.TOKEN_ENV] = token
    os.environ[sec.PORT_ENV] = str(args.port)
    os.environ[sec.ROOT_ENV] = str(root)

    print(f"AUTOSTATION Studio  ->  http://127.0.0.1:{args.port}/#token={token}")
    print(f"  open that exact link (the token changes on every start); projects are written under {root}")
    # the app object, not an import string: the token lives in this process and a reload would respawn it
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
