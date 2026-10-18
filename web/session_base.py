"""Session construction and lifetime: the public API, the run loop, and command dispatch."""

import asyncio
import os
import random
import sys
import traceback

from pathlib import Path

# Make the repo root importable so we can reuse existing (frozen) modules.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from .eras import START_ERA, render_era_index, render_era_text
from .scenes import discard_scene_manifest
from .ollama_stream import stream_chat
from .player_file import _player_arrival, _player_difficulty, _player_era, _player_mode
from .protocol import (
    DEFAULT_CONTEXT_WINDOW, MODE_CLASSIC, MODE_TIME_TRAVELER, SESSION_TIMELINE_HEADER,
    _tool_schema, filter_tools, render_protocol
)
from .timeline import _max_timeline_turn, load_chronicle, load_legends, load_timeline
from forge.world import render_world_text
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ollama import AsyncClient

# GM protocol file (also the session lock sentinel) and the world output dir.
LOCK_FILE = "GameMaster_MCP.md"
OUTPUT_DIR = "output"


class SessionBase:
    def __init__(self, base_dir, model: str, context_window: int = DEFAULT_CONTEXT_WINDOW,
                 temperature: float = 1.0, think=None, thinking_level=None, verbose: bool = False,
                 debug: bool = False, scene_images: bool = False, provider: str = "ollama",
                 dev: bool = False):
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
        # Developer mode: unlocks the live state editor and the GM-context inspector.
        # Off by default; the dice server is also started with `--debug` so the debug
        # tools exist. Never changes the GM protocol.
        self.dev = bool(dev)
        # The GM prompt journal (dev only): one full snapshot per model call, so the
        # inspector can show EVERYTHING fed to the GM even across a jump's compaction.
        self._gm_journal: list[dict] = []
        self._gm_journal_bytes = 0
        self._gm_capped = False
        self._gm_watch = False
        self._gm_seq = 0
        self._all_tools: list[dict] = []
        # Estimated tokens of the offered tool schemas (set at session start).
        self._tool_tokens = 0
        # Set at session start; gates the GM's scene-imagery instructions + tool.
        self.scene_images = bool(scene_images)
        # Set at session start from the character's `.player`; gates the EASY GM rules.
        self.difficulty = "hard"
        # Set at session start from the character's `.player`; which of the two games this
        # save is. Until a save says otherwise the session behaves as the era game, which
        # is what a bare session (a test, a probe) has always been.
        self.mode = MODE_TIME_TRAVELER
        self.classic = False
        # Set at session start from the character's `.player`; the era the Traveler is in.
        # A classic save carries no era at all.
        self.era = START_ERA
        # ... and where in it the Device left them (rolled, never stable).
        self.arrival = ""

        self.player_path: str | None = None
        self.timeline_path: str | None = None
        self.active_name: str | None = None
        # The live scene registry's write ticket for this session, captured at start (see
        # `discard_scene_manifest`): passed to every place/scene writer, so one that outlived
        # the session is refused instead of leaking into the next session's registry.
        self.scene_generation: int | None = None

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
        # True once narrative prose has reached the player this turn; guards against
        # asking the GM to narrate a second time (duplicate segment).
        self._narrative_emitted_turn = False
        # The places THIS turn declared (accepted `note_place` paths): text mode has no scene
        # call to carry the place, and `_ensure_place_note` asks when the count is not exactly
        # one -- none at all, or several (the engine cannot tell which one the turn ended in).
        self._place_declarations: list[list[str]] = []
        # KNOWN places: slug path -> (kingdom, area). Identity and address only -- a place is
        # known whether or not its picture exists. `_scene_seeded` is the subset whose hidden
        # establishing view is on disk (or was accepted this session); a known-but-unseeded
        # place must still supply `establishing` before its seed is accepted.
        self._scene_places: dict[tuple[str, ...], tuple[str, str]] = {}
        self._scene_seeded: set[tuple[str, ...]] = set()
        self._scene_rejected = False
        self._scene_rejected_place: list[str] | None = None
        # Deaths the GM DECLARED (`state: dead`), per (era, place): one writer of death beside
        # the combat registry. The permanent set (`_dead_perm`) and the provisional one are
        # unioned with it for display; the index is never cleared by a declaration.
        self._dead_declared: dict[tuple, set[str]] = {}
        # Declared NPC names (place main NPCs + the storyline cast), lowercased, and the
        # declarations buffered this turn which the server persists on the next scene call.
        self._cast_names: set[str] = set()
        # The place-less recurring people, by identity key -> display name: the on-demand
        # place record shows them as a footer (the per-turn block shows place regulars only).
        self._cast_display: dict[str, str] = {}
        # Static combatant sheets (name -> rendered lines) from register_combatants, merged
        # with the live roster and sent to the client for the combatant tooltips.
        self._combat_sheets: dict[str, list[str]] = {}
        self._combat_order: list[str] = []
        self._combat_initiative: dict[str, dict] = {}
        self._combat_turn: dict = {}                  # {round, actor, skipped} from the engine
        self._combat_last_roster: list[dict] = []     # last non-empty roster (for the tally)
        # True while the registry holds a hostile, living, active non-player (derived from
        # every combat tool's `registry_summary`). The Device does not count such turns, and
        # a save is refused while it holds -- an unfinished fight would snapshot transient
        # HP/conditions with no roster, and the timeline would not survive the reload.
        self._in_combat = False
        # The engine-composed Mechanics block for the current turn: each tool result's
        # narrative_format, in call order, sent to the client as the `mechanics` event.
        self._mechanics_lines: list[str] = []
        self._pending_npcs: list[dict] = []
        # ── the Device ──────────────────────────────
        # The Device and its parts are real inventory items (engine-seeded at creation). It is
        # player-controlled from the first turn; the recovered parts are the key to the future.
        # `has_device` False means the Device itself is gone and there are no controls.
        self.has_device = False
        self.parts_recovered = 0
        # The eras the Traveller has stood in, oldest first: "previous" means the one before
        # the current, which the timeline legends cannot give (a return does not rewrite one).
        self.journey: list[str] = []
        # The GM's memory line for the age it is leaving, captured from `{{remembers: ...}}`
        # on the closing turn; `_jump` prefers it over the config template and clears it.
        self._pending_legend: str = ""
        # Injected in tests so the "random" era is reproducible.
        self._rng = random.Random()
        # Where the always-on era messages live, so a jump can rewrite them in place
        # instead of appending one index (and one tree) per age ever visited.
        self._era_index_at: int | None = None
        self._places_at: int | None = None
        self._primed: list[dict] = []
        # The scene caption's place: the last declared/used place (age and place are the only
        # scene fields the engine knows; time and weather are dropped in text mode).
        self._current_place: dict | None = None
        # The live ordering stamp for the place list (see `_reload_era_scene_state`): bumped on
        # every touch of a place this session, seeded above the manifest's own stamps.
        self._place_tick: int = 0
        # The tool result's place correction for the call in flight (path identity or address):
        # set by `_execute_tool`, appended to the result, reset at the start of the next call.
        self._place_note: str = ""
        # ── what the ages remember ──────────────────────────────────────
        # One or two lines per era, read from the save's timeline and injected lazily: the
        # eras the Traveller is not in cost nothing.
        self._legends: dict[str, str] = {}
        # The current-truth notebook(s) and the collapsed event chronicle: all live in memory
        # until Save, like the legends, so an unsaved session never touches the file. A classic
        # save has one `_state`; the era game keeps one page per age, and a page captured on
        # the closing turn waits in `_pending_era_state` until the jump.
        self._state: str = ""
        self._era_states: dict[str, str] = {}
        # The engine-injected lean CHARACTER SHEET block (see `_sheet_body`), rebuilt at session
        # build and at every jump. The GM never fetches it (`dump_player_db` is engine-only).
        self._sheet_block: str = ""
        self._pending_era_state: tuple[str, str] | None = None
        self._chronicle: tuple[int, int, str] | None = None
        # The kept entries a compaction produced, consumed once by `_write_timeline` so the
        # folded raw entries are not re-read from disk and written back.
        self._timeline_kept: str | None = None
        self._era_started_turn = 0
        # Declared NPC identity (role, race, class), by lowercased name ->
        # (canonical name, role, race, class). Echoed with the character that appears,
        # never primed: needed on the turn an NPC walks on stage, not on every turn.
        self._npc_people: dict[str, tuple[str, str, str, str]] = {}
        # Main NPC liveness for the era game. `_dead_perm` mirrors the manifest's permanent
        # `dead`; `_dead_provisional` holds deaths detected THIS visit, which a heal can
        # undo until they are frozen at Save / era-turn.
        self._dead_perm: dict[tuple, set] = {}
        self._dead_provisional: dict[tuple, set] = {}

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
        self.difficulty = _player_difficulty(self.player_path)
        # Which game this is. A classic save carries no era at all, and that single fact
        # is what makes every era-scoped path below fall away cleanly.
        self.mode = _player_mode(self.player_path)
        self.classic = self.mode == MODE_CLASSIC
        self.era = "" if self.classic else _player_era(self.player_path)
        self.arrival = "" if self.classic else _player_arrival(self.player_path, self.era)
        # The places/NPC registry is memory-only for a session: start it from the last Save,
        # never from a working copy a previous unsaved session left behind. The generation it
        # returns is this session's write ticket, handed to every writer; a worker that outlives
        # the session still carries it and is refused.
        self.scene_generation = discard_scene_manifest(
            os.path.dirname(self.player_path), self.active_name)
        # A resumed save already has its place declared (the manifest's `current`): connect the
        # arrival string to it now. A new character has no declared place, so the GM declares the
        # opening one.
        self._connect_arrival_to_declared_place()
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

    async def submit_device(self, action: str, era: str = "", area: str = "",
                            place: str = "") -> None:
        await self._cmd_q.put({"type": "device", "action": action,
                               "era": era, "area": area, "place": place})

    async def submit_dev(self, cmd: dict) -> None:
        """A developer-mode command (state edit, Device control, GM-inspector watch).

        Queued like any other command, so it is applied after an in-flight turn and can
        never interleave with one."""
        await self._cmd_q.put({"type": "dev", **dict(cmd or {})})

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
        # Drop the memory-only registry: a session that ends without saving must not leak
        # its places/NPCs into the next one.
        if self.player_path:
            discard_scene_manifest(os.path.dirname(self.player_path), self.active_name)

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
                args=["dice_server.py", self.player_path] + (["--debug"] if self.dev else []),
                cwd=str(self.base_dir),
            )
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    self.session = session
                    await session.initialize()
                    listing = await session.list_tools()
                    tools = [_tool_schema(t) for t in listing.tools]
                    # The full listing (before filtering) is what the dev inspector shows as
                    # "not offered to the GM".
                    self._all_tools = tools
                    self.tools_schema = filter_tools(tools, self.scene_images, self.mode)
                    # The tools ride every request (a separate `tools` parameter), so the
                    # inspector's per-prompt token estimate must include them. Fixed per
                    # session, so cost it once.
                    self._tool_tokens = self._estimate_tokens(self.tools_schema)
                    if self.provider != "gemini":
                        self._client = AsyncClient()

                    with open(self.base_dir / LOCK_FILE, "r", encoding="utf-8") as f:
                        lock_content = f.read()
                    kept = load_timeline(self.timeline_path)
                    self._chronicle = load_chronicle(self.timeline_path)
                    # The STATE pages: one per age, or a classic save's single unkeyed
                    # notebook.
                    self._load_state_pages()
                    # Continue turn numbering where the timeline left off: the kept entries,
                    # or the chronicle's covered range when it reaches further back.
                    self.turn_counter = self.last_timeline_turn = _max_timeline_turn(kept)
                    if self._chronicle and self._chronicle[1] > self.last_timeline_turn:
                        self.turn_counter = self.last_timeline_turn = self._chronicle[1]
                    # A save with a timeline has been played: this session RESUMES the story
                    # rather than opening a new game, so the opening line must not say
                    # "You arrive" -- the era file is framed before the GM reads it.
                    resumed = bool(kept) or bool(self._chronicle)
                    if self.classic:
                        # The classic game hands the GM the world it was built on.
                        key_content = render_world_text()
                    else:
                        key_content = (f"{self._era_opening(resumed)}\n\n"
                                       f"{render_era_text(self.era)}")

                    self.messages = [{"role": "system", "content": render_protocol(
                        lock_content, self.scene_images, self.difficulty, self.mode)}]
                    # The ERA INDEX is always-on *in the era game only*: the ladder, one
                    # line each, and nothing else. The era's own scaffold arrives with the
                    # awakening; any other era is fetched, never injected. A classic
                    # session pays for none of it and reads the world scaffold instead.
                    self._era_index_at = None
                    if not self.classic:
                        self._era_index_at = len(self.messages)
                        self.messages.append({"role": "system", "content": render_era_index(self.era)})
                    memory_body = self._timeline_body()
                    if memory_body:
                        self.messages.append({"role": "system",
                                              "content": SESSION_TIMELINE_HEADER + memory_body})

                    # The place/NPC registry is primed in BOTH modes: with images on it comes
                    # from the scene manifest, with images off from the text registry. The block
                    # itself goes LAST in the fixed prefix (below the sheet): it is the one
                    # always-on message a move rewrites in place.
                    self._reload_era_scene_state()

                    # What the ages remember: this era's legend, if it has one. The ages
                    # exist only in the era game, so a classic session never hears them.
                    self._legends = {} if self.classic else load_legends(self.timeline_path)
                    memory = "" if self.classic else self._memory_message("")
                    if memory:
                        self.messages.append({"role": "system", "content": memory})

                    # The character sheet, engine-injected: the GM never fetches it, so it is
                    # always in context (and re-injected at every jump -- see `_compact`). Lean:
                    # only the fields the GM narrates from and the key-sets it addresses writes
                    # with, never the combat math.
                    self._sheet_block = await self._fetch_sheet()
                    if self._sheet_block:
                        self.messages.append({"role": "system", "content": self._sheet_block})
                    # The KNOWN PLACES block: scoped to where the GM is, and last in the fixed
                    # prefix, so a move invalidates nothing before it.
                    self._places_at = len(self.messages)
                    self.messages.append({"role": "system", "content": self._places_body()})
                    self._era_started_turn = self.turn_counter

                    # The Device: how many parts are in the pack, and the eras visited, from
                    # the live DB.
                    await self._refresh_device()

                    await self._emit({
                        "type": "ready",
                        "world": f"{self.active_name}.player",
                        "player": os.path.basename(self.player_path),
                        "model": self.model,
                        "provider": self.provider,
                        "difficulty": self.difficulty,
                        "dev": self.dev,
                        "era": self.era,
                        "arrival": self.arrival,
                        "cadence": self._device_state(),
                        "context_window": self.context_window,
                        "turn": self.turn_counter,
                        "tools": [t["function"]["name"] for t in self.tools_schema],
                    })
                    # The scene caption the client shows as the GM header (era + place).
                    await self._emit_caption()

                    # ── Session start: ERA_FILE -> tools -> pause token -> resume -> narrative ──
                    # (A new game opens the scene; a resumed save continues it -- the era file
                    # carries the framing. The opening illustration is requested by the GM
                    # itself, in the session-start tool batch -- see GameMaster_MCP.md.)
                    await self._emit({"type": "busy", "value": True})
                    self._mechanics_lines = []
                    self._narrative_emitted_turn = False
                    awakening = await self._run_role(key_content, "awakening")
                    awakening = await self._ensure_turn_prose(awakening)
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
                self._narrative_emitted_turn = False
                text = await self._run_role("{{resume}}", "resume")
                await self._emit({"type": "turn_end", "text": text or ""})
            finally:
                await self._emit({"type": "busy", "value": False})
        elif kind == "save":
            await self._handle_save_command()
        elif kind == "device":
            await self._handle_device_command(cmd)
        elif kind == "dev":
            await self._handle_dev_command(cmd)
        elif kind == "close":
            await self._cmd_q.put(None)

    async def _run_turn(self, content: str, label: str) -> None:
        """One GM turn: the prose, at most one illustration, then the turn counter.

        Used for the player's turns and for the Device's jump -- a turn nobody asked for."""
        await self._emit({"type": "busy", "value": True})
        self._scene_requested_turn = False  # at most one illustration per turn
        self._narrative_emitted_turn = False  # the turn's prose is emitted once
        self._place_declarations = []      # the turn's place echo (see _ensure_place_note)
        self._mechanics_lines = []          # the engine-composed block is per turn
        try:
            result = await self._run_role(content, label)
            result = await self._ensure_turn_prose(result)
            await self._ensure_scene_image(result or "")
            await self._ensure_place_note()
            self.turn_counter += 1
            await self._emit({"type": "turn_end", "text": result or "", "turn": self.turn_counter})
        finally:
            await self._emit({"type": "busy", "value": False})

    async def _handle_action(self, text: str) -> None:
        await self._run_turn(text, "turn")
        # The Device never fires on its own; the panel state is refreshed for the turn.
        await self._emit({"type": "cadence", **self._device_state()})

    async def _handle_device_command(self, cmd: dict) -> None:
        """A player-driven Device action: release it now, to a chosen age and place."""
        action = str(cmd.get("action") or "").strip().lower()
        if self.classic or not self.has_device:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "There is no Device to command."})
            return
        if action == "release":
            await self._device_release(cmd)
        else:
            await self._emit({"type": "error", "message": f"unknown Device action: {action}"})

    def _compact(self, just_left: str) -> None:
        """The era switch IS the compaction: prefix + what the ages remember + what is true
        now, and nothing behind it. This is what stops the context growing ~104 tokens per turn
        at the test cadence -- and the thing that saves the tokens is the legend, not a prose
        summary. The STATE pages are re-injected here: a return to an age must be handed what
        the Traveller was doing there, and compaction used to drop the notebook entirely. The
        character sheet is re-injected too (it is engine-owned and would otherwise be lost)."""
        head = [self.messages[0], {"role": "system", "content": render_era_index(self.era)}]
        self._era_index_at = 1
        memory = self._memory_message(just_left)
        if memory:
            head.append({"role": "system", "content": memory})
        state = self._state_body()
        if state:
            head.append({"role": "system", "content": state})
        if self._sheet_block:
            head.append({"role": "system", "content": self._sheet_block})
        # The KNOWN PLACES block LAST: it is the one always-on message a move rewrites, so the
        # messages before it stay cache-stable.
        self._places_at = len(head)
        head.append({"role": "system", "content": self._places_body()})
        self.messages = head
