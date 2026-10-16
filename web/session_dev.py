"""Developer mode: live state edits and the GM-context inspector."""

import copy
import json
import time
import sys
from pathlib import Path

# Make the repo root importable so we can reuse existing (frozen) modules.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from .eras import era_ids, render_era_index
from .protocol import DEV_NOTE_HEADER, GM_JOURNAL_MAX_BYTES, _safe_json
from .stats import build_stats
import device


class DevMixin:
    # ── developer mode: live state edits + the GM-context inspector ───────

    async def _handle_dev_command(self, cmd: dict) -> None:
        """A developer-mode command. Never reachable unless the session was created dev."""
        if not self.dev:
            await self._emit({"type": "error",
                              "message": "developer mode is not enabled for this session"})
            return
        op = str(cmd.get("op") or "").strip().lower()
        if op == "apply":
            await self._dev_apply(cmd.get("ops") or [])
        elif op == "stats":
            await self._emit_stats()
        elif op == "raw":
            await self._emit_dev_raw()
        elif op == "gm_open":
            self._gm_watch = True
            await self._emit_gm_context(flush=True)
        elif op == "gm_close":
            self._gm_watch = False
        elif op == "gm_dump":
            await self._emit_gm_context(flush=True)
        else:
            await self._emit({"type": "error", "message": f"unknown dev op: {op}"})

    async def _dev_apply(self, ops) -> None:
        """Apply a batch of dev edits in one go, then refresh and tell the GM once."""
        results: list[dict] = []
        notes: list[str] = []
        db_touched = False
        device_touched = False  # an inventory edit may change the Device's recovered parts
        new_era = None
        for raw in (ops if isinstance(ops, list) else []):
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "").strip().lower()
            if kind == "set":
                key = str(raw.get("key") or "").strip()
                if not key:
                    continue
                value = raw.get("value")
                text = await self._call_tool_text(
                    "debug_set_field", {"key": key, "value": json.dumps(value)})
                results.append({"kind": "set", "key": key, **_safe_json(text)})
                notes.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
                db_touched = True
                if key == "inventory":
                    device_touched = True
                if key == "era":
                    new_era = str(value or "").strip().lower()
            elif kind == "device":
                text = await self._call_tool_text(
                    "debug_device", {"parts": raw.get("parts") or [],
                                     "present": bool(raw.get("present", True))})
                results.append({"kind": "device", **_safe_json(text)})
                parts = [str(p) for p in (raw.get("parts") or [])]
                notes.append("Device present" if raw.get("present", True) else "Device absent")
                if parts:
                    notes.append("Device parts recovered: " + ", ".join(parts))
                db_touched = True
                device_touched = True
            elif kind == "jump":
                era = str(raw.get("era") or "").strip().lower()
                notes.append(f"Device jumped to {device.era_label(era) if era else 'a new age'}")
                results.append({"kind": "jump", "era": era})
                await self._dev_force_jump(era)
            else:
                results.append({"kind": kind, "error": "unknown dev op"})
        if db_touched:
            await self._refresh_device()
        # A raw `era` edit must move the engine too, or the place tree and the jump go stale.
        if isinstance(new_era, str) and new_era in era_ids():
            self.era = new_era
            self._reload_era_scene_state()
            if (self._era_index_at is not None
                    and 0 <= self._era_index_at < len(self.messages)):
                self.messages[self._era_index_at] = {
                    "role": "system", "content": render_era_index(self.era)}
        await self._emit_stats()
        await self._emit({"type": "cadence", **self._device_state()})
        if notes:
            self.messages.append({"role": "system",
                                  "content": DEV_NOTE_HEADER + "\n"
                                  + "\n".join(f"- {n}" for n in notes)})
        await self._emit({"type": "dev_applied", "results": results,
                          "note": "\n".join(notes), "capped": self._gm_capped})
        # The stored rows can change shape as a result (a new key, a new list entry),
        # so the field browser re-reads them.
        await self._emit_dev_raw()

    async def _dev_force_jump(self, era: str) -> None:
        """The developer's jump: a named age, now, even in combat."""
        if self.classic:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "There is no Device in a classic game."})
            return
        era = str(era or "").strip().lower()
        if not era or era == self.era:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "Name another age to jump to."})
            return
        await self._jump(to_era=era)

    async def _emit_stats(self) -> None:
        """Emit the live sheet (the same payload `/stats` sends)."""
        try:
            text = await self._call_tool_text("dump_player_db", {})
            db_data = json.loads(text)
        except Exception:  # noqa: BLE001 - a stats read must never sink the command
            db_data = {}
        await self._emit({"type": "stats",
                          "data": build_stats(db_data) if isinstance(db_data, dict) else {}})

    async def _emit_dev_raw(self) -> None:
        """Emit the raw stored player rows (the settable fields) for the field browser.

        `dump_player_save_state` is the unreduced row map -- the derived blocks the sheet
        shows (`_carrying`/`_equipment`/`_passive`) are not in it, because they are
        recomputed and cannot be set."""
        try:
            text = await self._call_tool_text("dump_player_save_state", {})
            data = json.loads(text)
        except Exception:  # noqa: BLE001 - a raw read must never sink the command
            data = {}
        await self._emit({"type": "dev_raw",
                          "data": data if isinstance(data, dict) else {}})

    # ── the GM-context inspector ──────────────────────────────────────────

    def _gm_tool_listing(self) -> dict:
        """What the GM is offered this session, and what was filtered out."""
        offered = {t["function"]["name"]: t["function"] for t in self.tools_schema}
        hidden = [{"name": t["function"]["name"],
                   "description": t["function"].get("description", "")}
                  for t in self._all_tools if t["function"]["name"] not in offered]
        return {
            "offered": [{"name": fn["name"], "description": fn.get("description", ""),
                         "parameters": fn.get("parameters")} for fn in offered.values()],
            "hidden": hidden,
        }

    @staticmethod
    def _estimate_tokens(messages) -> int:
        try:
            chars = len(json.dumps(messages, ensure_ascii=True))
        except (TypeError, ValueError):
            return 0
        return max(1, chars // 4)

    def _gm_snapshot(self, label: str, round=None, trigger=None, messages=None) -> dict:
        self._gm_seq += 1
        messages = copy.deepcopy(self.messages if messages is None else messages)
        message_tokens = self._estimate_tokens(messages)
        tool_tokens = int(getattr(self, "_tool_tokens", 0) or 0)
        return {"seq": self._gm_seq, "label": label, "turn": self.turn_counter,
                "ts": time.time(), "round": round, "trigger": trigger,
                "message_tokens": message_tokens, "tool_tokens": tool_tokens,
                "tokens": message_tokens + tool_tokens, "messages": messages}

    async def _record_gm_prompt(self, label: str, round=None, trigger=None, messages=None):
        """Record exactly what the GM is about to be sent (dev only), and stream it live.

        Returns the snapshot so the caller can stamp the provider's real token count once
        the reply lands; `None` when dev mode is off."""
        if not self.dev:
            return None
        snap = self._gm_snapshot(label, round=round, trigger=trigger, messages=messages)
        if not self._gm_capped:
            size = len(json.dumps(snap, ensure_ascii=True))
            if self._gm_journal_bytes + size > GM_JOURNAL_MAX_BYTES:
                self._gm_capped = True
            else:
                self._gm_journal.append(snap)
                self._gm_journal_bytes += size
        if self._gm_watch:
            await self._emit({"type": "gm_context", "mode": "live", "snapshot": snap,
                              "capped": self._gm_capped})
        return snap

    async def _stamp_actual_tokens(self, snap, prompt_eval) -> None:
        """Attach the provider's own prompt-token count to the finished snapshot.

        This is the exact size of the request (messages + tools), so it supersedes the
        chars/4 estimate. The panel is patched in place by `seq` when the watch is on."""
        if not self.dev or not isinstance(snap, dict) or not prompt_eval:
            return
        snap["actual_tokens"] = int(prompt_eval)
        if self._gm_watch:
            await self._emit({"type": "gm_context", "mode": "update",
                              "seq": snap.get("seq"), "actual_tokens": int(prompt_eval)})

    async def _emit_gm_context(self, flush: bool = False) -> None:
        payload = {"type": "gm_context", "mode": "flush" if flush else "live",
                   "tools": self._gm_tool_listing(), "turn": self.turn_counter,
                   "capped": self._gm_capped}
        if flush:
            payload["journal"] = list(self._gm_journal)
            payload["messages"] = copy.deepcopy(self.messages)
        await self._emit(payload)
