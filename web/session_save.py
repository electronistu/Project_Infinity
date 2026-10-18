"""Save / load: the player file, STATE pages, the timeline and its compaction."""

import json
import os
import shutil
from . import timeline
from .eras import playable_eras
from .gm_view import _clamp_block, _extract_met, _trim_legend
from .places import name_key
from .scenes import commit_scene_manifest
from .ollama_stream import stream_chat
from .protocol import (
    CHRONICLE_PROMPT, COMBAT_HOLD_MESSAGE, STATE_RECOVERY_NUDGE, TIMELINE_PROMPT,
    TIMELINE_PROMPT_ERA, strip_markers
)
from .timeline import (
    _STATE_FIELDS, _TIMELINE_HEADER, MAX_MET_STORE, _entry_turn_range, _extract_state,
    _extract_states, _split_timeline_entries, _state_has_fields, load_state, load_states,
    load_timeline
)


# The timeline is bounded by COMPACTION, not by dropping history: once the injected body
# passes the cap (or the entry-count floor), the older entries are rewritten as one
# GM-authored chronicle and the raw entries are discarded. Events decay; the STATE pages
# are rewritten in place and do not grow. Only the current age's page is ever injected, so
# the other ages cost disk, not tokens.
TIMELINE_KEEP_ENTRIES = 2   # newest entries kept verbatim at a compaction
MAX_CHRONICLE_CHARS = 1200  # the event chronicle
MAX_STATE_CHARS = 1200      # one age's STATE page


class SaveMixin:
    # ── save / load support ───────────────────────────────────────────────

    async def _collect_player_save(self) -> dict:
        # The save is built from the RAW row map, never from `dump_player_db`: the GM's dump
        # reduces `reputation` to the current era, and persisting that view throws away the
        # `{era: ...}` wrapper and every other era's standing (one save used to disable
        # reputation for good).
        text = await self._call_tool_text("dump_player_save_state", {})
        try:
            db_data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            db_data = {}
        buff_data = db_data.get("_active_buff_data", {})
        if isinstance(buff_data, str):
            try:
                buff_data = json.loads(buff_data)
            except (json.JSONDecodeError, TypeError):
                buff_data = {}
        for _spell_name, entries in (buff_data or {}).items():
            for entry in entries:
                field = entry["field"]
                delta = entry["delta"]
                if field == "temporary_hit_points":
                    db_data[field] = 0
                else:
                    current_val = db_data.get(field, 0)
                    if isinstance(current_val, str):
                        try:
                            current_val = int(current_val)
                        except (ValueError, TypeError):
                            continue
                    db_data[field] = current_val - delta
        db_data["active_effects"] = []
        db_data["_active_buff_data"] = {}
        # `_carrying` and `_equipment` are derived (SRD 5.1 carrying rules and the
        # equipped-items model) — never persist them: they are recomputed wherever
        # they are needed.
        db_data.pop("_carrying", None)
        db_data.pop("_equipment", None)
        return db_data

    def _write_player_atomic(self, db_data: dict, player_path: str) -> None:
        os.makedirs(os.path.dirname(player_path) or ".", exist_ok=True)
        tmp = f"{player_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db_data, f, indent=2)
        os.replace(tmp, player_path)

    async def _save_to_active(self, timeline_entry=None):
        if self.session is None or not self.player_path:
            return None
        try:
            db_data = await self._collect_player_save()
            self._write_player_atomic(db_data, self.player_path)
        except Exception as exc:  # noqa: BLE001
            await self._emit({"type": "error", "message": f"save failed: {type(exc).__name__}: {exc}"})
            return None
        # The places/NPC registry is memory-only until now: commit it with the save (and
        # prune scene images the registry no longer lists). A failure here must not sink
        # the character save.
        # A Save also freezes this visit's deaths: they are permanent from now on.
        try:
            self._freeze_dead()
        except Exception:  # noqa: BLE001 - never sink a save over liveness
            pass
        try:
            commit_scene_manifest(os.path.dirname(self.player_path), self.active_name)
        except Exception:  # noqa: BLE001 - never sink a save over places
            pass
        # The timeline is written here and only here: the ages' legends live in memory until
        # the player saves, so an unsaved session leaves the previous save's timeline intact.
        try:
            self._write_timeline(timeline_entry)
        except OSError:  # pragma: no cover - a read-only save must not sink the save
            pass
        info = {
            "type": "saved",
            "name": self.active_name,
            "player": os.path.basename(self.player_path),
            "timeline": os.path.basename(self.timeline_path) if self.timeline_path else None,
            "save": f"{self.active_name}.player" if self.active_name else None,
        }
        await self._emit(info)
        return info

    async def _plain_call(self, messages, label: str = "summary") -> str:
        """Tool-free, one-off model call; does not touch history. `messages` IS the whole
        request, so a housekeeping call can send only what it needs, not the full history."""
        snap = await self._record_gm_prompt(label, messages=messages)
        parts: list[str] = []
        if self.provider == "gemini":
            if self._gemini is None:
                from .gemini_stream import GeminiChat
                self._gemini = GeminiChat(os.environ.get("GEMINI_API_KEY") or "")
            events = self._gemini.stream(
                messages, self.model, tools=None,
                thinking_level=self.thinking_level, think=self.think, persist=False,
            )
        else:
            events = stream_chat(
                self._client, self.model, messages, None, self._options(), think=self.think,
            )
        async for evt in events:
            if evt["type"] == "narrative_delta":
                parts.append(evt["text"])
            elif evt["type"] == "done":
                await self._stamp_actual_tokens(snap, evt.get("prompt_eval_count", 0) or 0)
            elif evt["type"] == "error":
                return ""
        return "".join(parts)

    async def _plain_summary(self, prompt: str) -> str:
        """The timeline summary rides the whole session history, so the GM can see the
        STATE page(s) it is rewriting."""
        return await self._plain_call(self.messages + [{"role": "user", "content": prompt}],
                                      "summary")

    async def _summarize_timeline(self, target_turn: int):
        """Produce the save's event entry AND the current STATE page(s).

        Returned, never written: the caller writes the whole timeline in one go on Save. A
        page is kept even when the entry's shape drifts -- a malformed entry must not cost
        the notebook. A page the reply did not carry leaves the page already held."""
        if target_turn <= self.last_timeline_turn:
            return None
        start = self.last_timeline_turn + 1
        await self._emit({"type": "notice", "title": "Timeline",
                          "text": f"Summarising turns {start}-{target_turn} for the save..."})
        if self.classic:
            prompt = TIMELINE_PROMPT.replace("X-Y", f"{start}-{target_turn}")
        else:
            prompt = (TIMELINE_PROMPT_ERA.replace("X-Y", f"{start}-{target_turn}")
                      .replace("{era}", self.era))
        raw = await self._plain_summary(prompt) or ""
        if not self.classic:
            met, raw = _extract_met(raw)
            if met:
                self._merge_met(self.era, met)
        reply = strip_markers(raw)
        if self.classic:
            state, reply = _extract_state(reply)
            if state:
                self._state = _clamp_block(state, MAX_STATE_CHARS)
        else:
            pages, reply = _extract_states(reply, [self.era])
            await self._apply_state_page(self.era, pages.get(self.era, ""), reply, "Goal")
        entry = reply.strip()
        if entry and "**Key Events**" in entry:
            self.last_timeline_turn = target_turn
            await self._emit({"type": "timeline", "entry": entry})
            return entry
        return None

    async def _apply_state_page(self, scope: str, page: str, reply: str, *required: str) -> None:
        """Store the state page a Save produced, or keep/repair it.

        A page carrying its core (`Goal`) is cleaned and stored. A page without it keeps the
        one already held -- nothing is lost, and the next Save rewrites it. Only when there
        is nothing to keep (the first save in that age) is the GM asked once, tool-free, for
        the page; if that fails too, nothing is stored for this page.

        The other fields are wanted, not required: a page that omits one is still far better
        than the page it would replace, and discarding it would stall the ledger over a
        single missing line."""
        cap = MAX_STATE_CHARS
        held = self._era_states.get(scope, "") or ""
        stored = _clamp_block(page, cap) if page else ""
        if not (stored and _state_has_fields(stored, required)):
            if held:
                return  # keep what we have; silence is cheaper than a correction
            repaired = await self._repair_state_page(scope, reply)
            stored = _clamp_block(repaired, cap) if repaired else ""
            if not (stored and _state_has_fields(stored, required)):
                return
        self._era_states[scope] = stored

    async def _repair_state_page(self, scope: str, reply: str) -> str:
        """One tool-free call to repair a page with no core. The malformed reply is the only
        context it gets -- cheap, and the session history is never touched. It asks for the
        scope's full field set, so one round fixes the shape, not just the missing line."""
        fields = " / ".join(_STATE_FIELDS)
        prompt = STATE_RECOVERY_NUDGE.format(
            scope=scope, fields=fields, reply=(reply or "")[:4000])
        text = strip_markers(await self._plain_call(
            [{"role": "user", "content": prompt}], "state-fix") or "")
        pages, _ = _extract_states(text, [scope])
        return pages.get(scope, "")

    def _load_state_pages(self) -> None:
        """Read the save's STATE pages into memory: a classic save's single notebook, or one
        page per age.

        A save made before the pages were scoped has only the unkeyed block -- it belongs to
        the age the Traveller is standing in, and the next Save rewrites it keyed."""
        pages = load_states(self.timeline_path)
        self._state = load_state(self.timeline_path)
        if self.classic:
            self._era_states = {}
            return
        # Only real ages are kept: a page written under a retired key (a legacy `traveller`)
        # is not an age and must never be re-written or injected.
        known = set(playable_eras())
        self._era_states = {k: v for k, v in pages.items() if k in known}
        if not self._era_states and self._state:
            self._era_states[self.era] = self._state
            self._state = ""

    def _capture_state_pages(self, text: str, store: bool = True) -> str:
        """Capture a STATE page the GM wrote into a narrative turn and strip it, so it can
        never reach the client. Only a departure asks for one; `store=False` strips a stray
        page from a same-age move without registering it."""
        if self.classic:
            return text
        pages, cleaned = _extract_states(text, [self.era])
        if store and pages.get(self.era):
            self._pending_era_state = (self.era,
                                       _clamp_block(pages[self.era], MAX_STATE_CHARS))
        return cleaned

    def _merge_met(self, era: str, names) -> None:
        """Union the declared names into the age's register, marker order first.

        The marker is written story-first; a name it omits but the save already holds is
        appended, so a lossy rewrite never loses a person. Bounded by `MAX_MET_STORE`, the
        tail (side characters) dropped first."""
        if self.classic or not era:
            return
        incoming = [str(n).strip() for n in (names or []) if str(n or "").strip()]
        if not incoming:
            return
        out: list[str] = []
        seen: set[str] = set()
        for name in incoming + list(self._met.get(era, [])):
            key = name_key(name)
            if key and key not in seen:
                seen.add(key)
                out.append(name)
        self._met[era] = out[:MAX_MET_STORE]

    def _capture_met(self, text: str, store: bool = True) -> str:
        """Capture the `{{met: ...}}` marker and strip it.

        `store=False` is a same-age move: the marker is discarded, because the full history
        still carries every meeting and the age is not being left."""
        if self.classic:
            return text
        names, cleaned = _extract_met(text)
        if store and names:
            self._merge_met(self.era, names)
        return cleaned

    def _commit_departure_state(self, old: str) -> None:
        """Store the page captured on the closing turn for the age being left. A miss keeps
        the page already held (the next Save in that age would rewrite it anyway)."""
        if self._pending_era_state and self._pending_era_state[0] == old:
            page = (self._pending_era_state[1] or "").strip()
            if page:
                self._era_states[old] = _clamp_block(page, MAX_STATE_CHARS)
        self._pending_era_state = None

    def _companions_block(self, store: dict) -> str:
        """The party, for the sheet: name, identity and the stable look.

        The store is the save's flat `{lowername: sheet}` map -- one party, it travels with the
        Traveller, so it is not scoped to an age. The description (the look) rides along so the
        GM keeps the same person across saves and ages. Combat data (stats, attacks, AC, HP) is
        never shown -- the engine fills the block in a fight."""
        lines = []
        for p in (store or {}).values():
            p = p if isinstance(p, dict) else {"name": p}
            name = " ".join(str(p.get("name") or "").split())
            if not name:
                continue
            ident = " ".join(str(x) for x in (p.get("race"), p.get("class")) if x)
            desc = " ".join(str(p.get("description") or "").split())[:240]
            line = f"- {name}" + (f" ({ident})" if ident else "")
            if desc:
                line += f" — {desc}"
            lines.append(line)
        return ("companions:\n" + "\n".join(lines)) if lines else ""

    def _state_body(self, era: str | None = None) -> str:
        """The STATE page to inject: what is true now, and nothing else.

        A classic save has one notebook; the era game injects this age's page -- the ages the
        Traveller is not in are on disk and cost nothing. `era` overrides the current age, so a
        jump can inject the one it just landed in. (The companions live in the CHARACTER SHEET,
        not here.)"""
        key = era if era is not None else self.era
        if self.classic:
            state = (self._state or "").strip()
            return "STATE — current truth; honour it.\n" + state if state else ""
        page = (self._era_states.get(key, "") or "").strip()
        return "THIS AGE — current truth; honour it.\n" + page if page else ""

    def _timeline_body(self, era: str | None = None) -> str:
        """The injected session memory: the STATE page(s), then the EVENTS (chronicle +
        kept entries). Compaction measures this whole body; the jump re-injects the pages
        alone, so the events still decay."""
        state = self._state_body(era)
        chronicle = self._chronicle[2].strip() if self._chronicle else ""
        kept = (self._timeline_kept if self._timeline_kept is not None
                else load_timeline(self.timeline_path))
        parts: list[str] = []
        if state:
            parts.append(state)
        if chronicle:
            parts.append("EVENTS\n" + chronicle)
        if kept:
            parts.append(kept)
        return "\n\n".join(parts)

    async def _summarize_chronicle(self, old_text: str, a: int, b: int) -> str:
        """One tool-free call that folds older entries into a single chronicle. It gets
        only the old text -- never the session history -- so the compaction is cheap."""
        prompt = CHRONICLE_PROMPT.format(a=a, b=b, old=old_text)
        return strip_markers(await self._plain_call(
            [{"role": "user", "content": prompt}], "chronicle") or "").strip()

    async def _compact_timeline(self, entry: str | None) -> None:
        """Collapse the older entries into ONE chronicle when the injected memory would
        pass its cap (or the entry-count floor). Events decay; the STATE is untouched.

        A failed or non-shrinking chronicle leaves everything as it was -- history is never
        lost to a bad summariser."""
        if not self.timeline_path:
            return
        entries = _split_timeline_entries(load_timeline(self.timeline_path))
        pending_count = len(entries) + (1 if entry else 0)
        candidate = self._timeline_body()
        if entry:
            candidate += "\n\n" + entry.strip()
        if len(candidate) <= timeline.MAX_TIMELINE_CHARS and pending_count <= timeline.TIMELINE_MAX_ENTRIES:
            return
        if len(entries) <= 1:
            return  # nothing old enough to fold without losing the only entry
        keep_n = TIMELINE_KEEP_ENTRIES if len(entries) > TIMELINE_KEEP_ENTRIES else 1
        fold, keep = entries[: len(entries) - keep_n], entries[len(entries) - keep_n:]
        chron_body = self._chronicle[2] if self._chronicle else ""
        fold_lo, _ = _entry_turn_range(fold[0])
        _, fold_hi = _entry_turn_range(fold[-1])
        a = self._chronicle[0] if self._chronicle else fold_lo
        old_text = "\n\n".join(x for x in [chron_body] + fold if x.strip())
        await self._emit({"type": "notice", "title": "Chronicle",
                          "text": f"Condensing turns {a}-{fold_hi} into the story so far..."})
        body = await self._summarize_chronicle(old_text, a, fold_hi)
        if not body or "<" in body or len(body) >= len(old_text):
            return
        # Insurance: keep the pre-fold file (the old chronicle + the raw entries about to be
        # folded) so a plausible-but-lossy chronicle can be undone. Disk only; never read.
        try:
            shutil.copy2(self.timeline_path, self.timeline_path + ".bak")
        except OSError:
            pass
        self._chronicle = (a, fold_hi, _trim_legend(body, MAX_CHRONICLE_CHARS))
        # The entries the rewrite keeps, so `_write_timeline` does not read the folded ones
        # back from disk; the fold is now represented by the chronicle.
        self._timeline_kept = "\n\n".join(keep)

    def _write_timeline(self, entry=None) -> None:
        """Rewrite the save's timeline atomically: header, the ages' legends, the STATE
        pages, the chronicle, the kept entries, and the new entry.

        Called on Save only. Legends, the STATE pages and the chronicle live in memory until
        here, so an unsaved session never touches the file; rebuilding each block keeps a
        save idempotent (no duplicates)."""
        if not self.timeline_path:
            return
        source = self._timeline_kept
        self._timeline_kept = None
        session_text = source if source is not None else load_timeline(self.timeline_path)
        if not (session_text or entry or self._legends or self._met or self._state
                or self._era_states or self._chronicle):
            return  # nothing to record yet: do not create an empty timeline
        blocks = [_TIMELINE_HEADER]
        if self._legends:
            blocks.append("\n".join(f"<!-- legend:{era} --> {text}"
                                    for era, text in self._legends.items()))
        # Who the Traveller met, one line per age, first-met order: names only, so the roll
        # stays cheap and the age's legend stays the story.
        if self._met:
            blocks.append("\n".join(
                f"<!-- met:{era} --> {', '.join(names)}"
                for era, names in sorted(self._met.items()) if names))
        # The STATE pages: a classic save's single unkeyed notebook, or one page per age.
        # Sorted, so a rewrite is byte-stable and two saves of the same state produce the
        # same file.
        if self._state:
            blocks.append(f"<!-- state -->\n{self._state.strip()}\n<!-- /state -->")
        for era in sorted(self._era_states):
            page = (self._era_states.get(era) or "").strip()
            if page:
                blocks.append(f"<!-- state:{era} -->\n{page}\n<!-- /state:{era} -->")
        if self._chronicle:
            a, b, body = self._chronicle
            blocks.append(f"<!-- chronicle:{a}-{b} -->\n{body.strip()}\n<!-- /chronicle -->")
        if session_text:
            blocks.append(session_text)
        if entry:
            blocks.append(entry.strip())
        body = "\n\n".join(b.strip() for b in blocks if b and b.strip()) + "\n"
        os.makedirs(os.path.dirname(self.timeline_path) or ".", exist_ok=True)
        tmp = f"{self.timeline_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, self.timeline_path)

    async def _handle_save_command(self) -> None:
        """Save in place: the session writes the world's own files (never renames).

        Renaming was dropped when images arrived: portraits and scenes live in
        `output/images/{stem}/`, keyed to the world name, so a new stem would
        orphan them.

        Refused mid-fight: the `.player` snapshot would capture transient HP/conditions with
        no registry, while the timeline summary described an unfinished fight -- on reload the
        two disagree and the fight cannot be resumed.
        """
        if self._in_combat:
            await self._emit({"type": "save_refused", "reason": "combat",
                              "text": COMBAT_HOLD_MESSAGE})
            return
        # A save is long (a model call for the timeline, then the writes) and runs inline on
        # the command queue, so the client is told to lock its controls -- otherwise a message
        # sent now would only be answered after the save.
        await self._emit({"type": "saving", "value": True})
        try:
            entry = await self._summarize_timeline(self.turn_counter)
            await self._compact_timeline(entry)
            await self._save_to_active(entry)
        finally:
            await self._emit({"type": "saving", "value": False})
