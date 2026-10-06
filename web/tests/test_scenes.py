"""Engine scene hook: one scene_request per turn, only for the GM's tool.

No network. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_scenes.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import (  # noqa: E402
    GameSession, filter_tools, render_protocol, format_known_places, _gm_tool_view,
)

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.isError = False


class _FakeMCP:
    async def call_tool(self, name, arguments=None):
        return _Result('{"status":"requested"}')


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def main() -> bool:
    # Conditional GM instructions: markers stripped, imagery kept only when on.
    proto = "A\n<!-- SCENE:ON -->\nIMAGERY RULES\n<!-- SCENE:END -->\nB"
    on = render_protocol(proto, True)
    off = render_protocol(proto, False)
    rec("protocol keeps imagery when on", "IMAGERY RULES" in on and "SCENE:" not in on, repr(on))
    rec("protocol drops imagery when off", "IMAGERY RULES" not in off and "SCENE:" not in off, repr(off))
    rec("protocol keeps the surrounding rules", "A" in off and "B" in off)

    tools = [{"function": {"name": "roll_dice"}},
             {"function": {"name": "request_scene_image"}},
             {"function": {"name": "register_npcs"}}]
    rec("filter_tools hides the scene + NPC-declaration tools when off",
        [t["function"]["name"] for t in filter_tools(tools, False)] == ["roll_dice"])
    rec("filter_tools keeps the scene + NPC-declaration tools when on",
        len(filter_tools(tools, True)) == 3)

    tree = format_known_places([
        {"kingdom": "Kingdom of Eldoria", "area": "Eldoria City", "location": "The Drowned Lantern",
         "sublocation": "Common Room", "description": "low-ceilinged, peat fire",
         "main_npcs": [
             {"name": "Maera", "role": "the innkeeper",
              "description": "a one-eared, broad-shouldered barkeep"},
             {"name": "the grandson", "role": "the table-runner",
              "description": "a wiry young man"}]},
        {"kingdom": "Kingdom of Eldoria", "area": "Eldoria City", "location": "The Drowned Lantern",
         "sublocation": "", "description": ""},
        {"kingdom": "Borderlands", "area": "", "location": "Waystone", "sublocation": "", "description": ""},
    ])
    rec("priming tree nests kingdom > area > location > sublocation",
        "- Kingdom of Eldoria" in tree and "    - Eldoria City" in tree
        and "        - The Drowned Lantern" in tree
        and "            - Common Room — low-ceilinged, peat fire · main NPCs: "
            "Maera (the innkeeper), the grandson (the table-runner)" in tree
        and "- Borderlands" in tree and "    - (unknown area)" in tree, tree)
    rec("the priming shows the place main NPCs' names + roles, never the descriptions",
        "broad-shouldered barkeep" not in tree and "wiry young man" not in tree, tree)

    gs = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs.session = _FakeMCP()

    gs._scene_requested_turn = False
    await gs._execute_tool({"function": {
        "name": "request_scene_image",
        # A legacy `caption` is still tolerated (dropped) even though the field is gone.
        "arguments": {"description": "a forge at dusk", "caption": "Forge",
                      "kingdom": "Kingdom of Eldoria", "area": "Eldoria City",
                      "location": "Hask & Daughters Smithy", "sublocation": "the forge",
                      "time_of_day": "dusk", "weather": "light rain",
                      "characters": {"the smith": "hammering at the anvil"},
                      "establishing": "a hot forge",
                      "main_npcs": [{"name": "Gorson", "role": "the smith",
                                     "description": "a soot-stained man"}],
                      "npcs": [{"name": "Maera", "description": "a one-eared barkeep"}]},
    }})
    scene = [e for e in drain(gs._evt_q) if e.get("type") == "scene_request"]
    rec("scene_request emitted for the GM tool",
        len(scene) == 1 and scene[0]["description"] == "a forge at dusk" and scene[0]["kind"] == "story",
        str(scene))
    rec("scene_request carries kingdom/area/location/sublocation + time/weather + characters",
        bool(scene) and scene[0].get("kingdom") == "Kingdom of Eldoria"
        and scene[0].get("area") == "Eldoria City"
        and scene[0].get("location") == "Hask & Daughters Smithy"
        and scene[0].get("sublocation") == "the forge" and scene[0].get("establishing") == "a hot forge"
        and scene[0].get("time_of_day") == "dusk" and scene[0].get("weather") == "light rain"
        and scene[0].get("main_npcs") == [{"name": "Gorson", "role": "the smith",
                                          "description": "a soot-stained man"}]
        and scene[0].get("characters") == {"the smith": "hammering at the anvil"}
        and scene[0].get("npcs") == [{"name": "Maera", "description": "a one-eared barkeep"}])
    rec("legacy caption dropped from the scene event",
        bool(scene) and "caption" not in scene[0], str(scene))

    import dice_server as ds  # noqa: E402
    tools = await ds.mcp.list_tools()
    scene_tool = next((t for t in tools if t.name == "request_scene_image"), None)
    props = list((scene_tool.inputSchema or {}).get("properties", {})) if scene_tool else []
    rec("scene tool speaks names + declarations, not descriptions",
        props == ["description", "kingdom", "area", "location", "sublocation", "time_of_day",
                  "weather", "characters", "establishing", "main_npcs", "npcs", "seed_change",
                  "mood"], str(props))
    rec("register_npcs is exposed to the GM", any(t.name == "register_npcs" for t in tools))

    await gs._execute_tool({"function": {
        "name": "request_scene_image", "arguments": {"description": "another"},
    }})
    rec("second request in the same turn is dropped",
        not any(e.get("type") == "scene_request" for e in drain(gs._evt_q)))

    gs._scene_requested_turn = False
    await gs._execute_tool({"function": {
        "name": "request_scene_image", "arguments": {"description": "third"},
    }})
    rec("a new turn allows one again",
        sum(1 for e in drain(gs._evt_q) if e.get("type") == "scene_request") == 1)

    await gs._execute_tool({"function": {
        "name": "perform_check", "arguments": {"modifier": 1, "dc": 10},
    }})
    rec("other tools emit no scene_request",
        not any(e.get("type") == "scene_request" for e in drain(gs._evt_q)))

    # Declarations: register_npcs is buffered and flushed with the image call; an
    # undeclared name gets a soft note (a declared one does not).
    gs_note = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_note.session = _FakeMCP()
    gs_note._scene_requested_turn = False
    await gs_note._execute_tool({"function": {"name": "register_npcs", "arguments": {
        "npcs": [{"name": "Maera", "description": "a one-eared barkeep"}]}}})
    rec("register_npcs is buffered for the next scene call",
        [p["name"] for p in gs_note._pending_npcs] == ["Maera"], str(gs_note._pending_npcs))
    await gs_note._execute_tool({"function": {"name": "request_scene_image", "arguments": {
        "description": "x", "location": "L", "sublocation": "s", "establishing": "a cold hall",
        "characters": {"Maera": "pouring", "the harbourmaster": "watching"}}}})
    note = [e for e in drain(gs_note._evt_q)
            if e.get("type") == "tool_result" and "NOTE:" in str(e.get("text") or "")]
    rec("an undeclared name gets a soft note (a declared one does not)",
        len(note) == 1 and "the harbourmaster" in note[0]["text"]
        and "Maera" not in note[0]["text"], str(note))

    # New-place seed gate: a scene call with no establishing view is rejected with
    # a warning and no scene_request, then accepted once `establishing` is supplied.
    gs7 = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs7.session = _FakeMCP()
    gs7._scene_requested_turn = False
    await gs7._execute_tool({"function": {"name": "request_scene_image", "arguments": {
        "description": "x", "location": "Brand New Hall", "sublocation": "the antechamber"}}})
    evts7 = drain(gs7._evt_q)
    warns = [e for e in evts7 if e.get("type") == "tool_result" and e.get("is_error")]
    rec("a new place without establishing is rejected with a warning",
        len(warns) == 1 and "establishing" in warns[0]["text"]
        and not any(e.get("type") == "scene_request" for e in evts7), str(evts7))
    await gs7._execute_tool({"function": {"name": "request_scene_image", "arguments": {
        "description": "x", "location": "Brand New Hall", "sublocation": "the antechamber",
        "establishing": "a cold stone hall"}}})
    rec("the same place is accepted once establishing is supplied",
        any(e.get("type") == "scene_request" for e in drain(gs7._evt_q)))
    gs7._scene_requested_turn = False
    await gs7._execute_tool({"function": {"name": "request_scene_image", "arguments": {
        "description": "y", "location": "Brand New Hall", "sublocation": "the antechamber"}}})
    rec("a place seeded this session needs no establishing again",
        any(e.get("type") == "scene_request" for e in drain(gs7._evt_q)))

    gs_off = GameSession(base_dir=REPO, model="test", scene_images=False)
    gs_off.session = _FakeMCP()
    await gs_off._execute_tool({"function": {
        "name": "request_scene_image", "arguments": {"description": "off"},
    }})
    rec("scenes disabled -> no scene_request even if the tool is called",
        not any(e.get("type") == "scene_request" for e in drain(gs_off._evt_q)))

    # Enforcement: a missing turn call is nudged, then the engine falls back.
    async def _noop_role(role_content, label, quiet=False):
        return ""

    gs2 = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs2._run_role = _noop_role  # type: ignore[assignment]
    gs2._scene_requested_turn = True
    await gs2._ensure_scene_image("some narrative")
    rec("ensure_scene is a no-op when the GM already requested one",
        not any(e.get("type") == "scene_request" for e in drain(gs2._evt_q)))

    gs2._scene_requested_turn = False
    await gs2._ensure_scene_image("You enter the smithy and the forge roars.")
    evts = [e for e in drain(gs2._evt_q) if e.get("type") == "scene_request"]
    rec("ensure_scene falls back to an auto scene_request",
        len(evts) == 1 and evts[0].get("kind") == "auto"
        and "forge roars" in evts[0].get("description", ""), str(evts))

    gs3 = GameSession(base_dir=REPO, model="test", scene_images=False)
    gs3._run_role = _noop_role  # type: ignore[assignment]
    await gs3._ensure_scene_image("narrative")
    rec("ensure_scene no-op when scenes are disabled",
        not any(e.get("type") == "scene_request" for e in drain(gs3._evt_q)))

    with tempfile.TemporaryDirectory() as td:
        manifest_dir = Path(td) / "output" / "images" / "save" / "scenes"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "manifest.json").write_text(json.dumps({
            "hask-smithy": {"location": "Hask's Smithy"},
            "gilded-stag": {"location": "The Gilded Stag"},
        }), encoding="utf-8")
        gs4 = GameSession(base_dir=Path(td), model="test", scene_images=True)
        gs4.active_name = "save"
        rec("match_known_location finds the named location",
            gs4._match_known_location("You step into Hask's Smithy, heat rolling out.")
            == "Hask's Smithy")
        rec("match_known_location empty when none is named",
            gs4._match_known_location("You walk down a nameless road.") == "")

    # Real protocol: the base continuity block is always sent; the gated
    # narrative-phase wording only when scenes are on.
    real = (REPO / "GameMaster_MCP.md").read_text(encoding="utf-8")
    real_on = render_protocol(real, True)
    real_off = render_protocol(real, False)
    rec("base continuity block is always sent",
        "continuity:" in real_on and "continuity:" in real_off)
    rec("gated scene wording only when scenes are on",
        "Re-narrated Turn" in real_on and "Re-narrated Turn" not in real_off)
    rec("the image call ends the turn (gated wording)",
        "ENDS the turn" in real_on and "ENDS the turn" not in real_off)
    rec("the GM is told mechanics are displayed, not transcribed (no tokens)",
        "already displayed in the mechanics section" in real_on and "{{_MECHANICS}}" not in real_on
        and "already displayed in the mechanics section" in real_off)

    # A narrative-phase tool call ends the turn: no second model round, so the
    # GM cannot re-narrate the whole turn (the duplicate-answer regression).
    gs5 = GameSession(base_dir=REPO, model="test", scene_images=True)
    rounds5: list[str] = []
    executed5: list[str] = []

    async def _exec5(tc):
        executed5.append((tc.get("function") or {}).get("name"))

    async def _stream5(label, quiet=False):
        rounds5.append(label)
        if len(rounds5) == 1:
            return ("You step into the forge, heat rolling out.", "", [
                {"function": {"name": "request_scene_image", "arguments": {"description": "a forge"}}}
            ], False)
        return ("DUPLICATE NARRATIVE", "", [], False)

    gs5._execute_tool = _exec5  # type: ignore[assignment]
    gs5._stream_assistant = _stream5  # type: ignore[assignment]
    out5 = await gs5._chat_with_tools("turn")
    rec("prose + scene tool ends the turn (no second model round)",
        len(rounds5) == 1, f"rounds={rounds5}")
    rec("prose + scene tool returns that narrative, not a re-run",
        out5.startswith("You step into the forge"), repr(out5))
    rec("prose + scene tool still executes the tool",
        executed5 == ["request_scene_image"], str(executed5))

    # A rejected scene round (new place, no establishing) keeps looping so the GM
    # can re-call; the accepted round then ends the turn.
    gs8 = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs8.session = _FakeMCP()
    gs8._scene_requested_turn = False
    rounds8: list[str] = []

    async def _stream8(label, quiet=False):
        rounds8.append(label)
        if len(rounds8) == 1:
            return ("You push open the door.", "", [{"function": {
                "name": "request_scene_image", "arguments": {
                    "description": "entering", "location": "New Hall", "sublocation": "the door"}}}], False)
        return ("You step into the cold hall.", "", [{"function": {
            "name": "request_scene_image", "arguments": {
                "description": "entering", "location": "New Hall", "sublocation": "the door",
                "establishing": "a cold stone hall"}}}], False)

    gs8._stream_assistant = _stream8  # type: ignore[assignment]
    out8 = await gs8._chat_with_tools("turn")
    rec("a rejected scene round loops for a re-call, then the accepted round ends the turn",
        len(rounds8) == 2 and out8.startswith("You step into the cold hall"), f"rounds={rounds8}")

    # A mechanical tool round (no prose) still loops to the narrative.
    gs6 = GameSession(base_dir=REPO, model="test", scene_images=True)
    rounds6: list[str] = []

    async def _exec6(tc):
        executed5.append((tc.get("function") or {}).get("name"))

    async def _stream6(label, quiet=False):
        rounds6.append(label)
        if len(rounds6) == 1:
            return ("", "", [{"function": {"name": "roll_dice", "arguments": {}}}], False)
        return ("The blade bites deep.", "", [], False)

    gs6._execute_tool = _exec6  # type: ignore[assignment]
    gs6._stream_assistant = _stream6  # type: ignore[assignment]
    out6 = await gs6._chat_with_tools("turn")
    rec("mechanical tool rounds still loop to the narrative",
        len(rounds6) == 2 and out6 == "The blade bites deep.", f"rounds={rounds6}")

    # Tool results: the GM reads a trimmed view (new info + mechanics + errors); the
    # client keeps the full result for debugging.
    full_list = json.dumps({
        "success": True, "key": "inventory", "item": "Dagger", "action": "add",
        "current_list": ["Longsword", "Dagger"],
        "carrying": {"status": "unencumbered", "carried": 4.0, "capacity": 270.0,
                     "items": [{"name": "Dagger", "weight": 1.0}], "thresholds": {}},
        "equipment": {"armor": None, "hands": []},
        "narrative_format": "Added to inventory: Dagger."})
    view = json.loads(_gm_tool_view("update_player_list", full_list))
    rec("the GM view drops current_list, the carrying breakdown, equipment and narrative_format",
        "current_list" not in view and "equipment" not in view
        and "narrative_format" not in view
        and view["carrying"] == {"status": "unencumbered", "carried": 4.0, "capacity": 270.0},
        str(view))
    rec("the GM view is smaller than the full result",
        len(_gm_tool_view("update_player_list", full_list)) < len(full_list))

    # A pass-through mechanics tool keeps its fields but still loses state snapshots.
    full_attack = json.dumps({"success": True, "outcome": "Hit", "damage_total": 7,
                              "equipment": {"armor": None}, "current_list": ["x"],
                              "narrative_format": "Hit for 7."})
    atk_view = json.loads(_gm_tool_view("resolve_attack", full_attack))
    rec("pass-through mechanics stay intact, state snapshots + narrative_format dropped",
        atk_view.get("damage_total") == 7 and "equipment" not in atk_view
        and "current_list" not in atk_view and "narrative_format" not in atk_view, str(atk_view))
    rec("dump_player_db passes through untouched (full sheet)",
        _gm_tool_view("dump_player_db", json.dumps({"name": "X", "_equipment": {}}))
        == json.dumps({"name": "X", "_equipment": {}}))
    rec("a non-dict tool result passes through untouched",
        _gm_tool_view("equip_item", "Tool error: boom") == "Tool error: boom")

    class _RichMCP:
        async def call_tool(self, name, arguments=None):
            return _Result(json.dumps({
                "success": True, "key": "inventory", "item": "Dagger", "action": "add",
                "current_list": ["Longsword", "Dagger"],
                "carrying": {"status": "unencumbered", "items": [{"name": "Dagger"}]},
                "equipment": {"armor": None},
                "narrative_format": "Added to inventory: Dagger."}))

    gs_view = GameSession(base_dir=REPO, model="test", scene_images=False)
    gs_view.session = _RichMCP()
    await gs_view._execute_tool({"function": {"name": "resolve_attack",
                                              "arguments": {"actor": "x"}}})
    all_view = drain(gs_view._evt_q)
    evt = [e for e in all_view if e.get("type") == "tool_result"]
    mech = [e for e in all_view if e.get("type") == "mechanics"]
    stored = gs_view.messages[-1]["content"]
    rec("the client tool_result event carries the full result (debugging)",
        len(evt) == 1 and "current_list" in evt[0]["text"], str(evt))
    rec("narrative_format is composed into the engine's `mechanics` event",
        len(mech) == 1 and mech[0].get("lines") == ["Added to inventory: Dagger."], str(mech))
    rec("the event's gm_text is the trimmed view, matching the GM message",
        len(evt) == 1 and "current_list" not in evt[0].get("gm_text", "")
        and evt[0].get("gm_text") == stored, str(evt[0].get("gm_text")))
    rec("the GM message carries the trimmed result (no narrative_format)",
        "current_list" not in stored and "equipment" not in stored
        and "narrative_format" not in stored, stored)

    # Combat rosters: the engine forwards the sheets + live state to the client (tooltips),
    # and drops them from the GM view.
    class _CombatMCP:
        async def call_tool(self, name, arguments=None):
            return _Result(json.dumps({
                "success": True,
                "initiative_order": ["Goblin", "Borin"],
                "initiative": [{"name": "Goblin", "roll": 14, "modifier": 2, "total": 16},
                               {"name": "Borin", "roll": 12, "modifier": 2, "total": 14}],
                "registry_summary": [
                    {"name": "Goblin", "hp": "7/7", "ac": 15, "role": "hostile",
                     "is_player": False},
                    {"name": "Borin", "hp": "44/44", "ac": 17, "role": "ally",
                     "is_player": True}],
                "sheets": [{"name": "Goblin",
                            "lines": ["Goblin — hostile", "  HP 7/7  AC 15",
                                      "  Attacks: Scimitar +4"]}],
                "narrative_format": "Combatants registered (2 total). Initiative Order"}))

    gs_combat = GameSession(base_dir=REPO, model="test", scene_images=False)
    gs_combat.session = _CombatMCP()
    await gs_combat._execute_tool({"function": {"name": "register_combatants",
                                                "arguments": {"combatants": []}}})
    cevts = drain(gs_combat._evt_q)
    roster = [e for e in cevts if e.get("type") == "combat_roster"]
    rec("register_combatants emits a combat_roster event for the client",
        len(roster) == 1, str(roster))
    by = {c["name"]: c for c in (roster[0]["combatants"] if roster else [])}
    rec("the roster carries live HP/AC and the static sheet lines",
        by.get("Goblin", {}).get("hp") == "7/7"
        and by.get("Goblin", {}).get("sheet", [None])[0] == "Goblin — hostile", str(roster))
    rec("the roster event carries the initiative order + roll breakdown",
        roster and roster[0].get("order") == ["Goblin", "Borin"]
        and roster[0].get("initiative", {}).get("Goblin", {}).get("total") == 16, str(roster))
    rec("register_combatants' narrative_format becomes the engine `mechanics` event",
        any(e.get("type") == "mechanics"
            and e.get("lines") == ["Combatants registered (2 total). Initiative Order"]
            for e in cevts), str(cevts))
    rec("registry_summary + sheets + narrative_format are dropped from the GM view",
        "registry_summary" not in gs_combat.messages[-1]["content"]
        and "sheets" not in gs_combat.messages[-1]["content"]
        and "narrative_format" not in gs_combat.messages[-1]["content"],
        gs_combat.messages[-1]["content"])

    return all(RESULTS)


if __name__ == "__main__":
    ok = asyncio.run(main())
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
