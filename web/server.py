"""FastAPI server for the Project Infinity web client.

REST sets up sessions; the WebSocket streams the engine's event stream and
accepts player input. The frozen CLI and its files are untouched.

Endpoints
---------
GET    /api/health
GET    /api/worlds
GET    /api/models
POST   /api/sessions            {save, model?, temperature?, think?}
GET    /api/sessions/{id}
DELETE /api/sessions/{id}
WS     /ws/{id}
"""

import asyncio
import json
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .creation import CreationManager
from .icons import IconService, all_icon_keys, slugify_key, spell_detail
from .images import ImageError, ImageService, SceneService, image_mime
from forge.world import render_world_text  # noqa: E402
from .models import (
    DEFAULT_ICON_MODEL,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    list_image_models,
    list_models,
    resolve_image_model,
    resolve_model,
)
from .session_manager import SessionManager

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "output"
ASSETS_DIR = REPO_ROOT / "assets"

manager = SessionManager(REPO_ROOT)
creation_manager = CreationManager(REPO_ROOT, OUTPUT_DIR)
image_service = ImageService(OUTPUT_DIR)
scene_service = SceneService(OUTPUT_DIR)
icon_service = IconService(ASSETS_DIR)
_portrait_lock = asyncio.Lock()
_icon_lock = asyncio.Lock()
_scene_lock = asyncio.Lock()
MAX_ICON_BATCH = 6

# Kinds the shared icon store may write under. An allowlist keeps an arbitrary
# client key from picking the directory it writes into (and bounds prompt abuse).
_ICON_KINDS = {
    "ability", "alignment", "armor", "background", "class", "condition", "damage",
    "faction", "feature", "item", "kingdom", "language", "race", "save", "school",
    "skill", "spell", "stat", "tool", "weapon",
}
_ICON_NAME_MAX = 120


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
    return sorted(p.name for p in OUTPUT_DIR.glob("*.player"))


_ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp"}


def _portrait_url(stem: str) -> str | None:
    found = image_service.portrait_file(stem)
    if not found:
        return None
    return f"/api/portraits/{stem}.{found[0].suffix.lstrip('.')}"


def _portrait_mtime(stem: str) -> float | None:
    """The portrait file's mtime — changes on regenerate, unlike the save mtime."""
    found = image_service.portrait_file(stem)
    if not found:
        return None
    try:
        return found[0].stat().st_mtime
    except OSError:
        return None


def _world_list() -> list[dict]:
    entries = []
    paths = sorted(OUTPUT_DIR.glob("*.player")) if OUTPUT_DIR.exists() else []
    for path in paths:
        entry = {
            "file": path.name,
            "character": None,
            "class": None,
            "level": None,
            "modified": path.stat().st_mtime,
            "portrait": _portrait_url(path.stem),
            "portrait_modified": _portrait_mtime(path.stem),
        }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry["character"] = data.get("name")
            entry["class"] = data.get("character_class")
            entry["level"] = data.get("level")
        except Exception:  # noqa: BLE001
            pass
        entries.append(entry)
    return entries


class CreateSessionBody(BaseModel):
    save: str
    model: str | None = None
    temperature: float | None = None
    think: bool | None = None
    scene_images: bool = False


class PortraitBody(BaseModel):
    save: str
    force: bool = False
    model: str | None = None
    # Optional live sheet (dump_player_db snapshot); when present it is used
    # instead of the on-disk .player, so the portrait reflects the current
    # character (e.g. a higher level) mid-session.
    player: dict | None = None


class IconItem(BaseModel):
    key: str
    name: str = ""
    detail: str = ""


class IconsBody(BaseModel):
    # `items` is the preferred shape (carries the display name for per-item
    # prompts); `keys` is kept for backward compatibility.
    keys: list[str] = []
    items: list[IconItem] = []
    model: str | None = None


class SceneBody(BaseModel):
    session_id: str
    description: str
    mood: str = ""
    kind: str = "story"
    kingdom: str = ""
    area: str = ""
    location: str = ""
    sublocation: str = ""
    time_of_day: str = ""
    weather: str = ""
    characters: dict[str, str] = {}
    establishing: str = ""
    main_npcs: list = []
    npcs: list = []
    seed_change: str = ""
    model: str | None = None


def _image_model_or_400(model: str | None) -> str | None:
    """Validate an image-model override; None means 'use the service default'."""
    if not model:
        return None
    if resolve_image_model(model) is None:
        raise HTTPException(status_code=400, detail=f"unknown image model: {model}")
    return model


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
    if name != filename or not name.lower().endswith(".player"):
        raise HTTPException(status_code=400, detail="invalid save file")
    save = OUTPUT_DIR / name
    if not save.exists():
        raise HTTPException(status_code=404, detail="unknown save")
    stem = save.with_suffix("")
    targets = [save, Path(f"{stem}.timeline"), Path(f"{stem}.wwf")]
    removed = []
    for target in targets:
        if target.exists():
            target.unlink()
            removed.append(target.name)
    image_dir = OUTPUT_DIR / "images" / save.stem
    if image_dir.is_dir():
        shutil.rmtree(image_dir, ignore_errors=True)
        removed.append(f"images/{save.stem}")
    return {"deleted": name, "removed": removed}


@app.get("/api/images/status")
async def images_status():
    return image_service.status()


@app.post("/api/portrait")
async def generate_portrait(body: PortraitBody):
    name = Path(body.save).name
    if name != body.save or not name.lower().endswith(".player"):
        raise HTTPException(status_code=400, detail="invalid save file")
    save = OUTPUT_DIR / name
    if not save.exists():
        raise HTTPException(status_code=404, detail="unknown save")
    if not image_service.available():
        raise HTTPException(status_code=503, detail="image generation is not configured (set GEMINI_API_KEY)")
    model = _image_model_or_400(body.model)
    if body.player is not None:
        player = body.player
    else:
        player_path = save
        if not player_path.exists():
            raise HTTPException(status_code=404, detail="no character data for this save")
        try:
            player = json.loads(player_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise HTTPException(status_code=500, detail="could not read character data")
    async with _portrait_lock:
        try:
            result = await asyncio.to_thread(image_service.ensure_portrait, save.stem, player, body.force, model)
        except ImageError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
    return {"portrait": _portrait_url(save.stem), **result}


@app.get("/api/icons/index")
async def icons_index():
    return {"available": icon_service.available(), "icons": icon_service.index()}


@app.post("/api/icons")
async def generate_icons(body: IconsBody):
    if not icon_service.available():
        raise HTTPException(status_code=503, detail="image generation is not configured (set GEMINI_API_KEY)")
    model = _image_model_or_400(body.model)
    catalog = all_icon_keys()

    def _clean_name(value, fallback):
        text = " ".join(str(value or "").split())
        return (text[:_ICON_NAME_MAX].strip() or fallback)

    wanted: list[dict] = []
    seen: set[str] = set()

    def _add(kind: str, slug: str, name: str, detail: str) -> None:
        key = f"{kind}/{slug}"
        if key in seen or len(wanted) >= MAX_ICON_BATCH:
            return
        seen.add(key)
        fallback = catalog.get(key) or slug.replace("-", " ").title()
        detail = " ".join(str(detail or "").split())[:_ICON_NAME_MAX]
        if kind == "spell" and not detail:
            detail = spell_detail(name)
        wanted.append({"key": key, "kind": kind, "slug": slug,
                       "name": _clean_name(name, fallback), "detail": detail})

    # Preferred: explicit items with display names (covers per-item keys).
    for item in body.items:
        raw = str(item.key or "")
        kind, sep, slug = raw.partition("/")
        kind, slug = kind.strip(), slug.strip()
        if not sep or kind not in _ICON_KINDS or slug != slugify_key(slug) or not slug:
            continue
        _add(kind, slug, item.name, item.detail)

    # Backward compatible: bare catalog keys, resolved to their display names.
    for raw in body.keys:
        key = str(raw).strip()
        if key not in catalog:
            continue
        kind, _, slug = key.partition("/")
        if kind not in _ICON_KINDS:
            continue
        _add(kind, slug, catalog[key], "")

    generated: list[str] = []
    failed: list[dict] = []
    async with _icon_lock:
        for entry in wanted:
            try:
                result = await asyncio.to_thread(
                    icon_service.ensure, entry["kind"], entry["slug"],
                    entry["name"], entry["detail"], False, model,
                )
            except ImageError as exc:
                failed.append({"key": entry["key"], "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - report, do not abort the batch
                failed.append({"key": entry["key"], "error": str(exc)})
            else:
                if result.get("generated"):
                    generated.append(result["key"])
    # Return only the affected keys (the full index can be ~600 KB); the client
    # merges these into its cached map.
    icons = {}
    for entry in wanted:
        url = icon_service.url_for(entry["kind"], entry["slug"])
        if url:
            icons[entry["key"]] = url
    return {"generated": generated, "failed": failed, "icons": icons}


@app.get("/api/icons/{kind}/{slug}")
async def get_icon(kind: str, slug: str):
    from .icons import slugify_key  # local import keeps the module graph light
    if kind != slugify_key(kind) or slug != slugify_key(slug) or not kind or not slug:
        raise HTTPException(status_code=400, detail="invalid icon key")
    path = icon_service.icon_path(kind, slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="no icon")
    return FileResponse(path, media_type=image_mime(path), headers={"Cache-Control": "no-store"})


@app.get("/api/portraits/{filename}")
async def get_portrait(filename: str):
    name = Path(filename).name
    stem, dot, ext = name.rpartition(".")
    if (name != filename or not dot or not stem or ".." in name
            or ext.lower() not in _ALLOWED_IMAGE_EXTS):
        raise HTTPException(status_code=400, detail="invalid portrait file")
    path = image_service.image_dir(stem) / f"portrait.{ext.lower()}"
    if not path.exists():
        raise HTTPException(status_code=404, detail="no portrait")
    return FileResponse(path, media_type=image_mime(path), headers={"Cache-Control": "no-store"})


def _world_brief(world_text: str) -> str:
    """The first couple of world-history lines, for scene continuity."""
    lines: list[str] = []
    in_history = False
    for raw in (world_text or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("history:"):
            in_history = True
            continue
        if not in_history:
            continue
        if not stripped:
            continue
        if stripped.startswith("-"):
            lines.append(stripped.lstrip("- ").strip())
            if len(lines) >= 2:
                break
        elif stripped.endswith(":"):
            break
    return " ".join(lines)[:400]


def _scene_context(session) -> tuple[dict, str]:
    """Character data + a short world brief for the scene prompt."""
    player: dict = {}
    player_path = Path(session.player_path) if session.player_path else None
    if player_path and player_path.exists():
        try:
            player = json.loads(player_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            player = {}
    world = _world_brief(render_world_text())
    return player, world


@app.post("/api/scene")
async def generate_scene(body: SceneBody):
    session = manager.get(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    if not scene_service.available():
        raise HTTPException(status_code=503, detail="image generation is not configured (set GEMINI_API_KEY)")
    description = " ".join(str(body.description or "").split())[:1200]
    if not description:
        raise HTTPException(status_code=400, detail="description required")
    mood = " ".join(str(body.mood or "").split())[:120]
    kingdom = " ".join(str(body.kingdom or "").split())[:80]
    area = " ".join(str(body.area or "").split())[:80]
    location = " ".join(str(body.location or "").split())[:120]
    sublocation = " ".join(str(body.sublocation or "").split())[:120]
    time_of_day = " ".join(str(body.time_of_day or "").split())[:60]
    weather = " ".join(str(body.weather or "").split())[:120]
    characters: dict[str, str] = {}
    if isinstance(body.characters, dict):
        for key, value in list(body.characters.items())[:20]:
            k = " ".join(str(key or "").split())[:80]
            if k:
                characters[k] = " ".join(str(value or "").split())[:160]
    establishing = " ".join(str(body.establishing or "").split())[:400]
    # Declared main NPCs: `[{name, role, description}]` (the engine injects the descriptions later).
    main_npcs: list[dict[str, str]] = []
    if isinstance(body.main_npcs, list):
        for entry in body.main_npcs[:20]:
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split())[:80]
            if name:
                main_npcs.append({
                    "name": name,
                    "role": " ".join(str(entry.get("role") or "").split())[:120],
                    "description": " ".join(str(entry.get("description") or "").split())[:400],
                })
    npcs: list[dict[str, str]] = []
    if isinstance(body.npcs, list):
        for entry in body.npcs[:20]:
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split())[:80]
            if name:
                npcs.append({"name": name,
                             "description": " ".join(str(entry.get("description") or "").split())[:400]})
    seed_change = " ".join(str(body.seed_change or "").split())[:400]
    model = _image_model_or_400(body.model)
    stem = session.active_name or ""
    if not stem:
        raise HTTPException(status_code=400, detail="session has no active save")
    player, world = _scene_context(session)
    async with _scene_lock:
        try:
            result = await asyncio.to_thread(
                scene_service.ensure_scene, stem, player, world,
                description=description, mood=mood, kingdom=kingdom, area=area,
                location=location, sublocation=sublocation,
                time_of_day=time_of_day, weather=weather, characters=characters,
                establishing=establishing, main_npcs=main_npcs, npcs=npcs,
                seed_change=seed_change, model=model,
            )
        except ImageError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
    return result


@app.get("/api/scenes/{stem}/{name}")
async def get_scene(stem: str, name: str):
    safe = re.compile(r"^[A-Za-z0-9_-]+$")
    if not safe.match(stem) or not safe.match(name):
        raise HTTPException(status_code=400, detail="invalid scene key")
    found = scene_service.get_scene(stem, name)
    if not found:
        raise HTTPException(status_code=404, detail="no scene")
    data, mime = found
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store"})


@app.get("/api/models")
async def models():
    gemini_ready = image_service.available()
    entries = []
    for model in list_models():
        entry = dict(model)
        # Gemini models need GEMINI_API_KEY; Ollama Cloud needs nothing local.
        entry["available"] = entry.get("provider") != "gemini" or gemini_ready
        entries.append(entry)
    return {
        "models": entries,
        "default": DEFAULT_MODEL,
        "default_temperature": DEFAULT_TEMPERATURE,
        "image_models": list_image_models(),
        "default_image_model": DEFAULT_IMAGE_MODEL,
        "default_icon_model": DEFAULT_ICON_MODEL,
        "images_available": image_service.available(),
    }


@app.post("/api/sessions")
async def create_session(body: CreateSessionBody):
    if body.save not in _world_names():
        raise HTTPException(status_code=404, detail=f"unknown save: {body.save}")
    if body.model and resolve_model(body.model) is None:
        raise HTTPException(status_code=400, detail=f"unknown model: {body.model}")
    sid, session = await manager.create(
        body.save, model=body.model, temperature=body.temperature, think=body.think,
        scene_images=body.scene_images,
    )
    return {
        "session_id": sid,
        "world": body.save,
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
            # Stamp the owning session so the client can drop events that race
            # in from a session it has already left.
            if isinstance(evt, dict):
                evt = {**evt, "session_id": sid}
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
                await session.submit_save()
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
