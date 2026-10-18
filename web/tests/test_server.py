"""The server surface, deterministic: REST + WebSocket over a scripted model.

Starts uvicorn in-process and drives the app the way the browser does -- but the GM's model is
scripted (`_scripted.py`), so the suite asserts the TRANSPORT and never a model's mood: the
routes, the WS command routing, the event stream with its `session_id` stamping, real MCP tool
execution over `dice_server.py`, the slash commands, and the server-side place persistence
(`place_note` -> `web.server._persist_place_note` -> `record_place`).

The GM's LIVE behaviour (the turn contract, the place-declaration tools per mode, whether the
model follows its protocol at all) is `web/tests/live_gm.py` -- opt-in, not in this gate. This
suite IS gate step 7 and calls no provider: the scripted block replaces the provider dispatch
and makes a real `stream_chat` call raise.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_server.py
"""

import asyncio
import json
import shutil
import sys
from pathlib import Path

import httpx
import uvicorn

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_scripted`, not a package

from _scripted import response, scripted, turn  # noqa: E402
from web.eras import playable_eras  # noqa: E402
from web.places import _scene_key  # noqa: E402
from web.scenes import (current_scene_place, discard_scene_manifest,
                        known_scene_places)  # noqa: E402
from web.server import app  # noqa: E402

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # older websockets
    from websockets import connect as ws_connect  # type: ignore

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
WS_BASE = f"ws://127.0.0.1:{PORT}/ws"
OUTPUT = REPO / "output"

RESULTS: list[tuple[str, bool, str]] = []

# ── the scripted GM ───────────────────────────────────────────────────────
# Session 1 (images ON) then session 2 (images OFF), in the order their model calls happen.
OPENING = "The lamp gutters and the room comes back to you in pieces: a low ceiling, wet stone."
LOOKING = "You turn a slow circle, and the dark gives up one doorway and nothing else."
OPENING_TEXT = "You come out of the dark into a market street that smells of smoke and spice."
HALL = "The great hall is yours for the night, and the steward is already counting your coin."

SCENE_CALL = {"description": "the protagonist stands at the threshold, lamp in hand",
              "kingdom": "Wallachia", "area": "Târgoviște",
              "place": ["the princely court", "the great hall"],
              "time_of_day": "night", "weather": "cold and damp",
              "characters": {}, "establishing": "a low vaulted hall, wet stone, one long table"}
PLACE_CALL = {"kingdom": "Wallachia", "area": "Târgoviște",
              "place": ["the princely court", "the great hall"],
              "main_npcs": [{"name": "Steward Radu", "role": "the prince's steward",
                             "race": "human", "class": "rogue"}]}
SCRIPT = [
    # session 1, images on: the awakening (prose + the opening image), then an action turn
    response(OPENING, [("request_scene_image", SCENE_CALL)]),
    *turn(LOOKING, batch=[("roll_dice", {"notation": "1d20"})],
          prose_calls=[("request_scene_image", dict(SCENE_CALL, description="the doorway"))]),
    # session 2, images off: the awakening, then a move declared with `note_place`
    response(OPENING_TEXT),
    *turn(HALL, batch=[("note_place", PLACE_CALL)]),
]


def rec(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)


async def wait_for_server(client: httpx.AsyncClient, timeout: float = 30.0) -> bool:
    for _ in range(int(timeout * 10)):
        try:
            r = await client.get("/api/health")
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.1)
    return False


async def recv_until(ws, predicate, timeout: float, sink: list) -> dict | None:
    async def _loop():
        while True:
            evt = json.loads(await ws.recv())
            sink.append(evt)
            if predicate(evt):
                return evt
    try:
        return await asyncio.wait_for(_loop(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    except Exception as e:  # noqa: BLE001 - surface why the socket closed
        print(f"  [diag] recv aborted: {type(e).__name__}: {e}", flush=True)
        return None


def diag(sink: list) -> None:
    types = [e.get("type") for e in sink]
    print(f"  [diag] events: {types[:25]}", flush=True)
    for e in sink:
        if e.get("type") in ("fatal", "error"):
            print(f"  [diag] {e.get('type')}: {e.get('message')}", flush=True)
            if e.get("traceback"):
                print(e["traceback"], flush=True)


def _build_temp_save():
    """Generate a throwaway world + character so the test never touches a user save."""
    from forge.config_loader import load_config
    from forge.character_creator import create_debug_character
    from web.creation import _generate_world

    out = REPO / "output"
    config = load_config()
    # This suite exercises the era game: the ERA INDEX, the era-scoped place tree, `lookup`.
    # (A classic save is the other fixture; `test_modes.py` covers both creation branches.)
    pc = create_debug_character(config)
    pc.mode = "time_traveler"
    gen = _generate_world(config, pc, out)
    stem = gen["slug"]
    return out / gen["player"], out / f"{stem}.timeline"


async def _drop_temp_save(player: Path, timeline: Path) -> None:
    """Remove everything the fixture created, including the scene registry it wrote."""
    discard_scene_manifest(OUTPUT, player.stem)
    player.unlink(missing_ok=True)
    timeline.unlink(missing_ok=True)
    shutil.rmtree(OUTPUT / "images" / player.stem, ignore_errors=True)


async def _session_one(client: httpx.AsyncClient, world: str) -> None:
    """Images ON: the awakening, an action turn with a real tool call, and the slashes."""
    r = await client.post("/api/sessions", json={"save": world, "temperature": 1.0,
                                                "scene_images": True})
    rec("POST /api/sessions", r.status_code == 200, f"HTTP {r.status_code}")
    sid = r.json().get("session_id")
    rec("session id returned", bool(sid), str(sid))
    try:
        r = await client.get(f"/api/sessions/{sid}")
        rec("GET /api/sessions/{id}", r.status_code == 200,
            f"model={r.json().get('model')}")

        async with ws_connect(f"{WS_BASE}/{sid}", max_size=None) as ws:
            # 1) Awakening
            sink: list = []
            got = await recv_until(ws, lambda e: e["type"] == "awakening_end", 120, sink)
            rec("WS awakening completes", got is not None)
            if got is None:
                diag(sink)
                raise RuntimeError("awakening did not complete")
            deltas = sum(1 for e in sink if e["type"] == "narrative_delta")
            rec("WS streaming (narrative deltas)", deltas > 0, f"{deltas} deltas")
            rec("awakening MCP tool result", any(e["type"] == "tool_result" for e in sink),
                str([e["name"] for e in sink if e["type"] == "tool_result"]))
            rec("WS events carry the owning session id",
                bool(sink) and all(e.get("session_id") == sid for e in sink),
                str([e.get("session_id") for e in sink[:3]]))
            ready = next((e for e in sink if e.get("type") == "ready"), None)
            rec("ready lists the scene-image tool",
                bool(ready) and "request_scene_image" in (ready.get("tools") or []),
                str((ready or {}).get("tools")))
            saved_era = json.loads((OUTPUT / world).read_text(encoding="utf-8")).get("era")
            rec("ready carries the era the save was created in",
                bool(ready) and ready.get("era") == saved_era and saved_era in playable_eras(),
                f"{str((ready or {}).get('era'))} (save: {saved_era})")
            rec("ready lists the generic lookup tool",
                bool(ready) and "lookup" in (ready.get("tools") or []),
                str((ready or {}).get("tools")))

            # The scene call is a declaration: with NO browser POST, the server pump still
            # records it file-less (the picture would only add the seed later).
            stem = Path(world).stem
            declared = next((p for p in known_scene_places(OUTPUT, stem, saved_era)
                             if p["place"] == SCENE_CALL["place"]), None)
            rec("images mode records the awakening's place without a picture",
                any(e.get("type") == "place_note" for e in sink)
                and declared is not None and declared.get("file") is None
                and declared.get("seeded") is False,
                f"event={any(e.get('type') == 'place_note' for e in sink)} entry={declared}")
            cur = current_scene_place(OUTPUT, stem) or {}
            rec("... and the registry's current pointer follows it",
                _scene_key(cur.get("place") or []) == _scene_key(SCENE_CALL["place"]),
                str(cur))

            # 2) Action turn with a scripted tool call
            sink2: list = []
            await ws.send(json.dumps({"type": "action",
                                      "text": "Roll a d20 for me to check for anything unusual."}))
            got = await recv_until(ws, lambda e: e["type"] == "turn_end", 120, sink2)
            rec("WS action turn completes", got is not None)
            rec("action MCP tool result", any(e["type"] == "tool_result" for e in sink2),
                str([e["name"] for e in sink2 if e["type"] == "tool_result"]))
            rec("action streamed narrative", any(e["type"] == "narrative_delta" for e in sink2))
            rec("scene_request fired for the action turn",
                any(e.get("type") == "scene_request" for e in sink2))
            rec("a stay turn on the same place emits no second place_note",
                not any(e.get("type") == "place_note" for e in sink2),
                str([e.get("type") for e in sink2]))
            rec("the turn handshake reached the client (paused)",
                any(e.get("type") == "paused" for e in sink2),
                str([e.get("type") for e in sink2]))

            # 3) Slash commands
            sink3: list = []
            await ws.send(json.dumps({"type": "slash", "command": "/stats"}))
            got = await recv_until(ws, lambda e: e["type"] == "stats", 60, sink3)
            has_stats = got is not None and isinstance(got.get("data"), dict) and bool(got["data"])
            rec("slash /stats", has_stats)
            rec("stats event carries the session id",
                got is not None and got.get("session_id") == sid)

            sink4: list = []
            await ws.send(json.dumps({"type": "slash", "command": "/help"}))
            got = await recv_until(ws, lambda e: e["type"] == "notice", 30, sink4)
            rec("slash /help", got is not None and "help" in str(got.get("title", "")).lower())

            # 4) Flags
            await ws.send(json.dumps({"type": "flags", "verbose": True, "temperature": 1.0}))
            rec("WS flags accepted", True)

        r = await client.get(f"/api/sessions/{sid}")
        state = r.json()
        rec("session context tracked", state.get("context_tokens", 0) > 0,
            f"{state.get('context_tokens')} tokens")
        rec("session turn tracked", state.get("turn_counter") is not None,
            f"turn_counter={state.get('turn_counter')}")
    finally:
        await client.delete(f"/api/sessions/{sid}")
    rec("DELETE /api/sessions/{id}", True)


async def _session_two(client: httpx.AsyncClient, world: str) -> None:
    """Images OFF: the mode's tool gating, and a place declaration reaching the registry."""
    stem = Path(world).stem
    era = json.loads((OUTPUT / world).read_text(encoding="utf-8")).get("era") or ""
    r = await client.post("/api/sessions", json={"save": world, "temperature": 1.0,
                                                "scene_images": False})
    sid = r.json().get("session_id")
    try:
        async with ws_connect(f"{WS_BASE}/{sid}", max_size=None) as ws:
            sink: list = []
            got = await recv_until(ws, lambda e: e["type"] == "awakening_end", 120, sink)
            rec("images off: the awakening completes", got is not None)
            ready = next((e for e in sink if e.get("type") == "ready"), None)
            tools = (ready or {}).get("tools") or []
            rec("images off: note_place offered, the scene tools hidden",
                "note_place" in tools and "request_scene_image" not in tools
                and "register_npcs" not in tools, str(tools))

            sink2: list = []
            await ws.send(json.dumps({"type": "action",
                                      "text": "I take the hall for the night."}))
            got = await recv_until(ws, lambda e: e["type"] == "turn_end", 120, sink2)
            rec("images off: the action turn completes", got is not None)
            notes = [e for e in sink2 if e.get("type") == "place_note"]
            rec("the declaration reaches the client as one place_note event", len(notes) == 1,
                str(notes))
            rec("images off: no scene_request at all",
                not any(e.get("type") == "scene_request" for e in sink2))

            # The engine hands the server the declaration; the pump (a worker thread) persists
            # it. Poll, because the write is dispatched off the event loop.
            want = _scene_key((notes[-1].get("place") if notes else []) or [])
            found = None
            for _ in range(60):
                places = known_scene_places(OUTPUT, stem, era)
                found = next((p for p in places if _scene_key(p["place"]) == want), None)
                if found is not None:
                    break
                await asyncio.sleep(0.05)
            rec("the declared place reaches the registry through the server pump",
                found is not None and bool(str(found.get("area") or "")),
                str(found))
    finally:
        await client.delete(f"/api/sessions/{sid}")


async def main() -> int:
    temp_player, temp_timeline = _build_temp_save()
    world = temp_player.name
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        with scripted(SCRIPT) as model:
            async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
                if not await wait_for_server(client):
                    rec("server up", False, "health check timed out")
                    raise RuntimeError("server did not start")
                rec("server up", True)

                r = await client.get("/")
                rec("GET / serves the UI",
                    r.status_code == 200 and "Project Infinity" in r.text,
                    f"HTTP {r.status_code}")
                r = await client.get("/app.js")
                rec("GET /app.js", r.status_code == 200 and "WebSocket" in r.text,
                    f"HTTP {r.status_code}")
                r = await client.get("/style.css")
                rec("GET /style.css", r.status_code == 200 and "--accent" in r.text,
                    f"HTTP {r.status_code}")

                r = await client.get("/api/worlds")
                worlds = r.json().get("worlds", [])
                world_files = [w.get("file") if isinstance(w, dict) else w for w in worlds]
                rec("GET /api/worlds", world in world_files, str(world_files))

                r = await client.get("/api/models")
                models = r.json().get("models", [])
                rec("GET /api/models",
                    any(m["id"] == "deepseek-v4.1-flash:cloud" for m in models),
                    f"{len(models)} models")

                await _session_one(client, world)
                await _session_two(client, world)

            rec("every model call came from the script (no provider was reached)",
                model.calls == len(SCRIPT), f"{model.calls} calls for {len(SCRIPT)} scripted")
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=30)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            server_task.cancel()
        await _drop_temp_save(temp_player, temp_timeline)

    print("\n" + "=" * 72)
    print("PHASE 2 RESULT")
    print("=" * 72)
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    all_ok = all(ok for _, ok, _ in RESULTS)
    print(f"\n  RESULT: {'ALL CHECKS PASSED' if all_ok else 'SOME CHECKS FAILED'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        code = asyncio.run(asyncio.wait_for(main(), timeout=300))
    except asyncio.TimeoutError:
        print("\nFAIL: timed out after 300s")
        code = 1
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
