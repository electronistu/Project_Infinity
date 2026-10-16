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
from web.protocol import filter_tools, render_protocol  # noqa: E402
from web.places import (  # noqa: E402
    KNOWN_PLACES_HEADER, KNOWN_PLACES_HEADER_TEXT, KNOWN_PLACES_EMPTY_TEXT, path_label,
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
                                        "race": "dwarf", "class": "rogue"}], str(places))
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
        rec("note_place adds the regular's race/class/role to the on-stage echo",
            gs._npc_people.get("bess") == ("Bess", "the lamp-lighter", "halfling", "rogue"),
            str(gs._npc_people))
        rec("note_place remembers the cast name",
            "corvin hale" in gs._cast_names, str(gs._cast_names))
        rec("the GM view is trimmed to the acknowledgement",
            "main_npcs" in gs.messages[-1]["content"]
            and "extra" not in gs.messages[-1]["content"], gs.messages[-1]["content"])

        # The `characters` key is the model's own spelling of a name, and live 2026-10-16 it
        # keyed two declared names with an underscore (`Master_Guo` for the declared `Master
        # Guo`; the same call passed `npcs: ["Master Guo"]`). The exact `name.lower()` check
        # then told the GM, in the result of the call that declared them, that those names
        # were undeclared -- and the identity echo (the one place a role/race/class rides)
        # stayed silent for them. Folding the separators fixes both.
        note = gs._undeclared_note({"characters": {"Corvin_Hale": "at the door",
                                                   "a stray dog": "in the alley"}})
        rec("an underscore-keyed declared name is not reported as undeclared",
            "Corvin_Hale" not in note and "a stray dog" in note, note)
        echo = gs._on_stage_note({"characters": {"Corvin_Hale": "at the door"}})
        rec("... and its identity still echoes on stage",
            echo == "on stage: Corvin Hale (the bounty hunter)", echo)

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

    return all(RESULTS)


if __name__ == "__main__":
    ok = asyncio.run(main())
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
