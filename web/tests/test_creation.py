"""Character-creation driver: direct bridge + full wizard over HTTP.

1. Direct: auto-answers the bridge, asserts `.player` naming, auto-suffix
   on collision, and that the character is playable by the MCP engine.
2. HTTP: boots the server in-process and drives the real `/api/creation*`
   endpoints end to end (the path the browser wizard uses).

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_creation.py
"""

import asyncio
import json
import sys
import threading
from pathlib import Path

import httpx
import uvicorn

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.creation import CreationBridge, _run_creation  # noqa: E402
from forge.config_loader import load_config  # noqa: E402

OUTPUT = REPO / "output"
DIRECT_NAME = "Test Hero"
HTTP_NAME = "Http Hero"
HTTP_PORT = 8125


def slug_prefix(name):
    return name.lower().replace(" ", "_")


def preclean(name):
    prefix = slug_prefix(name)
    for pattern in (f"{prefix}*.player",):
        for path in OUTPUT.glob(pattern):
            path.unlink()


def auto_answer(step, counters, name):
    kind = step["kind"]
    if kind == "text":
        counters["text"] += 1
        return name if counters["text"] == 1 else (step.get("default") or "Unknown")
    if kind == "number":
        index = counters["number"]
        counters["number"] += 1
        return 15 if index < 3 else 8
    if kind == "pointbuy":
        # Spend exactly 27: three 15s (9 each) + three 8s (0 each).
        return {a["key"]: (15 if i < 3 else 8) for i, a in enumerate(step["abilities"])}
    if kind == "single":
        return step["options"][0]["id"]
    if kind == "multi":
        count = step.get("min_choices") or 1
        return [opt["id"] for opt in step["options"][:count]]
    raise ValueError(f"unknown step kind: {kind}")


# ── direct bridge ─────────────────────────────────────────────────────────

def run_bridge_creation(name):
    bridge = CreationBridge(load_config(), OUTPUT)
    thread = threading.Thread(target=_run_creation, args=(bridge,), daemon=True)
    thread.start()
    counters = {"text": 0, "number": 0}
    prompts = 0
    terminal = None
    while True:
        steps, terminal = bridge.take_steps(timeout=120)
        if terminal is not None:
            break
        prompt = steps[-1]
        prompts += 1
        bridge.submit_answer(auto_answer(prompt, counters, name))
    thread.join(timeout=30)
    return terminal, prompts


def check_playable(player_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run():
        params = StdioServerParameters(
            command=sys.executable, args=["dice_server.py", str(player_path)], cwd=str(REPO),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("dump_player_db", {})
                text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
                return json.loads(text)

    return asyncio.run(_run())


# ── HTTP wizard ───────────────────────────────────────────────────────────

async def run_http_creation(name):
    from web.server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=HTTP_PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{HTTP_PORT}"
    try:
        async with httpx.AsyncClient(base_url=base, timeout=120) as client:
            for _ in range(100):
                try:
                    if (await client.get("/api/health")).status_code == 200:
                        break
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.1)

            resp = (await client.post("/api/creation")).json()
            cid = resp["creation_id"]
            counters = {"text": 0, "number": 0}
            prompts = 0
            while True:
                if resp.get("terminal"):
                    return resp["terminal"], prompts
                prompt = [s for s in resp.get("steps", []) if s.get("type") == "prompt"][-1]
                prompts += 1
                value = auto_answer(prompt, counters, name)
                resp = (await client.post(f"/api/creation/{cid}/answer", json={"value": value})).json()
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=30)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            server_task.cancel()


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 72)
    print("CHARACTER CREATION (bridge + HTTP)")
    print("=" * 72)
    created = []
    ok = True

    # 0) point-buy validator
    from web.creation import _validate_point_buy
    costs = {8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}
    abilities = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]
    good = {"strength": 15, "dexterity": 15, "constitution": 15,
            "intelligence": 8, "wisdom": 8, "charisma": 8}
    under = {**good, "constitution": 14}  # 25 points
    over = {**good, "strength": 16}        # out of range
    print("\n  [point-buy validation]")
    for label, case, expect in [("valid 27", good, True), ("under-spent", under, False),
                                ("out of range", over, False), ("not a dict", "nope", False)]:
        got = _validate_point_buy(case, abilities, costs, 27, 8, 15) is not None
        ok &= got == expect
        print(f"    [{'ok' if got == expect else 'FAIL'}] {label}")

    # 1) Direct bridge
    preclean(DIRECT_NAME)
    terminal, prompts = run_bridge_creation(DIRECT_NAME)
    ok &= bool(terminal) and terminal["type"] == "done"
    print(f"\n  [direct] terminal={terminal and terminal.get('type')} prompts={prompts}")
    if not ok:
        print(f"  [direct] message: {terminal.get('message') if terminal else None}")
        return 1
    player = OUTPUT / terminal["player"]
    created += [player]
    data = json.loads(player.read_text(encoding="utf-8"))
    ok &= player.exists() and data.get("name") == DIRECT_NAME
    ok &= not (OUTPUT / f"{terminal['slug']}.wwf").exists()  # no .wwf any more
    ok &= bool(data.get("reputation"))  # world scaffold seeds reputation
    print(f"  [direct] {player.name} | {data.get('race')} {data.get('character_class')} "
          f"HP {data.get('current_hit_points')} AC {data.get('armor_class')}")

    terminal2, _ = run_bridge_creation(DIRECT_NAME)
    created += [OUTPUT / terminal2["player"]]
    ok &= terminal2["slug"] == "test_hero_2"
    print(f"  [direct] collision stem: {terminal2['slug']}")
    ok &= not player.name.endswith("_weave.player")

    sheet = check_playable(player)
    ok &= sheet.get("name") == DIRECT_NAME
    print(f"  [direct] playable via MCP: {sheet.get('name')}")

    # 2) HTTP wizard
    preclean(HTTP_NAME)
    try:
        terminal3, prompts3 = asyncio.run(asyncio.wait_for(run_http_creation(HTTP_NAME), timeout=300))
    except asyncio.TimeoutError:
        terminal3, prompts3 = {"type": "error", "message": "http timeout"}, 0
    ok &= bool(terminal3) and terminal3["type"] == "done"
    print(f"\n  [http] terminal={terminal3 and terminal3.get('type')} prompts={prompts3}")
    if terminal3 and terminal3.get("type") == "done":
        player3 = OUTPUT / terminal3["player"]
        created += [player3]
        ok &= player3.exists()
        print(f"  [http] {player3.name}")

    for path in created:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    print(f"\n  RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
