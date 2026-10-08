"""The two games: classic (config/world.yml) and Time Traveler (the era ladder).

Locks the mode contract:
  - a save's mode is explicit if it says so, otherwise an `era` means Time Traveler and
    anything older is Classic -- so nothing has to be migrated;
  - `render_protocol` keeps exactly one game's rules and never leaves a marker behind;
  - a classic session is never offered `lookup`, and never pays for the ERA INDEX;
  - the era machinery (the cadence, the legends, the era-scoped place tree) is off.

No LLM / no network. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_modes.py
"""

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import (  # noqa: E402
    MODE_CLASSIC, MODE_TIME_TRAVELER, GameSession, _player_mode, filter_tools,
    render_protocol,
)

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _save(**fields) -> str:
    fd, path = tempfile.mkstemp(suffix=".player")
    import os
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(fields, f)
    return path


TOOLS = [{"function": {"name": n}} for n in
         ("dump_player_db", "lookup", "set_player_field", "dump_player_save_state",
          "request_scene_image", "register_npcs")]


def _names(tools):
    return [t["function"]["name"] for t in tools]


def main() -> bool:
    print("\n-- the mode a save is playing")
    p = _save(era="egypt")
    rec("an era save is Time Traveler", _player_mode(p) == MODE_TIME_TRAVELER)
    p2 = _save(name="Bizar", reputation={"eldoria": {"guard": []}})
    rec("a save with no era and no mode is Classic", _player_mode(p2) == MODE_CLASSIC)
    p3 = _save(mode="time_traveler")
    rec("an explicit mode wins with no era", _player_mode(p3) == MODE_TIME_TRAVELER)
    p4 = _save(mode="classic", era="egypt")
    rec("an explicit mode beats the era inference", _player_mode(p4) == MODE_CLASSIC)
    p5 = _save(mode="nonsense")
    rec("an unknown mode falls back to the inference", _player_mode(p5) == MODE_CLASSIC)
    p6 = _save(mode="CLASSIC ")
    rec("the mode is trimmed and case-folded", _player_mode(p6) == MODE_CLASSIC)
    rec("a missing file is Classic rather than an error",
        _player_mode(str(REPO / "no_such_save.player")) == MODE_CLASSIC)
    for f in (p, p2, p3, p4, p5, p6):
        Path(f).unlink()

    print("\n-- the protocol each game is handed")
    proto = (REPO / "GameMaster_MCP.md").read_text(encoding="utf-8")
    classic = render_protocol(proto, True, "hard", MODE_CLASSIC)
    tt = render_protocol(proto, True, "hard", MODE_TIME_TRAVELER)
    rec("no marker survives either mode", "<!--" not in classic and "<!--" not in tt)
    rec("Classic gets the WORLD_FILE awakening", "WORLD_FILE" in classic)
    rec("Classic never hears about an ERA_FILE", "ERA_FILE" not in classic)
    rec("Classic has no THE PEOPLES block", "THE PEOPLES" not in classic)
    rec("Time Traveler gets the ERA_FILE awakening", "ERA_FILE" in tt)
    rec("Time Traveler keeps THE PEOPLES", "THE PEOPLES" in tt)
    rec("... peopled by every class as well as every race (D35)",
        "all the common classes" in tt and "mercenary captain" in tt)
    rec("... with the guardrail extended to classes",
        "species or a class" in tt and "never let a class imply a culture" in tt)
    rec("the classes rule is Time Traveler only, so Classic pays nothing",
        "mercenary captain" not in classic)
    rec("Time Traveler is never handed a WORLD_FILE", "WORLD_FILE" not in tt)
    rec("the two are different documents", classic != tt)
    rec("the game not being played is the smaller prompt", len(classic) < len(tt))
    rec("the shared rules survive in both",
        "## INVARIANTS" in classic and "## INVARIANTS" in tt
        and "## FAILURE MODES" in classic and "## FAILURE MODES" in tt)
    rec("the difficulty gate still works alongside the mode",
        "Scale the ADVENTURE" not in render_protocol(proto, True, "hard", MODE_CLASSIC)
        and "Scale the ADVENTURE" in render_protocol(proto, True, "easy", MODE_CLASSIC)
        and "Scale the ADVENTURE" in render_protocol(proto, True, "easy", MODE_TIME_TRAVELER))
    rec("the scene gate still works alongside the mode",
        "request_scene_image" not in render_protocol(proto, False, "hard", MODE_TIME_TRAVELER)
        and "request_scene_image" in render_protocol(proto, True, "hard", MODE_TIME_TRAVELER))

    print("\n-- what the GM is offered")
    rec("Classic is never offered lookup", "lookup" not in _names(filter_tools(TOOLS, True, MODE_CLASSIC)))
    rec("Time Traveler is", "lookup" in _names(filter_tools(TOOLS, True, MODE_TIME_TRAVELER)))
    rec("the bookkeeping tools are hidden in both",
        all(engine_only not in _names(filter_tools(TOOLS, True, MODE_CLASSIC))
            and engine_only not in _names(filter_tools(TOOLS, True, MODE_TIME_TRAVELER))
            for engine_only in ("set_player_field", "dump_player_save_state")))
    rec("scenes off hides the scene and NPC tools",
        _names(filter_tools(TOOLS, False, MODE_TIME_TRAVELER)) == ["dump_player_db", "lookup"])
    rec("scenes on keeps them",
        set(_names(filter_tools(TOOLS, True, MODE_TIME_TRAVELER)))
        == {"dump_player_db", "lookup", "request_scene_image", "register_npcs"})

    print("\n-- a classic session runs no era machinery")
    gs = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs.classic = True
    gs.mode = MODE_CLASSIC
    gs.era = ""
    rec("the Device is off", gs._cadence_span() is None)
    rec("the counter reads nothing", gs._cadence_state()["turns_until"] is None)
    gs.classic = False
    gs.era = "egypt"
    gs.has_device = True
    rec("... and on in the era game", gs._cadence_span() is not None)

    print("\n-- what a new character is seeded with, in each game")
    import tempfile as _tempfile

    from forge.character_creator import create_debug_character
    from forge.config_loader import load_config
    from web.creation import _generate_world

    out = Path(_tempfile.mkdtemp())
    made, paths = [], []
    for want in ("classic", "time_traveler"):
        pc = create_debug_character(load_config())
        pc.mode = want
        gen = _generate_world(load_config(), pc, out)
        paths.append(out / gen["player"])
        data = json.loads(paths[-1].read_text(encoding="utf-8"))
        made.append(data)
        if want == "classic":
            rec("a classic character is written with mode classic", data.get("mode") == "classic")
            rec("... and no era at all", data.get("era") == "" and data.get("arrival") == "")
            rec("... and the world's own reputation map",
                isinstance((data.get("reputation") or {}).get("eldoria"), dict)
                and "egypt" not in (data.get("reputation") or {}))
            rec("... and no Device in the inventory",
                not any(isinstance(e, dict) and e.get("device")
                        for e in (data.get("inventory") or [])))
        else:
            rec("a time traveler is written with mode time_traveler",
                data.get("mode") == "time_traveler")
            from web.eras import era_arrivals, playable_eras

            rec("... starting in a rolled playable era, at a rolled arrival",
                data.get("era") in playable_eras()
                and data.get("arrival") in era_arrivals(data.get("era") or ""),
                f"{data.get('era')} / {data.get('arrival')!r}")
            rec("... with that era's own reputation seed, and no other era's",
                list(data.get("reputation") or {}) == [data.get("era")])
            rec("... with the Device in the inventory from the first turn",
                any(isinstance(e, dict) and e.get("device")
                    for e in (data.get("inventory") or [])))
            rec("... and the visit history starts at that age",
                data.get("journey") == data.get("era"), str(data.get("journey")))
    rec("the two seeds are genuinely different",
        made[0].get("reputation") != made[1].get("reputation"))
    rec("each created save resolves back to the game it chose",
        _player_mode(str(paths[0])) == MODE_CLASSIC
        and _player_mode(str(paths[1])) == MODE_TIME_TRAVELER,
        f"{_player_mode(str(paths[0]))}, {_player_mode(str(paths[1]))}")

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
