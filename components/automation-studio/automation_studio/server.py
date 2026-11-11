"""AUTOSTATION Studio — FastAPI server.

Hosts the design chain over HTTP + WebSocket so the wizard UI can drive
steps one at a time and stream LLM progress to the browser.

Launch via amx:
    python tools/amx.py studio [--port 8765] [--project <dir>]

Or directly:
    python -m automation_studio.server [--port 8765] [--project <dir>]

WebSocket protocol (per-step, one message per direction):
    Client → opens  ws://.../ws/{session_id}/{step_name}
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
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="AUTOSTATION Studio", version="0.1.0")

# Shared state — single-user dev tool; no persistence needed.
_sessions: dict[str, DesignSession] = {}
_ctx_cache: StudioContext | None = None
_ctx_lock = threading.Lock()


def _get_ctx(project: Path | None = None) -> StudioContext:
    global _ctx_cache
    with _ctx_lock:
        if _ctx_cache is None:
            _ctx_cache = build_studio_context()
    return _ctx_cache


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.post("/api/sessions")
async def create_session(body: dict[str, Any]) -> dict[str, str]:
    goal = (body.get("goal") or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="goal is required")
    ctx = _get_ctx()
    session = DesignSession.start(goal, ctx)
    _sessions[session.session_id] = session
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
    if text:
        s.add_clarification(text)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, bool]:
    _sessions.pop(session_id, None)
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

    raw_out = (body.get("out_dir") or "").strip()
    if raw_out:
        # Sanitize via os.path.basename on each component before any path construction;
        # this clears taint at the string level and prevents directory traversal.
        raw_parts = raw_out.replace("\\", "/").split("/")
        if any(p == ".." for p in raw_parts) or raw_out.startswith(("/", "\\")):
            raise HTTPException(status_code=400, detail="out_dir must be a relative path without '..'")
        safe_parts = [os.path.basename(p) for p in raw_parts if p not in ("", ".")]
        if not safe_parts:
            raise HTTPException(status_code=400, detail="out_dir must not be empty after normalization")
        out_base = Path.home().joinpath(*safe_parts)
    else:
        out_base = Path.home() / "autostation-projects"
    no_verify = bool(body.get("no_verify", True))

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
    await websocket.accept()

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
    p.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload (dev mode)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.project:
        # Warm the cache with project-specific context before accepting connections.
        global _ctx_cache
        _ctx_cache = build_studio_context()

    print(f"AUTOSTATION Studio  →  http://{args.host}:{args.port}")
    uvicorn.run(
        "automation_studio.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
