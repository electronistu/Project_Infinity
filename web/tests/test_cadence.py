"""The Device's cadence: the countdown, the warning, and the jump.

No LLM / no network: the transitions are driven directly, and the era and the band are
drawn from an injected RNG.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_cadence.py
"""

import asyncio
import json
import os
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

import device as device_mod  # noqa: E402

from web.engine import (  # noqa: E402
    CADENCE_BANDS, CARRY_NOTE, DEVICE_FIRES, DEVICE_WARNING, LEGEND_ASK, WARNING_TURNS,
    GameSession, load_legends, load_timeline,
)
from web.eras import era_arrivals, render_era_index, render_era_text  # noqa: E402

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


def _with_parts(gs, *parts):
    """Give a session a set of recovered parts, and derive the powers as _refresh_device does."""
    held = set(parts)
    gs.parts_recovered = len(parts)
    gs.device_powers = {ab: any(p in held for p, a in device_mod.POWERS.items() if a == ab)
                        for ab in set(device_mod.POWERS.values())}
    return gs


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
    gs._pending_legend = ""
    _stub_turns(gs)

    async def _no_legend():
        return None

    # The memory line is the GM's; tests that want one set `_pending_legend` directly.
    gs._ensure_legend = _no_legend
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
    rec("each recovered part widens the interval: 5 / 7 / 11 / 13 / 17",
        CADENCE_BANDS == {0: (5, 5), 1: (7, 7), 2: (11, 11), 3: (13, 13), 4: (17, 17)},
        str(sorted(CADENCE_BANDS.items())))
    rec("... and the Mainspring -- not the count -- is what stops the forced jumps",
        _with_parts(_session(), "Mainspring")._cadence_span() is None
        and _with_parts(_session(), "Escapement")._cadence_span() is not None)
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
    # The memory line is the GM's (there is no engine template): give it one for the jump.
    gs._pending_legend = "Egypt remembers a stranger who crossed it and vanished."

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
    rec("the legend is held in memory (it reaches disk only on Save)",
        gs._legends.get(before_era, "").startswith("Egypt remembers")
        and load_legends(gs.timeline_path) == {},
        str(gs._legends))
    rec("the legend is not a session event -- the timeline read back has no markers",
        "legend:" not in load_timeline(gs.timeline_path))
    rec("the Device is rearmed so the player gets the same 5 turns between jumps",
        gs._jump_at_turn == gs.turn_counter + 5 and gs._warned is False,
        f"next jump at {gs._jump_at_turn}, now turn {gs.turn_counter}")
    jump_msgs = [str(m.get("content") or "") for m in gs.messages
                 if DEVICE_FIRES.split("{")[0].strip() in str(m.get("content") or "")]
    rec("the jump hands the new era's scaffold, with no lookup step",
        bool(jump_msgs) and any(render_era_text(gs.era) in t for t in jump_msgs)
        and all('lookup("here")' not in t for t in jump_msgs),
        str(jump_msgs)[:120])

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
    rec("a jump leaves it to the GM whether anyone notices the arrival",
        "whether anyone notices a stranger appear" in DEVICE_FIRES
        and "who notices a stranger appear" not in DEVICE_FIRES)
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

    # -- a four-part Device without the Mainspring still fires (the widest band) ---------
    parked = _session(era="egypt")
    _with_parts(parked, "Escapement", "Compass Rose", "Regulator", "Vernier")
    rec("four parts with no Mainspring still fire -- the widest band, 17",
        parked._cadence_span() == 17, str(parked._cadence_span()))

    # -- a turn spent fighting is not a turn the Device counts (D34) ----------
    # `_in_combat` is sticky (derived from the combat registry, not a single turn), so the
    # counter holds for the whole fight -- including turns that only narrate -- and ticks
    # again on the first ordinary turn after it ends.
    fight = _session()

    async def fought():
        fight._in_combat = True                # a hostile is registered
        fight.turn_counter += 1
        await fight._check_cadence()
        held = fight._cadence_state()["turns_until"]
        fight.turn_counter += 1                # a second fight turn (no combat tool needed)
        await fight._check_cadence()
        held_again = fight._cadence_state()["turns_until"]
        fight._in_combat = False               # the last hostile is down
        fight.turn_counter += 1                # the first ordinary turn
        await fight._check_cadence()
        return held, held_again, fight._cadence_state()["turns_until"]

    held, held_again, after = asyncio.run(fought())
    rec("a battle turn does not tick the Device -- the counter holds", held == 5, str(held))
    rec("... and the flag is sticky, so a second fight turn holds too",
        held_again == 5, str(held_again))
    rec("... and the first ordinary turn after the fight ticks it again",
        after == 4, str(after))

    due = _session()

    async def due_during_a_fight():
        due._jump_at_turn = due.turn_counter + 1  # the jump is due on this very turn
        due._in_combat = True
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
        rearm._in_combat = True                # a fight starts on the warned turn
        await _player_turn(rearm)              # turn 5: the fight defers the jump
        at_rest = {"jump": rearm._jump_at_turn, "turn": rearm.turn_counter,
                   "warned": rearm._warned, "era": rearm.era,
                   "warnings": sum(1 for m in rearm.messages
                                   if isinstance(m, dict)
                                   and m.get("content") == DEVICE_WARNING)}
        rearm._in_combat = False               # the fight is over
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

    # -- one part = ONE power: the SET of recovered parts, not the count, grants abilities --
    zero = _session()
    zero.parts_recovered = 0
    zero._jump_at_turn = zero.turn_counter + zero._cadence_span()
    st0 = zero._device_state()
    rec("0 parts: no ability at all", not any(st0["abilities"].values()), str(st0["abilities"]))
    rec("0 parts: the interval is the worst case, 5", st0["turns_until"] == 5, str(st0["turns_until"]))
    rec("the cadence widens with the count, 5/7/11/13/17",
        [CADENCE_BANDS.get(n) for n in range(5)]
        == [(5, 5), (7, 7), (11, 11), (13, 13), (17, 17)], str(CADENCE_BANDS))

    # The Escapement releases; with nothing wound (no Mainspring) there is nothing to let out.
    esc = _with_parts(_session(), "Escapement")
    rec("Escapement alone: the release is NOT available (nothing is wound)",
        esc._device_state()["abilities"]["release"] is False,
        str(esc._device_state()["abilities"]))

    # The Mainspring is the charge: it stops the forced jumps at ANY count.
    main = _with_parts(_session(), "Mainspring")
    rec("Mainspring: the charge holds the Device (no forced jump) at one part",
        main._device_state()["abilities"]["charge"] is True
        and main._device_state()["holding"] is True and main._cadence_span() is None,
        str(main._device_state()))
    rec("... but alone it cannot release (no Escapement)",
        main._device_state()["abilities"]["release"] is False)
    rec("... and it holds whatever else is held",
        _with_parts(_session(), "Mainspring", "Vernier")._cadence_span() is None)

    # The Mainspring can be let go, so a partial Device is never stranded.
    hold = _with_parts(_session(), "Mainspring")
    hold._jump_at_turn = None
    asyncio.run(hold._device_hold(False))
    rec("the Mainspring can be let go -- the forced jumps resume",
        hold._hold is False and hold._cadence_span() is not None, str(hold._hold))
    asyncio.run(hold._device_hold(True))
    rec("... and held again -- the forced jumps stop",
        hold._hold is True and hold._cadence_span() is None, str(hold._hold))
    rec("the charge is off without the Mainspring",
        _with_parts(_session(), "Escapement")._device_state()["abilities"]["charge"] is False
        and _with_parts(_session(), "Escapement")._device_state()["holding"] is False)
    holdw = _with_parts(_session(), "Mainspring")
    asyncio.run(holdw.submit_device("hold", value=False))
    asyncio.run(holdw._handle_device_command(holdw._cmd_q.get_nowait()))
    rec("let-it-run over the wire resumes the countdown",
        holdw._hold is False and holdw._cadence_span() is not None, str(holdw._hold))

    # Mainspring + Escapement: the charge AND the release -- travel at will.
    wound = _with_parts(_session(era="tang"), "Mainspring", "Escapement")
    rec("Mainspring + Escapement: the release is available",
        wound._device_state()["abilities"]["release"] is True)

    # The Compass Rose: a coarse direction (an aim, never a jump).
    rose = _with_parts(_session(era="tang"), "Compass Rose")
    rose.journey = ["egypt", "tang"]
    asyncio.run(rose._device_steer("previous"))
    rec("Compass Rose: steers 'previous' to the last age visited",
        rose._steer == "previous" and rose._steered_era() == "egypt",
        f"{rose._steer} -> {rose._steered_era()}")
    rec("Compass Rose: 'forward' is strictly ahead (no wrap)",
        rose._forward_era_ids() == ["wallachia", "victorian", "future"],
        str(rose._forward_era_ids()))
    last = _with_parts(_session(era="victorian"), "Compass Rose")
    last.journey = ["egypt", "victorian"]
    asyncio.run(last._device_steer("forward"))
    rec("... and the last age still has one ahead: New York",
        last._steer == "forward" and last._steered_era() == "future",
        f"{last._steer} -> {last._steered_era()}")
    asyncio.run(rose._device_steer("previous"))
    rec("... and the active direction toggles off", rose._steer == "", rose._steer)

    # The Regulator names an age ahead -- it aims, it never fires.
    reg = _with_parts(_session(era="tang"), "Regulator")
    asyncio.run(reg._device_aim({"era": "wallachia"}))
    rec("Regulator: aims an age ahead without firing",
        reg._steer == "wallachia" and reg.era == "tang", f"{reg._steer} / {reg.era}")
    rec("... and the next jump takes the aimed age", reg._steered_era() == "wallachia")
    behind = _with_parts(_session(era="tang"), "Regulator")
    asyncio.run(behind._device_aim({"era": "egypt"}))
    rec("Regulator: names ANY other age -- behind as well as ahead",
        behind._steer == "egypt", behind._steer)
    newyork = _with_parts(_session(era="victorian"), "Regulator")
    asyncio.run(newyork._device_aim({"era": "future"}))
    rec("... and the last age still names any other (the menu is never empty)",
        newyork._steer == "future"
        and [e["id"] for e in newyork._device_state()["era_options"]]
        == ["egypt", "tang", "wallachia", "future"],
        str([e["id"] for e in newyork._device_state()["era_options"]]))
    from_last = _with_parts(_session(era="future"), "Regulator")
    asyncio.run(from_last._device_aim({"era": "egypt"}))
    rec("... from the far future, Egypt can be named",
        from_last._steer == "egypt", from_last._steer)

    # The Vernier: the exact place, from the places already visited.
    ven = _with_parts(_session(era="tang"), "Vernier")
    rec("Vernier: offers the place ability",
        ven._device_state()["abilities"]["place"] is True)
    rec("... and without the Regulator there is no age to attach it to",
        ven._device_state()["abilities"]["era"] is False)

    # Full control -- era + place + when -- needs all five; no proper subset has it.
    def _full(powers):
        return {"charge", "release", "direction", "era", "place"} <= {k for k, v in powers.items() if v}

    rec("only all five parts give full control",
        _full(device_mod.powers([device_mod.part_entry(p, "egypt")
                                 for p in device_mod.part_names()]))
        and not any(_full(device_mod.powers(
            [device_mod.part_entry(p, "egypt") for p in device_mod.part_names() if p != skip]))
            for skip in device_mod.part_names()),
        "every proper subset falls short")

    # No dead end: the Regulator's menu is never empty, in any age.
    empty_menus = [era for era in ("egypt", "tang", "wallachia", "victorian", "future")
                   if not _with_parts(_session(era=era), "Regulator")._device_state()["era_options"]]
    rec("the Regulator's age menu is never empty, in any age", not empty_menus, str(empty_menus))
    rec("the Mainspring's charge is offered in any age",
        all(_with_parts(_session(era=era), "Mainspring")._device_state()["abilities"]["charge"]
            for era in ("egypt", "future")))

    # A release is a jump: refused mid-fight. Aiming is not a jump, so it is allowed.
    melee = _with_parts(_session(era="tang"), "Mainspring", "Escapement", "Compass Rose")
    melee._in_combat = True
    asyncio.run(melee._device_release({}))
    rec("a release is refused mid-fight", melee.era == "tang", melee.era)
    asyncio.run(melee._device_steer("forward"))
    rec("... while aiming is allowed (the jump it aims waits out the fight)",
        melee._steer == "forward", melee._steer)

    # The wire: aim / release reach the engine through submit_device.
    wired = _with_parts(_session(era="tang"), "Regulator", "Mainspring", "Escapement")
    asyncio.run(wired.submit_device("aim", era="wallachia"))
    asyncio.run(wired._handle_device_command(wired._cmd_q.get_nowait()))
    rec("an aim over the wire names the age", wired._steer == "wallachia", wired._steer)
    asyncio.run(wired.submit_device("release"))
    cmd = wired._cmd_q.get_nowait()
    rec("submit_device carries a release through the command queue",
        cmd.get("action") == "release", str(cmd))
    asyncio.run(wired._handle_device_command(cmd))
    rec("a release over the wire jumps to the aimed age", wired.era == "wallachia", wired.era)

    # The Compass Rose's steer over the wire.
    wired_steer = _with_parts(_session(era="tang"), "Compass Rose")
    wired_steer.journey = ["egypt", "tang"]
    asyncio.run(wired_steer.submit_device("steer", direction="previous"))
    asyncio.run(wired_steer._handle_device_command(wired_steer._cmd_q.get_nowait()))
    rec("a steer over the wire sets the direction", wired_steer._steer == "previous",
        wired_steer._steer)

    # A Traveller whose Device has been taken has no cadence at all.
    taken0 = _session()
    taken0.has_device = False
    rec("no Device in the pack means no cadence", taken0._cadence_span() is None)

    # The SET and the powers are derived from the live items.
    async def fake_read(name, args):
        if name == "dump_player_db":
            return json.dumps({
                "inventory": [device_mod.device_entry(),
                              device_mod.part_entry("Mainspring", "wallachia")],
                "journey": "egypt"})
        return '{"success": true}'

    read = _session()
    read._call_tool_text = fake_read
    asyncio.run(read._refresh_device())
    rec("the engine derives the parts and their powers from the items",
        read.parts_recovered == 1 and read.has_device
        and read.device_powers.get("charge") is True
        and read.device_powers.get("era") is False and read.journey == ["egypt"],
        f"parts={read.parts_recovered} powers={read.device_powers}")

    armed = _session()
    armed._call_tool_text = fake_read
    armed._jump_at_turn = None
    asyncio.run(armed._sync_device_after_inventory('{"key": "inventory"}'))
    rec("recovering the Mainspring cancels the countdown (the charge holds it)",
        armed._jump_at_turn is None, str(armed._jump_at_turn))

    async def fake_all(name, args):
        if name == "dump_player_db":
            return json.dumps({
                "inventory": [device_mod.device_entry()]
                + [device_mod.part_entry(p, "egypt") for p in device_mod.part_names()],
                "journey": "egypt"})
        return '{"success": true}'

    five = _session()
    five._call_tool_text = fake_all
    five._jump_at_turn = 99
    asyncio.run(five._sync_device_after_inventory('{"key": "inventory"}'))
    rec("all five parts: the countdown is cancelled and every ability is on",
        five._jump_at_turn is None and five.parts_recovered == 5
        and all(five._device_state()["abilities"].values()),
        str(five._device_state()["abilities"]))

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

    # -- a jump lands in a place already visited in the target era --------------
    base = Path(tempfile.mkdtemp())
    stem = "trav"
    scenes = base / "output" / "images" / stem / "scenes"
    scenes.mkdir(parents=True)
    (scenes / "manifest.json").write_text(json.dumps({
        "version": 8,
        "seeds": {
            "egypt|wharf": {"era": "egypt", "kingdom": "Egypt", "area": "Memphis",
                            "place": ["Ropehaven Wharf", "Warehouse Nine"],
                            "used": 1, "created": 1},
            "egypt|stone": {"era": "egypt", "kingdom": "Egypt", "area": "Thebes",
                            "place": ["Stone Row"], "used": 2, "created": 1},
            "tang|market": {"era": "tang", "kingdom": "Tang", "area": "Chang'an",
                            "place": ["West Market"], "used": 3, "created": 1},
        },
    }), encoding="utf-8")

    landed = GameSession(base_dir=base, model="test")
    landed.active_name = stem
    landed.has_device = True
    landed._rng = random.Random(1)
    visited = {"Ropehaven Wharf, in Memphis", "Stone Row, in Thebes"}
    static = set(era_arrivals("egypt"))
    got = {landed._pick_jump_arrival("egypt") for _ in range(80)}
    rec("a jump may land in a visited place or one of the era's destinations",
        bool(got) and got <= (visited | static), str(sorted(got)))
    rec("... both sources are in play",
        bool(got & visited) and bool(got & static), str(sorted(got)))
    rec("... a visited place names the district, not the deepest room",
        "Warehouse Nine" not in " ".join(got), str(got))
    rec("... and never a place from another era",
        "West Market, in Chang'an" not in got, str(got))
    rec("an era with no visited places draws from its static destinations",
        landed._pick_jump_arrival("wallachia") in era_arrivals("wallachia"),
        landed._pick_jump_arrival("wallachia"))

    # The Vernier: the exact place, honoured only when the save has been there.
    _with_parts(landed, "Vernier", "Regulator")
    rec("the Vernier lands on the exact place chosen, once it has been visited",
        landed._chosen_arrival("egypt", "Ropehaven Wharf, in Memphis")
        == "Ropehaven Wharf, in Memphis")
    rec("... but never a place the save has not stood in",
        landed._chosen_arrival("egypt", "the sea of tranquillity") != "the sea of tranquillity")
    landed._aim_place = "Stone Row, in Thebes"
    rec("... and the aimed place rides the next jump",
        landed._chosen_arrival("egypt") == "Stone Row, in Thebes")
    landed._aim_place = ""

    # The Vernier's menu (the panel's place list): visited places grouped by age.
    places_map = landed._device_state()["places"]
    rec("the Vernier's places map groups visited places by age",
        places_map.get("egypt") == ["Ropehaven Wharf, in Memphis", "Stone Row, in Thebes"]
        and places_map.get("tang") == ["West Market, in Chang'an"]
        and places_map.get("wallachia") == [], str(places_map))
    rec("... and no places map without the Vernier",
        _with_parts(_session(era="egypt"), "Regulator")._device_state()["places"] == {})

    # The Vernier on its own names an age through a place the save has stood in.
    landed.era = "tang"
    only_ven = _with_parts(landed, "Vernier")
    asyncio.run(only_ven._device_aim({"era": "egypt", "place": "Ropehaven Wharf, in Memphis"}))
    rec("the Vernier alone names an age through a place the save has stood in",
        only_ven._steer == "egypt" and only_ven._aim_place == "Ropehaven Wharf, in Memphis",
        f"{only_ven._steer} / {only_ven._aim_place}")
    only_ven2 = _with_parts(landed, "Vernier")
    only_ven2._steer = ""
    asyncio.run(only_ven2._device_aim({"era": "egypt", "place": "the sea of tranquillity"}))
    rec("... but not through a place it has never visited",
        only_ven2._steer == "", only_ven2._steer)

    # A reload must keep a visited-place arrival (it is not in the static list).
    from web.engine import _player_arrival  # noqa: E402
    player_path = base / "output" / (stem + ".player")
    player_path.write_text(json.dumps({"arrival": "Ropehaven Wharf, in Memphis"}),
                           encoding="utf-8")
    rec("a reload honours a visited-place arrival",
        _player_arrival(str(player_path), "egypt") == "Ropehaven Wharf, in Memphis")
    player_path.write_text(json.dumps({"arrival": "somewhere else entirely"}),
                           encoding="utf-8")
    rec("... while an arrival that is not a known place is re-rolled",
        _player_arrival(str(player_path), "egypt") in era_arrivals("egypt"))

    # -- the legend is the GM's; the template is only a fallback -----------------
    from web.engine import _extract_legend  # noqa: E402

    got, cleaned = _extract_legend(
        "The age closes. {{_REMEMBERS: London remembers a person who was not there the next morning.}}")
    rec("the memory marker is pulled out and the prose is cleaned",
        got == "London remembers a person who was not there the next morning."
        and "REMEMBERS" not in cleaned and cleaned.startswith("The age closes."),
        f"{got!r} / {cleaned!r}")
    rec("an echoed placeholder is rejected",
        _extract_legend("{{_REMEMBERS: <one sentence -- what this age will remember>}}")[0] == "")

    # -- the legend is scoped to what the age can actually know -----------------
    from web.engine import (  # noqa: E402
        DEVICE_CLOSES, LEGEND_RECOVERY_NUDGE, MAX_LEGEND_CHARS, MAX_LEGEND_EXTRACT,
        _MEMORY_SCOPE, _trim_legend,
    )
    from web.eras import load_era  # noqa: E402

    rec("the memory ask is scoped: seen happen, or left behind -- not a face without a witness",
        "left behind" in _MEMORY_SCOPE and "witness lived to speak of it" in _MEMORY_SCOPE)
    rec("... and all three asks carry that scope",
        _MEMORY_SCOPE in LEGEND_ASK and _MEMORY_SCOPE in DEVICE_CLOSES
        and _MEMORY_SCOPE in LEGEND_RECOVERY_NUDGE)
    rec("... and the marker is still well-formed after DEVICE_CLOSES is formatted",
        "{{_REMEMBERS:" in DEVICE_CLOSES.format(name="Egypt")
        and "}}" in DEVICE_CLOSES.format(name="Egypt"))
    rec("an echo of the new placeholder is still rejected",
        _extract_legend("{{_REMEMBERS: <" + _MEMORY_SCOPE + ">}}")[0] == "")
    rec("the per-era fallbacks no longer claim a sighting",
        all("seen" not in str((load_era(e)["meta"] or {}).get("legend") or "")
            for e in ("egypt", "tang", "wallachia", "victorian")))
    rec("a too-short memory line is rejected",
        _extract_legend("{{_REMEMBERS: short.}}")[0] == "")
    rec("prose without a marker is untouched",
        _extract_legend("Just prose.") == ("", "Just prose."))

    long_legend = "Egypt remembers " + "a stranger crossed the flood and was gone again. " * 20
    rec("a long, multi-sentence legend is accepted (the cap is a backstop above the bound)",
        len(long_legend) < MAX_LEGEND_EXTRACT
        and _extract_legend("{{_REMEMBERS: " + long_legend + "}}")[0].startswith("Egypt remembers"),
        str(len(long_legend)))
    rec("a legend past the hard backstop is still rejected",
        _extract_legend("{{_REMEMBERS: " + "x" * (MAX_LEGEND_EXTRACT + 10) + "}}")[0] == "")
    trimmed = _trim_legend("One. " * 400)
    rec("the era's memory is trimmed oldest-first at a sentence boundary",
        len(trimmed) <= MAX_LEGEND_CHARS and trimmed.endswith("One."), str(len(trimmed)))

    # The GM rewrites the age's whole memory each departure (no first-wins).
    rw = _session()
    rw._pending_legend = "Egypt remembers a stranger who crossed it."
    asyncio.run(rw._jump())
    rw.era = "egypt"  # come back to the age
    rw._pending_legend = "Egypt remembers a stranger who crossed it twice, and the second time it rained."
    asyncio.run(rw._jump())
    rec("a later departure rewrites the age's memory (no first-wins)",
        rw._legends.get("egypt", "").startswith("Egypt remembers a stranger who crossed it twice"),
        str(rw._legends))

    plain = _session()
    asyncio.run(plain._jump())  # no GM line; `_ensure_legend` is a no-op in the harness
    rec("with no GM line the age remembers nothing (no engine template)",
        plain._legends.get("egypt", "") == "", str(plain._legends))

    # The warning turn asks for the line.
    want = _session()
    want._jump_at_turn = want.turn_counter + 2
    want.turn_counter += 1
    asyncio.run(want._check_cadence())
    rec("the closing turn asks the GM for the memory line",
        any(m.get("content") == LEGEND_ASK for m in want.messages))

    # `_jump` prefers the GM's line and clears it.
    prefers = _session()
    prefers._pending_legend = "Chang'an remembers a foreigner who was not there the next morning."
    asyncio.run(prefers._jump())
    rec("the GM's memory line wins over the template",
        prefers._legends.get("egypt", "").startswith("Chang'an remembers"),
        str(prefers._legends))
    rec("... and is cleared once the jump has used it", prefers._pending_legend == "")

    # Missing line: one quiet round is forced.
    forced = _session()
    forced_calls = []

    async def _record_legend():
        forced_calls.append(True)
        forced._pending_legend = "Egypt remembers a stranger who crossed it and vanished."

    forced._ensure_legend = _record_legend
    asyncio.run(forced._jump())
    rec("a missing memory line forces one quiet round",
        forced_calls == [True]
        and "crossed it" in forced._legends.get("egypt", ""))

    # The timeline is written on Save only: the in-memory legends reach disk there.
    saved = _session()
    saved._legends["egypt"] = "Egypt remembers a stranger who crossed it and vanished."
    fd2, saved.timeline_path = tempfile.mkstemp(suffix=".timeline")
    os.close(fd2)
    os.unlink(saved.timeline_path)
    saved._write_timeline("## Turns 1-1 | somewhere | dusk\n**Key Events**:\n- a thing happened")
    rec("a Save writes the in-memory legends to the timeline",
        load_legends(saved.timeline_path).get("egypt", "").startswith("Egypt remembers"))
    rec("... and the session summary alongside them",
        "**Key Events**" in load_timeline(saved.timeline_path))
    saved._write_timeline(None)
    with open(saved.timeline_path, "r", encoding="utf-8") as fh:
        twice = fh.read().count("<!-- legend:egypt -->")
    rec("rewriting the timeline does not duplicate a legend", twice == 1, str(twice))

    # A player-driven jump runs a closing turn first.
    closing = _session(era="tang")
    closing.parts_recovered = 3
    order = []

    async def _stub_jump(to_era=None, arrival=""):
        order.append(("jump", to_era))

    async def _record_turn(content, label):
        order.append(("turn", label, content))

    closing._jump = _stub_jump
    closing._run_turn = _record_turn
    asyncio.run(closing._close_and_jump("egypt"))
    rec("a player-driven jump gets a closing turn, then the jump",
        len(order) == 2 and order[0][0] == "turn" and order[0][1] == "closing"
        and "{{_REMEMBERS:" in order[0][2] and order[1] == ("jump", "egypt"), str(order))

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
