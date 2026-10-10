"""Developer mode: the live editor path and the GM-context inspector.

No LLM / no network: the MCP session is faked, so the commands are driven directly and
the tool calls the engine makes are asserted by name and argument.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_dev.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

import device as device_mod  # noqa: E402

from web.engine import (  # noqa: E402
    DEV_NOTE_HEADER, ENGINE_ONLY_TOOLS, GameSession, filter_tools,
)

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


_DUMP = {
    "name": "Tester", "level": 1, "xp": 0, "gold": 10,
    "stats": {"str": 16, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8},
    "inventory": [device_mod.device_entry(), device_mod.part_entry("Escapement", "egypt")],
    "journey": "egypt",
    "current_hit_points": 12, "total_hit_points": 12,
}

# The raw stored rows the field browser reads (no derived blocks).
_RAW = {
    "name": "Tester", "level": 1, "gold": 10,
    "stats": {"str": 16, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8},
    "spellcasting": {"ability": "intelligence", "slots": {"1": 2}},
    "inventory": ["Dagger"], "era": "egypt",
}


class _FakeMCP:
    """Stands in for the engine's MCP session: records the tools the engine calls."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))

        class _R:
            def __init__(self, text):
                self.content = [type("C", (), {"text": text})()]

        if name == "dump_player_db":
            text = json.dumps(_DUMP)
        elif name == "dump_player_save_state":
            text = json.dumps(_RAW)
        else:
            text = '{"success": true}'
        return _R(text)


ALL_TOOLS = [{"function": {"name": n, "description": f"{n} doc", "parameters": {}}}
             for n in ("dump_player_db", "lookup", "set_player_field",
                       "dump_player_save_state", "request_scene_image", "register_npcs",
                       "debug_set_field", "debug_device")]


def _session(dev=True, era="egypt"):
    gs = GameSession(base_dir=REPO, model="test", scene_images=True, dev=dev)
    gs.session = _FakeMCP()
    gs.era = era
    gs.has_device = True
    gs.journey = [era]
    gs.arrival = "the Giza quarry, on the Nile"
    gs.messages = [{"role": "system", "content": "protocol"}]
    gs._era_index_at = None
    gs._places_at = 1
    gs._primed = []
    gs._all_tools = list(ALL_TOOLS)
    gs.tools_schema = filter_tools(gs._all_tools, True, gs.mode)
    gs._rng = __import__("random").Random(7)
    fd, gs.timeline_path = tempfile.mkstemp(suffix=".timeline")
    os.close(fd)
    os.unlink(gs.timeline_path)

    async def _noop_turn(content, label):
        gs.messages.append({"role": "user", "content": content})
        gs.turn_counter += 1
        await gs._emit({"type": "turn_end", "text": "", "turn": gs.turn_counter})

    async def _no_legend():
        return None

    gs._run_turn = _noop_turn
    gs._ensure_legend = _no_legend
    return gs


def _drain(gs):
    out = []
    while not gs._evt_q.empty():
        out.append(gs._evt_q.get_nowait())
    return out


def main():
    # ── the tools the GM never sees ─────────────────────────────────────────
    names = [t["function"]["name"] for t in filter_tools(ALL_TOOLS, True, "time_traveler")]
    rec("the debug tools are engine-only, never offered to the GM",
        "debug_set_field" not in names and "debug_device" not in names, str(names))
    rec("... alongside the other bookkeeping tools",
        set(ENGINE_ONLY_TOOLS) == {"set_player_field", "dump_player_save_state",
                                   "debug_set_field", "debug_device"},
        str(ENGINE_ONLY_TOOLS))

    # ── dev is off unless asked ─────────────────────────────────────────────
    off = _session(dev=False)
    rec("a non-dev session says so", off.dev is False)
    asyncio.run(off._record_gm_prompt("awakening"))
    rec("... and records no GM prompts", off._gm_journal == [])
    asyncio.run(off._handle_dev_command({"op": "apply", "ops": [{"kind": "set", "key": "gold", "value": 1}]}))
    off_evts = _drain(off)
    rec("... and refuses a dev command",
        any(e.get("type") == "error" for e in off_evts)
        and not any(e.get("type") == "dev_applied" for e in off_evts),
        str([e.get("type") for e in off_evts]))

    # ── the prompt journal ──────────────────────────────────────────────────
    gs = _session(dev=True)
    asyncio.run(gs._record_gm_prompt("awakening"))
    rec("a dev session records one snapshot per model call",
        len(gs._gm_journal) == 1 and gs._gm_journal[0]["label"] == "awakening",
        str(gs._gm_journal))
    rec("... carrying the full message list and a token estimate",
        gs._gm_journal[0]["messages"] == gs.messages and gs._gm_journal[0]["tokens"] > 0)
    gs.messages.append({"role": "user", "content": "a turn"})
    asyncio.run(gs._record_gm_prompt("turn"))
    rec("... and another for the next call", len(gs._gm_journal) == 2,
        str([s["label"] for s in gs._gm_journal]))

    # A jump's compaction erases the live history -- the journal must survive it.
    gs.messages.extend({"role": "user", "content": f"history {i}"} for i in range(30))
    asyncio.run(gs._record_gm_prompt("late"))
    before = len(gs._gm_journal)
    gs._compact("egypt")
    rec("the compaction throws the live history away",
        len(gs.messages) <= 4 and not any("history" in str(m.get("content") or "") for m in gs.messages),
        f"{len(gs.messages)} messages")
    rec("... but the journal still holds every prompt",
        len(gs._gm_journal) == before and "history 29" in json.dumps(gs._gm_journal[-1]["messages"]),
        str(before))

    # ── gm_open flushes everything ──────────────────────────────────────────
    asyncio.run(gs._handle_dev_command({"op": "gm_open"}))
    evts = [e for e in _drain(gs) if e.get("type") == "gm_context"]
    flush = evts[-1] if evts else {}
    rec("gm_open flushes the whole journal", flush.get("mode") == "flush"
        and len(flush.get("journal", [])) == len(gs._gm_journal), str(flush.get("mode")))
    rec("... plus the current message list", isinstance(flush.get("messages"), list))
    tools = flush.get("tools") or {}
    offered = {t["name"] for t in tools.get("offered", [])}
    hidden = {t["name"] for t in tools.get("hidden", [])}
    rec("... and the tool listing the GM is offered",
        "dump_player_db" in offered and "debug_set_field" not in offered, str(offered))
    rec("... and what was hidden from it",
        "debug_set_field" in hidden and "set_player_field" in hidden, str(hidden))
    rec("gm_open starts the live watch", gs._gm_watch is True)
    asyncio.run(gs._record_gm_prompt("live"))
    live = [e for e in _drain(gs) if e.get("type") == "gm_context" and e.get("mode") == "live"]
    rec("a watched session streams each new prompt live",
        bool(live) and live[-1].get("snapshot", {}).get("label") == "live", str(live))
    asyncio.run(gs._handle_dev_command({"op": "gm_close"}))
    rec("gm_close stops the watch", gs._gm_watch is False)

    # ── the live sheet editor ───────────────────────────────────────────────
    edit = _session(dev=True)
    asyncio.run(edit._handle_dev_command({"op": "apply", "ops": [
        {"kind": "set", "key": "gold", "value": 999},
        {"kind": "set", "key": "current_hit_points", "value": 4},
        {"kind": "device", "present": True, "parts": ["Escapement", "Mainspring"]},
        {"kind": "cadence", "turns": 3, "steer": "forward"},
    ]}))
    calls = {n: a for n, a in edit.session.calls}
    rec("apply writes a scalar through debug_set_field as JSON",
        calls.get("debug_set_field", {}).get("value") == "999"
        or any(n == "debug_set_field" and a.get("key") == "gold" and a.get("value") == "999"
               for n, a in edit.session.calls),
        str([a for n, a in edit.session.calls if n == "debug_set_field"]))
    rec("apply writes the Device through debug_device",
        any(n == "debug_device" and a.get("parts") == ["Escapement", "Mainspring"]
            for n, a in edit.session.calls),
        str([a for n, a in edit.session.calls if n == "debug_device"]))
    rec("apply sets the cadence without a tool call",
        edit._jump_at_turn == edit.turn_counter + 3 and edit._steer == "forward",
        f"jump_at={edit._jump_at_turn} steer={edit._steer}")
    evts = _drain(edit)
    rec("apply refreshes the sheet and the Device panel",
        any(e.get("type") == "stats" for e in evts)
        and any(e.get("type") == "cadence" for e in evts)
        and any(e.get("type") == "dev_applied" for e in evts),
        str([e.get("type") for e in evts]))
    note = next((e for e in evts if e.get("type") == "dev_applied"), {})
    rec("apply reports what changed", "gold" in (note.get("note") or "")
        and "Escapement" in (note.get("note") or ""), str(note.get("note")))
    rec("apply leaves exactly one system note for the GM",
        sum(1 for m in edit.messages
            if isinstance(m, dict) and str(m.get("content") or "").startswith(DEV_NOTE_HEADER)) == 1)
    rec("... naming the changed fields", any(
        "gold = 999" in str(m.get("content") or "") for m in edit.messages))
    rec("the Device count is re-read after the edit (a part was recovered)",
        edit.parts_recovered == 1, str(edit.parts_recovered))

    # ── the raw field browser ───────────────────────────────────────────────
    browser = _session(dev=True)
    asyncio.run(browser._handle_dev_command({"op": "raw"}))
    raw_evts = [e for e in _drain(browser) if e.get("type") == "dev_raw"]
    data = raw_evts[-1].get("data", {}) if raw_evts else {}
    rec("the raw op emits the stored rows for the field browser",
        bool(raw_evts) and "stats" in data and "spellcasting" in data
        and (data.get("spellcasting") or {}).get("slots") == {"1": 2}, str(data))
    asyncio.run(browser._handle_dev_command({"op": "apply", "ops": [
        {"kind": "set", "key": "stats.dex", "value": 18}]}))
    rec("a dotted-path set from the browser reaches debug_set_field",
        any(n == "debug_set_field" and a.get("key") == "stats.dex" and a.get("value") == "18"
            for n, a in browser.session.calls),
        str([a for n, a in browser.session.calls if n == "debug_set_field"]))
    rec("apply re-reads the raw rows so the browser refreshes",
        any(e.get("type") == "dev_raw" for e in _drain(browser)))

    # ── the developer's jump ────────────────────────────────────────────────
    jump = _session(dev=True, era="egypt")
    jump._pending_legend = "Egypt remembers a stranger who vanished."
    before_era = jump.era
    asyncio.run(jump._handle_dev_command({"op": "apply", "ops": [{"kind": "jump", "era": "tang"}]}))
    rec("a dev jump moves the Traveller to the chosen age",
        jump.era == "tang" and jump.era != before_era, f"{before_era} -> {jump.era}")
    rec("... and the engine tells the DB the new era",
        any(n == "set_player_field" and a.get("key") == "era" and a.get("value") == "tang"
            for n, a in jump.session.calls),
        str([a for n, a in jump.session.calls if n == "set_player_field"]))

    # ── a classic game has no Device to command ─────────────────────────────
    classic = _session(dev=True, era="")
    classic.classic = True
    asyncio.run(classic._handle_dev_command({"op": "apply", "ops": [{"kind": "jump", "era": "tang"}]}))
    rec("a classic game refuses a Device jump", classic.era == "",
        str([e.get("type") for e in _drain(classic)]))

    # ── the token accounting names the tools ────────────────────────────────
    tok = _session(dev=True)
    tok._tool_tokens = 8500
    asyncio.run(tok._record_gm_prompt("awakening"))
    snap = tok._gm_journal[-1]
    rec("a snapshot counts messages AND tools, and sums them",
        snap["tool_tokens"] == 8500 and snap["message_tokens"] > 0
        and snap["tokens"] == snap["message_tokens"] + snap["tool_tokens"],
        str({k: snap[k] for k in ("message_tokens", "tool_tokens", "tokens")}))

    asyncio.run(tok._record_gm_prompt("resume", round=2, trigger="pause"))
    snap2 = tok._gm_journal[-1]
    rec("a snapshot carries its round and trigger",
        snap2["round"] == 2 and snap2["trigger"] == "pause", str(snap2))

    asyncio.run(tok._handle_dev_command({"op": "gm_open"}))
    _drain(tok)
    asyncio.run(tok._stamp_actual_tokens(snap2, 12345))
    rec("the provider's real token count is stamped on the snapshot",
        snap2.get("actual_tokens") == 12345, str(snap2.get("actual_tokens")))
    upd = [e for e in _drain(tok) if e.get("type") == "gm_context" and e.get("mode") == "update"]
    rec("... and patched to an open panel",
        bool(upd) and upd[-1].get("actual_tokens") == 12345, str(upd))
    asyncio.run(tok._handle_dev_command({"op": "gm_close"}))

    # ── the round/trigger plumbing through the tool loop ─────────────────────
    seq = _session(dev=True)
    calls: list = []

    async def fake_pause(label, quiet=False, gm=None):
        calls.append((label, gm.get("round"), gm.get("trigger")))
        return ("__SYSTEM_PAUSE__", "", [], False)

    seq._stream_assistant = fake_pause
    asyncio.run(seq._run_role("hello", "awakening"))
    rec("the awakening rounds are labelled start, then pause",
        calls[:4] == [("awakening", 1, "start"), ("resume", 2, "pause"),
                      ("resume", 3, "pause"), ("resume", 4, "pause")],
        str(calls[:4]))

    tools = _session(dev=True)
    tool_calls: list = []

    async def fake_tools(label, quiet=False, gm=None):
        tool_calls.append((label, gm.get("round"), gm.get("trigger")))
        if len(tool_calls) == 1:
            return ("", "", [{"function": {"name": "roll_dice", "arguments": {}}}], False)
        return ("prose", "", [], False)

    tools._stream_assistant = fake_tools
    asyncio.run(tools._run_role("hello", "turn"))
    rec("a tool-result round is labelled with trigger 'tools'",
        tool_calls == [("turn", 1, "start"), ("turn", 2, "tools")], str(tool_calls))

    # ── over the wire: `--debug` really enables the tools ──────────────────
    def _live_debug_tool():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        payload = {"name": "Tester", "level": 1, "gold": 10,
                   "stats": {"str": 10, "dex": 10, "con": 10, "int": 10,
                             "wis": 10, "cha": 10}, "inventory": []}
        fd, path = tempfile.mkstemp(suffix=".player")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        async def go():
            params = StdioServerParameters(
                command=sys.executable,
                args=["dice_server.py", path, "--debug"], cwd=str(REPO))
            devnull = open(os.devnull, "w", encoding="utf-8")
            try:
                async with stdio_client(params, errlog=devnull) as (r, w):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        res = await s.call_tool("debug_set_field",
                                                {"key": "gold", "value": "321"})
                        return json.loads(res.content[0].text)
            finally:
                devnull.close()

        try:
            return asyncio.run(go())
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    try:
        out = _live_debug_tool()
        rec("the `--debug` flag enables the tools over the real MCP wire",
            bool(out.get("success")) and out.get("value") == 321, str(out))
    except Exception as e:  # noqa: BLE001
        rec("the `--debug` flag enables the tools over the real MCP wire", False,
            f"{type(e).__name__}: {e}")

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
