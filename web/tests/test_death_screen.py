"""The engine-owned death turn: no GM skip, no narrated revive, no save after death.

No LLM / no network: the GM is scripted (`_scripted.py`) and the MCP server is a fake
that returns the payloads the real tools would. The tests observe the gate, the turn
stop, the runtime STATE note and the save refusal.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_death_screen.py
"""

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import GameSession  # noqa: E402
from web.protocol import ENGINE_ONLY_TOOLS, STATE_ZERO_HP, SITUATION_DEATH_SAVE  # noqa: E402
from web.tests._scripted import response, scripted  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, payload):
        self.content = [_Block(json.dumps(payload, ensure_ascii=True))]
        self.isError = False


class _FakeEngine:
    """Enough of the MCP session for the gate and one GM tool result."""

    def __init__(self, death_results, tool_results):
        self.death_results = list(death_results)
        self.tool_results = list(tool_results)
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if name == "make_death_save":
            payload = self.death_results.pop(0) if self.death_results else {
                "success": False, "error": "not_dying"}
        elif name == "dump_player_db":
            payload = {"current_hit_points": 0}  # only reached after death is refused
        else:
            payload = self.tool_results.pop(0) if self.tool_results else {"success": True}
        return _Result(payload)


def _drain(gs):
    out = []
    while not gs._evt_q.empty():
        out.append(gs._evt_q.get_nowait())
    return out


async def _noop(*_a, **_k):
    return None


def _session(death_results, tool_results):
    gs = GameSession(base_dir=REPO, model="test", scene_images=False)
    gs.session = _FakeEngine(death_results, tool_results)
    gs.era = "egypt"
    # Isolate the death logic: no scene/place corrective rounds consume script steps.
    gs._ensure_scene_image = _noop
    gs._ensure_place_note = _noop
    return gs


DROP = {
    "success": True, "key": "current_hit_points", "old_value": 12, "new_value": 0,
    "delta": -12, "hp_status": "0/12", "status": "Unconscious", "death_saves": True,
    "death_save_failures": 1,
    "message": "HP has reached 0. 0/12. Begin death saves.",
    "narrative_format": "Radu HP: 0/12 (Unconscious)",
}
SAVE_FAILURE = {
    "success": True, "roll": 5, "successes": 0, "failures": 1, "outcome": "failure",
    "narrative_format": "Death save: 5 \u2014 0 successes, 1 failure.",
}
SAVE_DEAD = {
    "success": True, "roll": 5, "successes": 0, "failures": 3, "outcome": "dead",
    "dead": True, "death_cause": "third death save failure",
    "narrative_format": "Death save: 5 \u2014 third failure. The character is dead.",
}
STABLE = {"success": False, "error": "stable", "stable": True, "current_hp": 0}


async def _run_turn(gs, text):
    await gs._handle_action(text)
    return _drain(gs)


async def _save(gs):
    calls = []

    async def _summarize(_turn):
        calls.append("summarize")

    async def _save_to_active(_entry=None):
        calls.append("save")

    gs._summarize_timeline = _summarize
    gs._save_to_active = _save_to_active
    await gs._handle_save_command()
    return calls, _drain(gs)


def main() -> bool:
    # ── the GM cannot skip the save, and a prose potion changes nothing ────────
    death_results = [
        {"success": False, "error": "not_dying"},   # turn 1: alive
        SAVE_FAILURE,                               # turn 2: the engine rolls
        SAVE_DEAD,                                  # turn 3: fatal
    ]
    gs = _session(death_results, [DROP])
    script = [
        response("", [("modify_player_numeric",
                       {"key": "current_hit_points", "delta": -12})]),
        response("The character falls; a flask is pressed to their lips."),
        response("The death save is not the GM's to refuse."),
        response("This prose must never be requested."),
    ]
    with scripted(script) as model:
        first = asyncio.run(_run_turn(gs, "cast magic missile on Old Wick"))
        rec("the drop to 0 HP appends the STATE note",
            any(m.get("role") == "user" and m.get("content") == STATE_ZERO_HP
                for m in gs.messages),
            str([m.get("content") for m in gs.messages if m.get("role") == "user"]))
        rec("the GM narration is still the turn's prose",
            not any(e.get("type") == "death" for e in first), str(first))

        second = asyncio.run(_run_turn(gs, "roll for death save"))
        rec("the engine calls make_death_save before the GM turn",
            any(name == "make_death_save" for name, _args in gs.session.calls),
            str(gs.session.calls))
        rec("the dying turn carries the situation line",
            any(SITUATION_DEATH_SAVE.split("{")[0].strip() in (m.get("content") or "")
                for m in gs.messages if m.get("role") == "user"),
            str([m.get("content") for m in gs.messages if m.get("role") == "user"]))
        rec("the dying turn shows the death-save mechanics",
            any(e.get("type") == "mechanics" and any("Death save" in ln for ln in e.get("lines", []))
                for e in second), str(second))
        rec("the GM gets a narrative turn after the roll", model.calls == 3, str(model.calls))

        fatal = asyncio.run(_run_turn(gs, "the next turn"))
        rec("a fatal gate sets the latch", gs.dead is True)
        rec("a fatal gate runs no GM turn", model.calls == 3, str(model.calls))
        rec("the fatal turn ends before the death event",
            [e.get("type") for e in fatal if e.get("type") in ("turn_end", "death")]
            == ["turn_end", "death"], str(fatal))
        rec("the death event carries the cause",
            any(e.get("type") == "death" and e.get("cause") == "third death save failure"
                for e in fatal), str(fatal))

        calls, evts = asyncio.run(_save(gs))
        rec("a dead character cannot save", calls == [], str(calls))
        rec("the save refusal names death",
            any(e.get("type") == "save_refused" and e.get("reason") == "dead" for e in evts),
            str(evts))

        after = asyncio.run(_run_turn(gs, "I stand up"))
        rec("an action after death runs no model", model.calls == 3, str(model.calls))
        rec("an action after death re-emits the death state",
            any(e.get("type") == "death" for e in after), str(after))

    # ── an NPC heal in the GM turn stops the next roll ────────────────────────
    gs2 = _session(
        [{"success": False, "error": "not_dying"}, SAVE_FAILURE,
         {"success": False, "error": "not_dying"}],
        [DROP, {"success": True, "key": "current_hit_points", "old_value": 0,
                "new_value": 10, "delta": 10, "hp_status": "10/12", "message": "Healed."}],
    )
    script2 = [
        response("", [("modify_player_numeric",
                       {"key": "current_hit_points", "delta": -12})]),
        response("The Traveller falls."),
        response("", [("modify_player_numeric",
                       {"key": "current_hit_points", "delta": 10})]),
        response("The Traveller wakes on the boards."),
        response("The Traveller acts."),
    ]
    with scripted(script2) as model2:
        asyncio.run(_run_turn(gs2, "cast magic missile"))
        asyncio.run(_run_turn(gs2, "the NPC pours a potion"))
        # The next turn's gate returns not_dying: no death save, normal turn.
        asyncio.run(_run_turn(gs2, "I get up"))
        death_calls = [c for c in gs2.session.calls if c[0] == "make_death_save"]
        rec("the dying turn's gate rolled before the NPC heal",
            len(death_calls) == 3, str(death_calls))
        rec("an applied heal makes the next gate not_dying",
            not gs2.dying and gs2.dead is False, str(death_calls))
        rec("the healed turn is a normal GM turn", model2.calls == 5, str(model2.calls))

    # ── stable is a state, not a cleared counter ─────────────────────────────
    gs3 = _session([STABLE], [])
    script3 = [response("The Traveller is stable.")]
    with scripted(script3) as model3:
        evts = asyncio.run(_run_turn(gs3, "wait"))
        rec("stable runs no death-save roll", len(model3.responses) == 0, str(model3.calls))
        rec("stable is reported to the client",
            any(e.get("type") == "vitals" and e.get("stable") for e in evts), str(evts))
        rec("stable gets a narrative turn", model3.calls == 1, str(model3.calls))

    rec("make_death_save is engine-only", "make_death_save" in ENGINE_ONLY_TOOLS)

    print()
    print(f"{sum(RESULTS)}/{len(RESULTS)} PASS")
    return all(RESULTS)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
