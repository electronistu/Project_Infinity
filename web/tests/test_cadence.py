"""The Device's cadence: the countdown, the warning, and the jump.

No LLM / no network: the transitions are driven directly, and the era and the band are
drawn from an injected RNG.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_cadence.py
"""

import asyncio
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

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
    rec("the later stages are deliberately undecided",
        all(stage not in CADENCE_BANDS for stage in (1, 2, 3, 4)), str(sorted(CADENCE_BANDS)))
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
        keys == ["era", "arrival"], str(gs.session.calls))
    rec("the DB is told the era the session is actually in",
        [a.get("value") for n, a in gs.session.calls if n == "set_player_field" and a.get("key") == "era"]
        == [gs.era], str(gs.session.calls))

    # -- the whole kit carries, and it stays literal ---------------------------
    mutating = {"update_player_list", "modify_player_numeric", "equip_item",
                "attune_item", "rest", "set_player_field"}
    tools_called = [n for n, _a in gs.session.calls if n in mutating]
    rec("a jump cannot touch the inventory: it calls nothing that mutates the player",
        tools_called == ["set_player_field", "set_player_field"]
        and [a.get("key") for n, a in gs.session.calls if n == "set_player_field"]
        == ["era", "arrival"],
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

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
