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

from web.engine import GameSession, filter_tools, render_protocol, format_known_places  # noqa: E402

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
             {"function": {"name": "request_scene_image"}}]
    rec("filter_tools hides the scene tool when off",
        [t["function"]["name"] for t in filter_tools(tools, False)] == ["roll_dice"])
    rec("filter_tools keeps the scene tool when on", len(filter_tools(tools, True)) == 2)

    tree = format_known_places([
        {"kingdom": "Kingdom of Eldoria", "area": "Eldoria City", "location": "The Drowned Lantern",
         "sublocation": "Common Room", "description": "low-ceilinged, peat fire",
         "main_npc": "Maera — a one-eared, broad-shouldered barkeep"},
        {"kingdom": "Kingdom of Eldoria", "area": "Eldoria City", "location": "The Drowned Lantern",
         "sublocation": "", "description": ""},
        {"kingdom": "Borderlands", "area": "", "location": "Waystone", "sublocation": "", "description": ""},
    ])
    rec("priming tree nests kingdom > area > location > sublocation",
        "- Kingdom of Eldoria" in tree and "    - Eldoria City" in tree
        and "        - The Drowned Lantern" in tree
        and "            - Common Room — low-ceilinged, peat fire · main NPC: Maera — a one-eared, broad-shouldered barkeep" in tree
        and "- Borderlands" in tree and "    - (unknown area)" in tree, tree)

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
                      "characters": {"the smith — a soot-stained man": "hammering at the anvil"},
                      "establishing": "a hot forge",
                      "main_npc": "the smith — a soot-stained man"},
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
        and scene[0].get("main_npc") == "the smith — a soot-stained man"
        and scene[0].get("characters") == {"the smith — a soot-stained man": "hammering at the anvil"})
    rec("legacy caption dropped from the scene event",
        bool(scene) and "caption" not in scene[0], str(scene))

    import dice_server as ds  # noqa: E402
    tools = await ds.mcp.list_tools()
    scene_tool = next((t for t in tools if t.name == "request_scene_image"), None)
    props = list((scene_tool.inputSchema or {}).get("properties", {})) if scene_tool else []
    rec("scene tool dropped the caption field",
        props == ["description", "kingdom", "area", "location", "sublocation", "time_of_day",
                  "weather", "characters", "establishing", "main_npc", "seed_change", "mood"],
        str(props))

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
    rec("the mechanics block ends with an explicit marker",
        "**END MECHANICS**" in real_on and "**END MECHANICS**" in real_off
        and "immediately after the last mechanics line" in real_on)

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

    return all(RESULTS)


if __name__ == "__main__":
    ok = asyncio.run(main())
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
