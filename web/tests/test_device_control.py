"""The Device: player control from turn 0, and the future gate.

The Device never fires on its own -- the player controls when, which historical age, and (if
they have stood there) the place. The four parts are the key to the future (New York): it is
unreachable until every one is held. There are no preset arrivals: the GM chooses the landing in
an age the Traveller has not stood in. No LLM / no network: the transitions are driven directly.

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
from web import session_device as _device_mod  # noqa: E402
from web import session_scene as _scene_mod  # noqa: E402
from web import session_turn as _turn_mod  # noqa: E402

from web.engine import GameSession  # noqa: E402


class _Patch:
    """The image-layer helpers the Device code calls are imported per mixin module now (the
    split), so a patch must land on every module whose code calls the name -- engine.py no
    longer re-exports them. `engine_mod.X = v` / `engine_mod.X` keep working through here."""

    _TARGETS = {
        "known_scene_places": (_device_mod, _scene_mod, _turn_mod),
        "current_scene_place": (_scene_mod,),
    }

    def __getattr__(self, name):
        return getattr(self._TARGETS[name][0], name)

    def __setattr__(self, name, value):
        for mod in self._TARGETS[name]:
            setattr(mod, name, value)


engine_mod = _Patch()

from web.protocol import DEVICE_DECLARES  # noqa: E402
from web.eras import render_era_index  # noqa: E402

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
    gs.arrival = ""
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

    # -- the destination names any historical age, but never the locked future --
    print("\n-- the destination: any age but the future")
    rec("any other historical age is a legal destination",
        all(gs._era_allowed(e) for e in ("tang", "wallachia", "victorian")))
    rec("the age it stands in is not", gs._era_allowed(gs.era) is False)
    rec("the future is locked with fewer than four parts",
        gs._era_allowed("future") is False and gs._future_unlocked() is False)
    rec("the future is not a reachable destination yet",
        "future" not in gs._reachable_eras())
    locked_opts = [o for o in gs._era_options(gs._all_other_eras()) if o["id"] == "future"]
    rec("the panel still lists the future, marked locked",
        bool(locked_opts) and locked_opts[0]["locked"] is True, str(locked_opts))

    # -- the release refuses the locked future -----------------------------------
    refused = _session(era="egypt")
    asyncio.run(refused._device_release({"era": "future"}))
    rec("a release to the locked future is refused", refused.era == "egypt")

    # -- unlocking: every part opens the future --------------------------------
    print("\n-- four parts open the future")
    opened = _with_parts(_session(era="victorian"), *ALL_PARTS)
    rec("four parts unlock it", opened._future_unlocked() is True)
    rec("... the future becomes a legal destination", opened._era_allowed("future") is True)
    rec("... and reachable", "future" in opened._reachable_eras())
    rec("... and the panel no longer locks it",
        [o for o in opened._era_options(opened._all_other_eras())
         if o["id"] == "future"][0]["locked"] is False)
    three = _with_parts(_session(era="victorian"), *ALL_PARTS[:3])
    rec("one part short keeps it locked", three._future_unlocked() is False)

    # -- the place menu: age -> area -> up to 12 places --------------------------
    print("\n-- the place menu: age, area, then places")
    menu = _session(era="egypt")._place_options()
    rec("the menu is scoped to the reachable eras",
        "egypt" not in menu and "future" not in menu and "tang" in menu, str(sorted(menu)))
    rec("an era offers no preset arrivals -- only areas and their places",
        "arrivals" not in menu["tang"] and isinstance(menu["tang"]["areas"], list),
        str(menu["tang"]))
    rec("an invented place is never legal",
        _session(era="egypt")._place_allowed("tang", "Chang'an", "nowhere") is False)

    fake = ([{"place": [f"West {i}"], "area": "Chang'an", "kingdom": "Tang",
              "used": 100 + i, "era": "tang"} for i in range(20)]
            + [{"place": [f"East {i}"], "area": "Luoyang", "kingdom": "Tang",
                "used": i, "era": "tang"} for i in range(20)])
    orig_known = engine_mod.known_scene_places
    engine_mod.known_scene_places = lambda *a, **k: fake
    try:
        gsv = _session(era="egypt")
        gsv.active_name = "save"
        opts = gsv._place_options()["tang"]["areas"]
        area_ok = gsv._area_allowed("tang", "Chang'an")
        visited_ok = gsv._place_allowed("tang", "Chang'an", "West 19")
        cross = gsv._place_allowed("tang", "Luoyang", "West 19")
        landing = gsv._landing_place("tang", "Chang'an", "West 19")
    finally:
        engine_mod.known_scene_places = orig_known
    rec("each area keeps its own capped list (<=12), newest first",
        [(a["area"], len(a["places"])) for a in opts] == [("Chang'an", 12), ("Luoyang", 12)]
        and opts[0]["places"][0] == "West 19", str(opts)[:120])
    rec("a visited area is a legal joint", area_ok is True)
    rec("a place is legal in its own area", visited_ok is True)
    rec("... and not in another area", cross is False)
    rec("a picked place resolves to its real address",
        isinstance(landing, dict) and landing.get("place") == ["West 19"]
        and landing.get("area") == "Chang'an" and landing.get("kingdom") == "Tang",
        str(landing))

    # -- the release jumps: an age alone, or an age and a visited place ---------
    print("\n-- release fires the jump")
    solo = _session(era="egypt")
    asyncio.run(solo._device_release({"era": "tang"}))
    rec("a release needs only an age", solo.era == "tang", solo.era)
    rec("... and leaves the landing to the GM", solo.arrival == "" and solo._current_place is None)
    solo_text = "".join(str(m.get("content") or "") for m in solo.messages)
    rec("... the GM is told to choose where the Traveller lands",
        "This age is new to you; choose where the Traveller lands." in solo_text)
    rec("... the images-on jump needs no note_place instruction", DEVICE_DECLARES not in solo_text)
    rec("the engine DB is told the new age",
        any(n == "set_player_field" and a.get("key") == "era" for n, a in solo.session.calls))
    rec("... the jump is labelled a jump, not a new game",
        "SITUATION: DEVICE JUMP" in solo_text)
    rec("... and the era file is labelled, so it is not read as a session start",
        "THE AGE YOU HAVE ENTERED" in solo_text)
    rec("... the crossing resets the running spells",
        any(n == "device_jump_reset" for n, _a in solo.session.calls), str(solo.session.calls))

    text_solo = _session(era="egypt")
    text_solo.scene_images = False
    asyncio.run(text_solo._device_release({"era": "tang"}))
    ts_text = "".join(str(m.get("content") or "") for m in text_solo.messages)
    rec("a text-mode landing tells the GM to declare its place", DEVICE_DECLARES in ts_text)

    picked = _session(era="egypt")
    picked.active_name = "save"
    engine_mod.known_scene_places = lambda *a, **k: [
        {"place": ["the west market", "the stalls"], "area": "Chang'an", "kingdom": "Tang",
         "used": 1, "era": "tang"}]
    try:
        asyncio.run(picked._device_release(
            {"era": "tang", "area": "Chang'an", "place": "the west market — the stalls"}))
    finally:
        engine_mod.known_scene_places = orig_known
    rec("a release to a place lands on it",
        picked.era == "tang" and picked.arrival == "the west market, in Chang'an", picked.arrival)
    rec("... with its real address, so the caption is right at once",
        isinstance(picked._current_place, dict)
        and picked._current_place.get("place") == ["the west market", "the stalls"]
        and picked._current_place.get("era") == "tang", str(picked._current_place))
    picked_text = "".join(str(m.get("content") or "") for m in picked.messages)
    rec("... and the GM is told the place, not sent to choose one",
        f"You arrive at {picked.arrival}." in picked_text
        and "choose where the Traveller lands" not in picked_text)

    # -- the area alone: the GM chooses inside it -------------------------------
    area_only = _session(era="egypt")
    area_only.active_name = "save"
    engine_mod.known_scene_places = lambda *a, **k: [
        {"place": ["the west market", "the stalls"], "area": "Chang'an", "kingdom": "Tang",
         "used": 1, "era": "tang"}]
    try:
        asyncio.run(area_only._device_release({"era": "tang", "area": "Chang'an"}))
    finally:
        engine_mod.known_scene_places = orig_known
    area_text = "".join(str(m.get("content") or "") for m in area_only.messages)
    rec("a release to an area alone lands in it",
        area_only.era == "tang" and area_only.arrival == "Chang'an", area_only.arrival)
    rec("... the caption keeps the area and the GM chooses the spot",
        isinstance(area_only._current_place, dict)
        and area_only._current_place.get("area") == "Chang'an"
        and area_only._current_place.get("place") == []
        and "You arrive in Chang'an; choose the exact place inside it." in area_text,
        str(area_only._current_place))

    # -- an invented area or place is refused; the age is still required --------
    bad_area = _session(era="egypt")
    bad_area.active_name = "save"
    engine_mod.known_scene_places = lambda *a, **k: [
        {"place": ["the west market", "the stalls"], "area": "Chang'an", "kingdom": "Tang",
         "used": 1, "era": "tang"}]
    try:
        asyncio.run(bad_area._device_release({"era": "tang", "area": "Nowhere"}))
    finally:
        engine_mod.known_scene_places = orig_known
    rec("a release to an invented area is refused", bad_area.era == "egypt", bad_area.era)

    bad = _session(era="egypt")
    bad.active_name = "save"
    engine_mod.known_scene_places = lambda *a, **k: [
        {"place": ["the west market", "the stalls"], "area": "Chang'an", "kingdom": "Tang",
         "used": 1, "era": "tang"}]
    try:
        asyncio.run(bad._device_release(
            {"era": "tang", "area": "Chang'an", "place": "nowhere"}))
    finally:
        engine_mod.known_scene_places = orig_known
    rec("a release to an invented place is refused", bad.era == "egypt", bad.era)
    no_era = _session(era="egypt")
    asyncio.run(no_era._device_release({"area": "Chang'an", "place": "whatever"}))
    rec("a release with no era is refused", no_era.era == "egypt")

    # -- the GM's declaration is the arrival ------------------------------------
    print("\n-- the GM's declaration is the arrival")
    rec("an arrival label matches the visited-pool shape",
        _session()._arrival_label(["The quarry", "the working face"], "Giza")
        == "The quarry, in Giza")
    pin = _session(era="egypt")
    asyncio.run(pin._pin_arrival(["The quarry", "the working face"], "Giza"))
    rec("the declared place becomes the arrival", pin.arrival == "The quarry, in Giza", pin.arrival)
    rec("... and the player DB is told it",
        any(n == "set_player_field" and a.get("key") == "arrival"
            and a.get("value") == "The quarry, in Giza" for n, a in pin.session.calls))
    asyncio.run(pin._pin_arrival(["Somewhere else"], "Elsewhere"))
    rec("... a later declaration moves it (the arrival is where you are)",
        pin.arrival == "Somewhere else, in Elsewhere", pin.arrival)

    # -- a resume connects the arrival to the age's declared place --------------
    print("\n-- a resumed save connects its arrival")
    orig_csp = engine_mod.current_scene_place
    resume = _session(era="tang")
    engine_mod.current_scene_place = lambda *a, **k: {
        "era": "tang", "kingdom": "Tang", "area": "Chang'an",
        "place": ["the west market", "the stalls"]}
    try:
        ok = resume._connect_arrival_to_declared_place()
    finally:
        engine_mod.current_scene_place = orig_csp
    rec("a resumed save connects the arrival to its declared place",
        ok and resume.arrival == "the west market, in Chang'an", resume.arrival)
    engine_mod.current_scene_place = lambda *a, **k: {
        "era": "egypt", "area": "Giza", "place": ["The quarry"]}
    try:
        ok = resume._connect_arrival_to_declared_place()
    finally:
        engine_mod.current_scene_place = orig_csp
    rec("a place from another age is never connected",
        ok is False and resume.arrival == "the west market, in Chang'an", resume.arrival)

    engine_mod.known_scene_places = lambda *a, **k: [
        {"place": ["the west market", "the stalls"], "area": "Chang'an",
         "used": 1, "era": "tang"}]
    try:
        pool = resume._visited_pool("tang")
    finally:
        engine_mod.known_scene_places = orig_known
    rec("the arrival is the same phrase the visited pool lists",
        resume.arrival in pool, str(pool))

    # -- the release is refused in combat ---------------------------------------
    fight = _session(era="egypt")
    fight._in_combat = True
    asyncio.run(fight._device_release({}))
    rec("a release is refused while a fight is live", fight.era == "egypt")

    # -- the place control is base ----------------------------------------------
    print("\n-- the place control is base")
    rec("a chosen place is whitespace-collapsed",
        _session()._clean_place("  the east gate  ") == "the east gate")

    print(f"\n  RESULT: {'PASS' if all(RESULTS) else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
