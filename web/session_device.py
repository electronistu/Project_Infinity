"""The Device: its gating predicates and its client panel/menus."""

import json
import sys
from pathlib import Path

# Make the repo root importable so we can reuse existing (frozen) modules.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from .eras import origin_eras, playable_eras
from .places import group_identity, place_group
from .scenes import known_scene_places
from .player_file import _arrival_candidates, _split_journey
from .protocol import COMBAT_HOLD_MESSAGE, PLACE_MENU_RECENT
from .session_base import OUTPUT_DIR
import device


class DeviceMixin:
    def _future_era(self) -> str:
        """The origin age -- the future (New York) -- or '' when the config has none."""
        origins = origin_eras()
        return origins[0] if origins else ""

    def _future_unlocked(self) -> bool:
        """The future is reachable only while every part of the Device is held."""
        return self.parts_recovered >= device.part_total()

    def _era_allowed(self, era: str) -> bool:
        """Whether the Device may name this age as a destination.

        The age the Traveller stands in is a destination too: a release then moves them
        within it (the same session, not an age crossing). Any other playable age is legal --
        ahead or behind; there is no dead end at the last age. The future is the one
        exception: it is unreachable until every part is held, but standing in it is always
        legal.
        """
        era = str(era or "").strip().lower()
        if not era:
            return False
        if era == str(self.era or "").strip().lower():
            return True
        if era not in playable_eras():
            return False
        if era == self._future_era() and not self._future_unlocked():
            return False
        return True

    def _clean_place(self, place) -> str:
        """The chosen place or area, whitespace-collapsed."""
        return " ".join(str(place or "").split())

    def _area_allowed(self, era: str, area: str) -> bool:
        """Whether the save has stood anywhere in this area of `era`."""
        wanted = " ".join(str(area or "").split()).lower()
        return bool(wanted) and any(a.lower() == wanted for a, _ in self._visited_by_area(era))

    def _place_allowed(self, era: str, area: str, place: str) -> bool:
        """Whether an area of `era` holds a visited place with this picker label."""
        wanted = " ".join(str(place or "").split()).lower()
        if not wanted:
            return False
        area = " ".join(str(area or "").split()).lower()
        for a, groups in self._visited_by_area(era):
            if area and a.lower() != area:
                continue
            if any(self._place_option_label(p.get("place")).lower() == wanted
                   for g in groups for p in g["places"]):
                return True
        return False

    async def _device_release(self, cmd: dict) -> None:
        """Fire the Device: jump now, to the chosen age and (optionally) an area and place.

        A deliberate release is refused in combat. The age may be another reachable one, or
        the one the Traveller stands in -- then the Device moves them within it and the
        session continues. The area, ward and place are all optional: name just the age, an
        area you have stood in, one of its wards, or the exact place -- whatever you leave out,
        the Game Master chooses.
        """
        if self._in_combat:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": COMBAT_HOLD_MESSAGE})
            return
        era = str(cmd.get("era") or "").strip().lower()
        area = self._clean_place(cmd.get("area"))
        group = self._clean_place(cmd.get("group"))
        place = self._clean_place(cmd.get("place"))
        if not self._era_allowed(era):
            await self._device_refuse()
            return
        if area and not self._area_allowed(era, area):
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "Choose an area you have already stood in, or none and "
                                      "the Game Master chooses."})
            return
        if place and not self._place_allowed(era, area, place):
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "Choose one of the places you have already visited there, "
                                      "or none and the Game Master chooses."})
            return
        if group and not place and not self._group_allowed(era, area, group):
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "Choose a ward you have already stood in, or none and "
                                      "the Game Master chooses."})
            return
        if era == str(self.era or "").strip().lower():
            await self._move_within_era(area, group, place)
        else:
            await self._close_and_jump(era, area, place, group)

    async def _device_refuse(self) -> None:
        await self._emit({"type": "notice", "title": "The Device",
                          "text": "The Device cannot reach there."})

    # ── the Device: the cadence, and the jump itself ─────────────

    def _reachable_eras(self) -> list[str]:
        """Every playable era but this one that the Device may actually reach.

        The future is the one exception: while it is locked (not every part is held) it is not
        a legal destination anywhere."""
        future = self._future_era()
        return [e for e in playable_eras()
                if e != self.era and not (e == future and not self._future_unlocked())]

    def _destination_eras(self) -> list[str]:
        """The player's destination menu: this age first (a move within it), then the reachable
        others."""
        current = str(self.era or "").strip().lower()
        return ([current] if current else []) + [e for e in self._reachable_eras()
                                                 if e != current]

    def _group_allowed(self, era: str, area: str, group: str) -> bool:
        """Whether an area of `era` holds a top-level place (ward) with this picker label."""
        wanted = " ".join(str(group or "").split()).lower()
        if not wanted:
            return False
        area = " ".join(str(area or "").split()).lower()
        for a, groups in self._visited_by_area(era):
            if area and a.lower() != area:
                continue
            if any(group_identity(g["group"]) == group_identity(wanted) for g in groups):
                return True
        return False

    def _group_landing(self, era: str, area: str, group: str) -> dict | None:
        """A named ward as a landing: its area/kingdom and exact label, or None.

        The player may name a ward without a place; the engine lands them in that ward's
        settlement and hands the Game Master the exact place to choose. The newest place in
        the group supplies the address (never re-invented)."""
        wanted = " ".join(str(group or "").split()).lower()
        if not wanted:
            return None
        area = " ".join(str(area or "").split()).lower()
        for a, groups in self._visited_by_area(era):
            if area and a.lower() != area:
                continue
            for g in groups:
                if (group_identity(g["group"]) != group_identity(wanted)
                        or not g["places"]):
                    continue
                p = g["places"][0]
                return {"group": g["group"],
                        "area": " ".join(str(p.get("area") or "").split()),
                        "kingdom": " ".join(str(p.get("kingdom") or "").split())}
        return None

    def _all_other_eras(self) -> list[str]:
        """Every playable era but the one the Traveller is standing in (the destination menu).

        Includes the future even while locked, so the panel can show it disabled."""
        return [e for e in playable_eras() if e != self.era]

    def _era_options(self, eras: list[str]) -> list[dict]:
        future = self._future_era()
        locked = bool(future) and not self._future_unlocked()
        current = str(self.era or "").strip().lower()
        return [{"id": e, "name": device.era_label(e),
                 "locked": e == future and locked and e != current,
                 "current": e == current} for e in eras]

    def _device_state(self) -> dict:
        """Everything the Device panel needs to draw its controls."""
        total = device.part_total()
        live = (not self.classic) and self.has_device
        future = self._future_era()
        abilities = {
            "release": live,                            # fire the jump now
            "era": live,                                # name any age
            "area": live,                               # name an area the save has stood in
            "place": live,                              # name an offered place
        }
        return {
            "parts": self.parts_recovered,
            "total": total,
            "present": live,
            "in_combat": self._in_combat,
            "abilities": abilities,
            "future_era": future,
            "future_name": device.era_label(future) if future else "",
            "future_locked": bool(future) and not self._future_unlocked(),
            "era_options": self._era_options(self._destination_eras()),
            "era_names": {e: device.era_label(e) for e in playable_eras()},
            "all_eras": self._era_options(self._all_other_eras()),
            "places": self._place_options() if live else {},
        }

    async def _refresh_device(self) -> None:
        """Read the recovered parts and the visit history from the live player DB."""
        try:
            text = await self._call_tool_text("dump_player_db", {})
            player = json.loads(text)
        except Exception:  # noqa: BLE001 - a Device read must never sink a session
            player = {}
        if not isinstance(player, dict):
            player = {}
        inventory = player.get("inventory") if isinstance(player.get("inventory"), list) else []
        self.has_device = device.has_device(inventory)
        self.parts_recovered = len(device.recovered_parts(inventory))
        journey = _split_journey(player.get("journey"))
        if self.era and (not journey or journey[-1] != self.era):
            # The save is the truth about where the Traveller is now: keep the whole story
            # up to (and including) the last time they stood here, or add it if this is new.
            if self.era in journey:
                journey = journey[:journey.rindex(self.era) + 1]
            else:
                journey.append(self.era)
        self.journey = journey or ([self.era] if self.era else [])

    def _visited_pool(self, era: str, limit: int | None = None) -> list[str]:
        """The arrival phrases for the places this save has stood in, in an era.

        Newest first -- the manifest records a `used` timestamp per place -- and `limit` caps
        the list. Read from the scene manifest (memory or disk); it never enters the prompt."""
        try:
            places = known_scene_places(self.base_dir / OUTPUT_DIR, self.active_name or "", era)
        except Exception:  # noqa: BLE001 - never fail a jump over the arrival point
            places = []
        places = sorted(places, key=lambda p: int(p.get("used") or 0), reverse=True)
        out = _arrival_candidates(places)
        return out[:limit] if limit else out

    def _visited_by_area(self, era: str) -> list[tuple[str, list[dict]]]:
        """The save's visited places in `era`, grouped by area and then by top-level ward.

        Each area keeps its newest groups, and each group at most `PLACE_MENU_RECENT` places
        (newest first), so a city the Traveller walked all over cannot crowd another out of
        the picker -- the same bound the GM's block keeps, one level down. A one-segment
        legacy path has no ward and joins the area's own `""` group."""
        try:
            places = known_scene_places(self.base_dir / OUTPUT_DIR, self.active_name or "", era)
        except Exception:  # noqa: BLE001 - never fail a jump over the place tree
            places = []
        places = sorted(places, key=lambda p: int(p.get("used") or 0), reverse=True)
        grouped: dict[str, dict[str, dict]] = {}
        order: list[str] = []
        for p in places:
            area = " ".join(str(p.get("area") or "").split())
            if area not in grouped:
                grouped[area] = {}
                order.append(area)
            key = place_group(p)
            bucket = grouped[area].setdefault(key, {"group": "", "places": []})
            if not bucket["group"]:
                path = [str(s).strip() for s in (p.get("place") or [])
                        if str(s or "").strip()]
                bucket["group"] = path[0] if len(path) >= 2 else ""
            if len(bucket["places"]) < PLACE_MENU_RECENT:
                bucket["places"].append(p)
        return [(area, list(grouped[area].values())) for area in order]

    def _place_labels(self, groups: list[dict]) -> list[str]:
        """Every offered place label in an area's groups, groups newest-first."""
        return [self._place_option_label(p.get("place"))
                for g in groups for p in g["places"]]

    def _place_option_label(self, place) -> str:
        """A place's picker label: its full path, joined ('Western Market — the wine-shop')."""
        path = place if isinstance(place, list) else ([place] if place else [])
        return " — ".join(str(s).strip() for s in path if str(s or "").strip())

    def _place_options(self) -> dict:
        """{era id: {"areas": [{"area", "places", "groups"}]}} for every destination era.

        The picker has three joints: an **area** the save has stood in, the top-level
        **ward** the path starts with, then up to `PLACE_MENU_RECENT` places in that ward
        (newest first). `places` is the flat list the release matcher and older clients use;
        `groups` is what the UI draws the ward joint from. An age with none is new to the
        Traveller -- the Game Master chooses the landing. The age they stand in IS a
        destination: it moves them within it."""
        out: dict = {}
        for era in self._destination_eras():
            areas = [{"area": area,
                      "places": self._place_labels(groups),
                      "groups": [{"group": g["group"],
                                  "places": [self._place_option_label(p.get("place"))
                                             for p in g["places"]]}
                                 for g in groups]}
                     for area, groups in self._visited_by_area(era)]
            out[era] = {"areas": areas}
        return out

    def _landing_place(self, era: str, area: str, label: str) -> dict | None:
        """The visited place the player picked in `era`/`area`, or None to let the GM choose.

        `label` is a picker label (a full place path); it is matched back to the manifest entry
        it came from, so the landing carries the real address (kingdom / area / path)."""
        wanted = " ".join(str(label or "").split()).lower()
        if not wanted:
            return None
        area = " ".join(str(area or "").split()).lower()
        for a, groups in self._visited_by_area(era):
            if area and a.lower() != area:
                continue
            for g in groups:
                for p in g["places"]:
                    if self._place_option_label(p.get("place")).lower() != wanted:
                        continue
                    path = [str(s).strip() for s in (p.get("place") or [])
                            if str(s or "").strip()]
                    if not path:
                        continue
                    return {"kingdom": str(p.get("kingdom") or "").strip(),
                            "area": str(p.get("area") or "").strip(), "place": path}
        return None

    def _arrival_label(self, place, area: str) -> str:
        """A declared place as an arrival phrase: its first path segment, in its settlement.

        Deliberately the same shape `_arrival_candidates` derives for the visited pool, so
        a re-pinned arrival round-trips back through `_player_arrival` on the next load."""
        path = place if isinstance(place, list) else ([place] if place else [])
        labels = _arrival_candidates([{"place": path, "area": area}])
        return labels[0] if labels else ""
