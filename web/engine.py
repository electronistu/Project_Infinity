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

from .images import known_npc_names, known_scene_places  # noqa: E402
from forge.world import render_world_text  # noqa: E402
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
# Declares recurring NPCs (name + description). The engine buffers them and the
# server persists them; the GM then refers to those NPCs by name alone.
NPC_TOOL = "register_npcs"
# Tools the GM may attach to narrative prose (the Narrative Phase). A round that
# carries prose plus only these tools is the turn's last word: the tool loop must
# NOT run another model round, or the GM re-narrates the whole turn.
NARRATIVE_PHASE_TOOLS = frozenset({SCENE_TOOL})
# Tools whose result may change the combat roster the client shows as tooltips.
COMBAT_TOOLS = frozenset({"register_combatants", "update_combatant", "resolve_attack",
                          "resolve_magic", "modify_exhaustion", "make_death_save"})
# Scene-imagery instructions live in GameMaster_MCP.md between these markers;
# they are sent to the GM only when storyline image generation is enabled.
_SCENE_BLOCK = re.compile(
    r"(?ms)^[ \t]*<!-- SCENE:ON -->.*?^[ \t]*<!-- SCENE:END -->[ \t]*\n?"
)
_SCENE_MARKERS = re.compile(r"(?m)^[ \t]*<!-- SCENE:(?:ON|END) -->[ \t]*\n?")


def _scene_key(location, sublocation) -> tuple[str, str]:
    """Slug pair identifying a (location, sublocation) place."""
    def _s(text) -> str:
        return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return _s(location), _s(sublocation)


def format_known_places(places: list[dict]) -> str:
    """A kingdom -> area -> location -> sublocation tree for the awakening priming."""
    lines: list[str] = []
    last_k = last_a = last_l = None
    for p in places:
        if p["kingdom"] != last_k:
            lines.append(f"- {p['kingdom'] or '(unknown kingdom)'}")
            last_k, last_a, last_l = p["kingdom"], None, None
        if p["area"] != last_a:
            lines.append(f"    - {p['area'] or '(unknown area)'}")
            last_a, last_l = p["area"], None
        if p["location"] != last_l:
            lines.append(f"        - {p['location']}")
            last_l = p["location"]
        desc = f" — {p['description']}" if p["description"] else ""
        npc_labels: list[str] = []
        for npc in (p.get("main_npcs") or []):
            if not isinstance(npc, dict):
                continue
            name = str(npc.get("name") or "").strip()
            if not name:
                continue
            role = str(npc.get("role") or "").strip()
            npc_labels.append(f"{name} ({role})" if role else name)
        if npc_labels:
            label = "main NPC" if len(npc_labels) == 1 else "main NPCs"
            npc = f" · {label}: " + ", ".join(npc_labels)
        else:
            npc = ""
        lines.append(f"            - {p['sublocation'] or '(whole place)'}{desc}{npc}")
    return "\n".join(lines)


def render_protocol(text: str, scene_images: bool) -> str:
    """Strip the scene markers; drop the enclosed rules when scenes are off."""
    if scene_images:
        return _SCENE_MARKERS.sub("", text or "")
    return _SCENE_BLOCK.sub("", text or "")


def filter_tools(tools: list[dict], scene_images: bool) -> list[dict]:
    """Hide the scene-image and NPC-declaration tools unless storyline images are on."""
    if scene_images:
        return list(tools)
    return [t for t in tools
            if (t.get("function") or {}).get("name") not in (SCENE_TOOL, NPC_TOOL)]


# ── What the GM is shown of a tool result ─────────────────────────────────
#
# Tool results carry only what the GM does not already hold: the event/delta, the
# exact mechanics, and errors that explain what happened. The base sheet is already
# in context (dump_player_db), so state snapshots (`equipment`, the carrying
# breakdown, a full inventory list) are not re-sent. The FULL result still goes to the
# client (the tool_result event) for debugging; only the copy appended to the GM
# conversation is trimmed. Tools not listed here pass through untouched.
_GM_VIEW_KEEP: dict[str, tuple[str, ...]] = {
    "modify_player_numeric": (
        "success", "key", "old_value", "new_value", "delta", "clamped",
        "item_depleted", "depleted_item", "level_up", "old_level", "new_level",
        "level_up_changes", "level_up_summary", "hp_status", "message",
        "remaining_slots"),
    "update_player_list": (
        "success", "key", "item", "action", "unequipped", "note", "warning",
        "unweighed_item", "reverted", "spells_prepared_info"),
    "equip_item": (
        "success", "action", "item", "armor_class_before", "armor_class_after",
        "warnings", "time_cost", "already_equipped", "equipped", "held",
        "occupied", "worn", "slot"),
    "attune_item": (
        "success", "action", "item", "attuned", "attunement_slots_free",
        "warnings", "already_attuned"),
    "rest": ("success", "rest_type", "changes", "hints"),
    "register_combatants": ("success", "initiative_order", "registry_summary"),
    "update_combatant": ("success", "name", "is_player", "hp", "ac", "conditions",
                         "exhaustion", "blocked_conditions", "already_present", "note"),
    "modify_exhaustion": ("success", "exhaustion", "old", "delta", "effects", "max_hp"),
    "make_death_save": ("success", "roll", "successes", "failures", "outcome", "hp"),
    # Acknowledgements: the GM just made the call, so only the outcome is new.
    "request_scene_image": ("status", "note"),
    "register_npcs": ("status", "count", "note"),
}
# Added on failure so the GM can explain and act on a refused/wrong call.
_GM_VIEW_ERROR_EXTRA = (
    "error", "reason", "gm_instruction", "turn_lost", "action_consumed",
    "current_items", "held", "occupied", "worn", "slot", "available_keys",
    "attuned", "attunement_slots_free", "conflicting_item", "prerequisite")
# `carrying` is reduced to the one-line state that drives disadvantage.
_GM_VIEW_CARRYING = ("status", "speed_penalty", "carried", "capacity")


def _gm_tool_view(name: str, text: str) -> str:
    """The trimmed result the GM sees, as JSON (falls back to `text` when not a dict).

    Listed tools keep only their new-info keys; every other tool passes its mechanics
    through, but the heavy state snapshots (`equipment`, `current_list`, the full
    `carrying` breakdown) are always dropped — the GM already holds the sheet."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if not isinstance(payload, dict):
        return text
    if name in _GM_VIEW_KEEP:
        keys = list(_GM_VIEW_KEEP[name])
        if payload.get("success", True) is False:
            keys += list(_GM_VIEW_ERROR_EXTRA)
        view = {k: payload[k] for k in dict.fromkeys(keys) if k in payload}
    else:
        view = dict(payload)
    # Always drop the state the GM already holds, and the mechanics disclosure itself (the
    # engine composes that block from narrative_format — the GM never transcribes it).
    view.pop("equipment", None)
    view.pop("current_list", None)
    view.pop("registry_summary", None)
    view.pop("sheets", None)
    view.pop("narrative_format", None)
    carry = payload.get("carrying")
    if isinstance(carry, dict):
        slim = {k: carry[k] for k in _GM_VIEW_CARRYING if k in carry}
        if slim:
            view["carrying"] = slim
        else:
            view.pop("carrying", None)
    return json.dumps(view, ensure_ascii=True)


def _clean_pause_tokens(text: str) -> str:
    for token in PAUSE_TOKENS:
        text = text.replace(token, "")
    return text.strip()


class GameSession:
    """One in-memory, long-lived game session (single user, local server)."""

    def __init__(self, base_dir, model: str, context_window: int = DEFAULT_CONTEXT_WINDOW,
                 temperature: float = 1.0, think=None, thinking_level=None, verbose: bool = False,
                 debug: bool = False, scene_images: bool = False, provider: str = "ollama"):
        self.base_dir = Path(base_dir)
        self.model = model
        self.provider = provider or "ollama"
        self.context_window = context_window
        self.temperature = temperature
        self.think = think
        # Gemini 3.x thinking effort (None = the model's own default). Ollama
        # keeps `think`; Gemini also uses `think` for thought summaries.
        self.thinking_level = thinking_level
        self.verbose = verbose
        self.debug = debug
        # Set at session start; gates the GM's scene-imagery instructions + tool.
        self.scene_images = bool(scene_images)

        self.player_path: str | None = None
        self.timeline_path: str | None = None
        self.active_name: str | None = None

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
        # Known seeded places: (location_slug, sublocation_slug) -> (kingdom, area). A new
        # place must supply `establishing` before its seed is accepted.
        self._scene_places: dict[tuple[str, str], tuple[str, str]] = {}
        self._scene_rejected = False
        self._scene_rejected_place: tuple[str, str] | None = None
        # Declared NPC names (place main NPCs + the storyline cast), lowercased, and the
        # declarations buffered this turn which the server persists on the next scene call.
        self._cast_names: set[str] = set()
        # Static combatant sheets (name -> rendered lines) from register_combatants, merged
        # with the live roster and sent to the client for the combatant tooltips.
        self._combat_sheets: dict[str, list[str]] = {}
        self._combat_order: list[str] = []
        self._combat_initiative: dict[str, dict] = {}
        # The engine-composed Mechanics block for the current turn: each tool result's
        # narrative_format, in call order, sent to the client as the `mechanics` event.
        self._mechanics_lines: list[str] = []
        self._pending_npcs: list[dict] = []

    # ── public API ────────────────────────────────────────────────────────

    async def start(self, save_path) -> None:
        path = Path(save_path)
        if not path.is_absolute():
            candidate = self.base_dir / path
            if not candidate.exists():
                # Allow callers to pass just the save filename (lives in output/).
                candidate = self.base_dir / OUTPUT_DIR / path
            path = candidate.resolve()
        stem = os.path.splitext(str(path))[0]
        self.player_path = stem + ".player"
        self.timeline_path = stem + ".timeline"
        self.active_name = os.path.splitext(os.path.basename(str(path)))[0]
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
                thinking_level=self.thinking_level, think=self.think,
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
                    key_content = render_world_text()
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
                        places = known_scene_places(self.base_dir / OUTPUT_DIR, self.active_name)
                        self._scene_places = {
                            _scene_key(p["location"], p["sublocation"]): (p["kingdom"], p["area"])
                            for p in places
                        }
                        self._cast_names = known_npc_names(self.base_dir / OUTPUT_DIR,
                                                           self.active_name or "")
                        if places:
                            body = (
                                "KNOWN IMAGE PLACES (reuse these exact kingdom / area / location / "
                                "sublocation names; only invent a new name for a genuinely new place, "
                                "and request its seed with `establishing` only when it is not listed "
                                "here. Use the place's main NPCs by NAME; never restate their look):\n"
                                + format_known_places(places)
                            )
                        else:
                            body = (
                                "KNOWN IMAGE PLACES: none recorded yet. On entering a place, declare "
                                "its `kingdom` and `area` and request its seed with `establishing` (a "
                                "short, people-free description) together with the action image."
                            )
                        self.messages.append({"role": "system", "content": body})

                    await self._emit({
                        "type": "ready",
                        "world": f"{self.active_name}.player",
                        "player": os.path.basename(self.player_path),
                        "model": self.model,
                        "provider": self.provider,
                        "context_window": self.context_window,
                        "turn": self.turn_counter,
                        "tools": [t["function"]["name"] for t in self.tools_schema],
                    })

                    # ── Awakening: WWF -> tools -> pause token -> resume -> opening scene ──
                    # (The opening illustration is requested by the GM itself, in
                    # the awakening tool batch — see GameMaster_MCP.md.)
                    await self._emit({"type": "busy", "value": True})
                    self._mechanics_lines = []
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
        self._mechanics_lines = []          # the engine-composed block is per turn
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
            "save": f"{self.active_name}.player" if self.active_name else None,
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
                thinking_level=self.thinking_level, think=self.think, persist=False,
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
        entry = _clean_pause_tokens(text or "")
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
                self._scene_rejected = False
                for tc in tool_calls:
                    await self._execute_tool(tc)
                names = {(tc.get("function") or {}).get("name") for tc in tool_calls}
                if content and names <= NARRATIVE_PHASE_TOOLS and not self._scene_rejected:
                    # Prose + an accepted narrative-phase tool (request_scene_image) is
                    # the turn's final response: the image call ends the turn. A rejected
                    # scene call (missing seed) keeps looping so the GM can re-call with
                    # `establishing`.
                    return content
                continue

            if any(token in (content or "") for token in PAUSE_TOKENS):
                return "__SYSTEM_PAUSE__"

            return content

    async def _ensure_scene_image(self, narrative: str) -> None:
        """Guarantee one scene image per turn: a quiet corrective round for the
        GM to author the call (asking for `establishing` when the seed is missing),
        else an engine fallback built from the narrative."""
        if not self.scene_images or self._scene_requested_turn:
            return
        rejected = self._scene_rejected_place
        if rejected:
            location, sublocation = rejected
            where = f'{location} — {sublocation}' if sublocation else location
            nudge = (
                "SYSTEM: this turn's request_scene_image was rejected because the place has no "
                f"establishing view yet. Emit ONLY request_scene_image again for \"{where}\", "
                "this time WITH `establishing` = a short, empty description of the place (no "
                "people, creatures or animals), plus the action `description`."
            )
        else:
            nudge = (
                "SYSTEM: This turn is missing its required request_scene_image (the imagery rule). "
                "Emit ONLY the request_scene_image tool call now — no narrative — with the exact "
                "location and sublocation, plus `establishing` if this place has no establishing "
                "view yet."
            )
        self._scene_rejected_place = None
        await self._run_role(nudge, "scene-fix", quiet=True)
        if self._scene_requested_turn:
            return
        self._scene_requested_turn = True
        kingdom, area, location, sublocation = self._match_known_place(narrative)
        npcs, self._pending_npcs = self._pending_npcs, []
        await self._emit({
            "type": "scene_request", "kind": "auto",
            "description": narrative or "",
            "kingdom": kingdom, "area": area,
            "location": location, "sublocation": sublocation,
            "time_of_day": "", "weather": "", "characters": {},
            "establishing": "", "main_npcs": [], "npcs": npcs, "seed_change": "",
            **(await self._scene_player_hints()),
            "turn": self.turn_counter,
        })

    def _match_known_place(self, narrative: str) -> tuple[str, str, str, str]:
        """Best known (kingdom, area, location, sublocation) appearing in the narrative."""
        text = str(narrative or "").lower()
        if not text:
            return "", "", "", ""
        try:
            places = known_scene_places(self.base_dir / OUTPUT_DIR, self.active_name or "")
        except Exception:  # noqa: BLE001 - never fail a turn over location recovery
            return "", "", "", ""
        best, score = ("", "", "", ""), -1
        for p in places:
            loc, sub = p["location"], p["sublocation"]
            if loc and loc.lower() in text:
                s = len(loc) + (len(sub) if sub and sub.lower() in text else 0)
                if s > score:
                    best, score = (p["kingdom"], p["area"], loc,
                                   sub if (sub and sub.lower() in text) else ""), s
        return best

    def _match_known_location(self, narrative: str) -> str:
        """Longest known location name that actually appears in the narrative."""
        return self._match_known_place(narrative)[2]

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
        warning = self._scene_seed_warning(args) if name == SCENE_TOOL else None
        if warning is not None:
            text, is_error = warning, True
            self._scene_rejected = True
            self._scene_rejected_place = (str(args.get("location") or ""), str(args.get("sublocation") or ""))
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
        gm_text = _gm_tool_view(name, text)
        if name == SCENE_TOOL and warning is None:
            note = self._undeclared_note(args)
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
            roster = self._combat_roster_update(text)
            if roster is not None:
                await self._emit({"type": "combat_roster", "combatants": roster,
                                  "order": list(self._combat_order),
                                  "initiative": dict(self._combat_initiative)})
        if name == SCENE_TOOL and self.scene_images and warning is None and not self._scene_requested_turn:
            self._scene_requested_turn = True
            location = str(args.get("location") or "")
            sublocation = str(args.get("sublocation") or "")
            kingdom = str(args.get("kingdom") or "")
            area = str(args.get("area") or "")
            if str(args.get("establishing") or "") or str(args.get("seed_change") or ""):
                self._scene_places[_scene_key(location, sublocation)] = (kingdom, area)
            elif not kingdom and not area:
                kingdom, area = self._scene_places.get(_scene_key(location, sublocation), ("", ""))
            # Declarations flow to the server with the image call (the single writer).
            npcs, self._pending_npcs = self._pending_npcs, []
            main_npcs = args.get("main_npcs") if isinstance(args.get("main_npcs"), list) else []
            await self._emit({
                "type": "scene_request", "kind": "story",
                "description": str(args.get("description") or ""),
                "mood": str(args.get("mood") or ""),
                "kingdom": kingdom, "area": area,
                "location": location, "sublocation": sublocation,
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
        roster to emit, or None when the result carries no roster."""
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
        if not isinstance(summary, list) or not summary:
            return None
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
        return roster

    def _remember_declared(self, npcs) -> list[str]:
        """Buffer declared NPCs (name + description) and remember their names."""
        added: list[str] = []
        if not isinstance(npcs, list):
            return added
        for entry in npcs:
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split())[:80]
            if not name:
                continue
            self._cast_names.add(name.lower())
            self._pending_npcs.append({
                "name": name,
                "description": " ".join(str(entry.get("description") or "").split())[:400],
            })
            added.append(name)
        return added

    def _collect_declarations(self, args: dict) -> None:
        """Declarations that ride a scene call: the `npcs` list + the place's main NPCs."""
        self._remember_declared(args.get("npcs"))
        main = args.get("main_npcs")
        if isinstance(main, list):
            for entry in main:
                if not isinstance(entry, dict):
                    continue
                name = " ".join(str(entry.get("name") or "").split())
                if name:
                    self._cast_names.add(name.lower())

    def _undeclared_note(self, args: dict) -> str:
        """A soft note listing `characters` names that no declaration covers."""
        characters = args.get("characters")
        if not isinstance(characters, dict) or not characters:
            return ""
        unknown: list[str] = []
        for key in characters:
            name = " ".join(str(key or "").split())
            if name and name.lower() not in self._cast_names:
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
        location = str(args.get("location") or "").strip()
        sublocation = str(args.get("sublocation") or "").strip()
        if not location:
            return None  # no place -> portrait-only action, nothing to seed
        if str(args.get("establishing") or "").strip() or str(args.get("seed_change") or "").strip():
            return None
        if _scene_key(location, sublocation) in self._scene_places:
            return None
        where = f"{location} — {sublocation}" if sublocation else location
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
