"""Combat-aware gating: the engine derives "in combat" from the combat roster, refuses a
save mid-fight, and holds the Device for the whole fight.

No LLM / no network: the roster is fed straight to `_combat_roster_update`, and the save
path's two side effects are stubbed so the gate is observed without a model call.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_combat_gate.py
"""

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import GameSession  # noqa: E402
from web.session_turn import COMBAT_TOOLS  # noqa: E402
from web.protocol import COMBAT_HOLD_MESSAGE  # noqa: E402
import device as device_mod  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _session():
    gs = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs.era = "egypt"
    gs.has_device = True
    gs.parts_recovered = 1
    return gs


def _npc(name, role="hostile", hp="7/7", killed=False, status="active", is_player=False):
    return {"name": name, "hp": hp, "ac": 15, "is_player": is_player,
            "role": role, "killed": killed, "status": status}


def _feed(gs, entries):
    """Hand `_combat_roster_update` a combat tool result carrying this roster."""
    return gs._combat_roster_update(json.dumps({"registry_summary": entries}))


def _feed_full(gs, entries, turn=None):
    """`_feed` plus the engine's turn cursor (round/actor/skipped)."""
    payload = {"registry_summary": entries}
    if turn is not None:
        payload["combat_turn"] = turn
    return gs._combat_roster_update(json.dumps(payload))


def _save_attempt(in_combat):
    gs = _session()
    gs._in_combat = in_combat
    calls = []

    async def _summarize(_turn):
        calls.append("summarize")

    async def _save(_timeline_entry=None):
        calls.append("save")

    gs._summarize_timeline = _summarize
    gs._save_to_active = _save
    asyncio.run(gs._handle_save_command())
    evts = []
    while not gs._evt_q.empty():
        evts.append(gs._evt_q.get_nowait())
    return calls, evts


def main() -> bool:
    # -- the derivation: one living, hostile, active non-player keeps combat on ----
    gs = _session()
    _feed(gs, [_npc("Goblin")])
    rec("a living hostile puts the engine in combat", gs._in_combat is True)

    _feed(gs, [_npc("Borin", is_player=True), _npc("Goblin", killed=True)])
    rec("killing the last hostile ends combat", gs._in_combat is False)

    _feed(gs, [_npc("Scout", role="ally")])
    rec("an ally alone is not combat", gs._in_combat is False)

    _feed(gs, [_npc("Merchant", role="neutral")])
    rec("a neutral alone is not combat", gs._in_combat is False)

    _feed(gs, [_npc("Goblin", status="fled")])
    rec("a hostile that fled ends combat", gs._in_combat is False)

    _feed(gs, [_npc("Goblin", status="surrendered")])
    rec("a hostile that surrendered ends combat", gs._in_combat is False)

    _feed(gs, [_npc("Goblin")])
    rec("a fresh hostile restarts combat", gs._in_combat is True)
    rec("... and the Device state carries it for the client",
        gs._device_state().get("in_combat") is True, str(gs._device_state().get("in_combat")))

    _feed(gs, [])
    rec("an emptied registry clears combat", gs._in_combat is False)

    _feed(gs, [_npc("Goblin")])
    gs._combat_roster_update(json.dumps({"success": True}))  # no roster in the result
    rec("a tool result without a roster leaves the flag alone", gs._in_combat is True)

    # -- the save gate ------------------------------------------------------------
    rec("end_combat is a combat tool the engine routes", "end_combat" in COMBAT_TOOLS)
    calls, evts = _save_attempt(True)
    rec("a save mid-fight writes nothing", calls == [], str(calls))
    rec("... and the client is told why",
        any(e.get("type") == "save_refused" and e.get("reason") == "combat"
            and e.get("text") == COMBAT_HOLD_MESSAGE for e in evts),
        str(evts))
    rec("... and a refused save never locks the client",
        not any(e.get("type") == "saving" for e in evts), str(evts))

    calls, evts = _save_attempt(False)
    rec("a save out of combat still summarizes, then writes",
        calls == ["summarize", "save"], str(calls))
    rec("... with no refusal", not any(e.get("type") == "save_refused" for e in evts), str(evts))
    saving = [e.get("value") for e in evts if e.get("type") == "saving"]
    rec("... and brackets itself with the `saving` lock", saving == [True, False], str(saving))

    # -- the Device gate ----------------------------------------------------------
    gs2 = _session()
    gs2._in_combat = True
    asyncio.run(gs2._device_release({}))
    dev_evts = []
    while not gs2._evt_q.empty():
        dev_evts.append(gs2._evt_q.get_nowait())
    rec("releasing the Device mid-fight is refused with the hold message",
        any(e.get("type") == "notice" and e.get("text") == COMBAT_HOLD_MESSAGE
            for e in dev_evts),
        str(dev_evts))

    # -- the combat bar data: hp, conditions, turn cursor -------------------------
    gs = _session()
    goblin = {"name": "Goblin", "hp": "3/7", "ac": 15, "is_player": False,
              "role": "hostile", "killed": False, "status": "active",
              "conditions": ["prone"]}
    roster = _feed_full(gs, [goblin], {"round": 2, "actor": "Goblin", "next": "Borin",
                                      "skipped": ["Scout"]})
    rec("the roster carries hp + conditions for the bar",
        bool(roster) and roster[0]["hp"] == "3/7" and roster[0]["conditions"] == ["prone"],
        str(roster))
    rec("... and the engine keeps the turn cursor",
        gs._combat_turn == {"round": 2, "actor": "Goblin", "next": "Borin",
                            "skipped": ["Scout"]},
        str(gs._combat_turn))

    # -- an emptied registry still yields a roster, so the bar auto-closes ---------
    gs = _session()
    _feed(gs, [_npc("Goblin")])
    empty = _feed(gs, [])
    rec("an emptied registry still yields an empty roster so the bar closes",
        empty == [], str(empty))
    rec("... and combat is over", gs._in_combat is False)

    # -- the combat-over tally (goes to the mechanics block, not the panel) -------
    gs = _session()
    _feed(gs, [_npc("A", killed=True), _npc("B", status="fled"),
               _npc("C", status="surrendered"), _npc("Hero", is_player=True)])
    rec("the combat-over line tallies the fates",
        gs._combat_over_line() == "Combat over — 1 dead, 1 fled, 1 surrendered",
        gs._combat_over_line())
    gs = _session()
    _feed(gs, [_npc("Hero", is_player=True)])
    rec("the combat-over line reads 'no casualties' when none fell",
        gs._combat_over_line() == "Combat over — no casualties", gs._combat_over_line())

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
