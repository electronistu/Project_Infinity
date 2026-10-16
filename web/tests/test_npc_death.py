"""Main-NPC death: engine-committed, place-level, permanent per era, `(dead)` in the tree.

No network. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_npc_death.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import GameSession  # noqa: E402
from web.places import format_known_places  # noqa: E402
from web.scenes import (  # noqa: E402
    SceneService, known_scene_places, live_scene_manifest,
    commit_scene_manifest, discard_scene_manifest, mark_place_dead, _dead_names,
)

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


ERA = "victorian"
PLACE = ["Ropehaven Wharf", "Warehouse Nine"]
OTHER = ["Ropehaven Wharf", "the customs house"]
MAIN = [{"name": "Maera", "role": "the harbourmaster", "race": "dwarf", "class": "rogue"},
        {"name": "Bess", "role": "the lamp-lighter", "race": "halfling", "class": "rogue"}]
CAST = [{"name": "Corvin Hale", "role": "the bounty hunter"}]

KILL = [{"name": "Maera", "killed": True, "is_player": False, "status": "active"}]
ALIVE = [{"name": "Maera", "killed": False, "is_player": False, "status": "active"}]


def _place_of(places, path):
    return [p for p in places if p["place"] == path][0]


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.isError = False


class _FakeMCP:
    """Enough of the MCP session for a tool call that reads and writes player fields."""

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        return _Result("{}")


def _session(td):
    gs = GameSession(base_dir=Path(td), model="test", scene_images=False)
    gs.active_name = "save"
    gs.era = ERA
    gs.classic = False
    gs._reload_era_scene_state()
    # The manifest's `current` pointer is the last recorded place; point the session at
    # the place under test so a combat result is matched against its main NPCs.
    gs._set_current_place("London", "Wapping", PLACE)
    return gs


async def main() -> bool:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "output"
        svc = SceneService(out)
        svc.record_place("save", era=ERA, kingdom="London", area="Wapping",
                         place=PLACE, main_npcs=MAIN, cast=CAST)
        svc.record_place("save", era=ERA, kingdom="London", area="Wapping",
                         place=OTHER, main_npcs=[{"name": "the customs clerk",
                                                  "role": "the clerk"}])

        # ── the writer: a place-level dead list, outside the NPC dicts ─────────
        added = mark_place_dead(out, "save", ERA, "London", "Wapping", PLACE, ["Maera"])
        rec("mark_place_dead reports the name added", added == ["maera"], str(added))
        live = live_scene_manifest(out, "save")
        key = next(k for k, e in live["seeds"].items() if e.get("place") == PLACE)
        entry = live["seeds"][key]
        rec("the dead flag is a name list on the seed entry, not inside the NPC dict",
            entry.get("dead") == ["maera"]
            and all("dead" not in n for n in entry.get("main_npcs", [])), str(entry.get("dead")))
        rec("_dead_names reads it back", _dead_names(entry) == ["maera"], str(entry))

        places = known_scene_places(out, "save", ERA)
        rec("known_scene_places surfaces `dead`", _place_of(places, PLACE)["dead"] == ["maera"],
            str(_place_of(places, PLACE)))
        rec("a different place is unaffected (place-level keying)",
            _place_of(places, OTHER)["dead"] == [], str(_place_of(places, OTHER)))

        tree = format_known_places(places)
        rec("the primed tree annotates the dead main NPC: `Maera (dead)`",
            "Maera (dead)" in tree and "Bess" in tree and "Bess (dead)" not in tree, tree)
        rec("the other place's clerk is not annotated", "the customs clerk (dead)" not in tree, tree)
        rec("the cast is never primed", "Corvin Hale" not in tree, tree)

        # ── the engine: provisional, heal, freeze at Save ─────────────────────
        gs = _session(td)
        rec("the loaded tree shows the dead NPC from the manifest on turn one",
            "Maera (dead)" in gs._places_body(), gs._places_body())

        # A fresh visit: clear the manifest flag, then kill in-session.
        live["seeds"][key]["dead"] = []
        gs = _session(td)
        rec("a live, healed NPC is not annotated", "Maera (dead)" not in gs._places_body())
        gs._track_main_npc_deaths(KILL)
        rec("a killed main_npc of the current place is annotated for this visit",
            "Maera (dead)" in gs._places_body(), gs._places_body())
        rec("... but nothing is written to the manifest before the freeze",
            live_scene_manifest(out, "save")["seeds"][key].get("dead") == [])
        gs._track_main_npc_deaths(ALIVE)
        rec("a heal before the freeze clears it (provisional only)",
            "Maera (dead)" not in gs._places_body(), gs._places_body())

        gs._track_main_npc_deaths(KILL)
        gs._freeze_dead()
        rec("the freeze writes the dead list into the live manifest",
            live_scene_manifest(out, "save")["seeds"][key].get("dead") == ["maera"],
            str(live_scene_manifest(out, "save")["seeds"][key].get("dead")))
        gs._track_main_npc_deaths(ALIVE)
        rec("a heal AFTER the freeze does not clear it (permanent per era)",
            "Maera (dead)" in gs._places_body(), gs._places_body())

        # ── persistence: commit, restart, the tree is still annotated ─────────
        commit_scene_manifest(out, "save")
        discard_scene_manifest(out, "save")
        gs2 = _session(td)
        rec("after a Save + reload the dead NPC is still fed as dead",
            "Maera (dead)" in gs2._places_body(), gs2._places_body())

        # ── re-declaration preserves liveness, dropping names no longer present ─
        svc.record_place("save", era=ERA, kingdom="London", area="Wapping", place=PLACE,
                         main_npcs=[{"name": "Bess", "role": "the lamp-lighter"}])
        after = _place_of(known_scene_places(out, "save", ERA), PLACE)
        rec("re-declaring without the dead name drops it from `dead`",
            after["dead"] == [], str(after))
        mark_place_dead(out, "save", ERA, "London", "Wapping", PLACE, ["Bess"])
        svc.record_place("save", era=ERA, kingdom="London", area="Wapping", place=PLACE,
                         main_npcs=[{"name": "Bess", "role": "the lamp-lighter"},
                                    {"name": "the new keeper", "role": "the keeper"}])
        after = _place_of(known_scene_places(out, "save", ERA), PLACE)
        rec("re-declaring preserves the dead flag for a surviving name",
            after["dead"] == ["bess"], str(after))

        # ── a killed CAST member is not annotated (cast is the timeline's) ────
        gs3 = _session(td)
        gs3._track_main_npc_deaths([{"name": "Corvin Hale", "killed": True,
                                     "is_player": False}])
        rec("a name that is only cast (not a main_npc of the place) is not annotated",
            "Corvin Hale (dead)" not in gs3._places_body(), gs3._places_body())

        # ── images on: a scene call IS the movement signal ────────────────────
        # It used to be thrown away: a fight in the place the story just entered left its
        # regular un-marked, and a same-named regular of the place the save last recorded
        # could be marked instead. The two places below share one name on purpose.
        gasworks = ["Ropehaven Wharf", "the gasworks"]
        market = ["Ropehaven Wharf", "the fish market"]
        keeper = {"name": "the harbourmaster", "role": "the harbourmaster",
                  "race": "human", "class": "rogue"}
        svc.record_place("save", era=ERA, kingdom="London", area="Wapping",
                         place=gasworks, main_npcs=[keeper])
        svc.record_place("save", era=ERA, kingdom="London", area="Wapping",
                         place=market, main_npcs=[keeper])
        gsx = GameSession(base_dir=Path(td), model="test", scene_images=True)
        gsx.active_name = "save"
        gsx.era = ERA
        gsx.classic = False
        gsx.session = _FakeMCP()
        gsx._reload_era_scene_state()
        rec("the session opens where the save last stood",
            (gsx._current_place or {}).get("place") == market, str(gsx._current_place))
        await gsx._execute_tool({"function": {"name": "request_scene_image", "arguments": {
            "description": "the protagonist climbs the gantry", "place": gasworks}}})
        rec("a scene call in a known place moves the engine's idea of where the GM is",
            (gsx._current_place or {}).get("place") == gasworks
            and (gsx._current_place or {}).get("area") == "Wapping", str(gsx._current_place))
        gsx._track_main_npc_deaths([{"name": "the harbourmaster", "killed": True,
                                     "is_player": False}])
        gsx._freeze_dead()
        dead_now = {p["place"][-1]: p["dead"] for p in known_scene_places(out, "save", ERA)}
        rec("a death lands on the place the story is in",
            dead_now.get("the gasworks") == ["the harbourmaster"], str(dead_now))
        rec("... and never on the place the save merely last recorded",
            dead_now.get("the fish market") == [], str(dead_now))

    return all(RESULTS)


if __name__ == "__main__":
    ok = asyncio.run(main())
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
