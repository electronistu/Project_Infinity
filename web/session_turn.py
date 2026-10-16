"""The turn loop and the GM tool dispatch."""

import json
from .gm_view import _extract_legend, _gm_tool_view
from .scenes import known_scene_places
from .places import _people_list, _scene_key, name_key, path_label, resolve_place_path
from .protocol import (
    AWAKENING_RESUME, COMPANION_TOOL, MAX_RESUME_ROUNDS, MAX_THINKING_RETRIES,
    NARRATIVE_PHASE_TOOLS, NARRATIVE_RECOVERY_NUDGE, NPC_TOOL, PLACE_TOOL,
    PROSE_RECOVERY_NUDGE, RESUME_GUARD_NUDGE, SCENE_TOOL, _hidden_tools,
    has_unknown_marker, is_pause, strip_markers
)
from .session_base import OUTPUT_DIR

# Tools whose result may change the combat roster the client shows as tooltips.
COMBAT_TOOLS = frozenset({"register_combatants", "update_combatant", "end_combat",
                          "resolve_attack", "resolve_magic", "modify_exhaustion", "make_death_save"})


class TurnMixin:
    async def _run_role(self, role_content, label: str, quiet: bool = False) -> str:
        """Append a message and run the streaming tool loop, auto-resuming pauses."""
        if isinstance(role_content, str):
            self.messages.append({"role": "user", "content": role_content})
        else:
            self.messages.append(role_content)

        origin = label  # the first label matters; `label` becomes "resume" after a pause
        resumes = 0
        # Inspector metadata: `round` is the model-call number within this role, `trigger`
        # why the call is being made (start / tools / thinking / recovery / pause).
        gm = {"round": 0, "trigger": "start"}
        while True:
            result = await self._chat_with_tools(label, quiet=quiet, gm=gm)
            if result == "__SYSTEM_PAUSE__":
                await self._emit({"type": "paused"})
                resumes += 1
                if resumes > MAX_RESUME_ROUNDS * 3:
                    return ""  # give up; the caller's narrative recovery takes over
                # A model that echoes the pause token instead of narrating must not spin:
                # after a few resumes, tell it plainly to write the narrative. At session start
                # the first resume re-anchors the step it must resume at.
                if resumes > MAX_RESUME_ROUNDS:
                    nudge = RESUME_GUARD_NUDGE
                elif origin == "awakening":
                    nudge = AWAKENING_RESUME
                else:
                    nudge = "{{resume}}"
                self.messages.append({"role": "user", "content": nudge})
                label = "resume"
                gm["trigger"] = "pause"
                continue
            return result

    async def _stream_assistant(self, label: str, quiet: bool = False, gm: dict | None = None):
        """Stream one assistant message; emit deltas; append it to history.

        `quiet` suppresses narrative/thinking output (used for the scene-image
        corrective round, whose only visible effect is the tool call itself).
        `gm` carries the inspector's round/trigger metadata for this model call.
        """
        if not quiet:
            await self._emit({"type": "assistant_start", "label": label})
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict] = []
        prompt_eval = 0
        malformed = False

        gm = gm or {}
        snap = await self._record_gm_prompt(label, round=gm.get("round"),
                                            trigger=gm.get("trigger"))
        async for evt in self._stream(self.messages, self.tools_schema):
            et = evt["type"]
            if et == "thinking_delta":
                thinking_parts.append(evt["text"])
                if not quiet:
                    await self._emit({"type": "thinking_delta", "text": evt["text"]})
            elif et == "narrative_delta":
                content_parts.append(evt["text"])
                if not quiet:
                    await self._emit({"type": "narrative_delta", "text": evt["text"]})
            elif et == "tool_calls":
                tool_calls.extend(evt["calls"])
            elif et == "done":
                prompt_eval = evt.get("prompt_eval_count", 0) or 0
            elif et == "error":
                malformed = True
                await self._emit({"type": "error", "message": evt["message"]})

        content = "".join(content_parts)
        thinking = "".join(thinking_parts)
        # The closing turn's memory line rides the prose in a hidden marker; never let it
        # reach the client or the stored history as text.
        legend, content = _extract_legend(content)
        if legend:
            self._pending_legend = legend
        # A departure STATE page rides the closing turn the same way, and is stripped here
        # rather than at the jump so it can never reach the client.
        content = self._capture_state_pages(content)
        # A narrative has reached the player this turn; never ask for it again. This must test
        # the CLEANED text: a marker is emitted as *content*, so a bare `{{pause}}` used to mark
        # a turn as narrated that had no prose at all -- which defeated the narrative guarantee
        # and left the opening scene unwritten. A marker-shaped token no core matched (a drifted
        # spelling) is not prose either: it earns a recovery round instead of reaching the player.
        prose = strip_markers(content)
        if not quiet and prose and not has_unknown_marker(prose):
            self._narrative_emitted_turn = True
        if prompt_eval:
            self.current_context_tokens = prompt_eval
        await self._stamp_actual_tokens(snap, prompt_eval)
        await self._emit({"type": "context", "tokens": self.current_context_tokens,
                          "window": self.context_window})
        if not quiet:
            await self._emit({"type": "assistant_end", "text": content, "thinking": thinking})

        entry = {"role": "assistant", "content": content or ""}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if thinking:
            entry["thinking"] = thinking
        self.messages.append(entry)
        return content, thinking, tool_calls, malformed

    async def _chat_with_tools(self, label: str, quiet: bool = False,
                               gm: dict | None = None) -> str:
        gm = gm if gm is not None else {"round": 0, "trigger": "start"}
        recovered_prose = False
        while True:
            gm["round"] = int(gm.get("round", 0)) + 1
            content, thinking, tool_calls, malformed = await self._stream_assistant(
                label, quiet=quiet, gm=gm)
            if malformed:
                return "The GM stumbles over their words... (malformed response)"

            thinking_only = bool(thinking and not content and not tool_calls)
            retries = 0
            while thinking_only and retries < MAX_THINKING_RETRIES:
                retries += 1
                self.messages.append({"role": "user", "content": "Continue"})
                gm["round"] = int(gm.get("round", 0)) + 1
                gm["trigger"] = "thinking"
                content, thinking, tool_calls, malformed = await self._stream_assistant(
                    "continue", quiet=quiet, gm=gm)
                if malformed:
                    return "The GM stumbles over their words... (malformed response)"
                thinking_only = bool(thinking and not content and not tool_calls)

            if thinking_only and retries >= MAX_THINKING_RETRIES:
                return "The GM pauses, deep in thought..."

            if tool_calls:
                self._scene_rejected = False
                for tc in tool_calls:
                    await self._execute_tool(tc)
                names = {(tc.get("function") or {}).get("name") for tc in tool_calls}
                if content and not self._scene_rejected:
                    # The turn's narrative is delivered; the engine composes any
                    # mechanics. Running another round here is what makes the GM
                    # re-narrate (a duplicate segment in the client). A rejected scene
                    # call is the one case that must loop for a corrected re-call.
                    return content
                if not self._scene_rejected and names <= NARRATIVE_PHASE_TOOLS:
                    # Tool-only accepted image call (no prose).
                    if quiet:
                        # Corrective round: the image call was the whole point and any
                        # prose is deliberately invisible. Never loop here.
                        return content
                    if not recovered_prose and not self._narrative_emitted_turn:
                        # No prose this turn and the image arrived alone: never re-prompt
                        # on a bare tool result (the GM invents a meta/holding line); ask
                        # for the missing narration once, then end the turn either way.
                        recovered_prose = True
                        self.messages.append({"role": "user", "content": PROSE_RECOVERY_NUDGE})
                        gm["trigger"] = "recovery"
                        continue
                    return content
                # A rejected scene call (missing seed) keeps looping so the GM can
                # re-call with `establishing`; any other tool-only round loops back to
                # the prose the engine still needs.
                gm["trigger"] = "tools"
                continue

            if is_pause(content):
                return "__SYSTEM_PAUSE__"

            return content

    async def _ensure_turn_prose(self, result: str) -> str:
        """The turn's prose, guaranteed -- the one place both callers decide.

        Driven by the flag alone, never by the returned string: the engine's own placeholders
        ("The GM pauses, deep in thought...", "...(malformed response)") are truthy and are not
        prose, and testing them skipped the recovery while no narrative existed. A recovered
        round wins; otherwise whatever the model produced stands.
        """
        if self._narrative_emitted_turn:
            return result
        return await self._ensure_narrative() or result

    async def _ensure_narrative(self) -> str:
        """Guarantee the turn's prose: a non-quiet recovery round when the GM skipped the
        narrative (it echoed the pause token, or attached the image with no prose)."""
        if self._narrative_emitted_turn:
            return ""
        return await self._run_role(NARRATIVE_RECOVERY_NUDGE, "narrative-fix")

    async def _ensure_scene_image(self, narrative: str) -> None:
        """Guarantee one scene image per turn: a quiet corrective round for the
        GM to author the call (asking for `establishing` when the seed is missing),
        else an engine fallback built from the narrative."""
        if not self.scene_images or self._scene_requested_turn:
            return
        rejected = self._scene_rejected_place
        prose_missing = not self._narrative_emitted_turn
        if rejected:
            where = " — ".join(rejected)
            nudge = (
                "SYSTEM: this turn's request_scene_image was rejected because the place has no "
                f"establishing view yet. Emit ONLY request_scene_image again for \"{where}\", "
                "this time WITH `establishing` = a short, empty description of the place (no "
                "people, creatures or animals), plus the action `description`."
            )
        elif prose_missing:
            # Never assert a state the engine has not checked: with no prose this round IS
            # the narrative, so it runs non-quiet and asks for both beats at once.
            nudge = (
                "SYSTEM: this turn has no narrative yet. Write the narrative now — second "
                "person — in the SAME response as exactly ONE request_scene_image (it ends "
                "the turn), with the exact `place` path, plus `establishing` if this place "
                "has no establishing view yet."
            )
        else:
            nudge = (
                "SYSTEM: this turn still needs its request_scene_image (the imagery rule). The "
                "narrative is already written — do NOT repeat it. Emit ONLY the "
                "request_scene_image tool call now, with the exact `place` path, plus "
                "`establishing` if this place has no establishing view yet."
            )
        self._scene_rejected_place = None
        await self._run_role(nudge, "scene-fix", quiet=not prose_missing)
        if self._scene_requested_turn:
            return
        self._scene_requested_turn = True
        kingdom, area, place = self._match_known_place(narrative)
        if place:
            # The narrative named a place the save knows: that is where the story is, so the
            # engine's idea of it follows even when the GM never declared the move.
            self._set_current_place(kingdom, area, place)
            await self._after_place_change()
        npcs, self._pending_npcs = self._pending_npcs, []
        await self._emit({
            "type": "scene_request", "kind": "auto",
            "description": narrative or "",
            "era": self.era,
            "kingdom": kingdom, "area": area,
            "place": place,
            "time_of_day": "", "weather": "", "characters": {},
            "establishing": "", "main_npcs": [], "npcs": npcs, "seed_change": "",
            **(await self._scene_player_hints()),
            "turn": self.turn_counter,
        })

    def _match_known_place(self, narrative: str) -> tuple[str, str, list[str]]:
        """Best known (kingdom, area, place) whose path appears in the narrative."""
        text = str(narrative or "").lower()
        if not text:
            return "", "", []
        try:
            places = known_scene_places(self.base_dir / OUTPUT_DIR, self.active_name or "",
                                         self.era)
        except Exception:  # noqa: BLE001 - never fail a turn over location recovery
            return "", "", []
        best, score = ("", "", []), -1
        for p in places:
            path = [str(s).strip() for s in (p.get("place") or []) if str(s or "").strip()]
            if not path or path[-1].lower() not in text:
                continue
            s = sum(len(seg) for seg in path if seg.lower() in text)
            if s > score:
                best, score = (p["kingdom"], p["area"], path), s
        return best

    def _match_known_location(self, narrative: str) -> str:
        """Deepest known place name that actually appears in the narrative."""
        path = self._match_known_place(narrative)[2]
        return path[-1] if path else ""

    async def _refuse_unoffered_tool(self, name: str, call_id=None) -> None:
        """Refuse a tool this session never offered, without calling the MCP server.

        The schema filter only hides a tool from the offer; the server still registers it,
        so a hallucinated `request_scene_image` (images off) would otherwise run and return
        a false 'requested' promising an illustration that never comes."""
        if name == SCENE_TOOL:
            reason = ("Storyline images are OFF for this session; request_scene_image is "
                      "not available.")
            instruction = ("Do not request an illustration. Narrate the scene in prose, and "
                           "declare a new place once with note_place.")
        else:
            reason = f"'{name}' is not available for this session."
            instruction = "Use only the tools offered to you."
        text = json.dumps({"success": False, "error": "tool_not_available",
                           "reason": reason, "gm_instruction": instruction})
        gm_text = _gm_tool_view(name, text)
        await self._emit({"type": "tool_result", "name": name, "text": text,
                          "gm_text": gm_text, "is_error": True})
        self.messages.append({"role": "tool", "content": gm_text, "name": name})
        if self.provider == "gemini" and call_id:
            self.messages[-1]["id"] = str(call_id)

    async def _execute_tool(self, tool_call: dict) -> None:
        fn = tool_call.get("function", {})
        name = fn.get("name", "")
        args = fn.get("arguments", {}) or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        args = args if isinstance(args, dict) else {}
        if name == SCENE_TOOL:
            # `caption` was dropped from the tool; tolerate a stale model that still sends it.
            args.pop("caption", None)
        await self._emit({"type": "tool_call", "name": name, "arguments": args})
        self._place_note = ""
        # Identity first: a place the registry already holds keeps the registry's path, whatever
        # the GM's spelling (an article dropped, case, or a known place nested under a parent it
        # invented). Done BEFORE the seed check, so a drifted path cannot look like a new place.
        if name in (SCENE_TOOL, PLACE_TOOL):
            raw = args.get("place")
            declared = ([str(p).strip() for p in raw if str(p or "").strip()]
                        if isinstance(raw, list) else [])
            known = resolve_place_path(self._primed, declared)
            if known and _scene_key(known) != _scene_key(declared):
                self._place_note = (f'NOTE: "{path_label(declared)}" is held in the registry as '
                                    f'"{path_label(known)}" — the saved path is used; reuse it '
                                    "exactly.")
                args["place"] = known
        if name in _hidden_tools(self.scene_images, self.mode):
            await self._refuse_unoffered_tool(name, fn.get("id"))
            return
        warning = self._scene_seed_warning(args) if name == SCENE_TOOL else None
        if warning is not None:
            text, is_error = warning, True
            self._scene_rejected = True
            raw = args.get("place")
            self._scene_rejected_place = ([str(p).strip() for p in raw if str(p or "").strip()]
                                          if isinstance(raw, list) else [])
        else:
            self._scene_rejected = False
            try:
                result = await self.session.call_tool(name, arguments=args)
                text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
                is_error = bool(getattr(result, "isError", False))
            except Exception as e:  # noqa: BLE001
                text = f"Tool error: {type(e).__name__}: {e}"
                is_error = True
        if name == SCENE_TOOL:
            self._collect_declarations(args)
        if name == NPC_TOOL:
            self._remember_declared(args.get("npcs"))
        if name == COMPANION_TOOL and not is_error:
            self._remember_companion(args)
        if name == PLACE_TOOL and not is_error:
            self._record_place_live(args)
        gm_text = _gm_tool_view(name, text)
        notes: list[str] = []
        if name == SCENE_TOOL and warning is None:
            notes = [self._undeclared_note(args), self._on_stage_note(args)]
        if name in (SCENE_TOOL, NPC_TOOL, PLACE_TOOL):
            notes.append(self._missing_identity_note(args))
        if self._place_note:
            notes.append(self._place_note)
        for note in notes:
            if note:
                text = f"{text}\n\n{note}" if text else note
                gm_text = f"{gm_text}\n\n{note}" if gm_text else note
        # The client keeps the full result (debugging); the GM reads the trimmed view.
        await self._emit({"type": "tool_result", "name": name, "text": text,
                          "gm_text": gm_text, "is_error": is_error})
        self.messages.append({"role": "tool", "content": gm_text, "name": name})
        # Echo the Gemini function_call id alongside the result (strict matching on
        # Gemini 3.x). Ollama has no call ids, so it never sees this key.
        if self.provider == "gemini" and fn.get("id"):
            self.messages[-1]["id"] = str(fn.get("id"))
        # The engine composes the turn's Mechanics block from narrative_format; the client
        # shows it where the GM placed the {{_MECHANICS}} token.
        mech = self._collect_mechanics(text)
        if mech:
            self._mechanics_lines.extend(mech)
            await self._emit({"type": "mechanics", "lines": list(self._mechanics_lines)})
        if name in COMBAT_TOOLS:
            was_in_combat = self._in_combat
            roster = self._combat_roster_update(text)
            if roster is not None:
                await self._emit({"type": "combat_roster", "combatants": roster,
                                  "order": list(self._combat_order),
                                  "initiative": dict(self._combat_initiative),
                                  "combat_turn": dict(self._combat_turn),
                                  "in_combat": self._in_combat})
            if was_in_combat and not self._in_combat:
                line = self._combat_over_line()
                if line:
                    self._mechanics_lines.append(line)
                    await self._emit({"type": "mechanics",
                                      "lines": list(self._mechanics_lines)})
        if name == SCENE_TOOL and self.scene_images and warning is None and not self._scene_requested_turn:
            self._scene_requested_turn = True
            raw = args.get("place")
            place = [str(p).strip() for p in raw if str(p or "").strip()] if isinstance(raw, list) else []
            kingdom = str(args.get("kingdom") or "")
            area = str(args.get("area") or "")
            # A scene call may still name the wrong area for a place the registry holds: the
            # record decides (same rule as `note_place`).
            kingdom, area = self._corrected_address(kingdom, area, place)
            if str(args.get("establishing") or "") or str(args.get("seed_change") or ""):
                self._scene_places[_scene_key(place)] = (kingdom, area)
            if not (kingdom and area):
                # kingdom/area are optional once a place is known: recover them from the exact
                # path, else from the deepest known path that contains it (a new room inside a
                # known building keeps its settlement).
                kingdom, area = self._known_address(place, kingdom, area)
            # The GM's scene call is the images-on place declaration: the engine now knows where
            # the Traveller is, so the caption, the death attribution and the primed block follow.
            self._set_current_place(kingdom, area, place)
            await self._after_place_change()
            # Declarations flow to the server with the image call (the single writer).
            npcs, self._pending_npcs = self._pending_npcs, []
            main_npcs = args.get("main_npcs") if isinstance(args.get("main_npcs"), list) else []
            await self._emit({
                "type": "scene_request", "kind": "story",
                "description": str(args.get("description") or ""),
                "mood": str(args.get("mood") or ""),
                "era": self.era,
                "kingdom": kingdom, "area": area,
                "place": place,
                "time_of_day": str(args.get("time_of_day") or ""),
                "weather": str(args.get("weather") or ""),
                "characters": args.get("characters") if isinstance(args.get("characters"), dict) else {},
                "establishing": str(args.get("establishing") or ""),
                "main_npcs": main_npcs,
                "npcs": npcs,
                "seed_change": str(args.get("seed_change") or ""),
                **(await self._scene_player_hints()),
                "turn": self.turn_counter,
            })

        if name == PLACE_TOOL and not is_error:
            await self._emit_place_note(args)
            # The GM's note_place is the images-off place declaration: the arrival, the caption
            # and the primed block all follow it.
            await self._after_place_change()

        if name == "update_player_list":
            await self._sync_device_after_inventory(text)

    async def _sync_device_after_inventory(self, text: str) -> None:
        """A gear change can add or remove a Device part; the panel reads from the items."""
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(payload, dict) and payload.get("key") == "inventory":
            await self._refresh_device()
            await self._emit({"type": "cadence", **self._device_state()})

    def _collect_mechanics(self, text: str) -> list[str]:
        """The narrative_format lines from a tool result, for the engine-composed block."""
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(payload, dict):
            return []
        fmt = payload.get("narrative_format")
        if not isinstance(fmt, str):
            return []
        return [line for line in fmt.split("\n") if line.strip()]

    def _combat_roster_update(self, text: str) -> list[dict] | None:
        """Merge a combat tool result into the roster the client shows as tooltips.

        `sheets` (from register_combatants) carries the static stat lines; every combat
        tool's `registry_summary` carries the live HP/AC/conditions. Returns the merged
        roster (possibly empty, when a fight ends) or None when the result carries none."""
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        sheets = payload.get("sheets")
        if isinstance(sheets, list):
            for sheet in sheets:
                if isinstance(sheet, dict) and sheet.get("name"):
                    lines = sheet.get("lines")
                    if isinstance(lines, list):
                        self._combat_sheets[str(sheet["name"])] = [str(x) for x in lines]
        summary = payload.get("registry_summary")
        if not isinstance(summary, list):
            return None
        turn = payload.get("combat_turn")
        if isinstance(turn, dict):
            self._combat_turn = turn
        # Main NPCs of the current place: a name at 0 HP is marked dead for this visit.
        self._track_main_npc_deaths(summary)
        # One hostile, living, active non-player keeps combat on. Derived here so the save
        # gate and the Device cadence share the server's truth (`_in_active_combat`); an
        # empty summary (end_combat) clears it, so the bar always auto-closes.
        self._in_combat = any(
            isinstance(e, dict)
            and not e.get("is_player")
            and str(e.get("role") or "hostile").strip().lower() == "hostile"
            and not e.get("killed")
            and str(e.get("status") or "active").strip().lower() == "active"
            for e in summary)
        init = payload.get("initiative")
        if isinstance(init, list) and init:
            self._combat_initiative = {
                str(e.get("name")): {"roll": e.get("roll"), "modifier": e.get("modifier"),
                                    "total": e.get("total")}
                for e in init if isinstance(e, dict) and e.get("name")}
        order = payload.get("initiative_order")
        if isinstance(order, list) and order:
            self._combat_order = [str(x) for x in order]
        roster: list[dict] = []
        for entry in summary:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            item = dict(entry)
            item["sheet"] = self._combat_sheets.get(str(entry["name"]), [])
            roster.append(item)
        if roster:
            self._combat_last_roster = roster
        return roster

    def _combat_over_line(self) -> str:
        """One mechanics line when a fight ends: the combatants' fates (the panel is gone by
        then). Read from the last non-empty roster, so `end_combat` (an empty summary) still
        reports who fell."""
        dead = fled = surrendered = 0
        for entry in self._combat_last_roster:
            if not isinstance(entry, dict) or entry.get("is_player"):
                continue
            status = str(entry.get("status") or "").strip().lower()
            if entry.get("killed"):
                dead += 1
            elif status == "fled":
                fled += 1
            elif status == "surrendered":
                surrendered += 1
        parts = []
        if dead:
            parts.append(f"{dead} dead")
        if fled:
            parts.append(f"{fled} fled")
        if surrendered:
            parts.append(f"{surrendered} surrendered")
        return "Combat over — " + (", ".join(parts) if parts else "no casualties")

    def _remember_companion(self, args: dict) -> None:
        """A companion's look rides the image pipeline exactly like a recurring NPC's.

        `register_companion` declares the sheet once; the description inside it is buffered
        here so the illustrator keeps the face, and the GM names the companion from then on.
        The stats live in the engine's companion store, never in the prompt."""
        if str(args.get("action") or "declare").strip().lower() == "forget":
            return
        sheet = args.get("sheet") if isinstance(args.get("sheet"), dict) else {}
        name = " ".join(str(args.get("name") or "").split())
        if not name:
            return
        self._remember_declared([{"name": name, "race": sheet.get("race"),
                                  "class": sheet.get("class"),
                                  "description": sheet.get("description")}])

    def _remember_declared(self, npcs) -> list[str]:
        """Buffer declared NPCs (name + race/class + description) and remember their names."""
        added: list[str] = []
        if not isinstance(npcs, list):
            return added
        for entry in npcs:
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split())[:80]
            if not name:
                continue
            self._cast_names.add(name_key(name))
            self._pending_npcs.append({
                "name": name,
                "race": " ".join(str(entry.get("race") or "").split())[:40],
                "class": " ".join(str(entry.get("class") or "").split())[:40],
                "description": " ".join(str(entry.get("description") or "").split())[:400],
            })
            added.append(name)
        self._remember_people(npcs)
        return added

    def _collect_declarations(self, args: dict) -> None:
        """Declarations that ride a scene call: the `npcs` list + the place's main NPCs."""
        self._remember_declared(args.get("npcs"))
        main = args.get("main_npcs")
        self._remember_people(main)
        if isinstance(main, list):
            for entry in main:
                if not isinstance(entry, dict):
                    continue
                name = " ".join(str(entry.get("name") or "").split())
                if name:
                    self._cast_names.add(name_key(name))

    def _remember_people(self, entries) -> None:
        """Remember declared NPC identity (role, race, class), by identity key (`name_key`).

        An entry with none of the three is skipped: there is nothing to remind the GM of."""
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split())
            role = " ".join(str(entry.get("role") or "").split())
            race = " ".join(str(entry.get("race") or "").split())
            klass = " ".join(str(entry.get("class") or "").split())
            if name and (role or race or klass):
                self._npc_people[name_key(name)] = (name, role, race, klass)

    def _corrected_address(self, kingdom: str, area: str, place) -> tuple[str, str]:
        """A declared area that contradicts -- or omits -- an unambiguous known place.

        Live, the model given only "Wapping" filed a Wapping place under the area it was
        standing in (the block named the area but no path to reuse). When the path's first
        segment matches exactly ONE known place and the declared area differs, the registry
        wins: the record decides where a place is, not the declaration. Two areas sharing a
        first segment are left alone -- ambiguous, so the GM's word stands.

        A MISSING area is the same decision, and live 2026-10-16 it went the other way: the
        model declared a new path below a known district (`the Western Market / the
        dye-shops lane / the dyer's yard`) with no `kingdom`/`area` -- the shapes the tool
        calls for once the place exists -- and the engine kept neither, so the player's
        caption lost its settlement and the KNOWN PLACES block lost its scoping (an empty
        area matches EVERY place, see `places._in_area`).
        """
        key = _scene_key(place)
        declared_area = str(area or "").strip()
        if not key:
            return kingdom, area
        found: set[tuple[str, str]] = set()
        for known, address in self._scene_places.items():
            if not known or known[0] != key[0]:
                continue
            if address[0] or address[1]:
                found.add((address[0] or "", address[1] or ""))
            if len(found) > 1:
                return kingdom, area
        if len(found) != 1:
            return kingdom, area
        known_kingdom, known_area = next(iter(found))
        if not known_area.strip() or known_area.strip().lower() == declared_area.lower():
            return known_kingdom or kingdom, area
        return known_kingdom or kingdom, known_area

    def _record_place_live(self, args: dict) -> None:
        """Apply a `note_place` declaration to the live session (the tree updates at once).

        The path's identity was already resolved against the registry (`_execute_tool`); this
        adds the address correction and keeps the live tree, the map and the arrival in step."""
        raw = args.get("place")
        place = [str(p).strip() for p in raw if str(p or "").strip()] if isinstance(raw, list) else []
        if not place:
            return
        era = "" if self.classic else str(self.era or "")
        kingdom = " ".join(str(args.get("kingdom") or "").split())
        area = " ".join(str(args.get("area") or "").split())
        declared_area = area
        kingdom, area = self._corrected_address(kingdom, area, place)
        corrected = area if area != declared_area else ""
        # The correction must reach the manifest too (the `place_note` event is built from
        # `args`), so write it back rather than keeping it in the live session alone.
        args["kingdom"], args["area"] = kingdom, area
        main = args.get("main_npcs") if isinstance(args.get("main_npcs"), list) else []
        cast = args.get("cast") if isinstance(args.get("cast"), list) else []
        self._scene_places[_scene_key(place)] = (kingdom, area)
        self._remember_people(main)
        self._remember_people(cast)
        for entry in list(main) + list(cast):
            if isinstance(entry, dict):
                name = " ".join(str(entry.get("name") or "").split())
                if name:
                    self._cast_names.add(name_key(name))
        key = _scene_key(place)
        live = None
        for p in self._primed:
            if _scene_key(p.get("place")) == key and str(p.get("era") or "") == era:
                live = p
                break
        people = _people_list(main)
        if live is None:
            self._primed.append({"era": era, "kingdom": kingdom, "area": area,
                                 "place": place, "description": "",
                                 "main_npcs": people, "dead": [],
                                 "used": self._next_place_tick()})
        else:
            live["kingdom"] = kingdom or live.get("kingdom", "")
            live["area"] = area or live.get("area", "")
            # A re-declaration is the GM saying the story MOVED here: it re-stamps the live
            # ordering, so the place just returned to is the newest one here as well (the
            # manifest re-stamps its own `used` in `record_place`).
            live["used"] = self._next_place_tick()
            if people:
                live["main_npcs"] = people
                # A re-declaration must not wipe liveness: keep the dead names still declared.
                declared = {str(npc.get("name") or "").strip().lower() for npc in people}
                live["dead"] = [n for n in (live.get("dead") or []) if str(n).lower() in declared]
                self._dead_perm[(era, key)] = {n for n in self._dead_perm.get((era, key), set())
                                               if n in declared}
        # The place just declared is where the GM is now.
        self._set_current_place(kingdom, area, place)
        if corrected and corrected != declared_area:
            held = (f'not in "{declared_area}"' if declared_area else "and you declared none")
            note = (f'NOTE: "{path_label(place)}" is held in area "{corrected}", {held} — the '
                    "place is filed at the record's address; reuse its exact kingdom / area / "
                    "place path.")
            self._place_note = f"{self._place_note}\n\n{note}" if self._place_note else note

    def _set_current_place(self, kingdom: str = "", area: str = "", place=None) -> None:
        """The one writer for "where the GM is now".

        Four sources call it: an accepted scene call, a `note_place`, a Device landing, and the
        session start's registry read. Nothing else may assign `_current_place`: the caption,
        the death attribution and the primed block all read it, and a second writer is how the
        engine came to believe the story was still in the place it last declared.
        """
        path = [str(p).strip() for p in (place or []) if str(p or "").strip()]
        self._current_place = {"era": "" if self.classic else str(self.era or ""),
                               "kingdom": " ".join(str(kingdom or "").split()),
                               "area": " ".join(str(area or "").split()),
                               "place": path}

    async def _after_place_change(self) -> None:
        """Publish a place change: the arrival, the caption, and the primed block."""
        await self._pin_arrival((self._current_place or {}).get("place"),
                                (self._current_place or {}).get("area"))
        await self._emit_caption()
        self._refresh_places_block()

    def _known_address(self, place, kingdom: str = "", area: str = "") -> tuple[str, str]:
        """A place's kingdom/area from the registry.

        The deepest known path that contains this one and carries an address: a new room inside
        a known building keeps its settlement, so the caption, the arrival and the primed block
        stay anchored even though the room itself was never declared. An entry with no address
        (a declaration that omitted kingdom/area) is skipped rather than shadowing its parent.
        """
        key = _scene_key(place)
        if not key:
            return kingdom, area
        best: tuple | None = None
        for known, (known_kingdom, known_area) in self._scene_places.items():
            if (not (known_kingdom or known_area) or len(known) > len(key)
                    or key[:len(known)] != known):
                continue
            if best is None or len(known) > best[0]:
                best = (len(known), known_kingdom, known_area)
        if best is None:
            return kingdom, area
        return best[1] or kingdom, best[2] or area

    async def _pin_arrival(self, place, area) -> None:
        """Where the GM declares the Traveller is, that is the arrival string.

        There are no preset arrivals: the GM chooses the place, and this keeps the arrival's one
        string in step with it (persisted, so a reload can say "you are already at ..." before the
        save's place registry is even read)."""
        if self.classic:
            return
        label = self._arrival_label(place, area)
        if not label:
            return
        # Never degrade a specific arrival to a vaguer one: a room whose settlement the engine
        # could not resolve must not overwrite "<place>, in <settlement>" with the bare name.
        if (not str(area or "").strip() and ", in " in str(self.arrival or "")
                and label.split(",")[0].strip().lower()
                == str(self.arrival).split(",")[0].strip().lower()):
            return
        self.arrival = label
        await self._call_tool_text("set_player_field", {"key": "arrival", "value": label})

    async def _emit_place_note(self, args: dict) -> None:
        """Tell the server to persist the place (the manifest's single writer)."""
        raw = args.get("place")
        place = [str(p).strip() for p in raw if str(p or "").strip()] if isinstance(raw, list) else []
        await self._emit({
            "type": "place_note",
            "era": "" if self.classic else str(self.era or ""),
            "kingdom": " ".join(str(args.get("kingdom") or "").split()),
            "area": " ".join(str(args.get("area") or "").split()),
            "place": place,
            "main_npcs": args.get("main_npcs") if isinstance(args.get("main_npcs"), list) else [],
            "cast": args.get("cast") if isinstance(args.get("cast"), list) else [],
            "turn": self.turn_counter,
        })

    def _missing_identity_note(self, args: dict) -> str:
        """A soft note when a just-declared NPC carries no race and/or class.

        Rides the declaration's tool result, so the requirement costs no prefix."""
        entries: list = []
        for key in ("main_npcs", "npcs", "cast"):
            value = args.get(key)
            if isinstance(value, list):
                entries.extend(value)
        missing: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split())
            if not name:
                continue
            if not (str(entry.get("race") or "").strip() and str(entry.get("class") or "").strip()):
                missing.append(name)
        if not missing:
            return ""
        listed = "; ".join(f'"{n}"' for n in missing[:4])
        more = " (+more)" if len(missing) > 4 else ""
        return (f"NOTE: {listed}{more} declared with no race and/or class. Give every declared "
                "character a race and a class — never leave them human by default.")

    def _on_stage_note(self, args: dict) -> str:
        """The identity of each declared NPC the GM just put on stage.

        Roles/races/classes are deliberately absent from the priming tree -- they are wanted
        when a character appears, not on every turn -- so they ride the scene result instead,
        which costs nothing in the always-on prefix. Only names the GM itself listed, and
        only ones with a declared identity: a one-off extra's is invented on the spot."""
        characters = args.get("characters")
        if not isinstance(characters, dict) or not characters or not self._npc_people:
            return ""
        labels: list[str] = []
        seen: set[str] = set()
        for key in characters:
            key_l = name_key(key)
            found = self._npc_people.get(key_l)
            if not found or key_l in seen:
                continue
            seen.add(key_l)
            name, role, race, klass = found
            ident = " ".join(p for p in (race, klass) if p)
            tag = ", ".join(p for p in (ident, role) if p)
            labels.append(f"{name} ({tag})" if tag else name)
        return ("on stage: " + ", ".join(labels)) if labels else ""

    def _undeclared_note(self, args: dict) -> str:
        """A soft note listing `characters` names that no declaration covers.

        The key is the model's own spelling of a name (`Master_Guo` for a declared
        `Master Guo`), so it is folded with `name_key` before the check: an exact match
        told the GM its own declaration was missing."""
        characters = args.get("characters")
        if not isinstance(characters, dict) or not characters:
            return ""
        unknown: list[str] = []
        for key in characters:
            name = " ".join(str(key or "").split())
            if name and name_key(name) not in self._cast_names:
                unknown.append(name)
        if not unknown:
            return ""
        listed = "; ".join(f'"{n}"' for n in unknown[:4])
        return (
            f"NOTE: {listed} not declared. Declare recurring characters with register_npcs "
            "(name + description) so the illustrator keeps them consistent; an undeclared name "
            "is drawn from its own key text and is not remembered."
        )

    def _scene_seed_warning(self, args: dict) -> str | None:
        """Reject a new-place scene call that is missing its establishing view.

        The GM asks for the seed once per place; re-requesting an existing seed is
        harmless (the server is idempotent)."""
        if not self.scene_images:
            return None
        raw = args.get("place")
        place = [str(p).strip() for p in raw if str(p or "").strip()] if isinstance(raw, list) else []
        if not place:
            return None  # no place -> portrait-only action, nothing to seed
        if str(args.get("establishing") or "").strip() or str(args.get("seed_change") or "").strip():
            return None
        if _scene_key(place) in self._scene_places:
            return None
        where = " — ".join(place)
        return (
            f'WARNING: no establishing view exists yet for "{where}". This is a NEW place, so '
            "request its seed: call request_scene_image again with `establishing` = a short "
            "description of the place (empty and unpopulated — no people, creatures or animals), "
            "plus the same action `description`. The establishing view is generated once and is "
            "never shown to the player."
        )

    async def _call_tool_text(self, name: str, args: dict) -> str:
        result = await self.session.call_tool(name, arguments=args)
        return "\n".join(b.text for b in result.content if hasattr(b, "text"))

    async def _scene_player_hints(self) -> dict:
        """Live character bits for the scene image, from the engine's authoritative DB.

        The `.player` file is only a save-time snapshot (and drops active effects), so the scene
        server cannot see the live state. Returns `{active_effects, equipped}` so an
        appearance-changing effect (Disguise Self) and the current gear reach the image prompt.
        """
        try:
            text = await self._call_tool_text("dump_player_db", {})
            player = json.loads(text)
        except Exception:  # noqa: BLE001 - never fail a turn over optional scene hints
            return {}
        if not isinstance(player, dict):
            return {}
        hints: dict = {}
        effects = player.get("active_effects")
        if isinstance(effects, list):
            hints["active_effects"] = effects
        equipped = player.get("equipped")
        if isinstance(equipped, dict):
            hints["equipped"] = equipped
        return hints
