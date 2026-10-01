"""Save driver: no autosave; a manual save generates the timeline and continues
turn numbering from the loaded timeline.

Builds its OWN temporary save (via the Forge's debug character + world gen), so
it never touches a user's save. Seeds a legacy "Rounds 1-19" timeline, plays one
turn, saves, and asserts the new entry continues at "Turns 20-20".

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_save.py
"""

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import GameSession  # noqa: E402
from web.naming import sanitize_save_name  # noqa: E402

OUTPUT = REPO / "output"
NEW = "My Epic Save"
MODEL = "deepseek-v4.1-flash:cloud"


def unit_tests() -> bool:
    from web.engine import _max_timeline_turn

    ok = True
    print("  -- naming --")
    for inp, expect in [
        ("My Save", ("My Save", "My Save.wwf")),
        ("bad/name:here", ("bad_name_here", "bad_name_here.wwf")),
        ("already.wwf", ("already", "already.wwf")),
        ("", ("save", "save.wwf")),
        ("CON", ("CON_save", "CON_save.wwf")),
    ]:
        got = sanitize_save_name(inp)
        ok &= got == expect
        print(f"    [{'ok' if got == expect else 'FAIL'}] {inp!r} -> {got}")

    print("  -- timeline turn parsing --")
    for label, text, expect in [
        ("legacy Rounds 1-19", "# Session Timeline\n\n## Rounds 1-19 | a | b\n", 19),
        ("new Turns 20-21", "## Turns 20-21 | a | b\n", 21),
        ("single Turn 5", "## Turn 5 | a | b\n", 5),
        ("en-dash Turns 3-7", "## Turns 3\u20137 | a | b\n", 7),
        ("highest wins", "## Turns 2-4\n## Turns 10-12\n## Rounds 1-19\n", 19),
        ("no headings", "just prose", 0),
        ("empty", "", 0),
    ]:
        got = _max_timeline_turn(text)
        ok &= got == expect
        print(f"    [{'ok' if got == expect else 'FAIL'}] {label} -> {got}")
    return ok


async def _dump_player(player_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["dice_server.py", str(player_path)], cwd=str(REPO),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("dump_player_db", {})
            text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
            return json.loads(text)


def _build_temp_save():
    """Generate a brand-new world + character; return (stem, wwf, player, timeline)."""
    from forge.config_loader import load_config
    from forge.character_creator import create_debug_character
    from web.creation import _generate_world

    config = load_config()
    gen = _generate_world(config, create_debug_character(config), OUTPUT)
    stem = gen["slug"]
    return stem, OUTPUT / gen["wwf"], OUTPUT / gen["player"], OUTPUT / f"{stem}.timeline"


async def functional() -> bool:
    stem, wwf, player, timeline = _build_temp_save()
    new_files = [OUTPUT / f"{NEW}.wwf", OUTPUT / f"{NEW}.player", OUTPUT / f"{NEW}.timeline"]

    # Seed a legacy timeline: a previous session already recorded 19 turns.
    timeline.write_text(
        "# Session Timeline\n\n"
        "## Rounds 1-19 | Old Town | Day 1\n"
        "**Key Events**:\n- Seeded history for the test.\n",
        encoding="utf-8",
    )
    player_mtime_before = player.stat().st_mtime_ns

    got_timeline = got_saved = saved_before_manual = False
    session = GameSession(base_dir=REPO, model=MODEL, context_window=1_048_576, temperature=1.0)
    ok = False
    try:
        await session.start(wwf)
        turn_done = False
        async for evt in session.events():
            t = evt["type"]
            if t == "awakening_end":
                await session.submit("Roll a d20 for me.")
            elif t == "turn_end":
                turn_done = True
                await session.submit_save(NEW)  # manual save -> timeline + player
            elif t == "saved":
                if turn_done:
                    got_saved = True
                    break
                saved_before_manual = True
            elif t == "timeline":
                got_timeline = True
            elif t == "fatal":
                print(f"    [fatal] {evt.get('message')}")
                break
            elif t == "closed":
                break

        tl = OUTPUT / f"{NEW}.timeline"
        text = tl.read_text(encoding="utf-8") if tl.exists() else ""
        continued = "Turns 20-20" in text
        ok = True
        ok &= got_saved and got_timeline
        ok &= not saved_before_manual
        ok &= session.turn_counter == 20 and session.last_timeline_turn == 20
        ok &= (OUTPUT / f"{NEW}.wwf").exists() and (OUTPUT / f"{NEW}.player").exists() and tl.exists()
        ok &= continued
        ok &= "Mechanical Changes" not in text
        ok &= player.stat().st_mtime_ns == player_mtime_before  # no autosave on the seeded save
        print(f"    saved={got_saved} timeline={got_timeline} turn={session.turn_counter} "
              f"continued_from_19={continued} orig_untouched={player.stat().st_mtime_ns == player_mtime_before}")

        if (OUTPUT / f"{NEW}.player").exists():
            sheet = await _dump_player(OUTPUT / f"{NEW}.player")
            print(f"    playable via MCP: {sheet.get('name')} {sheet.get('character_class')}")
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
        for path in new_files + [wwf, player, timeline]:
            path.unlink(missing_ok=True)
    return bool(ok)


def main() -> int:
    print("=" * 72)
    print("SAVE (no autosave; timeline on save; turns continue across sessions)")
    print("=" * 72)
    ok = unit_tests()
    print("  -- functional (self-contained temp save) --")
    try:
        ok &= asyncio.run(asyncio.wait_for(functional(), timeout=600))
    except asyncio.TimeoutError:
        print("    FAIL: timed out")
        ok = False
    print(f"\n  RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
