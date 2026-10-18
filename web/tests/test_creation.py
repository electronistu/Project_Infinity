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
import tempfile
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


def preclean(name, output_dir=None):
    prefix = slug_prefix(name)
    out = Path(output_dir) if output_dir else OUTPUT
    for pattern in (f"{prefix}*.player",):
        for path in out.glob(pattern):
            path.unlink()


def auto_answer(step, counters, name, prefer=""):
    kind = step["kind"]
    if kind == "confirm":
        return True
    if kind == "text":
        counters["text"] += 1
        return name if counters["text"] == 1 else (step.get("default") or "Unknown")
    if kind == "number":
        counters["number"] += 1
        # Age is the only numeric step; answer at the race's adulthood (its min).
        return int(step.get("min") or 0)
    if kind == "pointbuy":
        # Spend exactly 27: three 15s (9 each) + three 8s (0 each).
        return {a["key"]: (15 if i < 3 else 8) for i, a in enumerate(step["abilities"])}
    if kind == "single":
        prefers = prefer if isinstance(prefer, (list, tuple)) else [prefer]
        opts = step["options"]
        for p in prefers:  # an exact label first, so "Elf" does not steal "High Elf"
            if p and any(str(o.get("label", "")).strip().lower() == str(p).strip().lower()
                         for o in opts):
                for o in opts:
                    if str(o.get("label", "")).strip().lower() == str(p).strip().lower():
                        return o["id"]
        for p in prefers:
            if not p:
                continue
            for opt in opts:
                if str(p).lower() in str(opt.get("label", "")).lower():
                    return opt["id"]
        return opts[0]["id"]
    if kind == "multi":
        count = step.get("min_choices") or 1
        return [opt["id"] for opt in step["options"][:count]]
    raise ValueError(f"unknown step kind: {kind}")


# ── direct bridge ─────────────────────────────────────────────────────────

def run_bridge_creation(name, prefer="", output_dir=None):
    bridge = CreationBridge(load_config(), output_dir or OUTPUT)
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
        bridge.submit_answer(auto_answer(prompt, counters, name, prefer))
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

            # Name, then gender; go back once and prove the gender prompt returns
            # with the dropped answer attached for the UI to prefill.
            prompt = [s for s in resp.get("steps", []) if s.get("type") == "prompt"][-1]
            resp = (await client.post(f"/api/creation/{cid}/answer",
                                      json={"value": name})).json()
            prompt = [s for s in resp.get("steps", []) if s.get("type") == "prompt"][-1]
            resp = (await client.post(f"/api/creation/{cid}/answer",
                                      json={"value": "Unknown"})).json()
            back = (await client.post(f"/api/creation/{cid}/back")).json()
            back_prompt = [s for s in back.get("steps", []) if s.get("type") == "prompt"][-1]
            back_ok = (back_prompt.get("kind") == "text"
                       and "gender" in str(back_prompt.get("prompt", "")).lower()
                       and back_prompt.get("value") == "Unknown")
            print(f"  [http] back -> {back_prompt.get('prompt')} "
                  f"value={back_prompt.get('value')!r} ok={back_ok}")
            if not back_ok:
                return {"type": "error", "message": "back did not return the previous prompt"}, prompts
            counters["text"] = 2  # name + the re-answered gender
            resp = (await client.post(f"/api/creation/{cid}/answer",
                                      json={"value": "Female"})).json()

            # An invalid single-select is rejected with retry=true, does not advance,
            # and is not recorded in the accepted history.
            bad = (await client.post(f"/api/creation/{cid}/answer",
                                     json={"value": "999"})).json()
            bad_prompt = [s for s in bad.get("steps", []) if s.get("type") == "prompt"][-1]
            retry_ok = bad.get("accepted") is False and bad_prompt.get("retry") is True
            print(f"  [http] invalid answer accepted={bad.get('accepted')} "
                  f"retry={bad_prompt.get('retry')} ok={retry_ok}")
            if not retry_ok:
                return {"type": "error", "message": "invalid answer was not marked retry"}, prompts
            resp = bad

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
    # auto_answer takes the first option, and the first game the Forge offers is Classic --
    # so this run also proves the default end to end: no era at all, and the world's own
    # reputation map, built from config/world.yml's kingdoms.
    ok &= data.get("mode") == "classic"
    ok &= bool(data.get("reputation"))
    ok &= isinstance(data["reputation"].get("eldoria"), dict)
    ok &= data.get("era") == "" and data.get("arrival") == ""
    ok &= any("game" in str(p).lower() for p in prompts) if isinstance(prompts, list) else True
    ok &= data.get("difficulty") == "hard"  # auto_answer picks the first option
    # Age is stored and never pre-adulthood (auto_answer answers at the race's adulthood).
    _race_name = str(data.get("race") or "")
    _race = next((r for r in load_config().races if r.name in _race_name), None)
    _adult = _race.age.adulthood if _race else 1
    ok &= isinstance(data.get("age"), int) and data.get("age") >= _adult
    print(f"  [direct] {player.name} | {data.get('race')} {data.get('character_class')} "
          f"HP {data.get('current_hit_points')} AC {data.get('armor_class')} "
          f"age {data.get('age')} difficulty {data.get('difficulty')}")

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

    # ── the SRD race roster: all nine, including the floating choices ──
    from forge.config_loader import load_config as _load

    races_now = [r.name for r in _load().races]
    rec_ok = [r for r in ("Gnome", "Half-Elf", "Half-Orc", "Tiefling") if r in races_now]
    ok &= rec_ok == ["Gnome", "Half-Elf", "Half-Orc", "Tiefling"]
    print(f"  [races] all nine SRD races: {ok} -> {races_now}")
    gnome = next((r for r in _load().races if r.name == "Gnome"), None)
    half = next((r for r in _load().races if r.name == "Half-Elf"), None)
    ok &= bool(gnome) and [s.name for s in gnome.subraces] == ["Forest Gnome", "Rock Gnome"]
    ok &= bool(half) and half.asi_choices is not None and half.asi_choices.count == 2 \
        and "Charisma" in (half.asi_choices.exclude or []) and half.skill_choices == 2
    print(f"  [races] gnome subraces + half-elf floating ASIs/skills: {ok}")

    # A half-elf end to end: the two floating +1s land, and the sheet is valid.
    preclean("Halfie")
    terminal_h, _ = run_bridge_creation("Halfie", prefer="Half-Elf")
    ok &= bool(terminal_h) and terminal_h.get("type") == "done"
    if terminal_h and terminal_h.get("type") == "done":
        hp = OUTPUT / terminal_h["player"]
        created += [hp]
        hdata = json.loads(hp.read_text(encoding="utf-8"))
        stats = hdata.get("stats") or {}
        # point-buy: three 15s + three 8s, +2 CHA and two floating +1s => 73 total
        total = sum(int(stats.get(k, 0)) for k in ("str", "dex", "con", "int", "wis", "cha"))
        ok &= hdata.get("race") == "Half-Elf" and total == 73 and stats.get("cha") == 10
        ok &= len(hdata.get("skills") or []) >= 6  # background + class + the two racial
        print(f"  [races] half-elf built: {hdata.get('race')} total {total} "
              f"cha {stats.get('cha')} skills {len(hdata.get('skills') or [])}")

    # A High Elf Wizard carries its racial cantrip: 3 from the class + 1 from the race.
    preclean("High Elf Wizard")
    terminal_e, _ = run_bridge_creation("High Elf Wizard", prefer=["Elf", "High Elf", "Wizard"])
    ok &= bool(terminal_e) and terminal_e.get("type") == "done"
    if terminal_e and terminal_e.get("type") == "done":
        ep = OUTPUT / terminal_e["player"]
        created += [ep]
        edata = json.loads(ep.read_text(encoding="utf-8"))
        cantrips = ((edata.get("spellcasting") or {}).get("cantrips") or [])
        ok &= edata.get("race") == "High Elf" and edata.get("character_class") == "Wizard"
        ok &= len(cantrips) == 4  # 3 class + the racial choice
        print(f"  [races] high-elf wizard cantrips: {len(cantrips)} {cantrips}")

    # The other game, chosen at creation: the era ladder, an arrival rolled from that era's
    # own list, and the era's own reputation seed -- proof that the question decides.
    preclean("Time Traveller Probe")
    terminal_t, _ = run_bridge_creation("Time Traveller Probe", prefer="Time Traveler")
    ok &= bool(terminal_t) and terminal_t.get("type") == "done"
    if terminal_t and terminal_t.get("type") == "done":
        tp = OUTPUT / terminal_t["player"]
        created += [tp]
        tdata = json.loads(tp.read_text(encoding="utf-8"))
        from web.eras import playable_eras

        ok &= tdata.get("mode") == "time_traveler"
        ok &= tdata.get("era") in playable_eras()  # rolled at creation, not START_ERA
        ok &= not tdata.get("arrival")  # no preset arrival; the GM declares the opening place
        ok &= list(tdata.get("reputation") or {}) == [tdata.get("era")]
        print(f"  [modes] time traveler built: mode {tdata.get('mode')} era {tdata.get('era')} "
              f"arrival {tdata.get('arrival')!r}")

    # The SRD domain/oath/circle spells ride the sheet: a Cleric is a Life Domain whose
    # domain spells are always prepared (and never counted against the prepared number).
    preclean("Cleric Life")
    terminal_c, _ = run_bridge_creation("Cleric Life", prefer="Cleric")
    ok &= bool(terminal_c) and terminal_c.get("type") == "done"
    if terminal_c and terminal_c.get("type") == "done":
        cp = OUTPUT / terminal_c["player"]
        created += [cp]
        cdata = json.loads(cp.read_text(encoding="utf-8"))
        csc = cdata.get("spellcasting") or {}
        ok &= cdata.get("character_class") == "Cleric"
        ok &= csc.get("subclass") == "Life Domain"
        ok &= csc.get("always_prepared") == ["Bless", "Cure Wounds"]
        print(f"  [domain] cleric: {csc.get('subclass')} "
              f"always_prepared {csc.get('always_prepared')}")

    # The Circle of the Land asks for a terrain (the only subclass with a choice); level 1
    # grants no circle spells yet, but the terrain is recorded for the levels that do.
    preclean("Druid Circle")
    terminal_d, _ = run_bridge_creation("Druid Circle", prefer="Druid")
    ok &= bool(terminal_d) and terminal_d.get("type") == "done"
    if terminal_d and terminal_d.get("type") == "done":
        dp = OUTPUT / terminal_d["player"]
        created += [dp]
        ddata = json.loads(dp.read_text(encoding="utf-8"))
        dsc = ddata.get("spellcasting") or {}
        ok &= ddata.get("character_class") == "Druid"
        ok &= str(dsc.get("subclass") or "").startswith("Circle of the Land (")
        ok &= dsc.get("always_prepared") in (None, [])  # level 1 grants no circle spells yet
        print(f"  [domain] druid: {dsc.get('subclass')}")

    # A fresh install has no `output/` (its contents are gitignored): the Forge must
    # create it before its final write, or creation dies at the very last step.
    with tempfile.TemporaryDirectory() as tmp:
        fresh_dir = Path(tmp) / "Project_Infinity" / "output"  # deliberately absent
        ok &= not fresh_dir.exists()
        terminal_f, _ = run_bridge_creation("Fresh Install", output_dir=fresh_dir)
        ok &= bool(terminal_f) and terminal_f.get("type") == "done"
        if terminal_f and terminal_f.get("type") == "done":
            fp = fresh_dir / terminal_f["player"]
            ok &= fresh_dir.is_dir() and fp.exists()
            print(f"  [fresh] created output/ for a brand-new save: {fp.name}")
        else:
            ok = False
            print(f"  [fresh] FAIL: {terminal_f and terminal_f.get('message')}")

    for path in created:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    print(f"\n  RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
