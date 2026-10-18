"""Text-only place memory: `note_place`, the file-less registry and priming.

No network. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_places.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import GameSession  # noqa: E402
from web.protocol import PLACE_ECHO_NUDGE, filter_tools, render_protocol  # noqa: E402
from web.places import (  # noqa: E402
    KNOWN_PLACES_HEADER, KNOWN_PLACES_HEADER_TEXT, KNOWN_PLACES_EMPTY_TEXT, _npc_state,
    _scene_key, path_label, places_feed, places_query, render_places_answer,
    resolve_place_path,
)
from web.scenes import (  # noqa: E402
    SceneService, known_scene_places, known_npc_names, live_scene_manifest,
    commit_scene_manifest, discard_scene_manifest, current_scene_place,
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
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        return _Result(
            '{"status":"noted","kingdom":"London","area":"Wapping",'
            '"place":["Ropehaven Wharf","Warehouse Nine","the counting office"],'
            '"main_npcs":[{"name":"Bess","role":"the lamp-lighter"}],'
            '"cast":[{"name":"Corvin Hale","role":"the bounty hunter"}],'
            '"note":"The place is remembered.","extra":"drop me"}')


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def jpeg(w, h):
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 25, 20)).save(buf, format="JPEG")
    return buf.getvalue()


ERA = "victorian"
PLACE = ["Ropehaven Wharf", "Warehouse Nine", "the counting office"]
MAIN = [{"name": "Maera", "role": "the harbourmaster",
         "race": "dwarf", "class": "rogue"}]
CAST = [{"name": "Corvin Hale", "role": "the bounty hunter"}]


async def main() -> bool:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "output"
        svc = SceneService(out)

        # ── the writer: a file-less, name+role place ──────────────────────────
        res = svc.record_place("save", era=ERA, kingdom="London", area="Wapping",
                               place=PLACE, main_npcs=MAIN, cast=CAST)
        rec("record_place reports the place recorded", res.get("recorded") is True, str(res))
        rec("record_place stores no image file", res.get("file") is None, str(res))

        places = known_scene_places(out, "save", ERA)
        rec("known_scene_places reads the text-only entry",
            len(places) == 1 and places[0]["place"] == PLACE
            and places[0]["kingdom"] == "London" and places[0]["area"] == "Wapping", str(places))
        rec("the place carries its regulars by name + role + race + class",
            places[0]["main_npcs"] == [{"name": "Maera", "description": "",
                                        "role": "the harbourmaster",
                                        "race": "dwarf", "class": "rogue", "state": ""}], str(places))
        cur = current_scene_place(out, "save")
        rec("current_scene_place reports the last declared place",
            bool(cur) and cur["area"] == "Wapping" and cur["place"] == PLACE, str(cur))

        # The caption falls back to the rolled arrival when no place is declared yet (a jump).
        cap = GameSession(base_dir=Path(td), model="test", scene_images=False)
        cap.era = "wallachia"
        cap.arrival = "the princely court at Târgoviște"
        cap._current_place = None
        rec("the caption falls back to the rolled arrival before a place is declared",
            cap._caption_text() == "Wallachia · the princely court at Târgoviște",
            cap._caption_text())
        rec("names are era-scoped",
            known_scene_places(out, "save", "tang") == [], "not empty in tang")
        rec("known_npc_names includes the place's main NPC",
            "maera" in known_npc_names(out, "save"), str(known_npc_names(out, "save")))

        # Re-recording updates names/roles without touching an existing image file.
        live = live_scene_manifest(out, "save")
        key = next(iter(live["seeds"]))
        live["seeds"][key]["file"] = "seed-warehouse.png"
        live["seeds"][key]["slug"] = "seed-warehouse"
        live["seeds"][key]["style"] = "old"
        svc.record_place("save", era=ERA, kingdom="London", area="Wapping", place=PLACE,
                         main_npcs=[{"name": "Maera", "role": "the wharf-mistress"}],
                         cast=CAST)
        after = known_scene_places(out, "save", ERA)[0]
        raw = live_scene_manifest(out, "save")["seeds"][key]
        rec("re-recording keeps an existing image seed's file/slug",
            raw.get("file") == "seed-warehouse.png" and raw.get("slug") == "seed-warehouse",
            str(raw))
        rec("re-recording updates the regular's role",
            after["main_npcs"][0]["role"] == "the wharf-mistress", str(after))

        # ── the tool list + protocol are gated by images ──────────────────────
        tools = [{"function": {"name": n}} for n in
                 ("note_place", "request_scene_image", "register_npcs", "roll_dice")]
        offered_off = {t["function"]["name"] for t in filter_tools(tools, False)}
        offered_on = {t["function"]["name"] for t in filter_tools(tools, True)}
        rec("note_place is offered only with images off",
            "note_place" in offered_off and "note_place" not in offered_on,
            f"off={offered_off} on={offered_on}")
        rec("scene + npc tools are hidden with images off",
            "request_scene_image" not in offered_off and "register_npcs" not in offered_off,
            str(offered_off))

        proto = "A\n<!-- TEXT:ON -->\nPLACE RULES\n<!-- TEXT:END -->\nB"
        on = render_protocol(proto, True)
        off = render_protocol(proto, False)
        rec("protocol keeps the text-block rules when images are off",
            "PLACE RULES" in off and "TEXT:" not in off, repr(off))
        rec("protocol drops the text-block rules when images are on",
            "PLACE RULES" not in on and "TEXT:" not in on, repr(on))
        rec("the real protocol carries a note_place rule only with images off",
            "note_place" in render_protocol(
                (REPO / "GameMaster_MCP.md").read_text(encoding="utf-8"), False)
            and "note_place" not in render_protocol(
                (REPO / "GameMaster_MCP.md").read_text(encoding="utf-8"), True))

        # ── engine: note_place updates live state + emits place_note ──────────
        gs = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gs.session = _FakeMCP()
        gs.active_name = "save"
        gs.era = ERA
        await gs._execute_tool({"function": {"name": "note_place", "arguments": {
            "kingdom": "London", "area": "Wapping", "place": PLACE,
            "main_npcs": [{"name": "Bess", "role": "the lamp-lighter",
                           "race": "halfling", "class": "rogue"}],
            "cast": CAST,
        }}})
        evts = drain(gs._evt_q)
        notes = [e for e in evts if e.get("type") == "place_note"]
        rec("note_place emits one place_note event",
            len(notes) == 1 and notes[0]["place"] == PLACE
            and notes[0]["era"] == ERA, str(notes))
        caps = [e for e in evts if e.get("type") == "caption"]
        rec("note_place also emits the scene caption (era + place)",
            bool(caps) and str(caps[-1].get("text", "")).startswith(
                "Victorian Britain · Wapping · Ropehaven Wharf")
            and "time_of_day" not in str(caps[-1]),
            str(caps[-1] if caps else None))
        rec("note_place updates the live place map",
            gs._scene_places.get(tuple(s.lower().replace(" ", "-") for s in PLACE))
            == ("London", "Wapping"), str(gs._scene_places))
        rec("note_place adds the regular's race/class/role to the live identity map",
            gs._npc_people.get("bess") == ("Bess", "the lamp-lighter", "halfling", "rogue"),
            str(gs._npc_people))
        rec("... and the per-turn tree shows it",
            "Bess — the lamp-lighter, halfling rogue" in gs._places_body(), gs._places_body())
        rec("note_place remembers the cast name",
            "corvin hale" in gs._cast_names, str(gs._cast_names))
        # The GM made the declaration: the ack is its acceptance and nothing else. The path,
        # address and regulars are what it just sent; any correction rides a NOTE line AFTER
        # the JSON (here, the cast's missing race/class), so the result is read as the JSON
        # up to the first blank line.
        ack = gs.messages[-1]["content"].split("\n\n", 1)[0]
        rec("the place ack is the bare acceptance (no echo of the declaration)",
            json.loads(ack) == {"status": "noted"}, gs.messages[-1]["content"])

        # The `characters` key is the model's own spelling of a name, and live 2026-10-16 it
        # keyed two declared names with an underscore (`Master_Guo` for the declared `Master
        # Guo`; the same call passed `npcs: ["Master Guo"]`). The exact `name.lower()` check
        # then told the GM, in the result of the call that declared them, that those names
        # were undeclared. Folding the separators fixes the declaration check.
        note = gs._undeclared_note({"characters": {"Corvin_Hale": "at the door",
                                                   "a stray dog": "in the alley"}})
        rec("an underscore-keyed declared name is not reported as undeclared",
            "Corvin_Hale" not in note and "a stray dog" in note, note)
        rec("... and the cast's identity rides the on-demand footer, not a late echo",
            gs._cast_labels() == ["Corvin Hale — the bounty hunter"], str(gs._cast_labels()))

        # ── the path resolver itself ─────────────────────────────────────────
        # `the`/`a`/`an` carry no identity, a known path may appear as the TAIL of a longer
        # declared one (the model nesting a place it knows under a parent it invented), a new
        # room inside a known building stays NEW, and two equal matches resolve to nothing.
        registry = [{"place": ["the Western Market", "the House of Guo"]},
                    {"place": ["the Ximing Monastery", "the gate court"]}]
        cases = [
            (["Western Market", "the House of Guo"],
             ["the Western Market", "the House of Guo"]),
            (["western  market", "The House Of Guo"],
             ["the Western Market", "the House of Guo"]),
            (["Western Market", "the Ximing Monastery", "the gate court"],
             ["the Ximing Monastery", "the gate court"]),
            (["the Western Market", "the House of Guo", "the back room"], None),
            (["a brand new place", "somewhere else"], None),
        ]
        for place, want in cases:
            rec(f"resolve_place_path({path_label(place)})",
                resolve_place_path(registry, place) == want,
                str(resolve_place_path(registry, place)))
        rec("two equal matches resolve to nothing",
            resolve_place_path(registry + [{"place": ["Western Market", "the house of guo"]}],
                               ["western market", "the House of Guo"]) is None)

        # ── the movement declaration: the GM's return to a known place ────────
        # The doc used to say "declare a place once, on entering a NEW one", so a return said
        # nothing and the engine kept believing the story was still where it had last declared
        # it: a stale caption, a stale arrival, and a death attributed to the wrong place.
        svc.record_place("moving", era=ERA, kingdom="London", area="Wapping",
                         place=PLACE, main_npcs=MAIN)
        limehouse = ["Ratcliffe Highway", "the opium den"]
        svc.record_place("moving", era=ERA, kingdom="London", area="Limehouse",
                         place=limehouse, main_npcs=[])
        gsm = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gsm.session = _FakeMCP()
        gsm.active_name = "moving"
        gsm.era = ERA
        gsm._reload_era_scene_state()
        gsm.messages.insert(0, {"role": "system", "content": "the protocol"})
        gsm._places_at = 1
        gsm.messages.insert(1, {"role": "system", "content": gsm._places_body()})
        before = gsm.messages[gsm._places_at]["content"]
        rec("the block scopes to the place the save last stood in",
            "the opium den · ← you are here" in before
            and "        - Ropehaven Wharf" not in before
            and "London — Wapping: Ropehaven Wharf — Warehouse Nine" in before, before[-260:])

        await gsm._execute_tool({"function": {"name": "note_place", "arguments": {
            "kingdom": "London", "area": "Wapping", "place": PLACE,
            "main_npcs": [{"name": "Bess", "role": "the lamp-lighter",
                           "race": "halfling", "class": "rogue"}]}}})
        evts2 = drain(gsm._evt_q)
        caps2 = [e for e in evts2 if e.get("type") == "caption"]
        rec("a movement declaration moves the current place",
            (gsm._current_place or {}).get("area") == "Wapping"
            and (gsm._current_place or {}).get("place") == PLACE, str(gsm._current_place))
        rec("... and the caption follows it",
            bool(caps2) and "Wapping" in str(caps2[-1].get("text")), str(caps2[-1:] or []))
        after = gsm.messages[gsm._places_at]["content"]
        rec("... and the primed block is rewritten in place",
            after != before and f"{PLACE[-1]} · ← you are here" in after
            and f"{limehouse[-1]} · ← you are here" not in after
            and "London — Limehouse: Ratcliffe Highway — the opium den" in after, after[-260:])
        rec("... with the area just left kept as an exact anchor",
            "the opium den" in after and "London — Limehouse:" in after, after[-260:])
        rec("... and the place returned to is the newest in the live ordering",
            max(int(p.get("used") or 0) for p in gsm._primed)
            == int([p["used"] for p in gsm._primed if p["place"] == PLACE][0]),
            str([(p["area"], p["used"]) for p in gsm._primed]))

        # ── a wrong area is put back where the record holds the place ─────────
        # Live, the model given only an area name invented a path AND filed it under the area
        # it was standing in. The registry is the authority when the first path segment names
        # exactly one known place; the GM is told, so it reuses the exact area next time.
        await gsm._execute_tool({"function": {"name": "note_place", "arguments": {
            "kingdom": "London", "area": "Wapping",
            "place": ["Ratcliffe Highway", "a new den"]}}})
        rec("a place the record holds elsewhere is filed back into its area",
            (gsm._current_place or {}).get("area") == "Limehouse", str(gsm._current_place))
        told = [m for m in gsm.messages if m.get("name") == "note_place"][-1]["content"]
        rec("... and the GM is told the area it was filed under",
            "NOTE:" in told and "Limehouse" in told, told[-180:])

        svc.record_place("moving", era=ERA, kingdom="London", area="Wapping",
                         place=["Ratcliffe Highway", "the other den"], main_npcs=[])
        gsm._reload_era_scene_state()
        await gsm._execute_tool({"function": {"name": "note_place", "arguments": {
            "kingdom": "London", "area": "Wapping",
            "place": ["Ratcliffe Highway", "a second den"]}}})
        rec("an ambiguous first segment leaves the GM's own area alone",
            (gsm._current_place or {}).get("area") == "Wapping", str(gsm._current_place))

        # The same rule fixes a drifted KINGDOM when the area already agrees (the live model
        # wrote "Eldoria" for "Kingdom of Eldoria" on a return it had resolved correctly).
        await gsm._execute_tool({"function": {"name": "note_place", "arguments": {
            "kingdom": "Eldoria", "area": "Wapping", "place": PLACE}}})
        rec("a drifted kingdom is taken from the record",
            (gsm._current_place or {}).get("kingdom") == "London", str(gsm._current_place))

        # ── a drifted PATH keeps the registry's identity ──────────────────────
        # Live, the model declared ['Western Market', 'the House of Guo'] for a save holding
        # 'the Western Market' -- one dropped article filed the place a second time, and the
        # player's caption lost the article too. The registry's spelling is adopted instead.
        before_count = len(gsm._primed)
        drifted = ["Ratcliffe Highway", "opium den"]
        await gsm._execute_tool({"function": {"name": "note_place", "arguments": {
            "kingdom": "London", "area": "Limehouse", "place": drifted}}})
        rec("a dropped article is adopted as the registry spells the place",
            len(gsm._primed) == before_count and (gsm._current_place or {}).get("place") == limehouse
            and sum(1 for p in gsm._primed if p["place"] == limehouse) == 1,
            str(gsm._current_place))
        told = [m for m in gsm.messages if m.get("name") == "note_place"][-1]["content"]
        rec("... and the GM is told the exact saved path",
            "held in the registry as" in told and "the opium den" in told, told[-200:])

        # A MISSING area is the same decision as a wrong one. Live 2026-10-16 the model
        # declared a NEW path below a known district with no kingdom/area at all (`the Western
        # Market / the dye-shops lane / the dyer's yard`, the shapes the tool calls for once
        # the place exists): the engine kept neither, so the player's caption lost its
        # settlement and the KNOWN PLACES block lost its scoping -- an empty area matches
        # EVERY place (`places._in_area`), which is the whole-era tree the block exists to
        # avoid. The record's first segment names exactly one address, so it decides.
        no_area = PLACE[:1] + ["a lane off the wharf", "a yard never entered"]
        await gsm._execute_tool({"function": {"name": "note_place",
                                             "arguments": {"place": no_area}}})
        rec("a missing area is taken from the record's first segment",
            (gsm._current_place or {}).get("area") == "Wapping"
            and (gsm._current_place or {}).get("kingdom") == "London",
            str(gsm._current_place))
        told = [m for m in gsm.messages if m.get("name") == "note_place"][-1]["content"]
        rec("... and the GM is told the address it was filed at",
            "NOTE:" in told and "Wapping" in told and '"not in ""' not in told, told[-200:])

        # A scene call resolved the same way cannot look like a new place: without this the
        # drifted key missed `_scene_places` and the engine rejected the call as unseeded.
        gsd = GameSession(base_dir=Path(td), model="test", scene_images=True)
        gsd.session = _FakeMCP()
        gsd.active_name = "moving"
        gsd.era = ERA
        gsd._reload_era_scene_state()
        await gsd._execute_tool({"function": {"name": "request_scene_image", "arguments": {
            "description": "the protagonist comes back to the strongroom",
            "establishing": "a cramped counting office, ledgers on every shelf",
            "place": ["Ropehaven Wharf", "Warehouse Nine", "counting office"]}}})
        rec("a scene call with a drifted path keeps the saved identity",
            (gsd._current_place or {}).get("place") == PLACE and not gsd._scene_rejected,
            str(gsd._current_place))

        # ── a new room inside a known building keeps its settlement ───────────
        # kingdom/area are optional once a place is known, so a scene call that starts a seed
        # for a room (with `establishing`) and omits them used to lose the settlement: a
        # caption without its area, and a shortened arrival written into the player file.
        room = PLACE + ["the strongroom"]
        gsi = GameSession(base_dir=Path(td), model="test", scene_images=True)
        gsi.session = _FakeMCP()
        gsi.active_name = "moving"
        gsi.era = ERA
        gsi._reload_era_scene_state()
        await gsi._execute_tool({"function": {"name": "request_scene_image", "arguments": {
            "description": "the protagonist forces the strongroom door", "place": room,
            "establishing": "a stone strongroom, shelves of ledgers"}}})
        rec("a room inside a known building inherits its settlement",
            (gsi._current_place or {}).get("area") == "Wapping", str(gsi._current_place))
        rec("... and its arrival keeps the settlement",
            gsi.arrival == "Ropehaven Wharf, in Wapping", gsi.arrival)

        # The guard itself: an unresolved settlement must not shorten an arrival already held.
        gsp = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gsp.session = _FakeMCP()
        gsp.era = ERA
        gsp.arrival = "Ropehaven Wharf, in Wapping"
        await gsp._pin_arrival(PLACE, "")
        rec("an unresolved settlement never degrades a specific arrival",
            gsp.arrival == "Ropehaven Wharf, in Wapping"
            and not [c for c in gsp.session.calls if c[0] == "set_player_field"],
            f"{gsp.arrival} / {gsp.session.calls}")

        # ── the priming body is mode-aware ────────────────────────────────────
        gs._primed = known_scene_places(out, "save", ERA)
        gs.scene_images = False
        body_off = gs._places_body()
        gs.scene_images = True
        body_on = gs._places_body()
        rec("images-off priming tells the GM to use note_place",
            KNOWN_PLACES_HEADER_TEXT in body_off and "note_place" in body_off, body_off)
        rec("images-on priming keeps the establishing wording",
            KNOWN_PLACES_HEADER in body_on and "establishing" in body_on, body_on)
        rec("empty text-only priming names note_place",
            KNOWN_PLACES_EMPTY_TEXT in GameSession(base_dir=Path(td), model="t",
                                                   scene_images=False)._places_body())

        # ── a classic session reads the era-less registry ─────────────────────
        svc.record_place("classic", era="", kingdom="Eldoria", area="Eldoria City",
                         place=["Ropehaven Wharf", "The Drowned Lantern"], main_npcs=MAIN)
        gsc = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gsc.active_name = "classic"
        gsc.classic = True
        gsc._reload_era_scene_state()
        rec("a classic session primes an era-less place",
            any(p["place"] == ["Ropehaven Wharf", "The Drowned Lantern"]
                for p in gsc._primed), str(gsc._primed))

        # ── the place menu: visited only, resolved to the real address ────────
        gsj = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gsj.active_name = "save"
        gsj.era = "egypt"  # the era the Traveller stands in is never a destination
        label = "Ropehaven Wharf — Warehouse Nine — the counting office"
        areas = gsj._place_options().get(ERA, {}).get("areas", [])
        rec("the place menu groups by area, only the places already visited",
            len(areas) == 1 and areas[0]["area"] == "Wapping"
            and label in areas[0]["places"], str(areas))
        landing = gsj._landing_place(ERA, "Wapping", label)
        rec("a chosen visited place resolves to its real address",
            isinstance(landing, dict) and landing.get("place") == PLACE
            and landing.get("area") == "Wapping" and landing.get("kingdom") == "London",
            str(landing))
        rec("an unnamed or unseen place resolves to nothing -- the GM lands you",
            gsj._landing_place(ERA, "Wapping", "") is None
            and gsj._landing_place("egypt", "Wapping", label) is None)

        # ── ensure_scene redraws a file-less seed ─────────────────────────────
        scc = SceneService(out)
        scc.generate = (lambda prompt, aspect_ratio=None, refs=None,
                        ref_media_resolution=None, model=None, thinking_level=None,
                        seed=None: jpeg(800, 450))  # type: ignore[assignment]
        r = scc.ensure_scene("save", {"name": "Bess", "race": "Human",
                                       "character_class": "Rogue", "level": 3},
                             "world", description="the lamps gutter",
                             kingdom="London", area="Wapping", place=PLACE)
        rec("turning images on draws a text-only place's missing seed",
            r["seed_created"] is True, str(r))
        seeded = live_scene_manifest(out, "save")["seeds"][key]
        rec("the drawn seed keeps the text-declared regulars",
            seeded.get("file") and any(n["name"] == "Maera" for n in seeded.get("main_npcs", [])),
            str(seeded))

        # ── the registry is memory-only until Save ─────────────────────────────
        out2 = Path(td) / "persist"
        svc2 = SceneService(out2)
        svc2.record_place("hero", era=ERA, kingdom="London", area="Wapping", place=PLACE)
        manifest2 = out2 / "images" / "hero" / "scenes" / "manifest.json"
        rec("a recorded place is live in memory",
            len(known_scene_places(out2, "hero", ERA)) == 1)
        rec("... but nothing reaches disk until Save", not manifest2.exists())

        # an orphan seed (drawn but never saved) is pruned on commit; a listed one is kept
        scenes2 = out2 / "images" / "hero" / "scenes"
        scenes2.mkdir(parents=True, exist_ok=True)
        (scenes2 / "seed-orphan.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
        live = live_scene_manifest(out2, "hero")
        next(iter(live["seeds"].values()))["file"] = "seed-kept.png"
        (scenes2 / "seed-kept.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
        commit_scene_manifest(out2, "hero")
        rec("a Save writes the live registry to disk", manifest2.exists())
        rec("... and prunes scene images the registry does not list",
            not (scenes2 / "seed-orphan.png").exists() and (scenes2 / "seed-kept.png").exists())

        # a place declared AFTER the save is dropped when the next session starts
        svc2.record_place("hero", era=ERA, kingdom="Paris", area="Le Marais",
                          place=["the Rue des Rosiers"])
        rec("a post-save place is live in this session",
            len(known_scene_places(out2, "hero")) == 2)
        discard_scene_manifest(out2, "hero")  # a new session starts
        rec("... and is gone after the session restarts (it was never saved)",
            [p["area"] for p in known_scene_places(out2, "hero", ERA)] == ["Wapping"])

        # ── the on-demand place record: `lookup('places')` / `lookup('places: X')` ─────
        # The block the GM reads every turn is scoped to one area and capped: a reminder,
        # not the record. Everything it cannot hold is served on demand from the same live
        # tree, so a place the block cannot show is read instead of invented. Model-free:
        # the grammar, the shapes, and the engine's interception are all asserted here.
        limehouse = ["Ratcliffe Highway", "the opium den"]
        record = [
            {"kingdom": "London", "area": "Wapping", "place": PLACE, "used": 30,
             "main_npcs": [{"name": "Maera", "role": "the harbourmaster", "race": "dwarf",
                            "class": "rogue", "description": ""}], "dead": ["Maera"]},
            {"kingdom": "London", "area": "Wapping", "place": ["Ropehaven Wharf", "the crane"],
             "used": 20, "main_npcs": [], "dead": []},
            {"kingdom": "London", "area": "Limehouse", "place": limehouse, "used": 10,
             "main_npcs": [{"name": "Bess", "role": "the lamp-lighter", "race": "halfling",
                            "class": "rogue", "description": ""}], "dead": []},
            {"kingdom": "Kingdom of Eldoria", "area": "Eldoria City",
             "place": ["Hask's Smithy", "the forge"], "used": 5, "main_npcs": [], "dead": []},
            # A kingdom whose name is also a settlement's: the answer must label both.
            {"kingdom": "Wapping", "area": "the old town", "place": ["the quay"],
             "used": 1, "main_npcs": [], "dead": []},
            # ... and one settlement name standing in two kingdoms: each labelled.
            {"kingdom": "Paris", "area": "Wapping", "place": ["a cellar"], "used": 1,
             "main_npcs": [], "dead": []},
        ]
        here = {"kingdom": "London", "area": "Wapping", "place": ["Ropehaven Wharf", "the crane"]}
    
        rec("the places grammar takes the bare word and a colon or space scope",
            places_query("places") == ("map", "") and places_query("  PLACES  ") == ("map", "")
            and places_query("places:") == ("map", "")
            and places_query("places: the lower town") == ("scope", "the lower town")
            and places_query("places the lower town") == ("scope", "the lower town")
            and places_query("places: 'Wapping'") == ("scope", "Wapping"),
            str([places_query(q) for q in
                 ("places", "PLACES", "places:", "places: the lower town", "places X")]))
        rec("... and never shadows the era or polity queries the same tool serves",
            all(places_query(q) is None for q in
                ("here", "tang", "ref:update_player_list", "Wapping", "", None)),
            str([places_query(q) for q in ("here", "tang", "ref:update_player_list", "Wapping")]))
    
        atlas = render_places_answer(record, here, "places")
        rec("the map lists every kingdom and settlement, with a count each",
            atlas.startswith("PLACES YOU KNOW") and "- London" in atlas
            and "- Wapping: 2 places" in atlas and "- Limehouse: 1 place" in atlas
            and "- Kingdom of Eldoria" in atlas and "- Wapping\n    - the old town: 1 place"
            in atlas, atlas)
        rec("... and marks the area the story is in",
            "- Wapping: 2 places · ← you are here" in atlas, atlas)
    
        area = render_places_answer(record, here, "places: Wapping")
        rec("a settlement scope names that area's places, the current one marked",
            'PLACES IN "Wapping" (London)' in area
            and path_label(PLACE) in area and "Ropehaven Wharf — the crane · ← you are here" in area
            and "Ratcliffe Highway" not in area, area)
        rec("... and gives the regulars the identity the per-turn tree caps but shares",
            "NPC: Maera — the harbourmaster, dwarf rogue (dead)" in area, area)
        block = places_feed(record, here, False)
        rec("the per-turn tree carries the same identity and state",
            "Maera — the harbourmaster, dwarf rogue (dead)" in block, block)
    
        kingdom = render_places_answer(record, here, "places: london")
        rec("a kingdom scope lists its settlements and their places",
            'PLACES IN "London"' in kingdom and "- Wapping" in kingdom
            and "- Limehouse" in kingdom and path_label(limehouse) in kingdom
            and "NPC: Bess — the lamp-lighter, halfling rogue" in kingdom, kingdom)
        article = render_places_answer(record, here, "places: the Wapping")
        rec("a dropped article still matches (the declaration's own forgiveness)",
            'PLACES IN "Wapping" (London)' in article
            and "Ropehaven Wharf — the crane" in article, article)

        both = render_places_answer(record, here, "places: Wapping")
        rec("a name that is a kingdom and a settlement returns both, labelled",
            "is both a kingdom and a settlement" in both
            and 'PLACES IN "Wapping" — reuse' in both
            and 'PLACES IN "Wapping" (London)' in both
            and 'PLACES IN "Wapping" (Paris)' in both, both)

        unknown = render_places_answer(record, here, "places: Atlantis")
        rec("an unknown name returns the map instead of inventing anything",
            'no kingdom or settlement here is called "Atlantis"' in unknown
            and "- London" in unknown, unknown)
        rec("no places at all says so", render_places_answer([], None, "places")
            == "PLACES: none recorded yet.")
        rec("a non-place query renders nothing (the caller falls through to the MCP)",
            render_places_answer(record, here, "here") == ""
            and render_places_answer(record, here, "ref:update_player_list") == "")
    
        # The engine's half: a place query is answered from the LIVE tree and never reaches
        # the MCP server, which knows eras and polities but not where the story stands.
        gsl = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gsl.session = _FakeMCP()
        gsl._primed = record
        gsl._current_place = dict(here)
        await gsl._execute_tool({"function": {"name": "lookup",
                                              "arguments": {"query": "places: Limehouse"}}})
        frames = drain(gsl._evt_q)
        served = [e for e in frames if e.get("type") == "tool_result"]
        rec("the engine serves a place query itself (no MCP call)",
            gsl.session.calls == [] and len(served) == 1
            and path_label(limehouse) in str(served[0].get("text")),
            f"{gsl.session.calls} / {served}")
        rec("... the answer is appended as a tool result for the GM",
            gsl.messages[-1].get("name") == "lookup"
            and path_label(limehouse) in str(gsl.messages[-1].get("content")), str(gsl.messages[-1]))
        await gsl._execute_tool({"function": {"name": "lookup", "arguments": {"query": "here"}}})
        rec("... and every other lookup still reaches the MCP server",
            [c[0] for c in gsl.session.calls] == ["lookup"]
            and gsl.session.calls[0][1].get("query") == "here", str(gsl.session.calls))
    
        # ── the block says where the record is, and admits what it hides ───────
        many = [{"kingdom": "London", "area": f"area {i}", "place": [f"place {i}"],
                 "used": 100 - i, "main_npcs": [], "dead": []} for i in range(8)]
        gsb = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gsb._primed = many
        gsb._current_place = {"kingdom": "London", "area": "area 0", "place": ["place 0"]}
        gsb.scene_images = False
        body = gsb._places_body()
        rec("the block names the on-demand record it cannot hold",
            "lookup('places')" in body, body[:200])
        rec("... and says how many areas the cap hid instead of dropping them silently",
            "+1 more areas you have stood in" in body, body)

        # ── the turn's place echo: declare where you are, or the engine asks ───
        # Text mode has no scene call to carry the place every turn, so the engine checks
        # whether THIS turn declared and asks once (quietly) when it did not -- the one place
        # it can close "a move the GM never declares cannot be inferred". The declaration is
        # cheap: for a place the record holds, the address (and the spelling) come from the
        # record, so a bare leaf is enough.
        court = ["the market square", "the Stag inn"]
        porch = ["the market square", "the church porch"]
        reg = [{"kingdom": "Wallachia", "area": "Câmpulung", "place": court},
               {"kingdom": "Wallachia", "area": "Târgoviște",
                "place": ["the princely court", "the guard room"]}]
        rec("a declared leaf resolves to the known full path (the mirror rule)",
            resolve_place_path(reg, ["the Stag inn"]) == court
            and resolve_place_path(reg, ["the guard room"])
            == ["the princely court", "the guard room"],
            str(resolve_place_path(reg, ["the guard room"])))
        two_inns = [{"kingdom": "London", "area": "Wapping",
                     "place": ["the Blue Lotus", "the common room"]},
                    {"kingdom": "London", "area": "Limehouse",
                     "place": ["the Drowned Lantern", "the common room"]}]
        rec("an ambiguous leaf is never merged into one of them",
            resolve_place_path(two_inns, ["the common room"]) is None)
        rec("... unless exactly one match is in the area the story stands in",
            resolve_place_path(two_inns, ["the common room"],
                               {"kingdom": "London", "area": "Limehouse"})
            == ["the Drowned Lantern", "the common room"])
    
        svc_e = SceneService(out)
        svc_e.record_place("echo", era=ERA, kingdom="Wallachia", area="Câmpulung",
                           place=court)
        gse = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gse.session = _FakeMCP()
        gse.active_name = "echo"
        gse.era = ERA
        gse._reload_era_scene_state()
        drain(gse._evt_q)
    
        # A move to a NEW room in the same settlement, declared in full.
        await gse._execute_tool({"function": {"name": "note_place", "arguments": {
            "place": porch, "kingdom": "Wallachia", "area": "Câmpulung"}}})
        evts = drain(gse._evt_q)
        rec("a declared move records the place and publishes it",
            (gse._current_place or {}).get("place") == porch
            and len([e for e in evts if e.get("type") == "place_note"]) == 1
            and any(e.get("type") == "caption" for e in evts), str(gse._current_place))
        rec("... and satisfies the turn's place echo", len(gse._place_declarations) == 1)
    
        # A RETURN to a place the record holds, named by its leaf alone: the saved path and
        # the saved address are adopted (this is what makes the per-turn echo cheap).
        await gse._execute_tool({"function": {"name": "note_place",
                                             "arguments": {"place": ["the Stag inn"]}}})
        evts = drain(gse._evt_q)
        rec("an address-less leaf for a known place adopts the saved path and address",
            (gse._current_place or {}).get("place") == court
            and (gse._current_place or {}).get("kingdom") == "Wallachia"
            and (gse._current_place or {}).get("area") == "Câmpulung"
            and len(gse._primed) == 2, str(gse._current_place))
    
        # Re-declaring where the story already stands: accepted, and nothing is rewritten.
        await gse._execute_tool({"function": {"name": "note_place",
                                             "arguments": {"place": ["the Stag inn"]}}})
        evts = drain(gse._evt_q)
        rec("a re-declaration of the same place writes nothing",
            not [e for e in evts if e.get("type") in ("place_note", "caption")]
            and gse._place_declarations[-1] == court, str(evts))
        await gse._execute_tool({"function": {"name": "note_place", "arguments": {
            "place": ["the Stag inn"],
            "main_npcs": [{"name": "Stoica", "role": "the innkeeper", "race": "human",
                           "class": "commoner"}]}}})
        rec("... but a new regular still reaches the record",
            any(e.get("type") == "place_note" for e in drain(gse._evt_q)))

        # A FULL echo -- the whole note_place, regular included -- is inert when nothing
        # differs: live 2026-10-17 the stay turn re-sent exactly this and the manifest was
        # rewritten for it. A changed role, by contrast, is a real update.
        await gse._execute_tool({"function": {"name": "note_place", "arguments": {
            "place": ["the Stag inn"], "kingdom": "Wallachia", "area": "Câmpulung",
            "main_npcs": [{"name": "Stoica", "role": "the innkeeper", "race": "human",
                           "class": "commoner"}]}}})
        evts = drain(gse._evt_q)
        rec("repeating a known regular writes nothing (a full echo is inert)",
            not [e for e in evts if e.get("type") in ("place_note", "caption")], str(evts))
        await gse._execute_tool({"function": {"name": "note_place", "arguments": {
            "place": ["the Stag inn"], "kingdom": "Wallachia", "area": "Câmpulung",
            "main_npcs": [{"name": "Stoica", "role": "the innkeeper and the magistrate",
                           "race": "human", "class": "commoner"}]}}})
        rec("... while a changed role is a real update",
            any(e.get("type") == "place_note" for e in drain(gse._evt_q)))

        # ── the roster is a DELTA, and a regular's state is the GM's word ──────
        # Live 2026-10-18 a re-declaration carrying only the regular it had just met wiped
        # the other three: a declaration adds or updates, it never removes.
        court_entry = next(p for p in gse._primed if p["place"] == court)
        await gse._execute_tool({"function": {"name": "note_place", "arguments": {
            "place": ["the Stag inn"],
            "main_npcs": [{"name": "Barbu", "role": "the ostler", "race": "human",
                           "class": "commoner"}]}}})
        evts = drain(gse._evt_q)
        roster = [n["name"] for n in court_entry["main_npcs"]]
        note = [e for e in evts if e.get("type") == "place_note"]
        rec("a declaration ADDS a regular without losing the others",
            "Stoica" in roster and "Barbu" in roster, str(roster))
        rec("... and the persisted roster carries the whole list",
            bool(note) and {n["name"] for n in note[0].get("main_npcs") or []} == set(roster),
            str(note[0].get("main_npcs") if note else None))

        await gse._execute_tool({"function": {"name": "note_place", "arguments": {
            "place": ["the Stag inn"],
            "main_npcs": [{"name": "Stoica", "state": "petrified"}]}}})
        evts = drain(gse._evt_q)
        note = [e for e in evts if e.get("type") == "place_note"]
        stoica = next(n for n in court_entry["main_npcs"] if n["name"] == "Stoica")
        rec("a state-only entry sets the state and keeps the identity",
            stoica.get("state") == "petrified" and stoica.get("role")
            and stoica.get("race") == "human" and stoica.get("class") == "commoner", str(stoica))
        rec("... it is a real change, and earns no identity note",
            len(note) == 1 and "race and/or class" not in gse.messages[-1]["content"],
            gse.messages[-1]["content"])
        rec("... the per-turn block shows it",
            "Stoica" in gse._places_body() and "(petrified)" in gse._places_body(),
            gse._places_body())
        rec("... and the on-demand record shows it",
            "(petrified)" in render_places_answer(gse._primed, gse._current_place,
                                                  "places: Câmpulung"))

        # `dead` is the one state the ENGINE consumes: it joins the place's death index and
        # a later declaration cannot clear it (a death is permanent here).
        await gse._execute_tool({"function": {"name": "note_place", "arguments": {
            "place": ["the Stag inn"],
            "main_npcs": [{"name": "Barbu", "state": "dead"}]}}})
        drain(gse._evt_q)
        rec("a declared death joins the place's death index",
            "barbu" in court_entry["dead"], str(court_entry["dead"]))
        await gse._execute_tool({"function": {"name": "note_place", "arguments": {
            "place": ["the Stag inn"],
            "main_npcs": [{"name": "Barbu", "role": "the ostler"}]}}})
        drain(gse._evt_q)
        rec("... and a later declaration cannot clear it",
            next(n for n in court_entry["main_npcs"] if n["name"] == "Barbu")["state"] == "dead"
            and "barbu" in court_entry["dead"], str(court_entry["dead"]))
        rec("the state is capped for the block's budget",
            len(_npc_state("x" * 60)) == 24, str(_npc_state("x" * 60)))

        # Images mode has no `note_place`: the scene call carries the state there.
        svc_i = SceneService(out)
        square = ["Yanping Gate", "the gate square"]
        svc_i.record_place("gate", era=ERA, kingdom="the Tang empire", area="Chang'an",
                           place=square,
                           main_npcs=[{"name": "Warden Gao Lian", "role": "gate warden",
                                       "race": "human", "class": "fighter"}])
        gss = GameSession(base_dir=Path(td), model="test", scene_images=True)
        gss.session = _FakeMCP()
        gss.active_name = "gate"
        gss.era = ERA
        gss._reload_era_scene_state()
        drain(gss._evt_q)
        await gss._execute_tool({"function": {"name": "request_scene_image", "arguments": {
            "description": "the protagonist at the closing drum", "place": square,
            "establishing": "a timber gate house, banners hanging still",
            "main_npcs": [{"name": "Warden Gao Lian", "state": "dead"}]}}})
        square_entry = next(p for p in gss._primed if p["place"] == square)
        warden = next(n for n in square_entry["main_npcs"] if n["name"] == "Warden Gao Lian")
        rec("a scene call carries a regular's state with images on (no note_place)",
            warden.get("state") == "dead" and "warden gao lian" in square_entry["dead"]
            and warden.get("role") == "gate warden", str(square_entry["main_npcs"]))

        # A NEW room named by a bare leaf: filed in the settlement the story stands in, with a
        # NOTE -- an address-less place would blind the caption and the block's scoping.
        before_primed = len(gse._primed)
        gse._place_declarations = []  # a fresh turn: nothing declared yet
        await gse._execute_tool({"function": {"name": "note_place",
                                             "arguments": {"place": ["the battlements"]}}})
        evts = drain(gse._evt_q)
        note = [m for m in gse.messages if m.get("name") == "note_place"][-1]["content"]
        rec("a new room named by a bare leaf is filed in the settlement the story is in",
            len(gse._primed) == before_primed + 1
            and (gse._current_place or {}).get("area") == "Câmpulung"
            and (gse._current_place or {}).get("place") == ["the battlements"]
            and any(e.get("type") == "place_note" for e in evts), str(gse._current_place))
        rec("... and the GM is told which settlement it was filed in",
            "NOTE:" in note and "Câmpulung" in note, note[-220:])
        rec("... so the turn's place echo is satisfied", len(gse._place_declarations) == 1)

        # The corner left over: no address anywhere (not even where the story stands).
        gse._place_declarations = []
        gse._current_place = None
        before_primed = len(gse._primed)
        await gse._execute_tool({"function": {"name": "note_place",
                                             "arguments": {"place": ["an island"]}}})
        evts = drain(gse._evt_q)
        refused = [e for e in evts if e.get("type") == "tool_result"]
        rec("with no address anywhere the declaration is refused, not filed empty",
            len(gse._primed) == before_primed
            and not [e for e in evts if e.get("type") == "place_note"]
            and bool(refused) and refused[0].get("is_error") is True
            and "WARNING:" in str(refused[0].get("text")), str(refused))
        rec("... and the turn's echo still counts as unmet (the net will ask)",
            gse._place_declarations == [])
    
        # The net itself: one quiet ask, text mode only, never twice.
        asks: list[tuple] = []
        real_run = gse._run_role
    
        async def _spy_run(content, label, quiet=False):
            asks.append((label, quiet, content))
            return ""
    
        gse._run_role = _spy_run  # type: ignore[assignment]
        gse._place_declarations = []
        await gse._ensure_place_note()
        rec("the net asks once, quietly, when the turn declared nothing",
            len(asks) == 1 and asks[0][0] == "place-fix" and asks[0][1] is True
            and PLACE_ECHO_NUDGE in str(asks[0][2]), str(asks))
        asks.clear()
        gse._place_declarations = [court]
        await gse._ensure_place_note()
        rec("... and stays silent when the turn declared exactly one", asks == [], str(asks))
        asks.clear()
        # TWO declarations in one turn: the engine keeps one current place and cannot tell
        # which of them the turn ended in (live 2026-10-17: a travel turn declared the inn
        # first and the toll-farm second, and the engine adopted the toll-farm). It asks.
        gse._place_declarations = [porch, court]
        await gse._ensure_place_note()
        rec("... and asks which one the turn ENDED in when it declared several",
            len(asks) == 1 and asks[0][0] == "place-fix"
            and "2 places" in str(asks[0][2])
            and path_label(porch) in str(asks[0][2])
            and path_label(court) in str(asks[0][2]), str(asks))
        asks.clear()
        gse._place_declarations = []
        gse.scene_images = True
        await gse._ensure_place_note()
        rec("... and never fires with images on (the scene call carries the place)",
            asks == [], str(asks))

        # The place-less recurring people: the block shows place regulars only, so the
        # on-demand record carries the cast as a footer (it is not redundant with the
        # timeline -- this is the identity the engine checks declarations against).
        gsc = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gsc.active_name = "save"
        gsc.era = ERA
        gsc._reload_era_scene_state()
        footer = render_places_answer(gsc._primed, gsc._current_place, "places",
                                      cast=gsc._cast_labels())
        rec("the record lists the place-less recurring people with their identity",
            "OTHER PEOPLE YOU KNOW:" in footer
            and "Corvin Hale — the bounty hunter" in footer, footer)
        gse.scene_images = False
        gse._run_role = real_run  # type: ignore[assignment]
    
        # ── images mode: the scene call is the declaration, picture or not ────
        stem = "images"
        gsi = GameSession(base_dir=Path(td), model="test", scene_images=True)
        gsi.session = _FakeMCP()
        gsi.active_name = stem
        gsi.era = ERA
        gsi._reload_era_scene_state()
        scene_args = {
            "description": "the protagonist at the forge",
            "kingdom": "London", "area": "Wapping",
            "place": ["Ropehaven Wharf", "the smithy"],
            "establishing": "a low stone smithy, cold hearth",
            "main_npcs": [{"name": "Gorson", "role": "the smith", "race": "dwarf",
                           "class": "fighter", "description": "a soot-stained dwarf"}],
        }
        await gsi._execute_tool({"function": {"name": "request_scene_image",
                                              "arguments": dict(scene_args)}})
        evts = drain(gsi._evt_q)
        notes = [e for e in evts if e.get("type") == "place_note"]
        scenes = [e for e in evts if e.get("type") == "scene_request"]
        kinds = [e.get("type") for e in evts]
        rec("images mode records the scene call as a place_note",
            len(notes) == 1 and len(scenes) == 1 and notes[0]["place"] == scene_args["place"],
            str(evts))
        rec("... before the image request, so the browser draws into the entry",
            kinds.index("place_note") < kinds.index("scene_request"), str(kinds))
        rec("... and the live tree learns the place at once",
            any(p["place"] == scene_args["place"] for p in gsi._primed)
            and "the smithy" in gsi._places_body(), gsi._places_body())
        for e in notes:
            svc.record_place(stem, era=e["era"], kingdom=e["kingdom"], area=e["area"],
                             place=e["place"], main_npcs=e["main_npcs"], cast=e["cast"])
        entry = next((p for p in known_scene_places(out, stem, ERA)
                      if p["place"] == scene_args["place"]), None)
        rec("... the registry entry exists with no picture",
            bool(entry) and entry["file"] is None and entry["seeded"] is False, str(entry))
        rec("... and the current pointer follows the declaration",
            (current_scene_place(out, stem) or {}).get("place") == scene_args["place"],
            str(current_scene_place(out, stem)))
        rec("... the roster keeps the declared look",
            bool(entry) and entry["main_npcs"][0].get("description") == "a soot-stained dwarf",
            str(entry["main_npcs"] if entry else None))
        commit_scene_manifest(out, stem)
        disk = json.loads((out / "images" / stem / "scenes" / "manifest.json")
                          .read_text(encoding="utf-8"))
        rec("... and a Save writes the file-less place to disk",
            any(e.get("place") == scene_args["place"] for e in disk["seeds"].values()),
            str(disk.get("seeds")))

        gsi._scene_requested_turn = False
        await gsi._execute_tool({"function": {"name": "request_scene_image",
                                              "arguments": dict(scene_args)}})
        rec("a repeated scene call writes nothing (a stay turn)",
            not [e for e in drain(gsi._evt_q) if e.get("type") == "place_note"])

        gsi._scene_requested_turn = False
        state_change = dict(scene_args)
        state_change["main_npcs"] = [{"name": "Gorson", "state": "dead"}]
        await gsi._execute_tool({"function": {"name": "request_scene_image",
                                              "arguments": state_change}})
        state_notes = [e for e in drain(gsi._evt_q) if e.get("type") == "place_note"]
        rec("... while a regular's state is a real change",
            len(state_notes) == 1
            and next(n for n in state_notes[0]["main_npcs"] if n["name"] == "Gorson")["state"]
            == "dead",
            str(state_notes))

        gsi._scene_requested_turn = False
        look_change = dict(scene_args)
        look_change["main_npcs"] = [{"name": "Gorson", "role": "the smith", "race": "dwarf",
                                     "class": "fighter",
                                     "description": "a soot-stained dwarf with a burn scar"}]
        await gsi._execute_tool({"function": {"name": "request_scene_image",
                                              "arguments": look_change}})
        look_notes = [e for e in drain(gsi._evt_q) if e.get("type") == "place_note"]
        rec("... and a changed look is a real change too",
            len(look_notes) == 1
            and "burn scar" in look_notes[0]["main_npcs"][0]["description"],
            str(look_notes))

        before = dict(gsi._current_place or {})
        gsi._scene_requested_turn = False
        await gsi._execute_tool({"function": {"name": "request_scene_image",
                                              "arguments": {"description": "a portrait"}}})
        no_place = drain(gsi._evt_q)
        rec("a portrait-only scene call leaves the current place alone",
            before == (gsi._current_place or {})
            and not [e for e in no_place if e.get("type") == "place_note"],
            f"current={gsi._current_place} events={[e.get('type') for e in no_place]}")

        # Known is not seeded: a file-less place still asks for `establishing`.
        gsk = GameSession(base_dir=Path(td), model="test", scene_images=True)
        gsk.session = _FakeMCP()
        gsk.active_name = stem
        gsk.era = ERA
        gsk._reload_era_scene_state()
        rec("a known file-less place is not treated as seeded",
            not gsk._scene_seeded, str(gsk._scene_seeded))
        await gsk._execute_tool({"function": {"name": "request_scene_image", "arguments": {
            "description": "back at the smithy", "place": scene_args["place"]}}})
        rejected = [e for e in drain(gsk._evt_q)
                    if e.get("type") == "tool_result" and e.get("is_error")]
        rec("... so its scene call is refused until `establishing` is supplied",
            len(rejected) == 1 and "establishing" in rejected[0]["text"]
            and "recorded" in rejected[0]["text"], str(rejected))
        await gsk._execute_tool({"function": {"name": "request_scene_image", "arguments": {
            "description": "back at the smithy", "place": scene_args["place"],
            "establishing": "a low stone smithy, cold hearth"}}})
        accepted = drain(gsk._evt_q)
        rec("... and accepted once it is supplied",
            any(e.get("type") == "scene_request" for e in accepted)
            and _scene_key(scene_args["place"]) in gsk._scene_seeded, str(accepted))

        # A real seed file flips the same place to seeded on reload.
        live = live_scene_manifest(out, stem)
        seed_file = "seed-smithy.png"
        for seed in live.get("seeds", {}).values():
            if seed.get("place") == scene_args["place"]:
                seed["file"] = seed_file
                seed["slug"] = "seed-smithy"
        seed_dir = out / "images" / stem / "scenes"
        seed_dir.mkdir(parents=True, exist_ok=True)
        (seed_dir / seed_file).write_bytes(jpeg(80, 45))
        reloaded = known_scene_places(out, stem, ERA)
        seeded_entry = next((p for p in reloaded if p["place"] == scene_args["place"]), None)
        rec("a real seed file reports seeded",
            bool(seeded_entry) and seeded_entry["seeded"] is True
            and seeded_entry["file"] == seed_file, str(seeded_entry))
        gss2 = GameSession(base_dir=Path(td), model="test", scene_images=True)
        gss2.session = _FakeMCP()
        gss2.active_name = stem
        gss2.era = ERA
        gss2._reload_era_scene_state()
        rec("... and the reloaded session treats the place as seeded",
            any(_scene_key(p["place"]) in gss2._scene_seeded for p in gss2._primed),
            str(gss2._scene_seeded))

        # The arrival is only written when it actually changes.
        gsp = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gsp.session = _FakeMCP()
        gsp.era = ERA
        gsp.arrival = "the market square, in Câmpulung"
        await gsp._pin_arrival(court, "Câmpulung")
        rec("an unchanged arrival is never written twice",
            gsp.arrival == "the market square, in Câmpulung"
            and not [c for c in gsp.session.calls if c[0] == "set_player_field"],
            f"{gsp.arrival} / {gsp.session.calls}")
    
    return all(RESULTS)


if __name__ == "__main__":
    ok = asyncio.run(main())
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
