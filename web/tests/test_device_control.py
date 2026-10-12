"""The Device: player control from turn 0, and the future gate.

The Device never fires on its own any more -- the player controls when, which historical age,
and the place. The four parts are the key to the future (New York): it is unreachable until
every one is held. No LLM / no network: the transitions are driven directly.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_device_control.py
"""

import asyncio
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

import device as device_mod  # noqa: E402
import web.engine as engine_mod  # noqa: E402

from web.engine import GameSession  # noqa: E402
from web.eras import era_arrivals, render_era_index  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


class _FakeMCP:
    """Stands in for the engine's MCP session: records the tools the engine calls."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))

        class _R:
            def __init__(self, text):
                self.content = [type("C", (), {"text": text})()]

        if name == "dump_player_db":
            return _R('{"name": "Test", "level": 1}')
        return _R('{"success": true}')


def _with_parts(gs, *parts):
    """Set how many parts are recovered; the four parts are the future key."""
    gs.parts_recovered = len(parts)
    return gs


def _session(seed=7, era="egypt"):
    gs = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs.session = _FakeMCP()
    gs.era = era
    gs.has_device = True
    gs.parts_recovered = 0
    gs.journey = [era]
    gs._rng = random.Random(seed)
    fd, gs.timeline_path = tempfile.mkstemp(suffix=".timeline")
    import os

    os.close(fd)
    os.unlink(gs.timeline_path)
    gs.messages = [{"role": "system", "content": "protocol"}]
    gs._era_index_at = len(gs.messages)
    gs.messages.append({"role": "system", "content": render_era_index(gs.era)})
    gs._places_at = len(gs.messages)
    gs.messages.append({"role": "system", "content": "places"})
    gs._primed = []
    gs.arrival = "the Giza quarry, on the Nile"
    gs._pending_legend = ""
    _stub_turns(gs)

    async def _no_legend():
        return None

    gs._ensure_legend = _no_legend
    return gs


def _drain(gs):
    out = []
    while not gs._evt_q.empty():
        out.append(gs._evt_q.get_nowait())
    return out


def _stub_turns(gs):
    """Replace the GM turn with a bookkeeping stub: this suite is about the Device state."""
    async def _run_turn(content, label):
        gs.messages.append({"role": "user", "content": content})
        gs.turn_counter += 1
        await gs._emit({"type": "turn_end", "text": "", "turn": gs.turn_counter})
    gs._run_turn = _run_turn


async def _player_turn(gs):
    """One player turn, driven the way `_handle_action` drives it."""
    await gs._run_turn("a player action", "turn")
    await gs._emit({"type": "cadence", **gs._device_state()})


ALL_PARTS = ("Escapement", "Compass Rose", "Regulator", "Mainspring")


def main():
    print("=" * 72)
    print("THE DEVICE: player control from the start; the future gate")
    print("=" * 72)

    # -- the cadence machinery is gone -----------------------------------------
    print("\n-- no more forced jumps")
    gs = _session()
    rec("there is no cadence band table", not hasattr(gs, "_cadence_span"))
    rec("there is no countdown", not hasattr(gs, "_jump_at_turn"))
    rec("the clock check is gone", not hasattr(gs, "_check_cadence"))

    # -- the panel is alive from turn 0 ----------------------------------------
    st = gs._device_state()
    rec("the Device is present and player-controlled from turn 0",
        st["present"] is True and all(st["abilities"].values()), str(st["abilities"]))
    rec("there is no direction control (the era menu names any age)",
        "direction" not in st["abilities"])
    rec("it reports the four slots", st["total"] == device_mod.part_total() == 4,
        f"{st['total']}")
    rec("a fresh Device holds no parts", st["parts"] == 0)

    # -- a turn never moves the Traveller on its own ----------------------------
    before = gs.era
    asyncio.run(_player_turn(gs))
    asyncio.run(_player_turn(gs))
    rec("two turns pass and the Device does not fire", gs.era == before, gs.era)
    rec("the panel is refreshed every turn",
        any(e.get("type") == "cadence" for e in _drain(gs)))

    # -- the aim names any historical age, but never the locked future ----------
    print("\n-- the aim: any age but the future")
    rec("any other historical age is a legal aim",
        all(gs._aim_allowed(e) for e in ("tang", "wallachia", "victorian")))
    rec("the age it stands in is not", gs._aim_allowed(gs.era) is False)
    rec("the future is locked with fewer than four parts",
        gs._aim_allowed("future") is False and gs._future_unlocked() is False)
    rec("the future is not a reachable destination yet",
        "future" not in gs._reachable_eras())
    locked_opts = [o for o in gs._era_options(gs._all_other_eras()) if o["id"] == "future"]
    rec("the panel still lists the future, marked locked",
        bool(locked_opts) and locked_opts[0]["locked"] is True, str(locked_opts))

    # -- aiming writes the pending steer; the locked future is refused ----------
    aim = _session(era="egypt")
    asyncio.run(aim._device_aim({"era": "tang"}))
    rec("aiming a historical age sets the pending steer", aim._steer == "tang", aim._steer)
    refused = _session(era="egypt")
    asyncio.run(refused._device_aim({"era": "future"}))
    rec("aiming the locked future is refused", refused._steer == "")

    # -- unlocking: every part opens the future --------------------------------
    print("\n-- four parts open the future")
    opened = _with_parts(_session(era="victorian"), *ALL_PARTS)
    rec("four parts unlock it", opened._future_unlocked() is True)
    rec("... the future becomes aimable", opened._aim_allowed("future") is True)
    rec("... and reachable", "future" in opened._reachable_eras())
    rec("... and the panel no longer locks it",
        [o for o in opened._era_options(opened._all_other_eras())
         if o["id"] == "future"][0]["locked"] is False)
    three = _with_parts(_session(era="victorian"), *ALL_PARTS[:3])
    rec("one part short keeps it locked", three._future_unlocked() is False)

    # -- the place menu: authored arrivals + the last 12 visited ----------------
    print("\n-- the place menu: two sources")
    menu = _session(era="egypt")._place_options()
    rec("the menu is scoped to the reachable eras",
        "egypt" not in menu and "future" not in menu and "tang" in menu, str(sorted(menu)))
    rec("each era offers its authored arrivals",
        menu["tang"]["arrivals"] == era_arrivals("tang"), str(menu["tang"]["arrivals"])[:60])
    rec("... and a recent list", isinstance(menu["tang"]["recent"], list))
    rec("an authored arrival is a legal place",
        _session(era="egypt")._place_allowed("tang", era_arrivals("tang")[0]) is True)
    rec("an invented place is not",
        _session(era="egypt")._place_allowed("tang", "nowhere") is False)

    fake = [{"place": [f"Place {i}"], "area": "City", "used": i, "era": "tang"}
            for i in range(20)]
    orig_known = engine_mod.known_scene_places
    engine_mod.known_scene_places = lambda *a, **k: fake
    try:
        pool = _session(era="egypt")._visited_pool("tang", 12)
    finally:
        engine_mod.known_scene_places = orig_known
    rec("the recent list is capped at 12", len(pool) == 12, str(len(pool)))
    rec("... newest first", pool[0] == "Place 19, in City", pool[0])

    # -- the release jumps, and the closing turn is manufactured ----------------
    print("\n-- release fires the jump")
    jump = _session(era="egypt")
    jump._steer = "tang"
    jump._aim_place = era_arrivals("tang")[0]
    asyncio.run(jump._device_release({}))
    rec("the release lands in the aimed age", jump.era == "tang", jump.era)
    rec("the aim is consumed by the jump", jump._steer == "" and jump._aim_place == "")
    rec("the engine DB is told the new age",
        any(n == "set_player_field" and a.get("key") == "era" for n, a in jump.session.calls))

    # -- both choices are required ----------------------------------------------
    no_place = _session(era="egypt")
    no_place._steer = "tang"
    asyncio.run(no_place._device_release({}))
    rec("a release with no place is refused", no_place.era == "egypt")
    no_era = _session(era="egypt")
    no_era._aim_place = era_arrivals("tang")[0]
    asyncio.run(no_era._device_release({}))
    rec("a release with no era is refused", no_era.era == "egypt")

    # -- the release is refused in combat ---------------------------------------
    fight = _session(era="egypt")
    fight._in_combat = True
    asyncio.run(fight._device_release({}))
    rec("a release is refused while a fight is live", fight.era == "egypt")

    # -- the place control is base ----------------------------------------------
    print("\n-- the place control is base")
    rec("the aimed place is kept without any part",
        _session()._clean_aim_place("  the east gate  ") == "the east gate")

    print(f"\n  RESULT: {'PASS' if all(RESULTS) else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
