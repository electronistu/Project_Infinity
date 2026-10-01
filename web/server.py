"""FastAPI server for the Project Infinity web client.

REST sets up sessions; the WebSocket streams the engine's event stream and
accepts player input. The frozen CLI and its files are untouched.

Endpoints
---------
GET    /api/health
GET    /api/worlds
GET    /api/models
POST   /api/sessions            {wwf, model?, temperature?, think?}
GET    /api/sessions/{id}
DELETE /api/sessions/{id}
WS     /ws/{id}
"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .creation import CreationManager
from .models import DEFAULT_MODEL, DEFAULT_TEMPERATURE, list_models, resolve_model
from .session_manager import SessionManager

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "output"

manager = SessionManager(REPO_ROOT)
creation_manager = CreationManager(REPO_ROOT, OUTPUT_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    try:
        yield
    finally:
        creation_manager.cancel_all()
        await manager.shutdown()


app = FastAPI(title="Project Infinity Web", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Local tool: never let the browser cache UI assets after an update."""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/index.html") or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


def _world_names() -> list[str]:
    if not OUTPUT_DIR.exists():
        return []
    return sorted(p.name for p in OUTPUT_DIR.glob("*.wwf"))


def _world_list() -> list[dict]:
    entries = []
    paths = sorted(OUTPUT_DIR.glob("*.wwf")) if OUTPUT_DIR.exists() else []
    for path in paths:
        entry = {
            "file": path.name,
            "character": None,
            "class": None,
            "level": None,
            "modified": path.stat().st_mtime,
        }
        player = path.with_suffix(".player")
        if player.exists():
            try:
                data = json.loads(player.read_text(encoding="utf-8"))
                entry["character"] = data.get("name")
                entry["class"] = data.get("character_class")
                entry["level"] = data.get("level")
            except Exception:  # noqa: BLE001
                pass
        entries.append(entry)
    return entries


class CreateSessionBody(BaseModel):
    wwf: str
    model: str | None = None
    temperature: float | None = None
    think: bool | None = None


@app.get("/api/health")
async def health():
    return {"status": "ok", "sessions": manager.count()}


@app.get("/api/worlds")
async def worlds():
    # `worlds` stays a list of filenames (backward compatible); `saves` carries metadata.
    return {"worlds": _world_names(), "saves": _world_list(), "output_dir": str(OUTPUT_DIR)}


@app.delete("/api/worlds/{filename}")
async def delete_world(filename: str):
    name = Path(filename).name
    if name != filename or not name.lower().endswith(".wwf"):
        raise HTTPException(status_code=400, detail="invalid world file")
    wwf = OUTPUT_DIR / name
    if not wwf.exists():
        raise HTTPException(status_code=404, detail="unknown world")
    stem = wwf.with_suffix("")
    targets = [wwf, Path(f"{stem}.player"), Path(f"{stem}.timeline")]
    removed = []
    for target in targets:
        if target.exists():
            target.unlink()
            removed.append(target.name)
    return {"deleted": name, "removed": removed}


@app.get("/api/models")
async def models():
    return {
        "models": list_models(),
        "default": DEFAULT_MODEL,
        "default_temperature": DEFAULT_TEMPERATURE,
    }


@app.post("/api/sessions")
async def create_session(body: CreateSessionBody):
    if body.wwf not in _world_names():
        raise HTTPException(status_code=404, detail=f"unknown world: {body.wwf}")
    if body.model and resolve_model(body.model) is None:
        raise HTTPException(status_code=400, detail=f"unknown model: {body.model}")
    sid, session = await manager.create(
        body.wwf, model=body.model, temperature=body.temperature, think=body.think,
    )
    return {
        "session_id": sid,
        "world": body.wwf,
        "model": session.model,
        "context_window": session.context_window,
    }


@app.get("/api/sessions/{sid}")
async def session_state(sid: str):
    state = manager.state(sid)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return state


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    if manager.get(sid) is None:
        raise HTTPException(status_code=404, detail="unknown session")
    await manager.remove(sid)
    return {"closed": sid}


@app.websocket("/ws/{sid}")
async def ws_endpoint(websocket: WebSocket, sid: str):
    session = manager.get(sid)
    if session is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "unknown session"})
        await websocket.close(code=4404)
        return

    await websocket.accept()
    manager.touch(sid)

    async def pump_events():
        async for evt in session.events():
            await websocket.send_json(evt)
            if evt.get("type") == "closed":
                break

    async def pump_input():
        while True:
            try:
                msg = await websocket.receive_json()
            except WebSocketDisconnect:
                return
            except Exception:  # noqa: BLE001
                return
            manager.touch(sid)
            mtype = msg.get("type")
            if mtype == "action":
                await session.submit(msg.get("text", ""))
            elif mtype == "slash":
                await session.submit_slash(msg.get("command", ""))
            elif mtype == "resume":
                await session.resume()
            elif mtype == "save":
                await session.submit_save(msg.get("name"))
            elif mtype == "sync":
                await session.submit_slash("/sync")
            elif mtype == "stats":
                await session.submit_slash("/stats")
            elif mtype == "flags":
                await session.set_flags(
                    verbose=msg.get("verbose"),
                    debug=msg.get("debug"),
                    think=msg.get("think"),
                    temperature=msg.get("temperature"),
                )
            elif mtype == "ping":
                await websocket.send_json({"type": "pong"})

    tasks = [asyncio.create_task(pump_events()), asyncio.create_task(pump_input())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


# ── character creation ──────────────────────────────────────────────────

class CreationAnswerBody(BaseModel):
    value: Any = None


@app.post("/api/creation")
async def start_creation():
    try:
        bridge = creation_manager.start()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    steps, terminal = await asyncio.to_thread(bridge.take_steps)
    return {"creation_id": bridge.id, "steps": steps, "terminal": terminal}


@app.post("/api/creation/{creation_id}/answer")
async def answer_creation(creation_id: str, body: CreationAnswerBody):
    bridge = creation_manager.get(creation_id)
    if bridge is None:
        raise HTTPException(status_code=404, detail="unknown creation")
    if not bridge.submit_answer(body.value):
        raise HTTPException(status_code=409, detail="creation already finished")
    steps, terminal = await asyncio.to_thread(bridge.take_steps)
    if terminal is not None:
        creation_manager.forget(creation_id)
    return {"creation_id": creation_id, "steps": steps, "terminal": terminal}


@app.post("/api/creation/{creation_id}/cancel")
async def cancel_creation(creation_id: str):
    bridge = creation_manager.get(creation_id)
    if bridge is None:
        raise HTTPException(status_code=404, detail="unknown creation")
    bridge.cancel()
    return {"cancelled": creation_id}


# ── static front-end (added in Phase 3); fall back to an API notice ───────
_static_dir = Path(__file__).parent / "static"
if (_static_dir / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
else:
    @app.get("/")
    async def root():
        return {
            "service": "Project Infinity Web",
            "status": "ok",
            "note": "The browser UI arrives in Phase 3; use /api and /ws for now.",
        }
