"""The Device's cadence: the countdown, the warning, and the jump.

No LLM / no network: the transitions are driven directly, and the era and the band are
drawn from an injected RNG.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_cadence.py
"""

import asyncio
import json
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

import device as device_mod  # noqa: E402

from web.engine import (  # noqa: E402
    CADENCE_BANDS, CARRY_NOTE, DEVICE_FIRES, DEVICE_WARNING, WARNING_TURNS, GameSession,
    load_legends, load_timeline,
)
from web.eras import era_legend, render_era_index  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


class _FakeMCP:
    """Stands in for the engine's MCP session: records the tools the engine calls."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.events: list[dict] = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))

        class _R:
            def __init__(self, text):
                self.content = [type("C", (), {"text": text})()]

        return _R('{"success": true}')


def _session(seed=7, era="egypt"):
    gs = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs.session = _FakeMCP()
    gs.era = era
    gs.has_device = True
    gs.journey = [era]
    gs._rng = random.Random(seed)
    gs._jump_at_turn = gs.turn_counter + gs._cadence_span()
    # A real save's timeline, so the legend round-trip is the real one.
    fd, gs.timeline_path = tempfile.mkstemp(suffix=".timeline")
    import os

    os.close(fd)
    os.unlink(gs.timeline_path)
    # The prefix a real session builds: protocol, ERA INDEX, the place tree.
    gs.messages = [{"role": "system", "content": "protocol"}]
    gs._era_index_at = len(gs.messages)
    gs.messages.append({"role": "system", "content": render_era_index(gs.era)})
    gs._places_at = len(gs.messages)
    gs.messages.append({"role": "system", "content": "places"})
    gs._primed = []
    gs.arrival = "the Giza quarry, on the Nile"
    _stub_turns(gs)
    return gs


def _drain(gs):
    out = []
    while not gs._evt_q.empty():
        out.append(gs._evt_q.get_nowait())
    return out


def _stub_turns(gs):
    """Replace the GM turn with a bookkeeping stub: this suite is about the cadence, not
    about streaming a model. The stub mirrors `_run_turn` minus the model: the message the
    GM would have received, the turn counter, and `turn_end`."""
    async def _run_turn(content, label):
        gs.messages.append({"role": "user", "content": content})
        gs.turn_counter += 1
        await gs._emit({"type": "turn_end", "text": "", "turn": gs.turn_counter})
    gs._run_turn = _run_turn


async def _player_turn(gs):
    """One player turn, driven the way `_handle_action` drives it."""
    gs.turn_counter += 1
    await gs._check_cadence()
    await gs._emit({"type": "cadence", **gs._cadence_state()})


def main():
    # -- the band table -------------------------------------------------------
    rec("stage 0 (0 of 4) is the fixed worst case, every 5 turns",
        CADENCE_BANDS.get(0) == (5, 5), str(CADENCE_BANDS.get(0)))
    rec("each recovered part widens the interval: 5 / 7 / 11 / 13",
        CADENCE_BANDS == {0: (5, 5), 1: (7, 7), 2: (11, 11), 3: (13, 13)},
        str(sorted(CADENCE_BANDS.items())))
    rec("a complete Device does not fire on its own (band 4 is absent)",
        4 not in CADENCE_BANDS)
    rec("a warning lands on the last turn before the jump, not earlier",
        WARNING_TURNS == 1, str(WARNING_TURNS))

    gs = _session()
    rec("the Device is armed at session start", gs._jump_at_turn == 5, str(gs._jump_at_turn))
    rec("the counter reports the turns left", gs._cadence_state()["turns_until"] == 5,
        str(gs._cadence_state()))
    rec("no warning yet at turn 0", gs._cadence_state()["warning"] is False)

    # -- the countdown, the warning, then the jump ----------------------------
    async def run():
        # Four turns pass: 4, 3, 2, 1 (warn) -- and the Device is still here.
        for _ in range(4):
            await _player_turn(gs)
        warned = any(e.get("type") == "notice" and "tick" in str(e.get("text", "")).lower()
                     for e in _drain(gs))
        return warned

    warned = asyncio.run(run())
    rec("the warning fires once, on the last turn of the age",
        warned and gs._warned and any(m.get("content") == DEVICE_WARNING for m in gs.messages),
        f"turn {gs.turn_counter}, jump at {gs._jump_at_turn}")
    rec("the GM is told before the jump turn, not on it",
        any(m.get("content") == DEVICE_WARNING for m in gs.messages)
        and gs.turn_counter < gs._jump_at_turn)
    rec("the warning is one turn of notice, no more",
        gs._jump_at_turn - gs.turn_counter == WARNING_TURNS,
        f"turn {gs.turn_counter}, jump at {gs._jump_at_turn}")
    rec("the counter is visible while it ticks",
        gs._cadence_state()["turns_until"] == 1 and gs._cadence_state()["warning"] is True,
        str(gs._cadence_state()))

    # -- the jump -------------------------------------------------------------
    before_era = gs.era
    messages_before = len(gs.messages)
    index_at = gs._era_index_at
    places_at = gs._places_at
    # A session's worth of history, so the compaction has something to compact.
    gs.messages.extend({"role": "user", "content": f"a turn that happened ({i})"} for i in range(20))
    rec("the history is in the context before the jump", len(gs.messages) >= 24,
        f"{len(gs.messages)} messages")

    async def jump():
        await _player_turn(gs)

    asyncio.run(jump())
    rec("the jump fires exactly when the counter runs out", gs.era != before_era,
        f"{before_era} -> {gs.era}")
    rec("the new era is a playable one, never the one left behind",
        gs.era in ("egypt", "tang", "wallachia", "victorian") and gs.era != before_era, gs.era)
    rec("the arrival point is rolled again in the new era", bool(gs.arrival), gs.arrival)
    rec("the ERA INDEX is rewritten in place, carrying the new era",
        gs._era_index_at == index_at
        and f"(current: {gs.era})" in gs.messages[index_at]["content"], str(gs.messages[index_at])[:80])

    # -- the compaction: prefix + what the ages remember, and nothing behind -------
    rec("the jump COMPACTS: everything behind the Traveller is gone",
        len(gs.messages) == 5 and not any("a turn that happened" in str(m.get("content") or "")
                                          for m in gs.messages),
        f"{len(gs.messages)} messages, was {messages_before + 20} before the jump")
    rec("the era just left is now one line -- the legend",
        gs.messages[3]["content"].startswith("WHAT THE AGES REMEMBER")
        and before_era.title().split()[0] in gs.messages[3]["content"],
        gs.messages[3]["content"])
    rec("the legend is written to the save, permanently",
        load_legends(gs.timeline_path).get(before_era, "").startswith("Egypt remembers"),
        str(load_legends(gs.timeline_path)))
    rec("the legend is not a session event -- the timeline read back has no markers",
        "legend:" not in load_timeline(gs.timeline_path))
    rec("the Device is rearmed so the player gets the same 5 turns between jumps",
        gs._jump_at_turn == gs.turn_counter + 5 and gs._warned is False,
        f"next jump at {gs._jump_at_turn}, now turn {gs.turn_counter}")
    rec("the GM is told to fetch the new era, not handed it",
        any(DEVICE_FIRES.split("{")[0].strip() in str(m.get("content") or "")
            for m in gs.messages)
        and any('lookup("here")' in str(m.get("content") or "") for m in gs.messages))

    # -- the engine DB is told, or reputation lands in the wrong era ---------
    keys = [a.get("key") for n, a in gs.session.calls if n == "set_player_field"]
    rec("the engine tells the DB the new era and arrival",
        keys == ["era", "arrival", "journey"], str(gs.session.calls))
    rec("the DB is told the era the session is actually in",
        [a.get("value") for n, a in gs.session.calls if n == "set_player_field" and a.get("key") == "era"]
        == [gs.era], str(gs.session.calls))

    # -- the whole kit carries, and it stays literal ---------------------------
    mutating = {"update_player_list", "modify_player_numeric", "equip_item",
                "attune_item", "rest", "set_player_field"}
    tools_called = [n for n, _a in gs.session.calls if n in mutating]
    rec("a jump cannot touch the inventory: it calls nothing that mutates the player",
        tools_called == ["set_player_field", "set_player_field", "set_player_field"]
        and [a.get("key") for n, a in gs.session.calls if n == "set_player_field"]
        == ["era", "arrival", "journey"],
        str(gs.session.calls))
    rec("the jump says the kit is untouched and literal",
        "nothing is translated" in DEVICE_FIRES and "nothing is left behind" in DEVICE_FIRES)
    rec("the gear rule is stated with a new era, and never re-skinned",
        "seen its like" in CARRY_NOTE and "consumable" in CARRY_NOTE
        and "seen its like" in gs._era_opening(),
        gs._era_opening())

    # -- the counter the player sees -----------------------------------------
    evts = _drain(gs)
    rec("the client gets a cadence event", any(e.get("type") == "cadence" for e in evts),
        str([e.get("type") for e in evts]))
    rec("the player is told what the age now remembers",
        any(e.get("type") == "notice" and e.get("title") == "The Age Remembers" for e in evts),
        str([e.get("title") for e in evts if e.get("type") == "notice"]))
    cad = [e for e in evts if e.get("type") == "cadence"]
    rec("the cadence event carries the countdown after the jump",
        bool(cad) and cad[-1].get("turns_until") == 5, str(cad[-1:] if cad else []))

    # -- a Device with no band never fires -----------------------------------
    parked = _session(era="egypt")
    parked.parts_recovered = 4  # nothing is decided for the repaired Device yet
    parked._jump_at_turn = None
    rec("an undecided stage means no automatic jump",
        parked._cadence_span() is None
        and parked._cadence_state() == {"parts": 4, "turns_until": None, "warning": False},
        str(parked._cadence_state()))

    # -- a turn spent fighting is not a turn the Device counts (D34) ----------
    # `COMBAT_TOOLS` running during a turn sets the flag; the turn is consumed at the end
    # of it, and the jump is pushed one turn further out instead of ticking.
    fight = _session()

    async def fought():
        fight._battle_turn = True  # as if resolve_attack had run this turn
        fight.turn_counter += 1
        await fight._check_cadence()
        held = fight._cadence_state()["turns_until"]
        fight.turn_counter += 1  # the next turn is an ordinary one
        await fight._check_cadence()
        return held, fight._cadence_state()["turns_until"]

    held, after = asyncio.run(fought())
    rec("a battle turn does not tick the Device -- the counter holds", held == 5, str(held))
    rec("... and the next ordinary turn ticks it again", after == 4, str(after))
    rec("the flag is one turn's worth and is cleared", fight._battle_turn is False)

    due = _session()

    async def due_during_a_fight():
        due._jump_at_turn = due.turn_counter + 1  # the jump is due on this very turn
        due._battle_turn = True
        due.turn_counter += 1
        await due._check_cadence()
        return due.era

    era_before = due.era
    rec("the jump cannot fire during a fight",
        asyncio.run(due_during_a_fight()) == era_before and due.era == era_before)

    # -- a fight on the warned turn must not leave the warning stale ---------
    # The jump moves out of the fight, which makes the NEXT turn the jump turn -- so the
    # warning has to be re-issued on the fight's own turn, or the GM closes an age that then
    # keeps going. The re-warning replaces the old claim rather than piling up.
    rearm = _session()

    async def warned_then_a_fight():
        for _ in range(4):
            await _player_turn(rearm)          # turn 4 ends at remaining == 1: warned
        era0 = rearm.era
        rearm._battle_turn = True              # as if resolve_attack had run this turn
        await _player_turn(rearm)              # turn 5: a fight defers the jump
        at_rest = {"jump": rearm._jump_at_turn, "turn": rearm.turn_counter,
                   "warned": rearm._warned, "era": rearm.era,
                   "warnings": sum(1 for m in rearm.messages
                                   if isinstance(m, dict)
                                   and m.get("content") == DEVICE_WARNING)}
        await _player_turn(rearm)              # turn 6: the jump
        return era0, at_rest, rearm.era

    era0, at_rest, era_after = asyncio.run(warned_then_a_fight())
    rec("a fight on the warned turn defers the jump and does not fire it",
        at_rest["era"] == era0, str(at_rest))
    rec("... and the warning is re-issued for the turn that now precedes the jump",
        at_rest["warned"] is True and at_rest["jump"] - at_rest["turn"] == 1,
        str(at_rest))
    rec("... replacing the stale one, so the GM holds exactly one claim",
        at_rest["warnings"] == 1, str(at_rest))
    rec("... and the jump still lands on the next ordinary turn",
        era_after != era0, f"{era0} -> {era_after}")

    # -- the control ladder: each part unlocks an ability (and widens the interval) --
    # 0 parts: nothing to steer, the worst interval.
    zero = _session()
    zero.parts_recovered = 0
    zero._jump_at_turn = zero.turn_counter + zero._cadence_span()
    st0 = zero._device_state()
    rec("0 parts: no ability at all", not any(st0["abilities"].values()), str(st0["abilities"]))
    rec("0 parts: the interval is the worst case, 5", st0["turns_until"] == 5, str(st0["turns_until"]))

    # 1 part: adjust the count by two, either way, once per jump; -2 only at 3+ turns.
    one = _session()
    one.parts_recovered = 1
    rec("1 part: the interval widens to 7", one._cadence_span() == 7, str(one._cadence_span()))
    one._jump_at_turn = one.turn_counter + one._cadence_span()
    before = one._jump_at_turn
    asyncio.run(one._device_adjust(2))
    rec("1 part: wait +2 pushes the jump out and marks the ability used",
        one._jump_at_turn == before + 2 and one._adjusted_jump, str(one._jump_at_turn))
    one._jump_at_turn = before
    asyncio.run(one._device_adjust(2))
    rec("... and it cannot be used a second time in the same jump",
        one._jump_at_turn == before, str(one._jump_at_turn))

    # -2 (hasten): only while three or more turns remain, so the jump always keeps a turn.
    hasten = _session()
    hasten.parts_recovered = 1
    hasten._jump_at_turn = hasten.turn_counter + 3
    asyncio.run(hasten._device_adjust(-2))
    rec("1 part: hasten -2 pulls the jump in from three turns",
        hasten._jump_at_turn == hasten.turn_counter + 1, str(hasten._jump_at_turn))
    rec("... and raises the warning, because one turn now remains",
        hasten._warned and any(m.get("content") == DEVICE_WARNING for m in hasten.messages),
        str([m.get("content") for m in hasten.messages if isinstance(m, dict)]))
    blocked = _session()
    blocked.parts_recovered = 1
    blocked._jump_at_turn = blocked.turn_counter + 2
    asyncio.run(blocked._device_adjust(-2))
    rec("1 part: hasten -2 is refused at two turns -- too much control",
        blocked._jump_at_turn == blocked.turn_counter + 2 and not blocked._adjusted_jump)
    blocked1 = _session()
    blocked1.parts_recovered = 1
    blocked1._jump_at_turn = blocked1.turn_counter + 1
    asyncio.run(blocked1._device_adjust(-2))
    rec("... and at one turn", blocked1._jump_at_turn == blocked1.turn_counter + 1,
        str(blocked1._jump_at_turn))

    # Wait and hasten share the one adjustment per jump.
    shared = _session()
    shared.parts_recovered = 1
    shared._jump_at_turn = shared.turn_counter + 5
    asyncio.run(shared._device_adjust(-2))
    after_hasten = shared._jump_at_turn
    asyncio.run(shared._device_adjust(2))
    rec("wait and hasten are the same once-per-jump ability",
        shared._jump_at_turn == after_hasten, str(shared._jump_at_turn))

    # The state the panel reads: wait always offered; hasten only at 3+ turns.
    panel = _session(era="tang")
    panel.parts_recovered = 1
    panel._jump_at_turn = panel.turn_counter + 5
    rec("the 1-part state offers both directions at five turns",
        panel._device_state()["adjust"]["can_wait"]
        and panel._device_state()["adjust"]["can_hasten"],
        str(panel._device_state()["adjust"]))
    panel._jump_at_turn = panel.turn_counter + 2
    rec("... and only waiting at two",
        panel._device_state()["adjust"]["can_wait"]
        and not panel._device_state()["adjust"]["can_hasten"],
        str(panel._device_state()["adjust"]))

    # 2 parts: direction -- the last era visited, or a random era forward.
    two = _session(era="tang")
    two.parts_recovered = 2
    two.journey = ["egypt", "tang"]
    two._jump_at_turn = two.turn_counter + two._cadence_span()
    rec("2 parts: 'previous' is the last era visited, not the order",
        two._previous_era() == "egypt", str(two._previous_era()))
    asyncio.run(two._device_travel({"direction": "previous"}))
    rec("2 parts: the Traveller can go back to it", two.era == "egypt", two.era)
    two2 = _session(era="tang")
    two2.parts_recovered = 2
    forward = two2._forward_era_ids()
    rec("2 parts: 'forward' is the ages ahead, wrapping at the last",
        forward == ["wallachia", "victorian", "egypt"], str(forward))

    # 3 parts: choose which age forward.
    three = _session(era="tang")
    three.parts_recovered = 3
    asyncio.run(three._device_travel({"era": "wallachia"}))
    rec("3 parts: the Traveller picks a forward age", three.era == "wallachia", three.era)
    bad = _session(era="tang")
    bad.parts_recovered = 3
    asyncio.run(bad._device_travel({"era": "future"}))
    rec("... and a non-playable age (the future frame) is refused", bad.era == "tang", bad.era)
    wrap = _session(era="victorian")
    wrap.parts_recovered = 3
    rec("... and 'forward' wraps at the last age", wrap._forward_era_ids()[0] == "egypt",
        str(wrap._forward_era_ids()))

    # 4 parts: manual -- any age, and the player triggers the jump.
    four = _session(era="tang")
    four.parts_recovered = 4
    rec("4 parts: the Device no longer fires on its own", four._cadence_span() is None)
    rec("4 parts: the panel says it is the player's call",
        four._device_state()["manual"] and all(four._device_state()["abilities"].values()),
        str(four._device_state()["abilities"]))
    asyncio.run(four._device_travel({"era": "egypt"}))
    rec("4 parts: the Traveller can go anywhere, including back", four.era == "egypt", four.era)
    rec("... and the journey records where they have stood",
        four.journey == ["tang", "egypt"], str(four.journey))

    # A Traveller whose Device has been taken has no cadence at all.
    taken0 = _session()
    taken0.has_device = False
    rec("no Device in the pack means no cadence", taken0._cadence_span() is None)

    # The count is derived from the live items (the inventory is the source of truth).
    async def fake_read(name, args):
        if name == "dump_player_db":
            return json.dumps({
                "inventory": [device_mod.device_entry(),
                              device_mod.part_entry("Escapement", "egypt")],
                "journey": "egypt"})
        return '{"success": true}'

    read = _session()
    read._call_tool_text = fake_read
    asyncio.run(read._refresh_device())
    rec("the engine derives the parts from the inventory items",
        read.parts_recovered == 1 and read.has_device and read.journey == ["egypt"],
        f"parts={read.parts_recovered} has_device={read.has_device}")

    armed = _session()
    armed._call_tool_text = fake_read
    armed._jump_at_turn = None
    armed._adjusted_jump = True
    asyncio.run(armed._sync_device_after_inventory('{"key": "inventory"}'))
    rec("recovering a part arms the wider band (7)",
        armed._jump_at_turn == armed.turn_counter + 7, str(armed._jump_at_turn))

    async def fake_four(name, args):
        if name == "dump_player_db":
            return json.dumps({
                "inventory": [device_mod.device_entry()]
                + [device_mod.part_entry(p, "egypt") for p in device_mod.part_names()],
                "journey": "egypt"})
        return '{"success": true}'

    manual = _session()
    manual._call_tool_text = fake_four
    manual._jump_at_turn = 99
    asyncio.run(manual._sync_device_after_inventory('{"key": "inventory"}'))
    rec("reaching 4 of 4 cancels the countdown -- the Traveller decides when",
        manual._jump_at_turn is None and manual.parts_recovered == 4, str(manual._jump_at_turn))

    gone = _session()
    async def fake_gone(name, args):
        if name == "dump_player_db":
            return json.dumps({"inventory": [], "journey": "egypt"})
        return '{"success": true}'
    gone._call_tool_text = fake_gone
    gone._jump_at_turn = 99
    asyncio.run(gone._sync_device_after_inventory('{"key": "inventory"}'))
    rec("taking the Device cancels the countdown",
        gone._jump_at_turn is None and not gone.has_device)

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
