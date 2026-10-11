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
    _primed_places, PLACE_TREE_CAP,
    MAX_RESUME_ROUNDS,
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
        {"kingdom": "Kingdom of Eldoria", "area": "Eldoria City",
         "place": ["The Drowned Lantern", "Common Room"], "description": "low-ceilinged, peat fire",
         "main_npcs": [
             {"name": "Maera", "role": "the innkeeper",
              "description": "a one-eared, broad-shouldered barkeep"},
             {"name": "the grandson", "role": "the table-runner",
              "description": "a wiry young man"}]},
        {"kingdom": "Kingdom of Eldoria", "area": "Eldoria City",
         "place": ["The Drowned Lantern"], "description": ""},
        {"kingdom": "Kingdom of Eldoria", "area": "Eldoria City",
         "place": ["Ropehaven Wharf", "Warehouse Nine", "the counting office"],
         "description": "a locked strongroom"},
        {"kingdom": "Borderlands", "area": "", "place": ["Waystone"], "description": ""},
    ])
    rec("priming tree nests kingdom > area > place path",
        "- Kingdom of Eldoria" in tree and "    - Eldoria City" in tree
        and "        - The Drowned Lantern" in tree
        and "            - Common Room · NPCs: Maera, the grandson" in tree
        and "        - Ropehaven Wharf" in tree and "            - Warehouse Nine" in tree
        and "                - the counting office" in tree
        and "- Borderlands" in tree and "    - (unknown settlement)" in tree, tree)
    rec("the priming feeds names only, never the descriptions or the roles",
        "broad-shouldered barkeep" not in tree and "wiry young man" not in tree
        and "low-ceilinged, peat fire" not in tree and "a locked strongroom" not in tree
        and "the innkeeper" not in tree and "the table-runner" not in tree, tree)
    many = [{"kingdom": "K", "area": "A", "place": [f"Room {i}"],
             "main_npcs": [{"name": f"N{i}{j}", "role": "r"} for j in range(4)]}
            for i in range(3)]
    capped = format_known_places(many)
    rec("at most 2 NPC names per place, with a +N count for the rest",
        capped.count("+2") == 3 and "N00, N01" in capped and "N02" not in capped, capped)
    primed = _primed_places([{"kingdom": "K", "area": "A", "place": [f"Room {i}"],
                              "main_npcs": []} for i in range(20)])
    rec("the primed tree is capped at PLACE_TREE_CAP",
        len(primed) == PLACE_TREE_CAP, f"{len(primed)} places")

    gs = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs.session = _FakeMCP()

    gs._scene_requested_turn = False
    await gs._execute_tool({"function": {
        "name": "request_scene_image",
        # A legacy `caption` is still tolerated (dropped) even though the field is gone.
        "arguments": {"description": "a forge at dusk", "caption": "Forge",
                      "kingdom": "Kingdom of Eldoria", "area": "Eldoria City",
                      "place": ["Hask & Daughters Smithy", "the forge"],
                      "time_of_day": "dusk", "weather": "light rain",
                      "characters": {"the smith": "hammering at the anvil"},
                      "establishing": "a hot forge",
                      "main_npcs": [{"name": "Gorson", "role": "the smith",
                                     "race": "dwarf", "class": "fighter",
                                     "description": "a soot-stained man"}],
                      "npcs": [{"name": "Maera", "race": "human", "class": "rogue",
                                "description": "a one-eared barkeep"}]},
    }})
    scene = [e for e in drain(gs._evt_q) if e.get("type") == "scene_request"]
    rec("scene_request emitted for the GM tool",
        len(scene) == 1 and scene[0]["description"] == "a forge at dusk" and scene[0]["kind"] == "story",
        str(scene))
    rec("scene_request carries kingdom/area/place + time/weather + characters",
        bool(scene) and scene[0].get("kingdom") == "Kingdom of Eldoria"
        and scene[0].get("area") == "Eldoria City"
        and scene[0].get("place") == ["Hask & Daughters Smithy", "the forge"]
        and scene[0].get("establishing") == "a hot forge"
        and scene[0].get("time_of_day") == "dusk" and scene[0].get("weather") == "light rain"
        and scene[0].get("main_npcs") == [{"name": "Gorson", "role": "the smith",
                                          "race": "dwarf", "class": "fighter",
                                          "description": "a soot-stained man"}]
        and scene[0].get("characters") == {"the smith": "hammering at the anvil"}
        and scene[0].get("npcs") == [{"name": "Maera", "race": "human", "class": "rogue",
                                      "description": "a one-eared barkeep"}])
    rec("legacy caption dropped from the scene event",
        bool(scene) and "caption" not in scene[0], str(scene))

    # A declared NPC's ROLE rides the scene result -- it is never primed in the tree.
    rec("the seed's main NPC identity is remembered (not primed)",
        gs._npc_people.get("gorson") == ("Gorson", "the smith", "dwarf", "fighter"),
        str(gs._npc_people))
    gs._scene_requested_turn = False
    await gs._execute_tool({"function": {
        "name": "request_scene_image",
        "arguments": {"description": "back at the forge",
                      "place": ["Hask & Daughters Smithy", "the forge"],
                      "characters": {"Gorson": "at the anvil", "three dockhands": "drinking"}},
    }})
    results = [e for e in drain(gs._evt_q) if e.get("type") == "tool_result"]
    note = (results[-1].get("gm_text") or "") if results else ""
    rec("the scene result echoes `on stage: Name (race class, role)` for a declared NPC",
        "on stage: Gorson (dwarf fighter, the smith)" in note, note[-160:])
    rec("an undeclared one-off gets no role echoed (it has none)",
        "three dockhands (" not in note, note[-160:])

    # The .player file is only a save snapshot (and drops active effects), so the engine
    # attaches the LIVE active effects + equipped gear to the scene event.
    class _LiveMCP(_FakeMCP):
        async def call_tool(self, name, arguments=None):
            if name == "dump_player_db":
                return _Result(json.dumps({
                    "active_effects": [{"name": "Disguise Self (active)",
                                         "description": "a human merchant, forgettable"}],
                    "equipped": {"armor": None, "worn": [], "hands": ["Dagger", None]},
                }))
            return _Result('{"status":"requested"}')

    gs_live = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_live.session = _LiveMCP()
    gs_live._scene_requested_turn = False
    await gs_live._execute_tool({"function": {
        "name": "request_scene_image",
        "arguments": {"description": "haggling", "place": ["Market"],
                      "establishing": "an open market square"},
    }})
    live = [e for e in drain(gs_live._evt_q) if e.get("type") == "scene_request"]
    rec("scene_request carries the LIVE active effects + equipped gear",
        bool(live) and live[0].get("active_effects") == [{"name": "Disguise Self (active)",
                                                         "description": "a human merchant, forgettable"}]
        and live[0].get("equipped") == {"armor": None, "worn": [], "hands": ["Dagger", None]},
        str(live))

    import dice_server as ds  # noqa: E402
    tools = await ds.mcp.list_tools()
    scene_tool = next((t for t in tools if t.name == "request_scene_image"), None)
    props = list((scene_tool.inputSchema or {}).get("properties", {})) if scene_tool else []
    rec("scene tool speaks names + declarations, not descriptions",
        props == ["description", "kingdom", "area", "place", "time_of_day",
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
        "npcs": [{"name": "Maera", "race": "human", "class": "rogue",
                  "description": "a one-eared barkeep"}]}}})
    rec("register_npcs is buffered for the next scene call",
        [p["name"] for p in gs_note._pending_npcs] == ["Maera"], str(gs_note._pending_npcs))
    await gs_note._execute_tool({"function": {"name": "request_scene_image", "arguments": {
        "description": "x", "place": ["L", "s"], "establishing": "a cold hall",
        "characters": {"Maera": "pouring", "the harbourmaster": "watching"}}}})
    note = [e for e in drain(gs_note._evt_q)
            if e.get("type") == "tool_result" and "NOTE:" in str(e.get("text") or "")]
    rec("an undeclared name gets a soft note (a declared one does not)",
        len(note) == 1 and 'NOTE: "the harbourmaster"' in note[0]["text"]
        and 'NOTE: "Maera"' not in note[0]["text"], str(note))

    # A declaration missing race/class is nudged on its own result -- no prefix cost.
    gs_id = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_id.session = _FakeMCP()
    await gs_id._execute_tool({"function": {"name": "register_npcs", "arguments": {
        "npcs": [{"name": "Corvin", "description": "a thin man"}]}}})
    idnote = [e for e in drain(gs_id._evt_q)
              if e.get("type") == "tool_result" and "NOTE:" in str(e.get("text") or "")]
    rec("a declared NPC without race/class is nudged",
        len(idnote) == 1 and "Corvin" in idnote[0]["text"]
        and "race and a class" in idnote[0]["text"], str(idnote))

    # New-place seed gate: a scene call with no establishing view is rejected with
    # a warning and no scene_request, then accepted once `establishing` is supplied.
    gs7 = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs7.session = _FakeMCP()
    gs7._scene_requested_turn = False
    await gs7._execute_tool({"function": {"name": "request_scene_image", "arguments": {
        "description": "x", "place": ["Brand New Hall", "the antechamber"]}}})
    evts7 = drain(gs7._evt_q)
    warns = [e for e in evts7 if e.get("type") == "tool_result" and e.get("is_error")]
    rec("a new place without establishing is rejected with a warning",
        len(warns) == 1 and "establishing" in warns[0]["text"]
        and not any(e.get("type") == "scene_request" for e in evts7), str(evts7))
    await gs7._execute_tool({"function": {"name": "request_scene_image", "arguments": {
        "description": "x", "place": ["Brand New Hall", "the antechamber"],
        "establishing": "a cold stone hall"}}})
    rec("the same place is accepted once establishing is supplied",
        any(e.get("type") == "scene_request" for e in drain(gs7._evt_q)))
    gs7._scene_requested_turn = False
    await gs7._execute_tool({"function": {"name": "request_scene_image", "arguments": {
        "description": "y", "place": ["Brand New Hall", "the antechamber"]}}})
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

    # The engine guarantees the turn's prose, not just the image: a model that skips
    # NARRATIVE (a pause-token echo, or an image attached alone) gets one non-quiet round.
    narr_calls: list[tuple] = []

    async def _narr_role(role_content, label, quiet=False):
        narr_calls.append((role_content, label, quiet))
        return "You wake in the dark."

    gs_n = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_n._run_role = _narr_role  # type: ignore[assignment]
    gs_n._narrative_emitted_turn = False
    out_n = await gs_n._ensure_narrative()
    rec("ensure_narrative recovers the missing prose (non-quiet)",
        out_n == "You wake in the dark." and narr_calls
        and narr_calls[-1][1] == "narrative-fix" and narr_calls[-1][2] is False,
        str(narr_calls))

    gs_n2 = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_n2._run_role = _narr_role  # type: ignore[assignment]
    gs_n2._narrative_emitted_turn = True
    narr_calls.clear()
    out_n2 = await gs_n2._ensure_narrative()
    rec("ensure_narrative is a no-op once prose exists", out_n2 == "" and not narr_calls)

    # The scene-fix nudge describes state only after checking it: with prose it says
    # "already written", with NO prose it asks for the narrative too and runs non-quiet.
    fix_calls: list[tuple] = []

    async def _fix_role(role_content, label, quiet=False):
        fix_calls.append((role_content, quiet))
        return ""

    gs_w = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_w._run_role = _fix_role  # type: ignore[assignment]
    gs_w._scene_requested_turn = False
    gs_w._narrative_emitted_turn = True  # the prose reached the player
    await gs_w._ensure_scene_image("You wake in the dark.")
    nudge_w = fix_calls[-1][0] if fix_calls else ""
    rec("with prose, the scene-fix nudge says do not repeat and stays quiet",
        "do not repeat" in nudge_w.lower() and bool(fix_calls) and fix_calls[-1][1] is True,
        nudge_w)

    fix_calls.clear()
    gs_w._scene_requested_turn = False
    gs_w._narrative_emitted_turn = False  # nothing reached the player
    await gs_w._ensure_scene_image("")
    nudge_n = fix_calls[-1][0] if fix_calls else ""
    rec("with no prose, the scene-fix nudge asks for the narrative AND the image",
        "no narrative yet" in nudge_n and "already written" not in nudge_n
        and "request_scene_image" in nudge_n, nudge_n)
    rec("... and that round is NOT quiet, so the prose reaches the player",
        bool(fix_calls) and fix_calls[-1][1] is False)

    # A model that only echoes the pause token must be bounded, then nudged.
    gs_r = GameSession(base_dir=REPO, model="test", scene_images=True)
    pause_rounds: list[str] = []

    async def _pause_stream(label, quiet=False, gm=None):
        pause_rounds.append(label)
        return ("{{_NEED_AN_OTHER_PROMPT}}", "", [], False)

    gs_r._stream_assistant = _pause_stream  # type: ignore[assignment]
    out_r = await gs_r._run_role("go", "turn")
    rec("a pause-token-only model is bounded",
        out_r == "" and len(pause_rounds) <= MAX_RESUME_ROUNDS * 3 + 1,
        f"rounds={len(pause_rounds)}")
    rec("the resume guard nudges the model to narrate",
        any("already paused" in (m.get("content") or "") for m in gs_r.messages))

    # The flag that means "prose reached the player" must not be set by a pause-token echo:
    # that is what defeated the narrative guarantee and left the opening scene unwritten.
    def _stream_of(text):
        async def _gen(messages, tools):
            yield {"type": "narrative_delta", "text": text}
            yield {"type": "done", "prompt_eval_count": 0}
        return _gen

    gs_f = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_f._stream = _stream_of("{{_NEED_ANOTHER_PROMPT}}")  # type: ignore[assignment]
    await gs_f._stream_assistant("turn")
    rec("a bare pause token is not prose", gs_f._narrative_emitted_turn is False)

    gs_f2 = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_f2._stream = _stream_of("You wake in the dark.")  # type: ignore[assignment]
    await gs_f2._stream_assistant("turn")
    rec("real prose sets the flag", gs_f2._narrative_emitted_turn is True)

    # The guarantee is the flag, never the returned string: a truthy placeholder is not prose.
    prose_calls: list[tuple] = []

    async def _prose_role(role_content, label, quiet=False):
        prose_calls.append((label, quiet))
        return "You wake in the dark."

    gs_t = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_t._run_role = _prose_role  # type: ignore[assignment]
    gs_t._narrative_emitted_turn = False
    out_t = await gs_t._ensure_turn_prose("The GM pauses, deep in thought...")
    rec("a placeholder is not prose: the turn gets its recovery round",
        out_t == "You wake in the dark." and prose_calls[-1][0] == "narrative-fix"
        and prose_calls[-1][1] is False, str(prose_calls))

    prose_calls.clear()
    gs_t2 = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_t2._run_role = _prose_role  # type: ignore[assignment]
    gs_t2._narrative_emitted_turn = True
    out_t2 = await gs_t2._ensure_turn_prose("You wake in the dark.")
    rec("with prose, the turn is left exactly as the model wrote it",
        out_t2 == "You wake in the dark." and not prose_calls)

    # At awakening the resume re-anchors the step a bare token leaves implicit.
    resume_labels: list[str] = []

    async def _pause_then_prose(label, quiet=False, gm=None):
        resume_labels.append(label)
        return (("{{_NEED_ANOTHER_PROMPT}}", "", [], False) if len(resume_labels) == 1
                else ("You wake in the dark.", "", [], False))

    gs_a = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_a._stream_assistant = _pause_then_prose  # type: ignore[assignment]
    out_a = await gs_a._run_role("ERA_FILE", "awakening")
    nudges = [m.get("content") for m in gs_a.messages
              if "CONTINUE_EXECUTION" in str(m.get("content"))]
    rec("the awakening resume re-anchors step 3, narrative and image together",
        out_a == "You wake in the dark." and bool(nudges)
        and "AWAKENING step 3" in nudges[-1] and "request_scene_image" in nudges[-1],
        str(nudges[-1:]))

    resume_labels.clear()
    gs_a2 = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_a2._stream_assistant = _pause_then_prose  # type: ignore[assignment]
    await gs_a2._run_role("an action", "turn")
    nudges2 = [m.get("content") for m in gs_a2.messages
               if "CONTINUE_EXECUTION" in str(m.get("content"))]
    rec("every other resume stays the bare token",
        bool(nudges2) and nudges2[-1] == "{{_CONTINUE_EXECUTION}}", str(nudges2[-1:]))

    # The whole chain that live play hit, through the REAL stream/flag/loop code: a
    # pause-token echo, an empty resume, then the recovery round that finally narrates.
    script = [("{{_NEED_ANOTHER_PROMPT}}", []),
              ("", []),
              ("You wake in the dark, and the Device ticks.",
               [{"function": {"name": "request_scene_image", "arguments": {
                   "description": "waking on a cold floor",
                   "place": ["The Pit", "the floor"],
                   "establishing": "a cold stone room"}}}])]

    def _stream_chain():
        async def _gen(messages, tools):
            text, calls = script.pop(0)
            if text:
                yield {"type": "narrative_delta", "text": text}
            if calls:
                yield {"type": "tool_calls", "calls": calls}
            yield {"type": "done", "prompt_eval_count": 0}
        return _gen

    gs_c = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs_c.session = _FakeMCP()
    gs_c._stream = _stream_chain()  # type: ignore[assignment]
    chain_out = await gs_c._run_role("ERA_FILE", "awakening")
    chain_out = await gs_c._ensure_turn_prose(chain_out)
    rec("the chain: pause echo -> empty resume -> the recovery narrates the opening",
        chain_out == "You wake in the dark, and the Device ticks."
        and gs_c._narrative_emitted_turn is True
        and any("did not write the turn's narrative" in str(m.get("content"))
                for m in gs_c.messages), repr(chain_out))
    rec("... and the opening illustration went with it",
        gs_c._scene_requested_turn is True)

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
        "Advance, never restart" in real_on and "Advance, never restart" in real_off)
    rec("gated scene wording only when scenes are on",
        "exactly ONE per narrative turn" in real_on
        and "exactly ONE per narrative turn" not in real_off)
    rec("the image call ends the turn (gated wording)",
        "ENDS the turn" in real_on and "ENDS the turn" not in real_off)
    rec("the GM is told mechanics are displayed, not transcribed (no tokens)",
        "already displayed by the engine" in real_on and "{{_MECHANICS}}" not in real_on
        and "already displayed by the engine" in real_off)
    rec("recovery is scoped to calls that never ran",
        "Only for a call that never ran" in real_on
        and "Only for a call that never ran" in real_off)
    rec("an inline patch no longer routes into RECOVERY",
        "Use RECOVERY only for a call you never made" in real_off
        and "Tool call appended to prose → use RECOVERY instead" not in real_off)
    rec("tool-free turns skip the pause token",
        "skip both the batch and the pause token" in real_off)

    # A narrative-phase tool call ends the turn: no second model round, so the
    # GM cannot re-narrate the whole turn (the duplicate-answer regression).
    gs5 = GameSession(base_dir=REPO, model="test", scene_images=True)
    rounds5: list[str] = []
    executed5: list[str] = []

    async def _exec5(tc):
        executed5.append((tc.get("function") or {}).get("name"))

    async def _stream5(label, quiet=False, gm=None):
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

    async def _stream8(label, quiet=False, gm=None):
        rounds8.append(label)
        if len(rounds8) == 1:
            return ("You push open the door.", "", [{"function": {
                "name": "request_scene_image", "arguments": {
                    "description": "entering", "place": ["New Hall", "the door"]}}}], False)
        return ("You step into the cold hall.", "", [{"function": {
            "name": "request_scene_image", "arguments": {
                "description": "entering", "place": ["New Hall", "the door"],
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

    async def _stream6(label, quiet=False, gm=None):
        rounds6.append(label)
        if len(rounds6) == 1:
            return ("", "", [{"function": {"name": "roll_dice", "arguments": {}}}], False)
        return ("The blade bites deep.", "", [], False)

    gs6._execute_tool = _exec6  # type: ignore[assignment]
    gs6._stream_assistant = _stream6  # type: ignore[assignment]
    out6 = await gs6._chat_with_tools("turn")
    rec("mechanical tool rounds still loop to the narrative",
        len(rounds6) == 2 and out6 == "The blade bites deep.", f"rounds={rounds6}")

    # An accepted image call with NO prose must not be re-prompted with a bare tool
    # result: the engine asks for the missing narration once.
    gs9 = GameSession(base_dir=REPO, model="test", scene_images=True)
    rounds9: list[str] = []

    async def _exec9(tc):
        pass

    async def _stream9(label, quiet=False, gm=None):
        rounds9.append(label)
        if len(rounds9) == 1:
            return ("", "", [{"function": {"name": "request_scene_image",
                                     "arguments": {"description": "a beat"}}}], False)
        return ("You fasten the collar and speak.", "", [], False)

    gs9._execute_tool = _exec9  # type: ignore[assignment]
    gs9._stream_assistant = _stream9  # type: ignore[assignment]
    out9 = await gs9._chat_with_tools("turn")
    rec("image-only accepted round recovers the missing prose",
        len(rounds9) == 2 and out9 == "You fasten the collar and speak.", f"rounds={rounds9}")
    rec("the recovery nudge is an engine control message",
        any(m.get("role") == "user"
            and "attached the illustration but wrote no prose" in (m.get("content") or "")
            for m in gs9.messages))

    # The quiet corrective round is image-only by design: it must not recover or loop.
    gs10 = GameSession(base_dir=REPO, model="test", scene_images=True)
    rounds10: list[str] = []

    async def _exec10(tc):
        pass

    async def _stream10(label, quiet=False, gm=None):
        rounds10.append(label)
        return ("", "", [{"function": {"name": "request_scene_image",
                              "arguments": {"description": "a beat"}}}], False)

    gs10._execute_tool = _exec10  # type: ignore[assignment]
    gs10._stream_assistant = _stream10  # type: ignore[assignment]
    out10 = await gs10._chat_with_tools("scene-fix", quiet=True)
    rec("quiet corrective image round neither recovers nor loops",
        len(rounds10) == 1 and out10 == "", f"rounds={rounds10}")

    # The recovery is bounded: a second image-only round ends the turn with no prose.
    gs11 = GameSession(base_dir=REPO, model="test", scene_images=True)
    rounds11: list[str] = []

    async def _exec11(tc):
        pass

    async def _stream11(label, quiet=False, gm=None):
        rounds11.append(label)
        return ("", "", [{"function": {"name": "request_scene_image",
                              "arguments": {"description": "a beat"}}}], False)

    gs11._execute_tool = _exec11  # type: ignore[assignment]
    gs11._stream_assistant = _stream11  # type: ignore[assignment]
    out11 = await gs11._chat_with_tools("turn")
    rec("image-only without prose recovers at most once",
        len(rounds11) == 2 and out11 == "", f"rounds={rounds11}")

    # A mechanical tool appended to prose must not trigger another model round: that
    # re-prompt is what makes the GM re-narrate the whole turn (duplicate segment).
    gs12 = GameSession(base_dir=REPO, model="test", scene_images=True)
    rounds12: list[str] = []

    async def _exec12(tc):
        pass

    async def _stream12(label, quiet=False, gm=None):
        rounds12.append(label)
        if len(rounds12) == 1:
            return ("You set the strongbox on the table.", "", [
                {"function": {"name": "roll_dice", "arguments": {}}},
                {"function": {"name": "update_player_list", "arguments": {}}}], False)
        return ("DUPLICATE NARRATIVE", "", [], False)

    gs12._execute_tool = _exec12  # type: ignore[assignment]
    gs12._stream_assistant = _stream12  # type: ignore[assignment]
    out12 = await gs12._chat_with_tools("turn")
    rec("prose + mechanical tools ends the turn (no re-prompt)",
        len(rounds12) == 1 and out12 == "You set the strongbox on the table.", f"rounds={rounds12}")

    # Prose + a scene tool + a mechanical tool in one round is terminal too.
    gs13 = GameSession(base_dir=REPO, model="test", scene_images=True)
    rounds13: list[str] = []

    async def _exec13(tc):
        pass

    async def _stream13(label, quiet=False, gm=None):
        rounds13.append(label)
        if len(rounds13) == 1:
            return ("The lock gives with a click.", "", [
                {"function": {"name": "request_scene_image", "arguments": {"description": "a room"}}},
                {"function": {"name": "modify_player_numeric", "arguments": {}}}], False)
        return ("DUPLICATE NARRATIVE", "", [], False)

    gs13._execute_tool = _exec13  # type: ignore[assignment]
    gs13._stream_assistant = _stream13  # type: ignore[assignment]
    out13 = await gs13._chat_with_tools("turn")
    rec("prose + scene + mechanical tools ends the turn (no re-prompt)",
        len(rounds13) == 1 and out13 == "The lock gives with a click.", f"rounds={rounds13}")

    # After prose has been shown, a rejected-scene re-call must NOT trigger the
    # prose-recovery nudge: the turn's narrative already exists.
    gs14 = GameSession(base_dir=REPO, model="test", scene_images=True)
    gs14.session = _FakeMCP()
    gs14._scene_requested_turn = False
    rounds14: list[str] = []

    async def _stream14(label, quiet=False, gm=None):
        rounds14.append(label)
        if len(rounds14) == 1:
            gs14._narrative_emitted_turn = True  # the real _stream_assistant does this
            return ("You push open the door.", "", [{"function": {
                "name": "request_scene_image", "arguments": {
                    "description": "entering", "place": ["Cold Hall", "the door"]}}}], False)
        return ("", "", [{"function": {
            "name": "request_scene_image", "arguments": {
                "description": "entering", "place": ["Cold Hall", "the door"],
                "establishing": "a cold stone hall"}}}], False)

    gs14._stream_assistant = _stream14  # type: ignore[assignment]
    out14 = await gs14._chat_with_tools("turn")
    rec("prose then tool-only corrected scene: no second narrative, no recovery nudge",
        len(rounds14) == 2 and out14 == ""
        and not any("attached the illustration but wrote no prose" in (m.get("content") or "")
                    for m in gs14.messages), f"rounds={rounds14}")

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

    # A failed action: its narrative_format must reach the mechanics event, and a tool-shape
    # error (no narrative_format) must produce no mechanics line at all.
    class _FailMCP:
        async def call_tool(self, name, arguments=None):
            return _Result(json.dumps({
                "success": False,
                "error": "No level 1 spell slots remaining to cast Disguise Self.",
                "narrative_format": "Senna Disguise Self — not cast: no 1st-level slot remaining",
                "hint": "Take a long rest."}))

    class _ShapeErrMCP:
        async def call_tool(self, name, arguments=None):
            return _Result(json.dumps({
                "success": False, "error": "attack_modifier_required",
                "reason": "Pass attack_modifier, or weapon=/attack=."}))

    gs_fail = GameSession(base_dir=REPO, model="test", scene_images=False)
    gs_fail.session = _FailMCP()
    await gs_fail._execute_tool({"function": {"name": "resolve_magic",
                                              "arguments": {"spell_name": "Disguise Self"}}})
    fail_view = drain(gs_fail._evt_q)
    fail_mech = [e for e in fail_view if e.get("type") == "mechanics"]
    fail_gm = gs_fail.messages[-1]["content"]
    rec("a failed action's narrative_format reaches the mechanics event",
        len(fail_mech) == 1
        and fail_mech[0].get("lines") == ["Senna Disguise Self — not cast: no 1st-level slot remaining"],
        str(fail_mech))
    rec("the failure's narrative_format is still dropped from the GM view",
        "narrative_format" not in fail_gm and "No level 1 spell slots" in fail_gm, fail_gm)

    gs_shape = GameSession(base_dir=REPO, model="test", scene_images=False)
    gs_shape.session = _ShapeErrMCP()
    await gs_shape._execute_tool({"function": {"name": "resolve_attack",
                                               "arguments": {"actor": "x"}}})
    rec("a tool-shape error produces no mechanics line",
        not [e for e in drain(gs_shape._evt_q) if e.get("type") == "mechanics"])

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
