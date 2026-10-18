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

from web.protocol import DEVICE_DECLARES, ERA_MODEL_AT_JUMP  # noqa: E402
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
    gs.turn_calls = []

    async def _run_turn(content, label, **kwargs):
        gs.turn_calls.append((label, kwargs))
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
    rec("the age it stands in is a destination too (a move within it)",
        gs._era_allowed(gs.era) is True)
    rec("... and the destination menu names it first",
        gs._destination_eras()[0] == gs.era and "tang" in gs._destination_eras(),
        str(gs._destination_eras()[:2]))
    rec("... while the dev menu still lists only the other ages",
        gs.era not in gs._all_other_eras(), str(gs._all_other_eras()))
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
    rec("the menu includes the current age and every reachable other age",
        "egypt" in menu and "future" not in menu and "tang" in menu, str(sorted(menu)))
    rec("... and the current age is marked in the era options",
        [o for o in gs._era_options(gs._destination_eras()) if o["id"] == gs.era][0]["current"]
        is True)
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

    # -- the ward joint: a big city is offered one ward at a time ---------------
    print("\n-- the ward joint")
    wards = ([{"place": ["Ward A", f"Room {i}"], "area": "Chang'an", "kingdom": "Tang",
               "used": 100 - i, "era": "tang"} for i in range(20)]
             + [{"place": ["Ward B", "the inn"], "area": "Chang'an", "kingdom": "Tang",
                 "used": 50, "era": "tang"}])
    engine_mod.known_scene_places = lambda *a, **k: wards
    try:
        gsw = _session(era="egypt")
        gsw.active_name = "save"
        area = gsw._place_options()["tang"]["areas"][0]
        groups = area["groups"]
        first_ok = gsw._place_allowed("tang", "Chang'an", "Ward A — Room 11")
        first_over = gsw._place_allowed("tang", "Chang'an", "Ward A — Room 12")
        ward_ok = gsw._group_allowed("tang", "Chang'an", "Ward B")
        ward_landing = gsw._group_landing("tang", "Chang'an", "Ward B")
        landing_room = gsw._landing_place("tang", "Chang'an", "Ward A — Room 0")
    finally:
        engine_mod.known_scene_places = orig_known
    rec("a crowded area is split into its wards, 12 places each",
        [g["group"] for g in groups] == ["Ward A", "Ward B"]
        and [len(g["places"]) for g in groups] == [12, 1]
        and len(area["places"]) == 13, str(groups)[:160])
    rec("... the 12th place in a ward is selectable and the 13th is not",
        first_ok is True and first_over is False)
    rec("... a ward is a legal destination on its own",
        ward_ok is True and isinstance(ward_landing, dict)
        and ward_landing.get("area") == "Chang'an"
        and ward_landing.get("group") == "Ward B", str(ward_landing))
    rec("... and a place in a ward resolves normally",
        isinstance(landing_room, dict) and landing_room.get("place") == ["Ward A", "Room 0"])

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
    rec("... and the GM is told to build the age's model from it",
        ERA_MODEL_AT_JUMP in solo_text)
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
    landing_block = picked.messages[picked._places_at]["content"]
    rec("... and the primed block follows the landing into its area",
        "the west market" in landing_block and "the stalls · ← you are here" in landing_block,
        landing_block[-200:])
    picked_text = "".join(str(m.get("content") or "") for m in picked.messages)
    rec("... and the GM is told the exact place, not sent to choose one",
        "You arrive at the west market — the stalls, in Chang'an." in picked_text
        and "The Device has put the Traveller exactly here" in picked_text
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
    area_block = area_only.messages[area_only._places_at]["content"]
    rec("... and the block lists that area's places with no place marked yet",
        "the west market" in area_block and "you are here" not in area_block,
        area_block[-200:])

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

    # -- a return: the age's story and its register of people reach the GM ------
    print("\n-- a return re-injects the age's memory and its people")
    back = _session(era="wallachia")
    back._legends = {"tang": "Old Bao saw a stranger vanish by the door.",
                     "wallachia": "Bogdan saw a stranger in the yard and ran."}
    back._met = {"tang": ["the knife-man", "Old Bao", "Kang Sute"]}
    asyncio.run(back._device_release({"era": "tang"}))
    back_text = "".join(str(m.get("content") or "") for m in back.messages)
    rec("the return labels the age you are in and the one you left",
        "THE AGE YOU ARE IN (Tang): Old Bao saw a stranger vanish by the door." in back_text
        and "THE AGE YOU LEFT (Wallachia): Bogdan saw a stranger in the yard and ran."
        in back_text, back_text[-240:])
    rec("... and injects who knows him in the target age",
        "WHO KNOWS HIM IN Tang" in back_text and "Kang Sute" in back_text)
    rec("... story-first, so the age memory's people come first",
        back_text.index("WHO KNOWS HIM IN Tang\nOld Bao") < back_text.index("the knife-man"))
    rec("... with the recognition rule",
        "never present them as strangers" in back_text)

    # -- a release into the current age is a MOVE, not a crossing --------------
    print("\n-- a release into the current age is a move")
    same = _session(era="egypt")
    same.active_name = "save"
    same._met = {"egypt": ["Old Bao"]}
    same.messages.append({"role": "user", "content": "EARLIER-SCENE-MARKER"})
    before_count = len(same.messages)
    engine_mod.known_scene_places = lambda *a, **k: [
        {"place": ["The quarry", "the working face"], "area": "Giza", "kingdom": "Egypt",
         "used": 10, "era": "egypt"}]
    try:
        asyncio.run(same._device_release(
            {"era": "egypt", "area": "Giza", "place": "The quarry — the working face"}))
    finally:
        engine_mod.known_scene_places = orig_known
    same_text = "".join(str(m.get("content") or "") for m in same.messages)
    events = _drain(same)
    rec("a same-age release moves without changing the age",
        same.era == "egypt" and same.arrival == "The quarry, in Giza", same.arrival)
    rec("... the history is kept (no compaction)",
        any(m.get("content") == "EARLIER-SCENE-MARKER" for m in same.messages)
        and len(same.messages) > before_count, f"{before_count} -> {len(same.messages)}")
    rec("... the age's memory is not registered or shown",
        not same._legends and not same._pending_legend
        and "WHO KNOWS HIM IN" not in same_text
        and not any(e.get("title") == "The Age Remembers" for e in events),
        str(same._legends))
    rec("... no era write, no journey append, no spell reset",
        not any(n == "set_player_field" and a.get("key") == "era"
                for n, a in same.session.calls)
        and not any(n == "set_player_field" and a.get("key") == "journey"
                    for n, a in same.session.calls)
        and not any(n == "device_jump_reset" for n, _a in same.session.calls),
        str(same.session.calls))
    rec("... the move is ONE turn, with both departure captures off",
        same.turn_calls == [("jump", {"capture_legend": False, "capture_state": False})],
        str(same.turn_calls))
    landing_notes = [e for e in events if e.get("type") == "place_note"]
    rec("... the chosen landing reaches the manifest before the GM speaks",
        landing_notes and landing_notes[0].get("place") == ["The quarry", "the working face"],
        str(landing_notes[:1]))
    rec("... the GM is told it is the same session and the people react",
        "SITUATION: DEVICE MOVE" in same_text
        and "same session and the same story" in same_text
        and "You arrive at The quarry — the working face, in Giza." in same_text
        and "The Device has put the Traveller exactly here" in same_text, same_text[-300:])

    same_ward = _session(era="egypt")
    same_ward.active_name = "save"
    engine_mod.known_scene_places = lambda *a, **k: [
        {"place": ["Ward A", "the inn"], "area": "Giza", "kingdom": "Egypt",
         "used": 10, "era": "egypt"}]
    try:
        asyncio.run(same_ward._device_release(
            {"era": "egypt", "area": "Giza", "group": "Ward A"}))
    finally:
        engine_mod.known_scene_places = orig_known
    ward_text = "".join(str(m.get("content") or "") for m in same_ward.messages)
    rec("a same-age ward-only release lands in the ward and leaves the spot to the GM",
        same_ward.arrival == "Ward A, in Giza"
        and same_ward._current_place.get("place") == []
        and "You arrive in Ward A, in Giza; choose the exact place inside it." in ward_text,
        f"{same_ward.arrival} / {same_ward._current_place}")

    same_any = _session(era="egypt")
    same_any.active_name = "save"
    asyncio.run(same_any._device_release({"era": "egypt"}))
    any_text = "".join(str(m.get("content") or "") for m in same_any.messages)
    rec("a same-age release with no landing asks the GM to choose one in this age",
        "Choose where the Traveller appears in this age." in any_text
        and same_any._current_place is None, any_text[-200:])

    same_fight = _session(era="egypt")
    same_fight._in_combat = True
    asyncio.run(same_fight._device_release({"era": "egypt"}))
    rec("a same-age release is refused in combat too", same_fight.turn_calls == [])

    # -- a chosen landing is authoritative for the jump turn --------------------
    print("\n-- the chosen landing cannot be replaced")
    landing = {"kingdom": "Egypt", "area": "Giza",
               "place": ["The quarry", "the working face"]}
    known = {"kingdom": "Egypt", "area": "Giza",
             "place": ["The quarry", "the market square"]}
    sibling = ["The quarry", "the shuttered lane"]
    descendant = ["The quarry", "the working face", "the shaft"]

    def _locked_session():
        gs = _session(era="egypt")
        gs.scene_images = False  # note_place is the offered declaration in text mode
        gs._primed = [
            dict(landing, era="egypt", main_npcs=[], dead=[], used=10),
            dict(known, era="egypt", main_npcs=[], dead=[], used=9),
        ]
        gs._current_place = dict(landing, era="egypt")
        gs.arrival = "The quarry, in Giza"
        gs._landing_lock = dict(landing)
        return gs

    blocked = _locked_session()
    asyncio.run(blocked._execute_tool({"function": {"name": "note_place", "arguments": {
        "place": sibling, "kingdom": "Egypt", "area": "Giza", "main_npcs": []}}}))
    sibling_events = [e for e in _drain(blocked) if e.get("type") == "place_note"]
    blocked_text = str(blocked.messages[-1].get("content") or "")
    rec("a new sibling on a locked landing is persisted but does not move the Traveller",
        blocked._current_place.get("place") == landing["place"]
        and blocked._place_blocked_now is True and blocked._landing_blocked is True
        and any(p.get("place") == sibling for p in blocked._primed)
        and any(e.get("place") == sibling for e in sibling_events)
        and "Device landed" in blocked_text, blocked_text[-200:])
    asyncio.run(blocked._unlock_landing())
    final_notes = [e for e in _drain(blocked) if e.get("type") == "place_note"]
    rec("... the landing is restored, and its corrective note is the last write",
        blocked._current_place.get("place") == landing["place"]
        and blocked._landing_lock is None and blocked._landing_blocked is False
        and final_notes and final_notes[-1].get("place") == landing["place"],
        str(final_notes[-1].get("place") if final_notes else None))

    moved = _locked_session()
    moved._record_place_live({"place": known["place"], "kingdom": "Egypt", "area": "Giza"})
    rec("a place the registry already holds is a real move, and it stands",
        moved._current_place.get("place") == known["place"]
        and moved._place_blocked_now is False)

    inside = _locked_session()
    inside._record_place_live({"place": descendant, "kingdom": "Egypt", "area": "Giza"})
    rec("a room inside the landing is a real move, and it stands",
        inside._current_place.get("place") == descendant
        and inside._place_blocked_now is False)

    echo = _locked_session()
    echo_calls = []

    async def _no_echo(*a, **k):
        echo_calls.append(a)

    echo._run_role = _no_echo
    asyncio.run(echo._ensure_place_note())
    rec("a locked landing needs no text-mode place echo", echo_calls == [])

    queued = _session(era="egypt")
    asyncio.run(queued.submit_device("release", "tang", "Chang'an", "", "West Market"))
    queued_cmd = queued._cmd_q.get_nowait()
    rec("the queue/WS path keeps the chosen ward",
        queued_cmd.get("group") == "West Market"
        and queued_cmd.get("era") == "tang" and queued_cmd.get("place") == "",
        str(queued_cmd))

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

    # -- the crossing turn cannot write a place ------------------------------------------
    print("\n-- the crossing turn freezes the registry")
    gs_cross = _session(era="tang")
    gs_cross.scene_images = False
    gs_cross._primed = []
    gs_cross._current_place = None
    gs_cross._crossing = True
    call = {"function": {"name": "note_place", "arguments": {
        "kingdom": "Egypt", "area": "Memphis",
        "place": ["the Giza plateau", "the pyramid works"]}}}
    asyncio.run(gs_cross._execute_tool(call))
    evts = _drain(gs_cross)
    rec("a place declared on the crossing turn is not recorded",
        gs_cross._primed == [] and gs_cross._place_declarations == [],
        str(gs_cross._primed))
    rec("... and no place_note reaches the client",
        not any(e.get("type") == "place_note" for e in evts),
        str([e.get("type") for e in evts]))
    result = next((e for e in evts if e.get("type") == "tool_result"), {})
    rec("... the GM is told why", "not recorded" in str(result.get("gm_text") or ""),
        str(result.get("gm_text")))

    gs_cross._crossing = False
    asyncio.run(gs_cross._execute_tool(call))
    rec("... while an ordinary turn records the same declaration",
        any(p.get("area") == "Memphis" for p in gs_cross._primed), str(gs_cross._primed))

    gs_scene = _session(era="tang")     # images on
    gs_scene._primed = []
    gs_scene._crossing = True
    asyncio.run(gs_scene._execute_tool({"function": {
        "name": "request_scene_image", "arguments": {
            "description": "the world letting go",
            "kingdom": "Egypt", "area": "Memphis",
            "place": ["the Giza plateau", "the pyramid works"],
            "establishing": "a half-built pyramid under a white sun"}}}))
    scene_evts = _drain(gs_scene)
    rec("a scene call on the crossing turn writes no place and sends no image request",
        gs_scene._primed == []
        and not any(e.get("type") in ("place_note", "scene_request") for e in scene_evts),
        str([e.get("type") for e in scene_evts]))

    # -- a place belongs to ONE age: the picker and the landing never cross --------------
    print("\n-- place menus are era-scoped")
    import os
    import shutil

    from web.scenes import SceneService, commit_scene_manifest, discard_scene_manifest

    stem = f"device_ages_{os.getpid()}"
    out = REPO / "output"
    svc = SceneService(output_dir=out)
    try:
        discard_scene_manifest(out, stem)
        svc.record_place(stem, era="tang", kingdom="the Tang empire", area="Chang'an",
                         place=["West Market", "the camel yard"])
        svc.record_place(stem, era="egypt", kingdom="the Old Kingdom", area="Giza",
                         place=["The pyramid works", "the quarry road"])
        commit_scene_manifest(out, stem)
        gs_ages = _session(era="tang")
        gs_ages.active_name = stem
        gs_ages._reload_era_scene_state()
        opts = gs_ages._place_options()
        tang_areas = [a["area"] for a in opts.get("tang", {}).get("areas", [])]
        egypt_areas = [a["area"] for a in opts.get("egypt", {}).get("areas", [])]
        rec("the picker offers each age only its own areas",
            tang_areas == ["Chang'an"] and egypt_areas == ["Giza"],
            f"tang={tang_areas} egypt={egypt_areas}")
        label = "The pyramid works — the quarry road"
        rec("a landing cannot name another age's place",
            gs_ages._landing_place("tang", "Giza", label) is None
            and gs_ages._landing_place("egypt", "Giza", label) is not None)
    finally:
        discard_scene_manifest(out, stem)
        shutil.rmtree(out / "images" / stem, ignore_errors=True)

    print(f"\n  RESULT: {'PASS' if all(RESULTS) else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
