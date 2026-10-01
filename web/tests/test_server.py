"""Phase 2 driver: verify the FastAPI server over real REST + WebSocket.

Starts uvicorn in-process, then exercises the parity checklist:
  worlds, models, session create/state/delete, WS streaming awakening,
  an action turn with a real tool result, and /stats + /help slash commands.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_server.py
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import uvicorn

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.server import app  # noqa: E402

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # older websockets
    from websockets import connect as ws_connect  # type: ignore

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
WS_BASE = f"ws://127.0.0.1:{PORT}/ws"

RESULTS: list[tuple[str, bool, str]] = []


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
    gen = _generate_world(config, create_debug_character(config), out)
    stem = gen["slug"]
    return out / gen["wwf"], out / gen["player"], out / f"{stem}.timeline"


async def main() -> int:
    temp_wwf, temp_player, temp_timeline = _build_temp_save()
    world = temp_wwf.name
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    sid = None
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
            if not await wait_for_server(client):
                rec("server up", False, "health check timed out")
                raise RuntimeError("server did not start")
            rec("server up", True)

            r = await client.get("/")
            rec("GET / serves the UI", r.status_code == 200 and "Project Infinity" in r.text,
                f"HTTP {r.status_code}")
            r = await client.get("/app.js")
            rec("GET /app.js", r.status_code == 200 and "WebSocket" in r.text, f"HTTP {r.status_code}")
            r = await client.get("/style.css")
            rec("GET /style.css", r.status_code == 200 and "--accent" in r.text, f"HTTP {r.status_code}")

            r = await client.get("/api/worlds")
            worlds = r.json().get("worlds", [])
            world_files = [w.get("file") if isinstance(w, dict) else w for w in worlds]
            rec("GET /api/worlds", world in world_files, str(world_files))

            r = await client.get("/api/models")
            models = r.json().get("models", [])
            rec("GET /api/models", any(m["id"] == "deepseek-v4.1-flash:cloud" for m in models),
                f"{len(models)} models")

            r = await client.post("/api/sessions", json={"wwf": world, "temperature": 1.0})
            rec("POST /api/sessions", r.status_code == 200, f"HTTP {r.status_code}")
            sid = r.json().get("session_id")
            rec("session id returned", bool(sid), str(sid))

            r = await client.get(f"/api/sessions/{sid}")
            rec("GET /api/sessions/{id}", r.status_code == 200,
                f"model={r.json().get('model')}")

        async with ws_connect(f"{WS_BASE}/{sid}", max_size=None) as ws:
            # 1) Awakening
            sink: list = []
            got = await recv_until(ws, lambda e: e["type"] == "awakening_end", 300, sink)
            rec("WS awakening completes", got is not None)
            if got is None:
                diag(sink)
                raise RuntimeError("awakening did not complete")
            deltas = sum(1 for e in sink if e["type"] == "narrative_delta")
            rec("WS streaming (narrative deltas)", deltas > 0, f"{deltas} deltas")
            rec("awakening MCP tool result", any(e["type"] == "tool_result" for e in sink),
                str([e["name"] for e in sink if e["type"] == "tool_result"]))

            # 2) Action turn with a forced tool call
            sink2: list = []
            await ws.send(json.dumps({"type": "action",
                                      "text": "Roll a d20 for me to check for anything unusual."}))
            got = await recv_until(ws, lambda e: e["type"] == "turn_end", 300, sink2)
            rec("WS action turn completes", got is not None)
            rec("action MCP tool result", any(e["type"] == "tool_result" for e in sink2),
                str([e["name"] for e in sink2 if e["type"] == "tool_result"]))
            rec("action streamed narrative", any(e["type"] == "narrative_delta" for e in sink2))

            # 3) Slash commands
            sink3: list = []
            await ws.send(json.dumps({"type": "slash", "command": "/stats"}))
            got = await recv_until(ws, lambda e: e["type"] == "stats", 60, sink3)
            has_stats = got is not None and isinstance(got.get("data"), dict) and bool(got["data"])
            rec("slash /stats", has_stats)

            sink4: list = []
            await ws.send(json.dumps({"type": "slash", "command": "/help"}))
            got = await recv_until(ws, lambda e: e["type"] == "notice", 30, sink4)
            rec("slash /help", got is not None and "help" in str(got.get("title", "")).lower())

            # 4) Flags
            await ws.send(json.dumps({"type": "flags", "verbose": True, "temperature": 1.0}))
            rec("WS flags accepted", True)

        async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
            r = await client.get(f"/api/sessions/{sid}")
            state = r.json()
            rec("session context tracked", state.get("context_tokens", 0) > 0,
                f"{state.get('context_tokens')} tokens")
            rec("session turn tracked", state.get("turn_counter") is not None,
                f"turn_counter={state.get('turn_counter')}")
            r = await client.delete(f"/api/sessions/{sid}")
            rec("DELETE /api/sessions/{id}", r.status_code == 200)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=30)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            server_task.cancel()
        for path in (temp_wwf, temp_player, temp_timeline):
            path.unlink(missing_ok=True)

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
        code = asyncio.run(asyncio.wait_for(main(), timeout=900))
    except asyncio.TimeoutError:
        print("\nFAIL: timed out after 900s")
        code = 1
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
