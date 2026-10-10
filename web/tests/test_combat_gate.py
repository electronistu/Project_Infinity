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

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _session():
    gs = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs.era = "egypt"
    gs.has_device = True
    gs.parts_recovered = 1
    gs._jump_at_turn = gs.turn_counter + 5
    return gs


def _npc(name, role="hostile", hp="7/7", killed=False, status="active", is_player=False):
    return {"name": name, "hp": hp, "ac": 15, "is_player": is_player,
            "role": role, "killed": killed, "status": status}


def _feed(gs, entries):
    """Hand `_combat_roster_update` a combat tool result carrying this roster."""
    return gs._combat_roster_update(json.dumps({"registry_summary": entries}))


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
    calls, evts = _save_attempt(True)
    rec("a save mid-fight writes nothing", calls == [], str(calls))
    rec("... and the client is told why",
        any(e.get("type") == "save_refused" and e.get("reason") == "combat" for e in evts),
        str(evts))

    calls, evts = _save_attempt(False)
    rec("a save out of combat still summarizes, then writes",
        calls == ["summarize", "save"], str(calls))
    rec("... with no refusal", not any(e.get("type") == "save_refused" for e in evts), str(evts))

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
