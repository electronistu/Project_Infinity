"""Text-only place memory: `note_place`, the file-less registry, priming and arrivals.

No network. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_places.py
"""

import asyncio
import json
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import (  # noqa: E402
    GameSession, filter_tools, render_protocol,
    KNOWN_PLACES_HEADER, KNOWN_PLACES_HEADER_TEXT, KNOWN_PLACES_EMPTY_TEXT,
)
from web.images import (  # noqa: E402
    SceneService, known_scene_places, known_npc_names, live_scene_manifest,
    commit_scene_manifest, discard_scene_manifest, current_scene_place,
)
from web.eras import era_arrivals  # noqa: E402

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

        # ── jump arrivals: one uniform pool (visited + authored) ──────────────
        gsj = GameSession(base_dir=Path(td), model="test", scene_images=False)
        gsj.active_name = "save"
        gsj.era = ERA
        gsj._rng = random.Random(0)
        visited = "Ropehaven Wharf, in Wapping"
        authored = set(era_arrivals(ERA))
        picks = {gsj._pick_jump_arrival(ERA) for _ in range(400)}
        rec("a jump can land in a text-only visited place", visited in picks, str(sorted(picks)))
        rec("a jump still draws the era's authored arrivals",
            bool(picks & authored), str(sorted(picks)))

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
