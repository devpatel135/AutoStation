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
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

# Repo root derived from this file's location (components/automation-studio/automation_studio/server.py)
_REPO_ROOT = Path(__file__).resolve().parents[3]
# Per-session scaffold output paths, stored after a successful scaffold so run/test/submit can use them.
_scaffold_paths: dict[str, str] = {}

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
    if result.success:
        _scaffold_paths[session_id] = str(result.out_path)
    return {
        "success": result.success,
        "out_path": str(result.out_path),
        "output": result.output,
        "error": result.error,
    }


# ── Repo info ─────────────────────────────────────────────────────────────────

@app.get("/api/repo")
async def repo_info() -> dict[str, Any]:
    """Return context the UI needs to show the submit panel."""
    has_gh = shutil.which("gh") is not None
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(_REPO_ROOT),
                           capture_output=True, text=True, timeout=5)
        remote = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        remote = ""
    return {"repo_root": str(_REPO_ROOT), "has_gh": has_gh, "remote": remote}


# ── Submit (scaffold → branch → PR) ──────────────────────────────────────────

@app.post("/api/sessions/{session_id}/submit")
async def submit_session(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    out_path_str = _scaffold_paths.get(session_id, "").strip()
    if not out_path_str:
        raise HTTPException(status_code=422, detail="scaffold not yet completed for this session")
    out_path = Path(out_path_str)
    if not out_path.is_dir():
        raise HTTPException(status_code=422, detail=f"scaffold output not found: {out_path}")

    automation_name = out_path.name
    branch = f"automation/{automation_name}"
    pr_title = (body.get("pr_title") or f"feat: add {automation_name}").strip()
    pr_body = (body.get("pr_body") or (
        f"Scaffolded via AUTOSTATION Studio.\n\nAutomation: `{automation_name}`\n\n"
        "## Checklist\n- [ ] Review `automation.yaml` schedule and classification\n"
        "- [ ] Run `amx dev .` locally\n- [ ] All tests pass"
    )).strip()

    dest = _REPO_ROOT / "components" / "automations" / automation_name
    if dest.exists() and not body.get("force"):
        raise HTTPException(status_code=409, detail=f"{automation_name} already exists in repo; pass force=true to overwrite")

    def _do_submit() -> dict[str, Any]:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(str(out_path), str(dest))

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", *args], cwd=str(_REPO_ROOT),
                                  capture_output=True, text=True, check=True, timeout=30)

        git("checkout", "-b", branch)
        git("add", str(dest.relative_to(_REPO_ROOT)))
        git("commit", "-m", pr_title,
            "--trailer", "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>")
        git("push", "origin", branch)

        pr_url: str | None = None
        gh_note = ""
        if shutil.which("gh"):
            r = subprocess.run(
                ["gh", "pr", "create", "--title", pr_title, "--body", pr_body, "--head", branch],
                cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                pr_url = r.stdout.strip()
            else:
                gh_note = r.stderr.strip()[:300]
        else:
            gh_note = "gh CLI not found — push complete; open a PR manually."

        return {"branch": branch, "pr_url": pr_url, "automation_name": automation_name,
                "dest": str(dest), "gh_note": gh_note}

    try:
        return await asyncio.to_thread(_do_submit)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500,
                            detail=f"git error: {(exc.stderr or '').strip()[:400]}")


# ── Dev: stream subprocess output via WebSocket ───────────────────────────────

async def _stream_subprocess(websocket: WebSocket, cmd: list[str], cwd: str) -> None:
    """Run cmd, stream each stdout/stderr line as {type:'line',text:'...'}, finish with {type:'done',exit_code:N}."""
    env = dict(os.environ)
    env.setdefault("AUTOMAGIX_FRAMEWORK_DIR", str(_REPO_ROOT / "components" / "execution-framework"))
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(_REPO_ROOT / "components" / "wrapper-core"), env.get("PYTHONPATH", ""),
    ]))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=cwd, env=env,
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        try:
            await websocket.send_text(json.dumps({"type": "line", "text": raw.decode("utf-8", errors="replace")}))
        except Exception:
            break
    await proc.wait()
    await websocket.send_text(json.dumps({"type": "done", "exit_code": proc.returncode}))


@app.websocket("/ws/{session_id}/run")
async def run_ws(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    out_path = _scaffold_paths.get(session_id, "")
    if not out_path or not Path(out_path).is_dir():
        await websocket.send_text(json.dumps({"type": "error", "message": "scaffold output not found"}))
        await websocket.close()
        return
    await websocket.send_text(json.dumps({"type": "start", "step": "run"}))
    try:
        amx = str(_REPO_ROOT / "tools" / "amx.py")
        await _stream_subprocess(websocket, [sys.executable, amx, "dev", out_path], str(_REPO_ROOT))
    except Exception as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.websocket("/ws/{session_id}/test")
async def test_ws(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    out_path = _scaffold_paths.get(session_id, "")
    if not out_path or not Path(out_path).is_dir():
        await websocket.send_text(json.dumps({"type": "error", "message": "scaffold output not found"}))
        await websocket.close()
        return
    await websocket.send_text(json.dumps({"type": "start", "step": "test"}))
    try:
        await _stream_subprocess(
            websocket,
            [sys.executable, "-m", "pytest", "-v", "--tb=short", "-p", "no:cacheprovider", "."],
            out_path,
        )
    except Exception as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


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
