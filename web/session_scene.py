"""The scene: places, captions, the era opening, and the jump."""

import json
import sys
from pathlib import Path

# Make the repo root importable so we can reuse existing (frozen) modules.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from .eras import era_display, render_era_text
from .gm_view import _trim_legend, render_met_block
from .scenes import (current_scene_place, known_cast_names, known_cast_people,
                     known_npc_names, known_scene_places, mark_place_dead)
from .places import _scene_key, name_key, path_label, places_feed
from .player_file import _jump_reset_note, _sheet_body
from .protocol import (
    CARRY_NOTE, DEVICE_CLOSES, DEVICE_DECLARES, DEVICE_FIRES, DEVICE_MOVES, ERA_MODEL,
    ERA_MODEL_AT_JUMP, ERA_STATE_ASK, HELP_TEXT, JUMP_ERA_LABEL, LEGEND_RECOVERY_NUDGE,
    MET_ASK,
    OPENING_RULES, SITUATION_DEVICE_MOVE, SITUATION_JUMP, SITUATION_NEW, SITUATION_RESUME,
    WORLD_MODEL
)
from .session_base import OUTPUT_DIR
from .stats import build_stats
import device


class SceneMixin:
    def _connect_arrival_to_declared_place(self) -> bool:
        """A resumed save already has a place declared: make the arrival string be it.

        The arrival is the label of the place the GM declared for the age, and the same phrase
        the visited pool lists. Returns whether a place was connected."""
        if self.classic:
            return False
        try:
            current = current_scene_place(self.base_dir / OUTPUT_DIR, self.active_name or "",
                                          self.era)
        except Exception:  # noqa: BLE001 - never fail a session over the arrival point
            return False
        if not isinstance(current, dict):
            return False
        cur_era = str(current.get("era") or "").strip().lower()
        if cur_era and cur_era != str(self.era or "").strip().lower():
            return False
        label = self._arrival_label(current.get("place"), current.get("area"))
        if not label:
            return False
        self.arrival = label
        return True

    def _reload_era_scene_state(self) -> None:
        """Re-read the places, cast and roles for the era the Traveller is now in."""
        try:
            places = known_scene_places(self.base_dir / OUTPUT_DIR, self.active_name or "",
                                        None if self.classic else self.era)
        except Exception:  # noqa: BLE001 - never fail a session over the place tree
            places = []
        self._primed = places
        # The live ordering stamp starts ABOVE every stamp loaded from disk, so a place the GM
        # touches this session always outranks one that was merely loaded.
        self._place_tick = max((int(p.get("used") or 0) for p in places), default=0)
        self._dead_perm = {}
        self._dead_declared = {}
        for p in places:
            self._dead_perm[(str(p.get("era") or ""), _scene_key(p.get("place")))] = {
                str(n).strip().lower() for n in (p.get("dead") or []) if str(n or "").strip()}
        self._scene_places = {_scene_key(p["place"]): (p["kingdom"], p["area"]) for p in places}
        self._scene_seeded = {_scene_key(p["place"]) for p in places if p.get("seeded")}
        try:
            self._current_place = current_scene_place(
                self.base_dir / OUTPUT_DIR, self.active_name or "",
                None if self.classic else self.era)
        except Exception:  # noqa: BLE001 - never fail the caption
            self._current_place = None
        try:
            self._cast_names = {name_key(n) for n in
                               known_npc_names(self.base_dir / OUTPUT_DIR, self.active_name or "")}
            self._cast_display = {name_key(n): n for n in
                                 known_cast_names(self.base_dir / OUTPUT_DIR,
                                                  self.active_name or "")}
            cast_people = known_cast_people(self.base_dir / OUTPUT_DIR, self.active_name or "")
        except Exception:  # noqa: BLE001
            self._cast_names = set()
            self._cast_display = {}
            cast_people = []
        # Identity is learned from the era's places AND the storyline cast: the per-turn tree
        # reads the place rosters directly, while `_npc_people` backs the declaration checks
        # and the on-demand record's cast footer (there is no late "on stage" echo).
        self._npc_people = {}
        for p in places:
            self._remember_people(p.get("main_npcs"))
        self._remember_people(cast_people)
        self._sync_dead_display()

    def _sync_dead_display(self) -> None:
        """Merge the manifest's permanent dead names with this visit's provisional ones
        into each primed place, for `format_known_places`."""
        for p in self._primed:
            key = (str(p.get("era") or ""), _scene_key(p.get("place")))
            dead = (set(self._dead_perm.get(key, set()))
                    | set(self._dead_provisional.get(key, set()))
                    | set(self._dead_declared.get(key, set())))
            p["dead"] = sorted(dead)

    def _track_main_npc_deaths(self, summary) -> None:
        """Mirror the combat registry's `killed` state onto the current place's main NPCs.

        A main_npc of `_current_place` that reaches 0 HP is added to this visit's provisional
        dead set; a provisional name that comes back alive is cleared. The manifest's
        permanent `dead` is never cleared here -- it is frozen at Save / era-turn."""
        place = self._current_place
        if not isinstance(place, dict) or not isinstance(summary, list):
            return
        era = "" if self.classic else str(self.era or "")
        key = (era, _scene_key(place.get("place")))
        declared: set[str] = set()
        for p in self._primed:
            if str(p.get("era") or "") == era and _scene_key(p.get("place")) == key[1]:
                declared = {str(npc.get("name") or "").strip().lower()
                            for npc in (p.get("main_npcs") or []) if isinstance(npc, dict)}
                break
        if not declared:
            return
        provisional = self._dead_provisional.setdefault(key, set())
        for entry in summary:
            if not isinstance(entry, dict) or entry.get("is_player"):
                continue
            name = str(entry.get("name") or "").strip().lower()
            if not name or name not in declared:
                continue
            if entry.get("killed"):
                provisional.add(name)
            else:
                provisional.discard(name)
        self._sync_dead_display()

    def _freeze_dead(self) -> None:
        """Fold this visit's provisional deaths into the permanent manifest `dead`.

        Called at Save and at era-turn: from then on a heal cannot clear them. The live
        manifest is mutated now; `commit_scene_manifest` writes it on Save."""
        if not self._dead_provisional:
            return
        by_key: dict[tuple, dict] = {}
        for p in self._primed:
            by_key[(str(p.get("era") or ""), _scene_key(p.get("place")))] = p
        for key, names in list(self._dead_provisional.items()):
            if not names:
                continue
            self._dead_perm.setdefault(key, set()).update(names)
            p = by_key.get(key)
            if not p:
                continue
            try:
                mark_place_dead(self.base_dir / OUTPUT_DIR, self.active_name or "", key[0],
                                p.get("kingdom", ""), p.get("area", ""), p.get("place") or [],
                                list(names))
            except Exception:  # noqa: BLE001 - never fail a Save/era-turn over liveness
                pass
        self._dead_provisional = {}
        self._sync_dead_display()

    def _caption_text(self) -> str:
        """The scene caption from what the engine knows: the age (era game only) and the
        current place, falling back to the rolled arrival when no place is declared yet (e.g.
        just after a jump). Never time of day or weather -- text mode has no scene tool to carry
        them, so they are dropped."""
        parts: list[str] = []

        def add(part: str) -> None:
            part = str(part or "").strip()
            if part and (not parts or parts[-1].casefold() != part.casefold()):
                parts.append(part)

        if not self.classic and self.era:
            add(device.era_label(self.era) or "")
        placed = False
        current = self._current_place
        if isinstance(current, dict):
            cur_era = str(current.get("era") or "").strip().lower()
            # A place from another age is stale -- never caption it.
            if self.classic or not cur_era or cur_era == str(self.era or "").strip().lower():
                area = str(current.get("area") or "").strip()
                place = " — ".join(str(p).strip() for p in (current.get("place") or [])
                                   if str(p or "").strip())
                if area:
                    add(area)
                if place:
                    add(place)
                placed = bool(area or place)
        if not placed:
            add(self.arrival or "")
        return " · ".join(p for p in parts if p)

    async def _emit_caption(self) -> None:
        """Tell the client the current scene caption (the text-mode Game Master header)."""
        await self._emit({"type": "caption", "text": self._caption_text(),
                          "era": "" if self.classic else str(self.era or ""),
                          "era_name": "" if self.classic else device.era_label(self.era)})

    def _places_body(self) -> str:
        """The KNOWN PLACES message: the current area, the marked line, the other areas."""
        return places_feed(self._primed, self._current_place, self.scene_images)

    def _next_place_tick(self) -> int:
        """The live ordering stamp for the place list, one step past the last.

        Seeded in `_reload_era_scene_state` ABOVE every stamp loaded from disk, so a place the
        GM touches this session outranks one that was merely loaded, and two touches in the same
        second still order strictly (the manifest's own timestamps are second-resolution)."""
        self._place_tick += 1
        return self._place_tick

    def _refresh_places_block(self) -> None:
        """Rewrite the places message in place when what it says has changed.

        The block is scoped to where the GM is, so a move must move it -- but only a move the
        rendered text would show does any work, and the message sits LAST in the fixed prefix,
        so a rewrite invalidates the prompt cache from there on and nothing earlier.
        """
        at = self._places_at
        if at is None or not (0 <= at < len(self.messages)):
            return
        entry = self.messages[at]
        if entry.get("role") != "system":
            return
        body = self._places_body()
        if entry.get("content") != body:
            self.messages[at] = {"role": "system", "content": body}

    def _era_opening(self, resumed: bool = False) -> str:
        """The lines the GM is handed with an era: which situation this is, where the Traveller
        is in it, that the kit is their own -- literal in every age -- and the opening rules
        (no batch, no pause token; build the era model). Written once, never always-on: it
        rides the ERA_FILE at session start and the jump message after that."""
        parts = [SITUATION_RESUME if resumed else SITUATION_NEW]
        if resumed:
            at = f" You are already at {self.arrival}." if self.arrival else ""
            parts.append("The story continues from where it left off; you do not open a new "
                         "scene." + at)
        elif self.arrival:
            parts.append(f"You arrive at {self.arrival}.")
        parts.append(CARRY_NOTE)
        parts.append(OPENING_RULES)
        parts.append(ERA_MODEL)
        return "\n\n".join(parts)

    def _world_opening(self, resumed: bool = False) -> str:
        """The classic game's awakening: the situation and the opening rules, then the
        WORLD_FILE itself. The era game's mirror is `_era_opening` -- the classic game has no
        eras, so it gets no era index, no kit note and no `lookup` pointer."""
        parts = [SITUATION_RESUME if resumed else SITUATION_NEW]
        if resumed:
            parts.append("The story continues from where it left off; you do not open a new "
                         "scene.")
        parts.append(OPENING_RULES)
        parts.append(WORLD_MODEL)
        return "\n\n".join(parts)

    def _memory_message(self, just_left: str) -> str:
        """The eras' memory as one short block -- the compaction output.

        Labelled by role: the age the Traveller is in comes first, the one just left second.
        The closing sentence is load-bearing: the age's story names its main people, so a
        return must not play them as strangers. Everyone else is in the met register."""
        lines: list[str] = []
        seen: set[str] = set()
        for era, label in ((self.era, "THE AGE YOU ARE IN"),
                           (just_left, "THE AGE YOU LEFT")):
            line = self._legends.get(era, "")
            if not line or era in seen:
                continue
            seen.add(era)
            lines.append(f"{label} ({era_display(era)}): {line}")
        if not lines:
            return ""
        return ("WHAT THE AGES REMEMBER\n" + "\n".join(lines) +
                "\nPeople the memory of the age you are in names have met the Traveller and "
                "know him on sight; never present them as strangers.")

    def _met_message(self) -> str:
        """Who knows the Traveller in the age he stands in: names only, story-first.

        The legend's names come first (they are the story's people), then the rest. The dead
        of this age are marked, never dropped."""
        names = self._met.get(self.era) or []
        if not names:
            return ""
        dead: set[str] = set()
        for (era, _place), keys in (self._dead_perm or {}).items():
            if era == self.era:
                dead |= {str(k) for k in (keys or ())}
        return render_met_block(era_display(self.era), names,
                                dead=dead, legend=self._legends.get(self.era, ""))

    async def _fetch_sheet(self) -> str:
        """The lean CHARACTER SHEET block from the live player DB; '' on any failure.

        Engine-injected, so the sheet is always in context -- the GM cannot skip it, and a jump
        re-injects it. Built from the raw row map (the same payload the `/stats` and Device
        reads use); `_gm_tool_view` is bypassed, so `_carrying`'s encumbrance word survives."""
        try:
            text = await self._call_tool_text("dump_player_db", {})
            db = json.loads(text)
        except Exception:  # noqa: BLE001 - a missing sheet must never sink a session
            return ""
        block = _sheet_body(db) if isinstance(db, dict) else ""
        if not block:
            return ""
        store = db.get("companions") if isinstance(db, dict) else None
        companions = self._companions_block(store)
        self._companion_people = ([dict(p) for p in store.values() if isinstance(p, dict)]
                                  if isinstance(store, dict) else [])
        return f"{block}\n{companions}" if companions else block

    async def _ensure_legend(self) -> None:
        """Force the age's memory with quiet rounds (never shown as player prose).

        The legend is the only thing that survives the compaction, so it is worth a second
        attempt before giving up -- there is no engine-authored fallback."""
        for _ in range(2):
            if self._pending_legend:
                return
            await self._run_role(LEGEND_RECOVERY_NUDGE, "legend-fix", quiet=True)

    async def _close_and_jump(self, era: str, area: str = "", arrival: str = "",
                              group: str = "") -> None:
        """A player-driven jump: one closing turn first, so the GM can say what the age keeps.

        An automatic jump is warned ahead and the memory line rides its last turn; a jump the
        Traveller triggers has no such turn, so the engine manufactures one. `area`, `group`
        (a ward) and `arrival` (a place) are what the player named (all "" when they left it
        to the GM). The STATE ask comes first, because the memory marker must stay the last
        thing in the turn.
        """
        ask = (ERA_STATE_ASK.format(era=self.era) + "\n\n"
               + MET_ASK.format(known=", ".join(self._met.get(self.era, [])) or "none yet")
               + "\n\n" + DEVICE_CLOSES.format(name=device.era_label(era)))
        # The closing turn ENDS this age: the registry is frozen for it, so a place the GM
        # declares while narrating the departure's far side cannot be filed under the age
        # being left (the arrival turn declares it in the new age).
        self._crossing = True
        try:
            await self._run_turn(ask, "closing")
        finally:
            self._crossing = False
        await self._jump(to_era=era, area=area, arrival=arrival, group=group)

    async def _jump(self, to_era: str | None = None, area: str = "", arrival: str = "",
                    group: str = "") -> None:
        """The Device fires: a new age, and the place the Traveller lands in.

        `to_era` is the destination the player chose. `area` is one they have stood in,
        `group` a ward inside it and `arrival` a place inside that -- any may be "", and
        whatever is left out the GM chooses."""
        old = self.era
        # The memory line is the compaction output, so it has to be known BEFORE the era is
        # thrown away: the GM wrote it on the closing turn, and one quiet round forces a line
        # if it did not. The config template is the last resort.
        if not self.classic and not self._pending_legend:
            await self._ensure_legend()
        legend = _trim_legend(self._pending_legend)
        self._pending_legend = ""
        # The GM rewrites the age's whole memory on each departure: keep what still matters and
        # add what this leaving means. A failed rewrite keeps the memory already held. It lives
        # in memory only here -- the timeline is written on Save, so an unsaved session leaves the
        # previous save's memories intact.
        if legend:
            self._legends[old] = legend
        memory = self._legends.get(old, "")
        # The age's own STATE page, written on the closing turn beside the memory line: once
        # the age is left no Save can ever happen there again, so this is the only moment its
        # final truth can be captured. Best-effort: a miss keeps the page already held.
        self._commit_departure_state(old)
        # Freeze this visit's deaths before the age is thrown away: a return cannot undo them.
        self._freeze_dead()
        chosen = str(to_era or "").strip().lower()
        target = chosen if chosen in self._reachable_eras() else self.era
        self.era = target or self.era
        # The landing: a visited place the player picked (in an area they named), or None -- the
        # address is resolved from the save's own registry, never re-invented.
        landing = self._landing_place(self.era, area, arrival)
        ward = self._group_landing(self.era, area, group) if group and not landing else None
        chosen_area = self._clean_place(area)
        if landing:
            self.arrival = self._arrival_label(landing.get("place"), landing.get("area"))
        elif ward:
            chosen_area = ward["area"]
            self.arrival = self._arrival_label([ward["group"]], ward["area"])
        elif chosen_area:
            # The area alone: the GM chooses the exact place, but inside the area they named.
            self.arrival = chosen_area
        else:
            self.arrival = ""
        # The engine DB must agree FIRST: `dump_player_db` scopes reputation by the DB's era, so
        # a sheet fetched before this is scoped to the age just left. A save writes these back.
        await self._call_tool_text("set_player_field", {"key": "era", "value": self.era})
        await self._call_tool_text("set_player_field", {"key": "arrival", "value": self.arrival})
        # Where the Traveller has stood, for "previous era".
        self.journey = list(self.journey or []) + [self.era]
        await self._call_tool_text("set_player_field", {"key": "journey",
                                                         "value": ",".join(self.journey)})
        self._reload_era_scene_state()
        # The crossing ends the running spells before the sheet is rebuilt, so what the GM reads is
        # true: a buff reverts, a disguise ends, a curse or a geas stays.
        reset = await self._call_tool_text("device_jump_reset", {})
        reset_note = _jump_reset_note(reset)
        # The landing place when one was picked; an area alone keeps the caption to the area; else
        # None -- the GM declares the exact place below.
        if landing:
            self._set_current_place(landing.get("kingdom", ""), landing.get("area", ""),
                                    landing.get("place"))
        elif ward or chosen_area:
            self._set_current_place(ward["kingdom"] if ward else "", chosen_area, [])
        else:
            self._current_place = None
        # `_compact` throws the sheet away with the rest, so rebuild it from the live DB first.
        self._sheet_block = await self._fetch_sheet()
        self._compact(old)
        self._era_started_turn = self.turn_counter
        if memory:
            await self._emit({"type": "notice", "title": "The Age Remembers", "text": memory})
        # The new age's caption now (era only when the GM still has to choose the landing).
        await self._emit_caption()
        if landing:
            landing_line = self._landing_line(landing)
        elif ward:
            landing_line = (f"You arrive in {ward['group']}, in {chosen_area}; "
                            "choose the exact place inside it.")
        elif chosen_area:
            landing_line = f"You arrive in {chosen_area}; choose the exact place inside it."
        else:
            landing_line = "This age is new to you; choose where the Traveller lands."
        situation = SITUATION_JUMP.format(new=device.era_label(self.era) or self.era)
        jump = f"{situation}\n\n{DEVICE_FIRES.format(old=old, new=self.era, arrival=landing_line)}"
        if reset_note:
            jump = f"{jump}\n\n{reset_note}"
        if not landing and not self.classic and not self.scene_images:
            # Text mode has no scene call to carry the place, so the landing itself must declare it
            # (reusing the known path) -- that declaration is what pins the arrival.
            jump = f"{jump}\n\n{DEVICE_DECLARES}"
        # Hand the new age's scaffold WITH the jump, as the awakening does -- but labelled, so it
        # cannot be mistaken for the session's ERA_FILE. Asking the GM to `lookup` it first mixed a
        # tool call with narration (the protocol forbids that), and a model resolved the conflict
        # with a holding line -- "I'll consult the era first." -- which stalled the turn.
        if not self.classic:
            jump = (f"{jump}\n\n{JUMP_ERA_LABEL}\n{render_era_text(self.era)}"
                    f"\n\n{ERA_MODEL_AT_JUMP}")
        if landing:
            # The player chose the exact landing: it is authoritative for this turn, and it
            # reaches the manifest before the GM says a word. A declaration that invents a
            # sibling is corrected right after the turn (`_unlock_landing`).
            self._landing_lock = dict(landing)
            self._landing_blocked = False
        try:
            if landing:
                await self._emit_place_note(self._landing_args(landing), era=self.era)
            await self._run_turn(jump, "jump")
        finally:
            await self._unlock_landing()
        await self._emit({"type": "cadence", **self._device_state()})

    def _landing_line(self, landing: dict) -> str:
        """The exact place the player chose, in the prompt's arrival line.

        The stored arrival stays the short label (`Ward, in Settlement`) -- that is what a
        resumed save and the visited pool expect -- but the GM must be told the WHOLE path it
        was sent to, or it invents the exact room (live 2026-10-18: a same-age release to a
        chosen leaf produced "You arrive at the Western Market" and a new sibling room).
        """
        path = path_label(landing.get("place"))
        area = str(landing.get("area") or "").strip()
        where = f"{path}, in {area}" if path and area else (path or area)
        return (f"You arrive at {where}. The Device has put the Traveller exactly here: "
                "narrate THIS place and do not move them.")

    async def _unlock_landing(self) -> None:
        """Restore a chosen landing a declaration tried to replace, then clear the lock.

        A KNOWN place or a room inside/around the landing is a real move and stands; a new
        sibling the GM invented is kept in the registry (its roster included) but cannot move
        the Traveller. The corrective `place_note` puts the manifest's `current` back on the
        landing, after the declared place's own write.
        """
        lock = self._landing_lock
        try:
            if lock is not None and self._landing_blocked:
                self._set_current_place(lock.get("kingdom", ""), lock.get("area", ""),
                                        lock.get("place"))
                await self._after_place_change()
                await self._emit_place_note(self._landing_args(lock), era=self.era)
        finally:
            self._landing_lock = None
            self._landing_blocked = False

    def _landing_args(self, landing: dict) -> dict:
        """The landing as a `place_note`: an address-only write (empty roster is a delta)."""
        return {"kingdom": str(landing.get("kingdom") or ""),
                "area": str(landing.get("area") or ""),
                "place": [str(p).strip() for p in (landing.get("place") or [])
                          if str(p or "").strip()],
                "main_npcs": [], "cast": []}

    async def _move_within_era(self, area: str = "", group: str = "",
                               place: str = "") -> None:
        """A release into the age the Traveller is already in: a move, not an age crossing.

        Nothing is compacted and no age memory is captured: the session, its history, the
        STATE page, the places tree and reputation all continue, so the people can react to
        what came before. The Device only changes where the Traveller stands; the one turn it
        runs narrates the disappearance and the arrival.
        """
        landing = self._landing_place(self.era, area, place)
        ward = self._group_landing(self.era, area, group) if group and not landing else None
        chosen_area = self._clean_place(area)
        if landing:
            self.arrival = self._arrival_label(landing.get("place"), landing.get("area"))
            chosen_area = landing.get("area") or chosen_area
        elif ward:
            chosen_area = ward["area"]
            self.arrival = self._arrival_label([ward["group"]], ward["area"])
        elif chosen_area:
            self.arrival = chosen_area
        else:
            self.arrival = ""
        await self._call_tool_text("set_player_field", {"key": "arrival",
                                                          "value": self.arrival})
        if landing:
            self._set_current_place(landing.get("kingdom", ""), landing.get("area", ""),
                                    landing.get("place"))
        elif ward or chosen_area:
            self._set_current_place(ward["kingdom"] if ward else "", chosen_area, [])
        else:
            self._current_place = None
        # Publish the new place. A landing pins the arrival through `_after_place_change`;
        # a ward/area alone keeps the sharper arrival written above (the pin would degrade
        # "Ward A, in Giza" to the bare settlement), so it only redraws the caption/block.
        if landing:
            await self._after_place_change()
        else:
            await self._emit_caption()
            self._refresh_places_block()
        if landing:
            landing_line = self._landing_line(landing)
        elif ward:
            landing_line = (f"You arrive in {ward['group']}, in {chosen_area}; "
                            "choose the exact place inside it.")
        elif chosen_area:
            landing_line = f"You arrive in {chosen_area}; choose the exact place inside it."
        else:
            landing_line = "Choose where the Traveller appears in this age."
        situation = SITUATION_DEVICE_MOVE.format(
            era=device.era_label(self.era) or self.era)
        move = f"{situation}\n\n{DEVICE_MOVES.format(arrival=landing_line)}"
        if not landing and not self.classic and not self.scene_images:
            # Text mode has no scene call to carry the place, so the move itself must declare it.
            move = f"{move}\n\n{DEVICE_DECLARES}"
        if landing:
            # The chosen landing is authoritative for this turn and reaches the manifest
            # before the GM narrates; a sibling invention is recorded but corrected at once.
            self._landing_lock = dict(landing)
            self._landing_blocked = False
        # The session continues: no compaction, no age memory, no spell reset. A stray
        # `{{remembers}}` / STATE block is stripped and discarded by these capture flags.
        try:
            if landing:
                await self._emit_place_note(self._landing_args(landing), era=self.era)
            await self._run_turn(move, "jump", capture_legend=False, capture_state=False)
        finally:
            await self._unlock_landing()
        await self._emit({"type": "cadence", **self._device_state()})

    async def _handle_slash(self, command: str) -> None:
        cmd = command.strip().lower()
        if cmd == "/help":
            await self._emit({"type": "notice", "title": "Help", "text": HELP_TEXT})
        elif cmd == "/stats":
            text = await self._call_tool_text("dump_player_db", {})
            try:
                db_data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                db_data = {}
            await self._emit({"type": "stats", "data": build_stats(db_data) if isinstance(db_data, dict) else {}})
        elif cmd == "/save":
            await self._handle_save_command()
        elif cmd == "/quit":
            await self._emit({"type": "notice", "title": "Quit", "text": "Closing connection to the void..."})
            await self._cmd_q.put(None)
        else:
            await self._emit({"type": "notice", "title": "Unknown command",
                              "text": f"{cmd}\n{HELP_TEXT}"})
        await self._emit({"type": "turn_end", "text": ""})
