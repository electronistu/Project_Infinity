"""The turn loop and the GM tool dispatch."""

import json
from .eras import START_ERA, era_display
from .gm_view import _extract_legend, _gm_tool_view
from .scenes import _merge_npc_lists, known_cast_people, known_scene_places
from .places import (_people_list, _scene_key, move_placements, name_key, npc_query,
                     path_label, places_query, render_npc_answer, render_places_answer,
                     resolve_place_path)
from .protocol import (
    AWAKENING_RESUME, COMPANION_TOOL, MAX_RESUME_ROUNDS, MAX_THINKING_RETRIES,
    NARRATIVE_PHASE_TOOLS, NARRATIVE_RECOVERY_NUDGE, NPC_TOOL, PLACE_TOOL,
    PROSE_RECOVERY_NUDGE, RESUME_GUARD_NUDGE, SCENE_TOOL, STATE_ZERO_HP, _hidden_tools,
    has_unknown_marker, is_pause, place_echo_nudge, strip_markers
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
            if result == "__SYSTEM_DEAD__":
                return result
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
        if legend and self._capture_legend:
            self._pending_legend = legend
        # A departure STATE page rides the closing turn the same way, and is stripped here
        # rather than at the jump so it can never reach the client.
        content = self._capture_state_pages(content, store=self._capture_state)
        # Who the Traveller met in this age: names only, stripped like the STATE page. The
        # same capture flag applies -- a same-age move discards a stray marker (the full
        # history still carries every meeting; the age is not being left).
        content = self._capture_met(content, store=self._capture_state)
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
                    if self._dead:
                        break  # the fatal result ends the run: no further tool, no next round
                if self._dead:
                    return "__SYSTEM_DEAD__"
                # The 0 HP note rides AFTER the whole tool batch, never between tool results
                # (strict providers require the results to stay contiguous).
                if self._pending_zero_note:
                    self._pending_zero_note = False
                    self.messages.append({"role": "user", "content": STATE_ZERO_HP})
                names = {(tc.get("function") or {}).get("name") for tc in tool_calls}
                if content and not self._scene_rejected:
                    # The turn's narrative is delivered; the engine composes any
                    # mechanics. Running another round here is what makes the GM
                    # re-narrate (a duplicate segment in the client). A rejected scene
                    # call is the one case that must loop for a corrected re-call.
                    return content
                # A tool-only round is the turn's last word when it carries only the tools the
                # narrative phase may attach — and, in a QUIET corrective round, `note_place`
                # too: that round exists to get the declaration the engine asked for (see
                # `_ensure_place_note`), and looping here would send the model back to narrate
                # invisibly into history. The batch handshake is untouched: a `note_place`
                # batch still loops, so the model emits its `{{pause}}` and then the prose.
                end_tools = (NARRATIVE_PHASE_TOOLS | {PLACE_TOOL}) if quiet else NARRATIVE_PHASE_TOOLS
                if not self._scene_rejected and names <= end_tools:
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
        if self._crossing or not self.scene_images or self._scene_requested_turn:
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

    async def _ensure_place_note(self) -> None:
        """Guarantee the turn says where the Traveller is: ONE place, so one quiet round.

        Text mode's mirror of `_ensure_scene_image` -- with images on, the scene call carries
        the place every turn -- and the one place the engine can close the honest limit "a move
        the GM never declares cannot be inferred". It never reads the prose: a move into a room
        the record does not hold cannot be inferred from it anyway. It checks only what THIS
        turn declared, and asks when that is not exactly one: none (a forgotten move, a stale
        caption) or several (the engine keeps one current place and cannot tell which of them
        the turn ended in -- the model has the prose by now, so it can). A declaration the
        engine refused counts as none (the GM has the refusal in its context, and this round is
        where it corrects)."""
        if self._crossing:
            return  # the age is ending: the Traveller is not declaring a place in it
        if self._landing_lock is not None:
            return  # the engine chose the landing; the GM does not have to declare it
        if self.scene_images or len(self._place_declarations) == 1:
            return
        await self._run_role(place_echo_nudge(self._place_declarations), "place-fix", quiet=True)

    async def _ensure_opening_place(self) -> None:
        """The opening's place, guaranteed: a new game's arrival is a move no inference can
        recover, and text mode has no scene call to carry it. A resumed save already holds its
        current place (the manifest's `current`), so it is never asked to repeat itself; any
        declaration the opening made -- one, or several -- goes through the ordinary rule.
        Images mode is answered by the scene call itself (`_ensure_place_note` returns)."""
        if not self._place_declarations and (self._current_place or {}).get("place"):
            return
        await self._ensure_place_note()

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
            known = resolve_place_path(self._primed, declared, self._current_place)
            if known:
                # Adopt the saved spelling. The NOTE teaches; re-stating where the story
                # already stands teaches nothing, so a repeated echo stays quiet.
                if (_scene_key(known) != _scene_key(declared)
                        and _scene_key((self._current_place or {}).get("place") or [])
                        != _scene_key(known)):
                    self._place_note = (f'NOTE: "{path_label(declared)}" is held in the '
                                        f'registry as "{path_label(known)}" — the saved path '
                                        "is used; reuse it exactly.")
                    args["place"] = known
                else:
                    args["place"] = known
        if name in _hidden_tools(self.scene_images, self.mode):
            await self._refuse_unoffered_tool(name, fn.get("id"))
            return
        if name == "lookup" and places_query(args.get("query")) is not None:
            # A place query is answered here, from the live registry, and never reaches the
            # MCP server (which knows eras and polities, not where the story stands).
            await self._serve_places_lookup(args, fn.get("id"))
            return
        if name == "lookup" and npc_query(args.get("query")) is not None:
            # Name -> identity, status and exact place, from the same live registry. Same
            # rule: the MCP server has no registry, and must never answer a person query.
            await self._serve_npc_lookup(args, fn.get("id"))
            return
        warning = None
        if name == SCENE_TOOL:
            warning = self._scene_seed_warning(args)
        elif name == PLACE_TOOL:
            warning = self._place_address_warning(args)
        if warning is not None:
            text, is_error = warning, True
            if name == SCENE_TOOL:
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
        if name == NPC_TOOL:
            self._remember_declared(args.get("npcs"))
        if name == COMPANION_TOOL and not is_error:
            self._remember_companion(args)
        crossing = self._crossing
        if crossing and name in (SCENE_TOOL, PLACE_TOOL):
            # The Device is crossing: this turn ends the age. A place declared now is the
            # DESTINATION the GM narrated ahead of the crossing (live 2026-10-18: Memphis and
            # the Giza works were filed under Tang and offered by the Device there), so it is
            # not recorded -- the arrival turn declares it in the new age.
            self._place_note = ("NOTE: not recorded -- this age is ending; the arrival turn "
                                "declares the place in the new age.")
        place_changed = False
        raw_place = args.get("place")
        filed = ([str(p) for p in raw_place if str(p or "").strip()]
                 if isinstance(raw_place, list) else [])
        if name == SCENE_TOOL:
            # The scene call's place is a DECLARATION, recorded here like `note_place` so it no
            # longer waits for a picture. Only the turn's FIRST accepted call applies (the
            # engine keeps one current place); a second one is already dropped by the
            # scene-request flag. The declarations it carries still buffer for the illustrator.
            scene_applies = (warning is None and self.scene_images and not crossing
                             and not self._scene_requested_turn and bool(filed))
            if scene_applies:
                # kingdom/area are optional once a place is known: the record's first segment
                # decides, else the deepest known ancestor supplies the settlement.
                kingdom, area = self._corrected_address(
                    str(args.get("kingdom") or ""), str(args.get("area") or ""), filed)
                if not (kingdom and area):
                    kingdom, area = self._known_address(filed, kingdom, area)
                args["kingdom"], args["area"] = kingdom, area
                era = self._declaration_era(scene=True)
                # Asked BEFORE `_record_place_live` applies the delta: a roster that repeats
                # what the place already holds is not a change and must write nothing.
                roster_change = self._declares_roster_change(
                    filed, args.get("main_npcs"), era=era)
            if not crossing:
                self._collect_declarations(args)
            if scene_applies:
                before = self._current_place
                self._record_place_live(args, era=era)
                # A blocked declaration is still a change to PERSIST (its place and roster),
                # even though it did not move `_current_place`.
                place_changed = (not self._same_place(before, self._current_place)
                                 or roster_change or self._place_blocked_now)
        if name == PLACE_TOOL and warning is None and not is_error and not crossing:
            before = self._current_place
            # Asked BEFORE `_record_place_live` applies the delta: a declaration that repeats
            # what the place already holds -- a regular, a field, an unchanged state -- is not
            # a change and must write nothing. (Live 2026-10-17 an echo re-sent the whole
            # `note_place`, regular included, on a stay turn; the save was rewritten for it.)
            roster_change = self._declares_roster_change(
                args.get("place"), args.get("main_npcs"), era=self._declaration_era())
            cast_change = self._declares_new_people(args.get("cast"))
            self._record_place_live(args)
            # The turn declared where the Traveller is: this is what `_ensure_place_note`
            # counts. A REFUSED declaration never gets here, so the net still asks in the same
            # turn; a second declaration in the same turn makes the count 2, which is also an
            # ask (the engine cannot tell which of them the turn ended in).
            if filed:
                self._place_declarations.append(filed)
            place_changed = (not self._same_place(before, self._current_place)
                             or roster_change or cast_change or self._place_blocked_now)
        gm_text = _gm_tool_view(name, text)
        notes: list[str] = []
        if name == SCENE_TOOL and warning is None:
            notes = [self._undeclared_note(args)]
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
        self._note_player_state(text)
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
        if (name == SCENE_TOOL and self.scene_images and warning is None and not crossing
                and not self._scene_requested_turn):
            self._scene_requested_turn = True
            place = filed
            kingdom = str(args.get("kingdom") or "")
            area = str(args.get("area") or "")
            if place and (str(args.get("establishing") or "")
                          or str(args.get("seed_change") or "")):
                # The GM supplied the establishing view: accepted for this session (the server
                # draws it when the request arrives). The place stays unseeded ON DISK until
                # that image exists, so a later session still asks for the GM's description.
                self._scene_places[_scene_key(place)] = (kingdom, area)
                self._scene_seeded.add(_scene_key(place))
            if place_changed:
                # Persist BEFORE the image request: the server pump awaits the write, and the
                # browser's POST then sees the file-less entry (`ensure_scene` draws into it).
                await self._emit_place_note(args, era=self._declaration_era(scene=True))
                await self._after_place_change()
            if self._place_blocked_now and self._landing_lock:
                # The image follows the Device's landing, never the invented sibling.
                lock = self._landing_lock
                place = [str(p).strip() for p in (lock.get("place") or [])
                         if str(p or "").strip()]
                kingdom = str(lock.get("kingdom") or "")
                area = str(lock.get("area") or "")
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

        if name == PLACE_TOOL and not is_error and place_changed:
            # The GM's note_place is the images-off place declaration: the arrival, the caption
            # and the primed block all follow it. A re-declaration of where the story already
            # stands changes none of them -- and must not rewrite the save for it -- so the
            # persist and the three publish steps are skipped there (`_record_place_live` has
            # already re-stamped the live ordering).
            await self._emit_place_note(args)
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

    def _note_player_state(self, text: str) -> None:
        """Read a tool result's player HP/death marker.

        On the first drop to 0 HP in a turn it appends the STATE note once, so the GM knows
        a heal must be a tool call. It also latches death from any lethal player result --
        the marker rides top level (`modify_player_numeric`) or inside the player's
        `hp_change` (`resolve_attack` / `resolve_magic`).
        """
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(payload, dict):
            return

        def _hp(obj):
            if not isinstance(obj, dict):
                return None
            if obj.get("key") == "current_hit_points" and "new_value" in obj:
                try:
                    return int(obj["new_value"])
                except (TypeError, ValueError):
                    return None
            return None

        cause = str(payload.get("death_cause") or "")
        dead = payload.get("dead") is True or payload.get("outcome") == "dead"
        hp = _hp(payload)

        nested = payload.get("hp_change")
        nested_hp = _hp(nested)
        if nested_hp is not None:
            hp = nested_hp
        if isinstance(nested, dict):
            if nested.get("dead") is True:
                dead = True
            cause = str(nested.get("death_cause") or cause)

        targets = payload.get("targets")
        if isinstance(targets, list):
            for target in targets:
                if not isinstance(target, dict):
                    continue
                hp_change = target.get("hp_change")
                target_hp = _hp(hp_change)
                if target_hp is not None:
                    hp = target_hp
                if isinstance(hp_change, dict):
                    if hp_change.get("dead") is True:
                        dead = True
                    cause = str(hp_change.get("death_cause") or cause)

        if dead:
            self._dead = True
            self._stable = False
            self._at_zero = True
            if cause:
                self._death_cause = cause
            return
        if hp is None:
            return
        if hp > 0:
            self._at_zero = False
            self._stable = False
            self._dying_noted = False
            self._pending_zero_note = False
        elif not self._dying_noted:
            self._dying_noted = True
            self._pending_zero_note = True

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
        # Main NPCs of the current place: a name at 0 HP joins the dead index for this visit,
        # whoever dealt the blow; any other state is the GM's declared word.
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
            self._cast_display[name_key(name)] = name
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
        """Declarations that ride a scene call: the `npcs` list + the place's main NPCs.

        Buffer/remember only: applying the place and its roster is `_record_place_live`'s job
        (both tools share it), so nothing here touches `_primed` before the change gate runs."""
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

    def _declaration_era(self, scene: bool = False) -> str:
        """The era a place declaration is filed under.

        A scene-sourced declaration must use the same era the server's `/api/scene` writer
        uses (`session.era or START_ERA`), or a classic+images save would file one place twice:
        file-less under no era, then seeded under the fallback era. Text mode keeps the save's
        own convention (a classic save carries no era). The era game NEVER files a place
        without an era: an era-less entry is a wildcard in the registry and would be offered
        in every age's tree and Device picker."""
        if scene:
            return str(self.era or "") or START_ERA
        return "" if self.classic else (str(self.era or "") or START_ERA)

    def _record_place_live(self, args: dict, era: str | None = None) -> None:
        """Apply a place declaration to the live session (the tree updates at once).

        Used by both declaration tools: `note_place` (images off) and an accepted scene call
        (images on). The path's identity was already resolved against the registry
        (`_execute_tool`); this adds the address correction and keeps the live tree, the map,
        the roster and the current place in step. `era` lets a scene call use the server's
        scene-writer era instead of the text convention."""
        raw = args.get("place")
        place = [str(p).strip() for p in raw if str(p or "").strip()] if isinstance(raw, list) else []
        if not place:
            return
        self._place_blocked_now = False
        era = self._declaration_era() if era is None else str(era or "")
        kingdom = " ".join(str(args.get("kingdom") or "").split())
        area = " ".join(str(args.get("area") or "").split())
        declared_area = area
        kingdom, area = self._corrected_address(kingdom, area, place)
        if not (kingdom or area):
            # Nothing in the record names this place (a bare leaf the save has never held).
            # An address-less place would blind the caption AND the block's scoping (`_in_area`
            # with an empty area matches every place: the whole-era tree), so the settlement
            # the story stands in -- the only evidence there is -- is used, and the GM is told.
            here = self._current_place if isinstance(self._current_place, dict) else {}
            kingdom = str(here.get("kingdom") or "")
            area = str(here.get("area") or "")
            if area:
                note = (f'NOTE: "{path_label(place)}" is filed in the settlement the Traveller '
                        f'is in ("{area}"). If it is somewhere else, re-declare it with its '
                        "`kingdom` and `area`.")
                self._place_note = f"{self._place_note}\n\n{note}" if self._place_note else note
        in_record = any(_scene_key(p.get("place")) == _scene_key(place)
                        for p in self._primed)
        corrected = area if (area != declared_area
                             and (declared_area or not in_record)) else ""
        # The correction must reach the manifest too (the `place_note` event is built from
        # `args`), so write it back rather than keeping it in the live session alone.
        args["kingdom"], args["area"] = kingdom, area
        key = _scene_key(place)
        main = args.get("main_npcs") if isinstance(args.get("main_npcs"), list) else []
        cast = args.get("cast") if isinstance(args.get("cast"), list) else []
        self._scene_places[key] = (kingdom, area)
        # A declaration is a PLACEMENT: a person named here is recorded here and nowhere else,
        # so a regular recorded at another place moves (this also repairs a duplicate). Runs
        # BEFORE `_remember_people` so a state-only entry is enriched with the identity the
        # record already holds.
        moved = self._apply_people_moves(main, era, place)
        if moved:
            args["moved"] = moved
        self._remember_people(main)
        self._remember_people(cast)
        for entry in list(main) + list(cast):
            if isinstance(entry, dict):
                name = " ".join(str(entry.get("name") or "").split())
                if name:
                    self._cast_names.add(name_key(name))
        for entry in cast:
            if isinstance(entry, dict):
                name = " ".join(str(entry.get("name") or "").split())
                if name:
                    self._cast_display[name_key(name)] = name
        # The Device's chosen landing is authoritative for its turn. A KNOWN place (a real
        # move/return) or a room inside/around the landing may move the Traveller; a NEW
        # sibling the GM invented is recorded (roster included) but cannot replace the
        # landing. `_unlock_landing` restores it and the manifest's `current` after the turn.
        lock = self._landing_lock
        apply_current = True
        if lock is not None and lock.get("place"):
            lock_key = _scene_key(lock.get("place"))
            if key != lock_key:
                related = bool(key and lock_key) and (
                    key[:len(lock_key)] == lock_key or lock_key[:len(key)] == key)
                if not in_record and not related:
                    apply_current = False
                    self._place_blocked_now = True
                    self._landing_blocked = True
                    note = (f'NOTE: the Device landed the Traveller at '
                            f'"{path_label(lock.get("place"))}"; "{path_label(place)}" is '
                            "recorded, but it does not move them on this turn. If the turn "
                            "moved the Traveller, declare a place the registry already holds "
                            "or a room inside the landing.")
                    self._place_note = f"{self._place_note}\n\n{note}" if self._place_note else note
        live = None
        for p in self._primed:
            if _scene_key(p.get("place")) == key and str(p.get("era") or "") == era:
                live = p
                break
        people = _people_list(main)
        entry = live
        if entry is None:
            entry = {"era": era, "kingdom": kingdom, "area": area,
                     "place": place, "description": "",
                     "main_npcs": _merge_npc_lists([], people), "dead": [],
                     "used": self._next_place_tick()}
            self._note_declared_deaths(entry)
            self._primed.append(entry)
            # A declared death joins the death index at once, like an existing place's (a
            # moved regular's `state: dead` must show here too, not only in the roster).
            self._sync_dead_display()
        else:
            entry["kingdom"] = kingdom or entry.get("kingdom", "")
            entry["area"] = area or entry.get("area", "")
            # A re-declaration is the GM saying the story MOVED here: it re-stamps the live
            # ordering, so the place just returned to is the newest one here as well (the
            # manifest re-stamps its own `used` in `record_place`).
            entry["used"] = self._next_place_tick()
            if people:
                # A DELTA, never a wipe: declared names add or update and the recorded roster
                # stays -- live 2026-10-18 a re-declaration carrying only the regular it had
                # just met lost the other three. A state-only entry is the whole update.
                entry["main_npcs"] = _merge_npc_lists(entry.get("main_npcs") or [], people)
                declared = {str(npc.get("name") or "").strip().lower()
                            for npc in entry["main_npcs"] if isinstance(npc, dict)}
                entry["dead"] = [n for n in (entry.get("dead") or [])
                                 if str(n).lower() in declared]
                self._dead_perm[(era, key)] = {n for n in self._dead_perm.get((era, key), set())
                                               if n in declared}
                self._note_declared_deaths(entry)
                # A declared death must show at once: the index is perm U provisional U
                # declared, and this place just joined the third.
                self._sync_dead_display()
        # The event carries what the RECORD now holds, not just the delta (`record_place`
        # applies the same merge on the manifest side).
        if people:
            args["main_npcs"] = [dict(npc) for npc in entry["main_npcs"]]
        # The place just declared is where the GM is now -- unless a chosen Device landing
        # for this turn says otherwise.
        if apply_current:
            self._set_current_place(kingdom, area, place)
        if corrected and corrected != declared_area:
            held = (f'not in "{declared_area}"' if declared_area else "and you declared none")
            note = (f'NOTE: "{path_label(place)}" is held in area "{corrected}", {held} — the '
                    "place is filed at the record's address; reuse its exact kingdom / area / "
                    "place path.")
            self._place_note = f"{self._place_note}\n\n{note}" if self._place_note else note

    def _apply_people_moves(self, entries, era: str, place) -> list[dict]:
        """Make a declaration a PLACEMENT: a regular recorded elsewhere moves here.

        Returns the `moved` payload for the `place_note` event -- the server removes the same
        names from those manifest entries -- and tells the GM, in the tool result, what moved
        and from where. A person is recorded at one place at a time, so a base change (a
        regular leaving its house for the inn) is just the next declaration.

        The declared entries are enriched IN PLACE from the recorded person: a state-only
        declaration carries the recorded role / race / class / description / state along, so a
        move never loses identity and a death is not cleared by omission.

        Ambiguous matches (the same name in several places, with nothing to tell the people
        apart) move nothing and say what would disambiguate them."""
        drops: dict[int, tuple[dict, set[str]]] = {}
        moved: dict[tuple, dict] = {}
        ambiguous: list[str] = []
        for npc in entries:
            if not isinstance(npc, dict):
                continue
            name = " ".join(str(npc.get("name") or "").split())
            if not name:
                continue
            hits = move_placements(self._primed, npc, exclude=place, era=era)
            if hits is None:
                ambiguous.append(name)
                continue
            if not hits:
                continue
            source = hits[0][1]
            for field in ("description", "role", "race", "class", "state"):
                if (not str(npc.get(field) or "").strip()
                        and str(source.get(field) or "").strip()):
                    npc[field] = source[field]
            nk = name_key(name)
            for old, _recorded in hits:
                entry, names = drops.setdefault(id(old), (old, set()))
                names.add(nk)
                old_key = (str(entry.get("era") or ""), _scene_key(entry.get("place")))
                slot = moved.setdefault(old_key, {
                    "era": str(entry.get("era") or ""),
                    "kingdom": str(entry.get("kingdom") or ""),
                    "area": str(entry.get("area") or ""),
                    "place": [str(p) for p in (entry.get("place") or [])],
                    "names": []})
                if name not in slot["names"]:
                    slot["names"].append(name)
        if ambiguous:
            names = ", ".join(ambiguous[:4])
            more = f" (+{len(ambiguous) - 4} more)" if len(ambiguous) > 4 else ""
            note = (f"NOTE: I did not move {names}{more}: each is recorded in more than one "
                    "place and the declaration does not tell them apart. Give `race` and "
                    "`class` (or the role) to say which person you mean.")
            self._place_note = f"{self._place_note}\n\n{note}" if self._place_note else note
        if not drops:
            return []
        for entry, names in drops.values():
            entry["main_npcs"] = [
                n for n in (entry.get("main_npcs") or [])
                if not isinstance(n, dict) or name_key(n.get("name")) not in names]
            old_key = (str(entry.get("era") or ""), _scene_key(entry.get("place")))
            for table in (self._dead_perm, self._dead_provisional, self._dead_declared):
                if old_key in table:
                    table[old_key] = {n for n in table[old_key] if name_key(n) not in names}
        self._sync_dead_display()
        lines: list[str] = []
        slots = list(moved.values())
        for slot in slots[:3]:
            where = path_label(slot["place"])
            if slot["area"]:
                where = f"{where} ({slot['area']})"
            lines.append(f'NOTE: {", ".join(slot["names"])} moved here from "{where}" — no '
                         "longer listed there.")
        if len(slots) > 3:
            lines.append(f"NOTE: +{len(slots) - 3} more places no longer list a person you "
                         "named here.")
        if lines:
            text = "\n".join(lines)
            self._place_note = f"{self._place_note}\n\n{text}" if self._place_note else text
        return slots

    def _declares_new_people(self, entries) -> bool:
        """Whether a declaration carries a person the engine does not already know exactly.

        Compared as `_npc_people` keeps them (name, role, race, class): repeating a known
        person is not a change, while a changed field -- or a name the map has never seen --
        is. Used for `cast`, which only ever adds."""
        for npc in entries or []:
            if not isinstance(npc, dict):
                continue
            name = " ".join(str(npc.get("name") or "").split())
            if not name:
                continue
            declared = (name,
                        " ".join(str(npc.get("role") or "").split()),
                        " ".join(str(npc.get("race") or "").split()),
                        " ".join(str(npc.get("class") or "").split()))
            if self._npc_people.get(name_key(name)) != declared:
                return True
        return False

    def _declares_roster_change(self, place, entries, era: str | None = None) -> bool:
        """Whether a declaration changes the PLACE's recorded roster (a name, a field, a state).

        Asked BEFORE `_record_place_live` writes the delta: repeating what is already recorded
        -- including a regular's unchanged state -- is not a change and must write nothing.
        `description` counts: images mode can update a regular's look, and a file-less place has
        no picture through which that update could otherwise reach the manifest."""
        key = _scene_key(place if isinstance(place, (list, tuple)) else [place])
        if not key:
            return False
        era = self._declaration_era() if era is None else str(era or "")
        recorded: dict[str, dict] = {}
        for p in self._primed:
            if _scene_key(p.get("place")) == key and str(p.get("era") or "") == era:
                recorded = {name_key(str(n.get("name") or "")): n
                            for n in (p.get("main_npcs") or []) if isinstance(n, dict)}
                break
        for npc in entries or []:
            if not isinstance(npc, dict):
                continue
            name = " ".join(str(npc.get("name") or "").split())
            if not name:
                continue
            prior = recorded.get(name_key(name))
            if prior is None:
                return True
            for field in ("role", "race", "class", "state", "description"):
                declared = " ".join(str(npc.get(field) or "").split())
                if declared and declared != " ".join(str(prior.get(field) or "").split()):
                    return True
        # A person recorded at ANOTHER place makes this declaration a MOVE, even when the
        # target's roster already matches (a duplicate from an older session): the old entry
        # must be repaired and persisted, or the engine would keep two placements.
        for npc in entries or []:
            if not isinstance(npc, dict) or not name_key(npc.get("name")):
                continue
            if move_placements(self._primed, npc, exclude=key, era=era):
                return True
        return False

    def _note_declared_deaths(self, entry: dict) -> None:
        """Harvest a place's declared `state: dead` regulars into this session's death index.

        The GM is one writer of death, the combat registry the other; both feed
        `_dead_declared`, which `_sync_dead_display` unions with the permanent `_dead_perm` and
        this visit's provisional kills. The regular's own `state` is NEVER overwritten from the
        index: it is the GM's word, and a healed NPC (a cleared provisional kill) must lose the
        `(dead)` mark again -- which it would not if the display state had become the source."""
        key = (str(entry.get("era") or ""), _scene_key(entry.get("place")))
        dead = set(self._dead_declared.get(key, set()))
        for npc in entry.get("main_npcs") or []:
            if isinstance(npc, dict) and str(npc.get("state") or "").strip().lower() == "dead":
                name = str(npc.get("name") or "").strip().lower()
                if name:
                    dead.add(name)
        self._dead_declared[key] = dead

    def _same_place(self, before, after) -> bool:
        """Whether two `_current_place` readings name the same place at the same address.

        Used to tell a RE-declaration (the turn's place echo, which changes nothing the client
        or the save needs rewritten) from a move."""
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False
        return (_scene_key(before.get("place")) == _scene_key(after.get("place"))
                and str(before.get("kingdom") or "") == str(after.get("kingdom") or "")
                and str(before.get("area") or "") == str(after.get("area") or ""))

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
        if label == str(self.arrival or "").strip():
            return  # already where the story is: never write the same string again
        self.arrival = label
        await self._call_tool_text("set_player_field", {"key": "arrival", "value": label})

    async def _serve_places_lookup(self, args: dict, call_id=None) -> None:
        """Answer a `lookup` about places from the live registry -- never the MCP server.

        The KNOWN PLACES block the GM reads every turn is scoped to the current area and
        capped: a reminder, not the record. Anything it cannot hold -- another settlement's
        places, an area past its anchor cap, a regular past the per-place cap, the cast's
        identity -- is answered here, rendered from the same tree the block is built from, so
        the GM never has to invent a path or an identity it cannot see. The answer is the
        registry's own spellings, so the declaration that follows resolves to the saved place."""
        text = render_places_answer(self._primed, self._current_place, args.get("query"),
                                    cast=self._cast_labels())
        gm_text = _gm_tool_view("lookup", text)
        await self._emit({"type": "tool_result", "name": "lookup", "text": text,
                          "gm_text": gm_text, "is_error": False})
        self.messages.append({"role": "tool", "content": gm_text, "name": "lookup"})
        if self.provider == "gemini" and call_id:
            self.messages[-1]["id"] = str(call_id)

    async def _serve_npc_lookup(self, args: dict, call_id=None) -> None:
        """Answer a `lookup` about a person from the live registry -- never the MCP server.

        The per-turn KNOWN PLACES block is area-scoped and capped; `WHO KNOWS HIM` carries
        names only. This is the reverse index between them: a name -> identity, status and
        the exact place path to reuse, with the place's other regulars. Sources are the live
        place tree (the age the Traveller is in), the place-less cast, the companions and the
        met roll (names only). In classic there is no met roll and no age in the wording."""
        met: list[tuple[str, str]] = []
        if not self.classic:
            for era in [self.era] + [e for e in self._met if e != self.era]:
                for name in self._met.get(era, []):
                    met.append((name, era_display(era)))
        text = render_npc_answer(
            self._primed, self._current_place, args.get("query"),
            cast=self._cast_people(), companions=self._companion_people, met=met,
            world_label="this world" if self.classic else era_display(self.era))
        gm_text = _gm_tool_view("lookup", text)
        await self._emit({"type": "tool_result", "name": "lookup", "text": text,
                          "gm_text": gm_text, "is_error": False})
        self.messages.append({"role": "tool", "content": gm_text, "name": "lookup"})
        if self.provider == "gemini" and call_id:
            self.messages[-1]["id"] = str(call_id)

    async def _emit_place_note(self, args: dict, era: str | None = None) -> None:
        """Tell the server to persist the place (the manifest's single writer).

        Both modes use it: text mode's `note_place`, and an images-mode scene call whose place
        changed. `era` is the declaring writer's era (a scene call uses the server's scene
        convention -- see `_declaration_era`)."""
        raw = args.get("place")
        place = [str(p).strip() for p in raw if str(p or "").strip()] if isinstance(raw, list) else []
        await self._emit({
            "type": "place_note",
            "era": self._declaration_era() if era is None else str(era or ""),
            "kingdom": " ".join(str(args.get("kingdom") or "").split()),
            "area": " ".join(str(args.get("area") or "").split()),
            "place": place,
            "main_npcs": args.get("main_npcs") if isinstance(args.get("main_npcs"), list) else [],
            "cast": args.get("cast") if isinstance(args.get("cast"), list) else [],
            # A declaration is a placement: the names this move takes from those places must
            # leave the manifest there too (`record_place` applies the removal).
            "moved": args.get("moved") if isinstance(args.get("moved"), list) else [],
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
            race = str(entry.get("race") or "").strip()
            klass = str(entry.get("class") or "").strip()
            if race and klass:
                continue
            # A state-only entry updates a regular already on record ("Huiyuan", state
            # "petrified"): it declares no identity, so there is nothing to ask for.
            if (str(entry.get("state") or "").strip() and not (race or klass)
                    and name_key(name) in self._npc_people):
                continue
            missing.append(name)
        if not missing:
            return ""
        listed = "; ".join(f'"{n}"' for n in missing[:4])
        more = " (+more)" if len(missing) > 4 else ""
        return (f"NOTE: {listed}{more} declared with no race and/or class. Give every declared "
                "character a race and a class — never leave them human by default.")

    def _cast_labels(self) -> list[str]:
        """The place-less recurring people as `Name — role, race class`.

        The per-turn tree carries place regulars only; the on-demand record is where the cast's
        identity lives (there is no late "on stage" echo). The live identity map wins; a name
        the session has not declared yet falls back to the bare name."""
        labels: list[str] = []
        for key, name in self._cast_display.items():
            found = self._npc_people.get(key)
            if not found:
                labels.append(name)
                continue
            found_name, role, race, klass = found
            people = " ".join(x for x in (race, klass) if x)
            what = ", ".join(x for x in (role, people) if x)
            labels.append(f"{found_name} — {what}" if what else found_name)
        return sorted(labels, key=str.lower)

    def _cast_people(self) -> list[dict]:
        """The place-less recurring people with identity and status, for a person lookup.

        The live identity map (`_npc_people`) wins for name/role/race/class; the manifest's
        cast carries the recorded `state`. Only `_cast_display` keys are used -- that map is
        the cast (place regulars live in the tree, not here)."""
        manifest: dict[str, dict] = {}
        try:
            for person in known_cast_people(self.base_dir / OUTPUT_DIR,
                                            self.active_name or ""):
                manifest[name_key(person.get("name"))] = person
        except Exception:  # noqa: BLE001 - never fail a lookup over the manifest
            manifest = {}
        out: list[dict] = []
        for key, display in self._cast_display.items():
            found = self._npc_people.get(key)
            base = manifest.get(key) or {}
            out.append({
                "name": found[0] if found else display,
                "role": found[1] if found else str(base.get("role") or ""),
                "race": found[2] if found else str(base.get("race") or ""),
                "class": found[3] if found else str(base.get("class") or ""),
                "state": str(base.get("state") or ""),
            })
        return sorted(out, key=lambda p: str(p.get("name") or "").lower())

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
        """Reject a scene call for a place whose establishing view does not exist yet.

        KNOWN is not SEEDED: a declaration can be recorded (text mode, or an images-mode place
        whose picture never arrived) with no view on disk. That place must still supply
        `establishing`, or `ensure_scene` would invent a generic background instead of using
        the GM's. The GM asks for the seed once per place; re-requesting an existing seed is
        harmless (the server is idempotent)."""
        if not self.scene_images:
            return None
        raw = args.get("place")
        place = [str(p).strip() for p in raw if str(p or "").strip()] if isinstance(raw, list) else []
        if not place:
            return None  # no place -> portrait-only action, nothing to seed
        if str(args.get("establishing") or "").strip() or str(args.get("seed_change") or "").strip():
            return None
        if _scene_key(place) in self._scene_seeded:
            return None
        where = " — ".join(place)
        return (
            f'WARNING: no establishing view exists yet for "{where}". The place is recorded, '
            "but its seed has not been drawn, so request it: call request_scene_image again "
            "with `establishing` = a short description of the place (empty and unpopulated — no "
            "people, creatures or animals), plus the same action `description`. The establishing "
            "view is generated once and is never shown to the player."
        )

    def _place_address_warning(self, args: dict) -> str | None:
        """Reject a NEW place declared with no address and nothing to derive one from.

        `kingdom` / `area` are optional once a place is known -- a declaration may name only
        where the story stands, and the engine keeps the saved address and the saved spelling
        -- and a bare leaf the record has never held is filed in the settlement the Traveller
        is in, with a NOTE. What is left is the corner where there is no address anywhere: an
        empty settlement would blind the caption and the block's scoping (an empty area matches
        every place), so the call is refused and the result teaches it. The turn's place echo
        still counts as unmet, so `_ensure_place_note` asks again in the same turn."""
        raw = args.get("place")
        place = [str(p).strip() for p in raw if str(p or "").strip()] if isinstance(raw, list) else []
        if not place:
            return None
        if str(args.get("kingdom") or "").strip() or str(args.get("area") or "").strip():
            return None
        if resolve_place_path(self._primed, place, self._current_place):
            return None  # a place the record holds: its address comes from there
        kingdom, area = self._corrected_address("", "", place)
        if kingdom or area:
            return None  # the record's first-segment rule already names the address
        here = self._current_place if isinstance(self._current_place, dict) else {}
        if str(here.get("area") or "").strip():
            return None  # filed in the settlement the story stands in (`_record_place_live`)
        return (
            f'WARNING: "{path_label(place)}" is not in the record and no kingdom/area was '
            "declared, so this NEW place would be filed with no settlement at all. Call "
            "note_place again with `kingdom` (the realm) and `area` (the settlement) — or, if "
            "the save already holds this place, with its exact known path."
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
