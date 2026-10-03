"""Headless, streaming game engine for the Project Infinity web client.

A headless, streaming re-implementation of the game loop (the terminal client
has been removed). It reads `GameMaster_MCP.md` as the GM protocol, owns the
timeline helpers locally, and launches the same `dice_server.py` MCP engine.

Concurrency model
-----------------
Each session owns one long-lived background task (`_run`) that holds the MCP
stdio context managers for the whole session and consumes commands from a
queue. Events are pushed to an output queue that `events()` yields from. This
keeps the MCP anyio task group alive across many independent HTTP/WS requests.

Event stream (server -> client)
-------------------------------
ready, assistant_start, thinking_delta, narrative_delta, assistant_end,
tool_call, tool_result, context, paused, awakening_end, turn_end, stats,
notice, timeline, busy, error, fatal, closed
"""

import asyncio
import json
import os
import re
import sys
import traceback
from pathlib import Path

# Make the repo root importable so we can reuse existing (frozen) modules.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from ollama import AsyncClient  # noqa: E402

from .images import known_scene_locations  # noqa: E402
from .ollama_stream import stream_chat  # noqa: E402
from .stats import build_stats  # noqa: E402

# GM protocol file (also the session lock sentinel) and the world output dir.
LOCK_FILE = "GameMaster_MCP.md"
OUTPUT_DIR = "output"


def load_timeline(timeline_path):
    """Load an existing session timeline, if any."""
    if os.path.exists(timeline_path):
        with open(timeline_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def append_timeline_file(timeline_path, entry):
    """Append a timeline entry, writing the header on first use."""
    os.makedirs(os.path.dirname(timeline_path) or ".", exist_ok=True)
    header = "" if os.path.exists(timeline_path) else "# Session Timeline\n\n"
    with open(timeline_path, "a", encoding="utf-8") as f:
        if header:
            f.write(header)
        f.write(entry.strip() + "\n\n")


_TURN_HEADING_RE = re.compile(
    r"^##\s+(?:Rounds?|Turns?)\s+(\d+)\s*(?:[-\u2013\u2014]\s*(\d+))?",
    re.MULTILINE,
)


def _max_timeline_turn(text: str) -> int:
    """Highest turn recorded in a session timeline heading (0 if none).

    Matches the current "Turns X-Y" headings and legacy "Rounds X-Y" ones, so
    turn numbering continues correctly across sessions.
    """
    highest = 0
    for match in _TURN_HEADING_RE.finditer(text or ""):
        highest = max(highest, int(match.group(2) or match.group(1)))
    return highest

PAUSE_TOKENS = ("{{_NEED_AN_OTHER_PROMPT}}", "{{_NEED_ANOTHER_PROMPT}}")

# "**END MECHANICS**" on its own line (see GameMaster_MCP.md narrative_phase).
MECHANICS_MARKER_RE = re.compile(
    r"^[ \t]*\*{0,2}[ \t]*end(?:\s+of)?\s+mechanics:?[ \t]*\*{0,2}[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
MAX_THINKING_RETRIES = 3
DEFAULT_CONTEXT_WINDOW = 1_048_576

# Web-local timeline prompt: the database is authoritative, so mechanical changes
# are NOT summarised (the terminal CLI's prompt is gone).
TIMELINE_PROMPT = """SYSTEM INSTRUCTION: The player is saving the game. Write a session timeline entry covering the turns listed below, in the following EXACT format. Replace bracketed text with actual content. Keep it concise. Output ONLY the entry — no extra narration, no tool calls.

## Turns X-Y | [current location] | [in-game time]
**Key Events**:
- [event in one sentence]
**NPCs**: [names and roles of new NPCs encountered, or "none"]
**Active Hooks**: [all unresolved plot threads, one per line]"""

HELP_TEXT = (
    "/help  - show this help\n"
    "/stats - show the current character sheet\n"
    "/save  - write the character sheet to your .player file (active effects reverted)\n"
    "/sync  - force a database sync with the GM\n"
    "/quit  - end the session\n\n"
    "Anything else is sent to the Game Master as an action."
)


def _tool_schema(tool) -> dict:
    params = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": params,
        },
    }


SCENE_TOOL = "request_scene_image"
# Tools the GM may attach to narrative prose (the Narrative Phase). A round that
# carries prose plus only these tools is the turn's last word: the tool loop must
# NOT run another model round, or the GM re-narrates the whole turn.
NARRATIVE_PHASE_TOOLS = frozenset({SCENE_TOOL})
# Scene-imagery instructions live in GameMaster_MCP.md between these markers;
# they are sent to the GM only when storyline image generation is enabled.
_SCENE_BLOCK = re.compile(
    r"(?ms)^[ \t]*<!-- SCENE:ON -->.*?^[ \t]*<!-- SCENE:END -->[ \t]*\n?"
)
_SCENE_MARKERS = re.compile(r"(?m)^[ \t]*<!-- SCENE:(?:ON|END) -->[ \t]*\n?")


def render_protocol(text: str, scene_images: bool) -> str:
    """Strip the scene markers; drop the enclosed rules when scenes are off."""
    if scene_images:
        return _SCENE_MARKERS.sub("", text or "")
    return _SCENE_BLOCK.sub("", text or "")


def filter_tools(tools: list[dict], scene_images: bool) -> list[dict]:
    """Hide the scene-image tool from the GM unless storyline images are on."""
    if scene_images:
        return list(tools)
    return [t for t in tools if (t.get("function") or {}).get("name") != SCENE_TOOL]


def _clean_sync_tokens(text: str) -> str:
    for token in PAUSE_TOKENS:
        text = text.replace(token, "")
    # The GM's mechanics-block terminator is protocol, not prose — drop it from any
    # summary (e.g. a timeline entry) so it can never surface to the player.
    text = MECHANICS_MARKER_RE.sub("", text)
    return text.strip()


class GameSession:
    """One in-memory, long-lived game session (single user, local server)."""

    def __init__(self, base_dir, model: str, context_window: int = DEFAULT_CONTEXT_WINDOW,
                 temperature: float = 1.0, think=None, verbose: bool = False,
                 debug: bool = False, scene_images: bool = False, provider: str = "ollama"):
        self.base_dir = Path(base_dir)
        self.model = model
        self.provider = provider or "ollama"
        self.context_window = context_window
        self.temperature = temperature
        self.think = think
        self.verbose = verbose
        self.debug = debug
        # Set at session start; gates the GM's scene-imagery instructions + tool.
        self.scene_images = bool(scene_images)

        self.wwf_path: Path | None = None
        self.player_path: str | None = None
        self.timeline_path: str | None = None
        self.active_name: str | None = None
        self.active_wwf: str | None = None

        self.messages: list[dict] = []
        self.tools_schema: list[dict] = []
        self.current_context_tokens = 0
        self.turn_counter = 0
        self.narrative_counter = 0
        self.last_timeline_turn = 0

        self.session: ClientSession | None = None
        self._client: AsyncClient | None = None
        self._gemini = None  # GeminiChat, created lazily for gemini-provider models
        self._cmd_q: asyncio.Queue = asyncio.Queue()
        self._evt_q: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._scene_requested_turn = False

    # ── public API ────────────────────────────────────────────────────────

    async def start(self, wwf_path) -> None:
        path = Path(wwf_path)
        if not path.is_absolute():
            candidate = self.base_dir / path
            if not candidate.exists():
                # Allow callers to pass just the .wwf filename (lives in output/).
                candidate = self.base_dir / OUTPUT_DIR / path
            path = candidate.resolve()
        self.wwf_path = path
        stem = os.path.splitext(str(path))[0]
        self.player_path = stem + ".player"
        self.timeline_path = stem + ".timeline"
        self.active_name = os.path.splitext(os.path.basename(str(path)))[0]
        self.active_wwf = str(path)
        self._task = asyncio.create_task(self._run())

    async def events(self):
        while True:
            evt = await self._evt_q.get()
            if evt is None:
                break
            yield evt

    async def submit(self, text: str) -> None:
        await self._cmd_q.put({"type": "action", "text": text})

    async def submit_slash(self, command: str) -> None:
        await self._cmd_q.put({"type": "slash", "command": command})

    async def submit_save(self) -> None:
        await self._cmd_q.put({"type": "save"})

    async def resume(self) -> None:
        await self._cmd_q.put({"type": "resume"})

    async def set_flags(self, verbose=None, debug=None, think=None, temperature=None) -> None:
        if verbose is not None:
            self.verbose = bool(verbose)
        if debug is not None:
            self.debug = bool(debug)
        if think is not None:
            self.think = think
        if temperature is not None:
            self.temperature = float(temperature)

    async def close(self) -> None:
        if self._task is None:
            return
        await self._cmd_q.put({"type": "close"})
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=20)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    # ── internals ─────────────────────────────────────────────────────────

    async def _emit(self, evt: dict) -> None:
        await self._evt_q.put(evt)

    def _options(self) -> dict:
        return {"temperature": self.temperature, "num_ctx": self.context_window}

    def _stream(self, messages, tools):
        """Return an async iterator of normalized events for the active provider."""
        if self.provider == "gemini":
            if self._gemini is None:
                from .gemini_stream import GeminiChat  # local import keeps Ollama-only setups working
                self._gemini = GeminiChat(os.environ.get("GEMINI_API_KEY") or "")
            return self._gemini.stream(
                messages, self.model, tools=tools,
                temperature=self.temperature, think=self.think,
            )
        return stream_chat(
            self._client, self.model, messages, tools,
            self._options(), think=self.think,
        )

    async def _run(self) -> None:
        devnull = None
        errlog = sys.stderr
        if not self.debug:
            devnull = open(os.devnull, "w", encoding="utf-8")
            errlog = devnull
        try:
            params = StdioServerParameters(
                command=sys.executable,
                args=["dice_server.py", self.player_path],
                cwd=str(self.base_dir),
            )
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    self.session = session
                    await session.initialize()
                    listing = await session.list_tools()
                    tools = [_tool_schema(t) for t in listing.tools]
                    self.tools_schema = filter_tools(tools, self.scene_images)
                    if self.provider != "gemini":
                        self._client = AsyncClient()

                    with open(self.base_dir / LOCK_FILE, "r", encoding="utf-8") as f:
                        lock_content = f.read()
                    with open(self.wwf_path, "r", encoding="utf-8") as f:
                        key_content = f.read()
                    existing_timeline = load_timeline(self.timeline_path)
                    # Continue turn numbering where the loaded timeline left off.
                    self.turn_counter = self.last_timeline_turn = _max_timeline_turn(existing_timeline)

                    self.messages = [{"role": "system", "content": render_protocol(lock_content, self.scene_images)}]
                    if existing_timeline:
                        self.messages.append({
                            "role": "system",
                            "content": (
                                "SESSION_TIMELINE — these are events that happened earlier this session.\n"
                                "Refer to them when the player asks about past events. Do not replay or "
                                "re-describe them.\n\n" + existing_timeline
                            ),
                        })

                    if self.scene_images:
                        locations = known_scene_locations(self.base_dir / OUTPUT_DIR, self.active_name)
                        if locations:
                            body = (
                                "KNOWN IMAGE LOCATIONS (reuse the exact name whenever the scene is in "
                                "the same place; only use a new name for a genuinely new place):\n"
                                + "\n".join(f"- {loc}" for loc in locations)
                            )
                        else:
                            body = (
                                "KNOWN IMAGE LOCATIONS: none recorded yet. Choose a precise, stable "
                                "location name (the room or spot, not the building or district)."
                            )
                        self.messages.append({"role": "system", "content": body})

                    await self._emit({
                        "type": "ready",
                        "world": os.path.basename(self.wwf_path),
                        "player": os.path.basename(self.player_path),
                        "model": self.model,
                        "provider": self.provider,
                        "context_window": self.context_window,
                        "turn": self.turn_counter,
                        "tools": [t["function"]["name"] for t in self.tools_schema],
                    })

                    # ── Awakening: WWF -> tools -> sync token -> resume -> opening scene ──
                    # (The opening illustration is requested by the GM itself, in
                    # the awakening tool batch — see GameMaster_MCP.md.)
                    await self._emit({"type": "busy", "value": True})
                    awakening = await self._run_role(key_content, "awakening")
                    await self._ensure_scene_image(awakening or "")
                    await self._emit({"type": "awakening_end", "text": awakening or ""})
                    await self._emit({"type": "busy", "value": False})

                    while True:
                        cmd = await self._cmd_q.get()
                        if cmd is None:
                            break
                        try:
                            await self._handle_command(cmd)
                        except Exception as e:  # noqa: BLE001
                            await self._emit({"type": "error",
                                              "message": f"{type(e).__name__}: {e}",
                                              "traceback": traceback.format_exc()})
        except Exception as e:  # noqa: BLE001
            await self._emit({"type": "fatal",
                              "message": f"{type(e).__name__}: {e}",
                              "traceback": traceback.format_exc()})
        finally:
            self.session = None
            if devnull is not None:
                try:
                    devnull.close()
                except Exception:  # noqa: BLE001
                    pass
            await self._emit({"type": "closed"})
            await self._evt_q.put(None)

    async def _handle_command(self, cmd: dict) -> None:
        kind = cmd.get("type")
        if kind == "action":
            await self._handle_action(cmd.get("text", ""))
        elif kind == "slash":
            await self._handle_slash(cmd.get("command", ""))
        elif kind == "resume":
            await self._emit({"type": "busy", "value": True})
            try:
                text = await self._run_role("{{_CONTINUE_EXECUTION}}", "resume")
                await self._emit({"type": "turn_end", "text": text or ""})
            finally:
                await self._emit({"type": "busy", "value": False})
        elif kind == "save":
            await self._handle_save_command()
        elif kind == "close":
            await self._cmd_q.put(None)

    async def _handle_action(self, text: str) -> None:
        await self._emit({"type": "busy", "value": True})
        self._scene_requested_turn = False  # at most one illustration per turn
        try:
            result = await self._run_role(text, "turn")
            await self._ensure_scene_image(result or "")
            self.turn_counter += 1
            await self._emit({"type": "turn_end", "text": result or "", "turn": self.turn_counter})
        finally:
            await self._emit({"type": "busy", "value": False})

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
        elif cmd == "/sync":
            await self._emit({"type": "busy", "value": True})
            try:
                await self._run_role("{{_SYNC_DATABASE}}", "sync")
                await self._emit({"type": "notice", "title": "Sync", "text": "Database synchronized."})
            finally:
                await self._emit({"type": "busy", "value": False})
        elif cmd == "/save":
            await self._handle_save_command()
        elif cmd == "/quit":
            await self._emit({"type": "notice", "title": "Quit", "text": "Closing connection to the void..."})
            await self._cmd_q.put(None)
        else:
            await self._emit({"type": "notice", "title": "Unknown command",
                              "text": f"{cmd}\n{HELP_TEXT}"})
        await self._emit({"type": "turn_end", "text": ""})

    # ── save / load support ───────────────────────────────────────────────

    def _save_paths(self, stem: str):
        out = self.base_dir / OUTPUT_DIR
        return (out / f"{stem}.wwf", str(out / f"{stem}.player"), str(out / f"{stem}.timeline"))

    async def _collect_player_save(self) -> dict:
        text = await self._call_tool_text("dump_player_db", {})
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
        tmp = f"{player_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db_data, f, indent=2)
        os.replace(tmp, player_path)

    async def _save_to_active(self):
        if self.session is None or not self.player_path:
            return None
        try:
            db_data = await self._collect_player_save()
            self._write_player_atomic(db_data, self.player_path)
        except Exception as exc:  # noqa: BLE001
            await self._emit({"type": "error", "message": f"save failed: {type(exc).__name__}: {exc}"})
            return None
        info = {
            "type": "saved",
            "name": self.active_name,
            "player": os.path.basename(self.player_path),
            "timeline": os.path.basename(self.timeline_path) if self.timeline_path else None,
            "wwf": os.path.basename(self.active_wwf) if self.active_wwf else None,
        }
        await self._emit(info)
        return info

    async def _plain_summary(self, prompt: str) -> str:
        """Tool-free, one-off model call for a summary; does not touch history."""
        messages = self.messages + [{"role": "user", "content": prompt}]
        parts: list[str] = []
        if self.provider == "gemini":
            if self._gemini is None:
                from .gemini_stream import GeminiChat
                self._gemini = GeminiChat(os.environ.get("GEMINI_API_KEY") or "")
            events = self._gemini.stream(
                messages, self.model, tools=None,
                temperature=self.temperature, think=self.think, persist=False,
            )
        else:
            events = stream_chat(
                self._client, self.model, messages, None, self._options(), think=self.think,
            )
        async for evt in events:
            if evt["type"] == "narrative_delta":
                parts.append(evt["text"])
            elif evt["type"] == "error":
                return ""
        return "".join(parts)

    async def _summarize_timeline(self, target_turn: int):
        """Append a comprehensive timeline segment covering turns since the last save."""
        if target_turn <= self.last_timeline_turn:
            return None
        start = self.last_timeline_turn + 1
        await self._emit({"type": "notice", "title": "Timeline",
                          "text": f"Summarising turns {start}-{target_turn} for the save..."})
        prompt = TIMELINE_PROMPT.replace("X-Y", f"{start}-{target_turn}")
        text = await self._plain_summary(prompt)
        entry = _clean_sync_tokens(text or "")
        if entry and "**Key Events**" in entry:
            append_timeline_file(self.timeline_path, entry)
            self.last_timeline_turn = target_turn
            await self._emit({"type": "timeline", "entry": entry})
            return entry
        return None

    async def _handle_save_command(self) -> None:
        """Save in place: the session writes the world's own files (never renames).

        Renaming was dropped when images arrived: portraits and scenes live in
        `output/images/{stem}/`, keyed to the world name, so a new stem would
        orphan them.
        """
        await self._summarize_timeline(self.turn_counter)
        await self._save_to_active()

    async def _run_role(self, role_content, label: str, quiet: bool = False) -> str:
        """Append a message and run the streaming tool loop, auto-resuming pauses."""
        if isinstance(role_content, str):
            self.messages.append({"role": "user", "content": role_content})
        else:
            self.messages.append(role_content)

        while True:
            result = await self._chat_with_tools(label, quiet=quiet)
            if result == "__SYSTEM_PAUSE__":
                await self._emit({"type": "paused"})
                self.messages.append({"role": "user", "content": "{{_CONTINUE_EXECUTION}}"})
                label = "resume"
                continue
            return result

    async def _stream_assistant(self, label: str, quiet: bool = False):
        """Stream one assistant message; emit deltas; append it to history.

        `quiet` suppresses narrative/thinking output (used for the scene-image
        corrective round, whose only visible effect is the tool call itself).
        """
        if not quiet:
            await self._emit({"type": "assistant_start", "label": label})
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict] = []
        prompt_eval = 0
        malformed = False

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
        if prompt_eval:
            self.current_context_tokens = prompt_eval
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

    async def _chat_with_tools(self, label: str, quiet: bool = False) -> str:
        while True:
            content, thinking, tool_calls, malformed = await self._stream_assistant(label, quiet=quiet)
            if malformed:
                return "The GM stumbles over their words... (malformed response)"

            thinking_only = bool(thinking and not content and not tool_calls)
            retries = 0
            while thinking_only and retries < MAX_THINKING_RETRIES:
                retries += 1
                self.messages.append({"role": "user", "content": "Continue"})
                content, thinking, tool_calls, malformed = await self._stream_assistant("continue", quiet=quiet)
                if malformed:
                    return "The GM stumbles over their words... (malformed response)"
                thinking_only = bool(thinking and not content and not tool_calls)

            if thinking_only and retries >= MAX_THINKING_RETRIES:
                return "The GM pauses, deep in thought..."

            if tool_calls:
                for tc in tool_calls:
                    await self._execute_tool(tc)
                names = {(tc.get("function") or {}).get("name") for tc in tool_calls}
                if content and names <= NARRATIVE_PHASE_TOOLS:
                    # Prose + a narrative-phase tool (request_scene_image) is the
                    # turn's final response: the image call ends the turn. Without
                    # this the loop asks for another round and the GM re-narrates
                    # the whole turn (the duplicate-answer bug).
                    return content
                continue

            if any(token in (content or "") for token in PAUSE_TOKENS):
                return "__SYSTEM_PAUSE__"

            return content

    async def _ensure_scene_image(self, narrative: str) -> None:
        """Guarantee one scene image per turn: one quiet corrective round for the
        GM to author the call, else an engine fallback built from the narrative."""
        if not self.scene_images or self._scene_requested_turn:
            return
        await self._run_role(
            "SYSTEM: This turn is missing its required request_scene_image (the imagery rule). "
            "Emit ONLY the request_scene_image tool call now — no narrative — with the exact "
            "location of the scene and the most vivid moment you just narrated.",
            "scene-fix",
            quiet=True,
        )
        if self._scene_requested_turn:
            return
        self._scene_requested_turn = True
        await self._emit({
            "type": "scene_request", "kind": "auto",
            "description": narrative or "",
            "location": self._match_known_location(narrative),
            "turn": self.turn_counter,
        })

    def _match_known_location(self, narrative: str) -> str:
        """Longest known location name that actually appears in the narrative."""
        text = str(narrative or "").lower()
        if not text:
            return ""
        try:
            locations = known_scene_locations(self.base_dir / OUTPUT_DIR, self.active_name or "")
        except Exception:  # noqa: BLE001 - never fail a turn over location recovery
            return ""
        best = ""
        for loc in locations:
            if loc and loc.lower() in text and len(loc) > len(best):
                best = loc
        return best

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
        try:
            result = await self.session.call_tool(name, arguments=args)
            text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
            is_error = bool(getattr(result, "isError", False))
        except Exception as e:  # noqa: BLE001
            text = f"Tool error: {type(e).__name__}: {e}"
            is_error = True
        await self._emit({"type": "tool_result", "name": name, "text": text, "is_error": is_error})
        self.messages.append({"role": "tool", "content": text, "name": name})
        if name == SCENE_TOOL and self.scene_images and not self._scene_requested_turn:
            self._scene_requested_turn = True
            await self._emit({
                "type": "scene_request", "kind": "story",
                "description": str(args.get("description") or ""),
                "mood": str(args.get("mood") or ""),
                "location": str(args.get("location") or ""),
                "turn": self.turn_counter,
            })

    async def _call_tool_text(self, name: str, args: dict) -> str:
        result = await self.session.call_tool(name, arguments=args)
        return "\n".join(b.text for b in result.content if hasattr(b, "text"))
