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
import copy
import json
import os
import random
import re
import sys
import time
import traceback
from pathlib import Path

# Make the repo root importable so we can reuse existing (frozen) modules.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from ollama import AsyncClient  # noqa: E402

from .images import (  # noqa: E402
    commit_scene_manifest, current_scene_place, discard_scene_manifest, known_npc_names,
    known_scene_places,
)
from .tool_schema import compact_schema  # noqa: E402
from forge.world import render_world_text  # noqa: E402
import device  # noqa: E402  # the Device (Text Time Traveler): vocabulary, cadence, control
from web.eras import (  # noqa: E402
    START_ERA, era_arrivals, era_ids, pick_arrival, playable_eras,
    render_era_index, render_era_text,
)
from .ollama_stream import stream_chat  # noqa: E402
from .stats import build_stats  # noqa: E402

# GM protocol file (also the session lock sentinel) and the world output dir.
LOCK_FILE = "GameMaster_MCP.md"
OUTPUT_DIR = "output"


def load_timeline(timeline_path):
    """Load an existing session timeline, if any.

    The era legends live in the same file but are not session events: they are stripped
    here and read by `load_legends`."""
    if os.path.exists(timeline_path):
        with open(timeline_path, "r", encoding="utf-8") as f:
            return _LEGEND_RE.sub("", f.read()).strip()
    return ""


# A legend, marked so it survives in the save's timeline without being read back as a
# session event: `<!-- legend:egypt --> Egypt remembers a stranger who ...`.
_LEGEND_RE = re.compile(r"(?m)^<!-- legend:([a-z0-9_-]+) -->[ \t]*(.*?)[ \t]*$")


def load_legends(timeline_path) -> dict:
    """What each era remembers about the Traveller, per save: `{era: one or two lines}`.

    Era-local and lazy: only the era the Traveller is in is ever put in the prompt, so the
    eras he is not in cost nothing."""
    out: dict = {}
    if not os.path.exists(timeline_path):
        return out
    try:
        with open(timeline_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return out
    for era, line in _LEGEND_RE.findall(text or ""):
        cleaned = " ".join(str(line or "").split())
        if era and cleaned:
            out.setdefault(str(era).strip().lower(), cleaned)
    return out


def append_legend(timeline_path, era: str, text: str) -> None:
    """Record what an era now remembers, permanently."""
    append_timeline_file(timeline_path, f"<!-- legend:{era} --> {text}")


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
# Emitted only when the GM attached the illustration with no prose. The GM has no
# legal output after a bare tool result, so ask for the missing narration once
# instead of re-prompting it with the tool result alone.
PROSE_RECOVERY_NUDGE = (
    "SYSTEM: You attached the illustration but wrote no prose. Write this turn's "
    "narrative now — second person, no tool calls, no meta or out-of-character "
    "commentary. Do not re-request the image."
)
# Emitted when a whole turn produced no narrative at all (a skipped NARRATIVE step,
# a pause-token echo, an image attached alone). Non-quiet, so the recovered prose
# reaches the player.
NARRATIVE_RECOVERY_NUDGE = (
    "SYSTEM: You did not write the turn's narrative. Write it now — second person, "
    "covering the mechanics already resolved. Attach one request_scene_image only if "
    "this turn has no image yet."
)
# A model that keeps echoing the pause token instead of narrating must not spin.
RESUME_GUARD_NUDGE = (
    "SYSTEM: You have already paused; the engine resumed. Write the turn's narrative now."
)
MAX_RESUME_ROUNDS = 3
# The awakening's resume re-anchors the one step a bare `{{_CONTINUE_EXECUTION}}` leaves
# implicit: after the pause the next beat IS the opening narrative, and it carries the
# illustration. An empty turn there otherwise reads as valid. A runtime message, so it
# costs no prefix tokens.
AWAKENING_RESUME = (
    "{{_CONTINUE_EXECUTION}} — resume at AWAKENING step 3: the opening NARRATIVE, in the "
    "same response as exactly ONE `request_scene_image`."
)

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
            "parameters": compact_schema(params),
        },
    }


def _safe_json(text: str) -> dict:
    """A tool result parsed to a dict, or wrapped so a dev result is never lost."""
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"raw": text}
    return value if isinstance(value, dict) else {"raw": value}


SCENE_TOOL = "request_scene_image"
# Declares recurring NPCs (name + description). The engine buffers them and the
# server persists them; the GM then refers to those NPCs by name alone.
NPC_TOOL = "register_npcs"
# The text-only place/NPC registry (images off): declares a place and its regulars by
# name + role with no image. Hidden when images are on (the scene call records places there).
PLACE_TOOL = "note_place"
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
# The text-only place rules (`note_place`); sent only when images are OFF. The mirror
# of the SCENE block: whichever game is not being played costs nothing.
_TEXT_BLOCK = re.compile(
    r"(?ms)^[ \t]*<!-- TEXT:ON -->.*?^[ \t]*<!-- TEXT:END -->[ \t]*\n?"
)
_TEXT_MARKERS = re.compile(r"(?m)^[ \t]*<!-- TEXT:(?:ON|END) -->[ \t]*\n?")
# Easy-mode balancing rules live between these markers; they are sent to the GM
# only when the character was created at "easy" difficulty (see `.player`).
_EASY_BLOCK = re.compile(
    r"(?ms)^[ \t]*<!-- EASY:ON -->.*?^[ \t]*<!-- EASY:END -->[ \t]*\n?"
)
_EASY_MARKERS = re.compile(r"(?m)^[ \t]*<!-- EASY:(?:ON|END) -->[ \t]*\n?")
# The KNOWN PLACES awakening priming: a header + the place tree (names + main NPCs,
# never the descriptions). Shared with tools/token_budget.py so the count never drifts.
# Kept terse on purpose: this block is re-sent every turn, and the whole tree is capped.
KNOWN_PLACES_HEADER = (
    "KNOWN PLACES (reuse these exact kingdom / area / place paths; use a "
    "place's NPCs by NAME, never their look; a new place is seeded once with "
    "`establishing`):"
)
# The images-off variant: the same tree, but a new place is declared with `note_place`
# (no seed, no establishing description).
KNOWN_PLACES_HEADER_TEXT = (
    "KNOWN PLACES (reuse these exact kingdom / area / place paths; use a "
    "place's NPCs by NAME, never their look; a new place is declared once with "
    "`note_place`):"
)

# The primed list is scoped to the current era and capped: the tree is
# a reminder, not the record. Past the cap the GM still has `lookup` and the manifest.
PLACE_TREE_CAP = 12
# ... and a place's NPC names are capped too, so one busy place cannot blow the ceiling.
NPC_NAMES_PER_PLACE = 2
KNOWN_PLACES_EMPTY = (
    "KNOWN PLACES: none recorded yet. On entering a place, declare "
    "its `kingdom`, `area` and `place` path and request its seed with "
    "`establishing` (a short, people-free description) together with the "
    "action image."
)
KNOWN_PLACES_EMPTY_TEXT = (
    "KNOWN PLACES: none recorded yet. On entering a place, declare its "
    "`kingdom`, `area` and full `place` path once with `note_place`."
)


def _scene_key(place) -> tuple[str, ...]:
    """Slug path identifying a place (deepest segment last)."""
    def _s(text) -> str:
        return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    if isinstance(place, str):
        place = [place]
    if not isinstance(place, (list, tuple)):
        return ()
    return tuple(_s(p) for p in place if str(p or "").strip())


def _people_list(value) -> list[dict]:
    """Normalize a `note_place` people list to the seed shape ({name, role}, empty look)."""
    out: list[dict] = []
    for entry in value or []:
        if not isinstance(entry, dict):
            continue
        name = " ".join(str(entry.get("name") or "").split())
        if name:
            out.append({"name": name, "description": "",
                        "role": " ".join(str(entry.get("role") or "").split())})
    return out


def _primed_places(places: list[dict], cap: int = PLACE_TREE_CAP) -> list[dict]:
    """The places the priming shows: the `cap` most recently used, back in tree order.

    The primed tree is a *reminder*, so it is bounded; the record is the
    manifest, and `_match_known_place` deliberately keeps reading the whole list.
    """
    def _order(p: dict):
        return (p["kingdom"].lower(), p["area"].lower(), [s.lower() for s in p["place"]])

    recent = sorted(places, key=lambda p: (-int(p.get("used") or 0), *_order(p)))
    return sorted(recent[:cap], key=_order)


def format_known_places(places: list[dict], cap: int = NPC_NAMES_PER_PLACE) -> str:
    """A kingdom -> area -> place-path tree for the awakening priming (any depth).

    Names + at most `cap` main NPC names per place: the full establishing description is
    deliberately omitted (it lives in the manifest, which the image pipeline reads), and
    the NPC *roles* are dropped -- a role parenthetical costs ~3.5 tokens and cannot fit a
    12-place tree inside the budget. A place with more names than `cap` says so
    with `+N`, so the GM knows the place is not empty."""
    lines: list[str] = []
    last_k = last_a = None
    last_path: list[str] = []
    for p in places:
        path = [str(s).strip() for s in (p.get("place") or []) if str(s or "").strip()]
        if not path:
            continue
        if p["kingdom"] != last_k:
            lines.append(f"- {p['kingdom'] or '(unknown kingdom)'}")
            last_k, last_a, last_path = p["kingdom"], None, []
        if p["area"] != last_a:
            lines.append(f"    - {p['area'] or '(unknown settlement)'}")
            last_a, last_path = p["area"], []
        shared = 0
        while (shared < len(path) and shared < len(last_path)
               and path[shared].lower() == last_path[shared].lower()):
            shared += 1
        for i in range(shared, len(path)):
            lines.append("    " * (2 + i) + f"- {path[i]}")
        last_path = list(path)
        npc_labels: list[str] = []
        names = [str(npc.get("name") or "").strip() for npc in (p.get("main_npcs") or [])
                 if isinstance(npc, dict)]
        names = [n for n in names if n]
        npc_labels.extend(names[:cap])
        if len(names) > cap:
            npc_labels.append(f"+{len(names) - cap}")
        if npc_labels:
            label = "NPC" if len(npc_labels) == 1 else "NPCs"
            lines[-1] += f" · {label}: " + ", ".join(npc_labels)
    return "\n".join(lines)


# ── The two games ─────────────────────────────────────────────────────────
#
# CLASSIC is the invented world of `config/world.yml`: a fixed scaffold of kingdoms,
# fetched at awakening, and no eras at all. TIME_TRAVELER is the era ladder, the Device
# and the legend. A save says which it is in its own `mode`; a save that carries an `era`
# was made by the era game, and anything older than that is classic.
MODE_CLASSIC = "classic"
MODE_TIME_TRAVELER = "time_traveler"
MODES = (MODE_CLASSIC, MODE_TIME_TRAVELER)

# The mode blocks: `<!-- TT:ON -->` / `<!-- CLASSIC:ON -->` around the rules that only
# one of the two games uses. Exactly one of the pair survives `render_protocol`.
_TT_BLOCK = re.compile(r"(?ms)^[ \t]*<!-- TT:ON -->.*?^[ \t]*<!-- TT:END -->[ \t]*\n?")
_TT_MARKERS = re.compile(r"(?m)^[ \t]*<!-- TT:(?:ON|END) -->[ \t]*\n?")
_CLASSIC_BLOCK = re.compile(
    r"(?ms)^[ \t]*<!-- CLASSIC:ON -->.*?^[ \t]*<!-- CLASSIC:END -->[ \t]*\n?")
_CLASSIC_MARKERS = re.compile(r"(?m)^[ \t]*<!-- CLASSIC:(?:ON|END) -->[ \t]*\n?")


def render_protocol(text: str, scene_images: bool, difficulty: str = "hard",
                    mode: str = MODE_TIME_TRAVELER) -> str:
    """Strip the marker blocks and the markers themselves; drop the rules that are off.

    The mode is resolved per session, so the game that is not being played costs nothing:
    its block is removed before the prompt is built.
    """
    out = text or ""
    if scene_images:
        out = _SCENE_MARKERS.sub("", out)
        out = _TEXT_BLOCK.sub("", out)
    else:
        out = _SCENE_BLOCK.sub("", out)
        out = _TEXT_MARKERS.sub("", out)
    if str(difficulty or "").strip().lower() == "easy":
        out = _EASY_MARKERS.sub("", out)
    else:
        out = _EASY_BLOCK.sub("", out)
    if str(mode or "").strip().lower() == MODE_CLASSIC:
        out = _TT_BLOCK.sub("", out)
        out = _CLASSIC_MARKERS.sub("", out)
    else:
        out = _TT_MARKERS.sub("", out)
        out = _CLASSIC_BLOCK.sub("", out)
    return out


def _player_difficulty(player_path: str) -> str:
    """Read the character's difficulty from the `.player` JSON; default "hard"."""
    try:
        with open(player_path, "r", encoding="utf-8") as f:
            value = str((json.load(f) or {}).get("difficulty", "") or "").strip().lower()
        return value if value in ("hard", "easy") else "hard"
    except (OSError, ValueError, TypeError):
        return "hard"


def _player_mode(player_path: str) -> str:
    """Which game this save is playing (see the modes above).

    An explicit `mode` wins. Otherwise a save with an `era` was made by the era game,
    and one without is classic -- so every save that predates the eras keeps the game it
    was created for, with nothing to migrate.
    """
    try:
        with open(player_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError, TypeError):
        return MODE_CLASSIC
    value = str(data.get("mode", "") or "").strip().lower()
    if value in MODES:
        return value
    return MODE_TIME_TRAVELER if str(data.get("era", "") or "").strip() else MODE_CLASSIC


def _player_era(player_path: str) -> str:
    """Read the character's current era from the `.player` JSON; default START_ERA."""
    try:
        with open(player_path, "r", encoding="utf-8") as f:
            value = str((json.load(f) or {}).get("era", "") or "").strip().lower()
    except (OSError, ValueError, TypeError):
        return START_ERA
    return value if value in era_ids() else START_ERA


def _arrival_candidates(places) -> list[str]:
    """Arrival phrases for a set of seeded places: the first place entry, in its settlement.

    The deepest entry (a room) reads oddly as an arrival, so the district/neighbourhood
    stands for the place, qualified by the settlement it sits in.
    """
    out: list[str] = []
    seen: set[str] = set()
    for p in places or []:
        place = [str(s).strip() for s in (p.get("place") or []) if str(s or "").strip()]
        area = str(p.get("area") or "").strip()
        if place and area:
            phrase = f"{place[0]}, in {area}"
        elif place:
            phrase = place[0]
        elif area:
            phrase = area
        else:
            continue
        key = phrase.lower()
        if key not in seen:
            seen.add(key)
            out.append(phrase)
    return out


def _visited_arrival_candidates(player_path: str, era: str) -> list[str]:
    """The arrival phrases for the places a save has already seen in an era (disk only)."""
    try:
        stem = os.path.splitext(os.path.basename(player_path))[0]
        output_dir = os.path.dirname(player_path) or "."
        return _arrival_candidates(known_scene_places(output_dir, stem, era))
    except Exception:  # noqa: BLE001 - never fail a session over an arrival point
        return []


def _player_arrival(player_path: str, era: str) -> str:
    """The saved arrival point in `era`, or a fresh one.

    A visited-place arrival is engine-written and is not in the era's static list, so it
    has to be validated against the places the save has actually seen too -- otherwise a
    reload would discard it and re-roll a static phrase.
    """
    try:
        from web.eras import era_arrivals, pick_arrival

        options = era_arrivals(era)
    except Exception:  # noqa: BLE001 - never fail a session over the arrival point
        options = []
    try:
        with open(player_path, "r", encoding="utf-8") as f:
            value = str((json.load(f) or {}).get("arrival", "") or "").strip()
    except (OSError, ValueError, TypeError):
        value = ""
    if value and (value in options or value in _visited_arrival_candidates(player_path, era)):
        return value
    if options:
        return pick_arrival(era)
    return value


def _split_journey(value) -> list[str]:
    """The eras a save has stood in, oldest first (stored as a comma-joined string)."""
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    return [p.strip().lower() for p in str(value or "").split(",") if p.strip()]


# Tools the GM never sees: called directly by the session for its own bookkeeping, filtered
# out of the GM's tool list (and out of the measured prefix -- see tools/token_budget.py).
# The `debug_*` tools are the developer's levers (dev mode only); the GM is never offered
# them, and they no-op unless the dice server was started with `--debug`.
ENGINE_ONLY_TOOLS = ("set_player_field", "dump_player_save_state",
                     "debug_set_field", "debug_device")

# The one system message a dev Apply leaves for the GM, so the next turn narrates against
# the state the developer just set instead of a stale sheet.
DEV_NOTE_HEADER = (
    "DEBUG STATE UPDATE -- the developer changed state outside the story. These values are "
    "canonical now; do not contradict them or narrate the change as a story event:"
)
# How many bytes of GM-prompt snapshots the inspector journal will hold before it stops
# appending (a safety valve; dev mode only). The first entries are kept, and `capped` is
# reported so the panel can say the history is partial.
GM_JOURNAL_MAX_BYTES = 20_000_000

# The Device's cadence, keyed by how many of its four parts are back: each part widens the
# interval AND unlocks an ability (see `_device_state`). Band 4 is absent on purpose -- a
# complete Device does not fire on its own; the Traveller decides where and when.
CADENCE_BANDS = {0: (5, 5), 1: (7, 7), 2: (11, 11), 3: (13, 13)}
# How many turns before the jump the Device starts ticking: the turn it fires on is the one
# after this many turns, so 1 means the warning lands on the LAST turn of the age and the GM
# has that turn to close the age out -- no earlier.
WARNING_TURNS = 1

DEVICE_WARNING = (
    "The Device is ticking. This is the last turn of this age: bring it to a close now, "
    "because the jump comes next. Nobody else can hear it."
)

# The scope of an age's memory. The legend is what the era actually knows -- the Traveller
# may arrive unseen, so a look, a name or a piece of kit is only remembered when a witness
# survived to carry it; what happened in the open, and what was left behind, always is.
# The GM REWRITES the age's whole memory on each departure (keep what still matters, add what
# this leaving means); it may write a few sentences, up to roughly the cap below.
_MEMORY_SCOPE = (
    "one or a few sentences, up to about 1200 characters -- what this age now remembers about "
    "the Traveller. Rewrite the age's whole memory: keep what still matters and add what this "
    "leaving means. Only what was seen to happen or what was left behind; never the Traveller's "
    "look, name or kit unless a witness lived to speak of it"
)

# An era's memory is one growing paragraph; bound it so the always-on memory block stays cheap.
MAX_LEGEND_CHARS = 1200
# The extractor accepts more than the cap, so a slightly-over rewrite is trimmed, not rejected.
MAX_LEGEND_EXTRACT = 2000

# Asked on the closing turn: the Game Master writes what the age will remember, in a marker
# the client hides and the engine reads. This is the era-switch compaction output -- the GM
# rewrites the age's whole memory; there is no engine-authored fallback.
LEGEND_ASK = (
    "Close this age. End the narrative of THIS turn with "
    "{{_REMEMBERS: <" + _MEMORY_SCOPE + ">}} and nothing after it."
)

# A player-driven jump has no warning turn, so the engine runs one closing turn first.
DEVICE_CLOSES = (
    "The Traveller turns the Device toward {name}. Narrate the last moment of this age -- "
    "the world letting go -- and end with {{{{_REMEMBERS: <" + _MEMORY_SCOPE + ">}}}} and "
    "nothing after it."
)

LEGEND_RECOVERY_NUDGE = (
    "SYSTEM: this age's memory line is missing. Reply with ONLY the marker "
    "{{_REMEMBERS: <" + _MEMORY_SCOPE + ">}} -- no prose, no tool calls, nothing else."
)

DEVICE_FIRES = (
    "The Device fires. You are thrown out of {old} and into {new}.\n\n"
    "You arrive at {arrival}.\n\n"
    "You still have everything you carried, exactly as it is: nothing is translated, "
    "nothing is left behind. This age has never seen its like, and people will notice.\n\n"
    "Narrate the arrival -- the disorientation, what the place smells and sounds like at "
    "this hour, and whether anyone notices a stranger appear."
)

# The Traveller keeps the whole kit, and it stays LITERAL in every era. No
# translation, no re-skinning -- a Victorian pistol is a pistol in 2560 BC, and the era
# reacts to it. The world still introduces only era-plausible gear of its own.
CARRY_NOTE = (
    "Your gear is your own: whatever you carry you still have, and this age has never "
    "seen its like. Shoot, load and reload as the rules say -- a spent round is a consumable."
)


# `lookup` fetches another era's scaffold. There are no other eras in a classic game,
# so the tool is never offered one -- and its schema is never paid for either.
LOOKUP_TOOL = "lookup"


def filter_tools(tools: list[dict], scene_images: bool,
                 mode: str = MODE_TIME_TRAVELER) -> list[dict]:
    """What the GM is offered: hide the tools this game cannot use, and always hide the
    session's own bookkeeping tools."""
    hidden = set(ENGINE_ONLY_TOOLS)
    if not scene_images:
        hidden |= {SCENE_TOOL, NPC_TOOL}
    else:
        hidden.add(PLACE_TOOL)
    if str(mode or "").strip().lower() == MODE_CLASSIC:
        hidden |= {LOOKUP_TOOL}
    return [t for t in tools
            if (t.get("function") or {}).get("name") not in hidden]


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
    "note_place": ("status", "kingdom", "area", "place", "main_npcs", "cast", "note"),
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
    `carrying` breakdown) are always dropped — the GM already holds the sheet. The
    creation-time `difficulty`, `era` and `arrival` are dropped too: the GM learns
    the mode only from the EASY protocol block, the era only from the ERA INDEX, and
    the arrival only from the line the engine prepends to the ERA_FILE."""
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
    view.pop("difficulty", None)
    view.pop("era", None)
    view.pop("arrival", None)
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


# The GM's era memory, written on the closing turn: `{{_REMEMBERS: <one sentence>}}`. The
# client hides it while streaming; the engine keeps only the sentence.
_REMEMBERS_RE = re.compile(r"\{\{_REMEMBERS:\s*(.*?)\s*\}\}", re.DOTALL)


def _extract_legend(text: str) -> tuple[str, str]:
    """Pull the `{{_REMEMBERS: ...}}` marker out of an assistant message.

    Returns ``(legend, cleaned_text)``. A legend that merely echoes the placeholder, or is
    absurdly short or long, is rejected -- the template fallback is better than a broken
    memory line. A message without the marker is returned untouched.
    """
    raw = text or ""
    match = _REMEMBERS_RE.search(raw)
    if not match:
        return "", raw
    legend = " ".join(match.group(1).split())
    cleaned = _REMEMBERS_RE.sub("", raw).strip()
    if "<one sentence" in legend.lower() or legend.startswith("<") or "<" in legend:
        return "", cleaned
    if not (15 <= len(legend) <= MAX_LEGEND_EXTRACT):
        return "", cleaned
    return legend, cleaned


def _trim_legend(text: str, limit: int = MAX_LEGEND_CHARS) -> str:
    """Bound an era's memory to `limit` characters, dropping the OLDEST sentences.

    The GM rewrites the whole memory; on overflow the earliest events are dropped, at a
    sentence boundary, so the most recent memory survives intact."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    kept = ""
    for sentence in reversed(re.split(r"(?<=[.!?])\s+", text)):
        candidate = f"{sentence} {kept}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    return kept or text[-limit:].lstrip()


class GameSession:
    """One in-memory, long-lived game session (single user, local server)."""

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
        # Known seeded places: (location_slug, sublocation_slug) -> (kingdom, area). A new
        # place must supply `establishing` before its seed is accepted.
        self._scene_places: dict[tuple[str, str], tuple[str, str]] = {}
        self._scene_rejected = False
        self._scene_rejected_place: list[str] | None = None
        # Declared NPC names (place main NPCs + the storyline cast), lowercased, and the
        # declarations buffered this turn which the server persists on the next scene call.
        self._cast_names: set[str] = set()
        # Static combatant sheets (name -> rendered lines) from register_combatants, merged
        # with the live roster and sent to the client for the combatant tooltips.
        self._combat_sheets: dict[str, list[str]] = {}
        self._combat_order: list[str] = []
        self._combat_initiative: dict[str, dict] = {}
        # True while the registry holds a hostile, living, active non-player (derived from
        # every combat tool's `registry_summary`). The Device does not count such turns, and
        # a save is refused while it holds -- an unfinished fight would snapshot transient
        # HP/conditions with no roster, and the timeline would not survive the reload.
        self._in_combat = False
        # The engine-composed Mechanics block for the current turn: each tool result's
        # narrative_format, in call order, sent to the client as the `mechanics` event.
        self._mechanics_lines: list[str] = []
        self._pending_npcs: list[dict] = []
        # ── the Device's cadence ──────────────────────────────
        # The Device and its parts are real inventory items (engine-seeded at creation); the
        # count is derived from the present parts, so removing one re-locks the ability above
        # it. `has_device` False means the Device itself is gone and the cadence is off.
        self.has_device = False
        self.parts_recovered = 0
        # The eras the Traveller has stood in, oldest first: "previous" means the one before
        # the current, which the timeline legends cannot give (a return does not rewrite one).
        self.journey: list[str] = []
        # One adjustment per jump (wait +2 or hasten -2); reset when the jump fires.
        self._adjusted_jump = False
        # Pending steering for the next automatic jump: "" (drift), "previous" or
        # "forward". Set at 2-3 parts, consumed and cleared by the next jump.
        self._steer: str = ""
        # The GM's memory line for the age it is leaving, captured from `{{_REMEMBERS: ...}}`
        # on the closing turn; `_jump` prefers it over the config template and clears it.
        self._pending_legend: str = ""
        # Injected in tests so the "random" era and band are reproducible.
        self._rng = random.Random()
        self._jump_at_turn: int | None = None
        self._warned = False
        # Where the always-on era messages live, so a jump can rewrite them in place
        # instead of appending one index (and one tree) per age ever visited.
        self._era_index_at: int | None = None
        self._places_at: int | None = None
        self._primed: list[dict] = []
        # The scene caption's place: the last declared/used place (age and place are the only
        # scene fields the engine knows; time and weather are dropped in text mode).
        self._current_place: dict | None = None
        # ── what the ages remember ──────────────────────────────────────
        # One or two lines per era, read from the save's timeline and injected lazily: the
        # eras the Traveller is not in cost nothing.
        self._legends: dict[str, str] = {}
        self._era_started_turn = 0
        # Declared NPC roles, by lowercased name -> (canonical name, role). Roles are echoed
        # with the character that appears, never primed: they are needed on the turn an NPC
        # walks on stage, not on every turn.
        self._npc_roles: dict[str, tuple[str, str]] = {}

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
        # never from a working copy a previous unsaved session left behind.
        discard_scene_manifest(os.path.dirname(self.player_path), self.active_name)
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

    async def submit_device(self, action: str, era: str = "", direction: str = "",
                            delta=None) -> None:
        await self._cmd_q.put({"type": "device", "action": action,
                               "era": era, "direction": direction, "delta": delta})

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
                    if self.classic:
                        # The classic game hands the GM the world it was built on.
                        key_content = render_world_text()
                    else:
                        key_content = f"{self._era_opening()}\n\n{render_era_text(self.era)}"
                    existing_timeline = load_timeline(self.timeline_path)
                    # Continue turn numbering where the loaded timeline left off.
                    self.turn_counter = self.last_timeline_turn = _max_timeline_turn(existing_timeline)

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
                    if existing_timeline:
                        self.messages.append({
                            "role": "system",
                            "content": (
                                "SESSION_TIMELINE — these are events that happened earlier this session.\n"
                                "Refer to them when the player asks about past events. Do not replay or "
                                "re-describe them.\n\n" + existing_timeline
                            ),
                        })

                    # The place/NPC tree is primed in BOTH modes: with images on it comes
                    # from the scene manifest, with images off from the text registry.
                    self._reload_era_scene_state()
                    self._places_at = len(self.messages)
                    self.messages.append({"role": "system", "content": self._places_body()})

                    # What the ages remember: this era's legend, if it has one. The ages
                    # exist only in the era game, so a classic session never hears them.
                    self._legends = {} if self.classic else load_legends(self.timeline_path)
                    memory = "" if self.classic else self._memory_message("")
                    if memory:
                        self.messages.append({"role": "system", "content": memory})
                    self._era_started_turn = self.turn_counter

                    # The Device: how many parts are in the pack, and the eras visited, from
                    # the live DB. Nothing is armed until this has been read.
                    await self._refresh_device()

                    # Arm the Device for this session.
                    span = self._cadence_span()
                    self._jump_at_turn = None if span is None else self.turn_counter + span
                    self._warned = False

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

                    # ── Awakening: ERA_FILE -> tools -> pause token -> resume -> opening scene ──
                    # (The opening illustration is requested by the GM itself, in
                    # the awakening tool batch — see GameMaster_MCP.md.)
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
                text = await self._run_role("{{_CONTINUE_EXECUTION}}", "resume")
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
        self._mechanics_lines = []          # the engine-composed block is per turn
        try:
            result = await self._run_role(content, label)
            result = await self._ensure_turn_prose(result)
            await self._ensure_scene_image(result or "")
            self.turn_counter += 1
            await self._emit({"type": "turn_end", "text": result or "", "turn": self.turn_counter})
        finally:
            await self._emit({"type": "busy", "value": False})

    async def _handle_action(self, text: str) -> None:
        await self._run_turn(text, "turn")
        await self._check_cadence()
        # After the Device has had its say: one cadence event per turn, carrying the truth.
        await self._emit({"type": "cadence", **self._device_state()})

    async def _handle_device_command(self, cmd: dict) -> None:
        """A player-driven Device action (adjust the count / choose a destination)."""
        action = str(cmd.get("action") or "").strip().lower()
        if self.classic or not self.has_device:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "There is no Device to command."})
            return
        if action == "adjust":
            try:
                delta = int(cmd.get("delta"))
            except (TypeError, ValueError):
                delta = 0
            await self._device_adjust(delta)
        elif action == "travel":
            await self._device_travel(cmd)
        elif action == "steer":
            await self._device_steer(cmd.get("direction"))
        else:
            await self._emit({"type": "error", "message": f"unknown Device action: {action}"})

    async def _device_adjust(self, delta: int) -> None:
        """Move the countdown by two turns (1+ parts, once per jump).

        `+2` waits. `-2` hurries -- but only with three or more turns left: hurrying the
        last two would be too much control over the Device, so it is refused rather than
        firing a jump with no notice. Either direction spends the one adjustment per jump.
        The warning is re-evaluated afterwards -- waiting can withdraw one, hurrying can
        raise it.
        """
        if (self.parts_recovered < 1 or self._adjusted_jump
                or self._jump_at_turn is None):
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "The Device will not answer again this age."})
            return
        step = device.adjust_turns()
        if delta not in (step, -step):
            await self._emit({"type": "error",
                              "message": f"invalid Device adjustment: {delta}"})
            return
        remaining = self._jump_at_turn - self.turn_counter
        if delta < 0 and remaining < 3:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "The Device will not be hurried so near the end."})
            return
        self._jump_at_turn += delta
        self._adjusted_jump = True
        # The jump moved, so any "last turn of this age" the GM holds may now be false.
        self._warned = False
        self.messages = [m for m in self.messages
                         if not (isinstance(m, dict) and m.get("content") == DEVICE_WARNING)]
        if delta > 0:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": f"The Device is coaxed into waiting — {step} more turns."})
        else:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": f"The Device is hurried onward — {step} turns sooner."})
        # Hurrying from three leaves one turn, so the warning is raised right here.
        await self._check_cadence()
        await self._emit({"type": "cadence", **self._device_state()})

    async def _device_travel(self, cmd: dict) -> None:
        """Travel now to a chosen age, as far as the recovered parts allow.

        A deliberate jump is refused in combat: a fight is never interrupted, and the
        automatic jump already holds for its length. Direction is not a jump -- it steers
        the NEXT automatic one (`_device_steer`), so a direction message never lands here.
        """
        if self._in_combat:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "The Device will not answer in the middle of a fight."})
            return
        parts = self.parts_recovered
        era = str(cmd.get("era") or "").strip().lower()
        if parts >= device.part_total():
            # Complete: any age but this one, and the player chose to go now.
            if era not in self._all_other_eras():
                await self._device_refuse()
                return
        elif parts >= 3:
            # Choose which age ahead of the current one (no wrapping).
            if era not in self._forward_era_ids():
                await self._device_refuse()
                return
        else:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "The Device will not take you anywhere yet."})
            return
        await self._close_and_jump(era)

    async def _device_steer(self, direction) -> None:
        """Steer the NEXT automatic jump: "" (let it drift), "previous" or "forward".

        Unlocked at 2-3 parts. Not a jump, so it is allowed in combat -- the jump it steers
        will not fire until the fight is over. Sending the active direction toggles it off.
        """
        parts = self.parts_recovered
        if parts >= device.part_total():
            # A complete Device never fires on its own, so there is nothing to steer.
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "The Device no longer drifts — choose where to go."})
            return
        if parts < 2:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "The Device cannot be steered yet."})
            return
        want = str(direction or "").strip().lower()
        if want not in ("", "previous", "forward"):
            await self._emit({"type": "error",
                              "message": f"invalid Device steering: {direction}"})
            return
        if want == self._steer:
            want = ""  # toggle the active direction off
        if want == "previous" and not self._previous_era():
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "There is no earlier age to point back to yet."})
            return
        if want == "forward" and not self._forward_era_ids():
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "There is no age ahead to point to."})
            return
        self._steer = want
        if want == "previous":
            text = (f"The Device leans back toward "
                    f"{device.era_label(self._previous_era())}.")
        elif want == "forward":
            text = "The Device leans forward — where it lands is still chance."
        else:
            text = "The Device drifts again — the next jump goes where it likes."
        await self._emit({"type": "notice", "title": "The Device", "text": text})
        await self._emit({"type": "cadence", **self._device_state()})

    async def _device_refuse(self) -> None:
        await self._emit({"type": "notice", "title": "The Device",
                          "text": "The Device cannot reach there."})

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
            elif kind == "cadence":
                self._dev_set_cadence(raw.get("turns"), raw.get("steer"))
                results.append({"kind": "cadence", "turns": raw.get("turns"),
                                "steer": self._steer})
                remaining = self._cadence_state().get("turns_until")
                notes.append(f"Device countdown set to {remaining} turns")
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

    def _dev_set_cadence(self, turns, steer=None) -> None:
        """Set the Device countdown (absolute turns from now) and the pending steering."""
        if turns is None or (isinstance(turns, str) and not turns.strip()):
            self._jump_at_turn = None
        else:
            try:
                self._jump_at_turn = self.turn_counter + max(0, int(turns))
            except (TypeError, ValueError):
                self._jump_at_turn = None
        if steer is not None:
            want = str(steer or "").strip().lower()
            self._steer = want if want in ("previous", "forward") else ""
        self._warned = False
        self._adjusted_jump = False

    async def _dev_force_jump(self, era: str) -> None:
        """The developer's jump: any other age, now, even in combat."""
        if self.classic:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "There is no Device in a classic game."})
            return
        await self._jump(to_era=era or None)

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

    def _gm_snapshot(self, label: str, round=None, trigger=None) -> dict:
        self._gm_seq += 1
        messages = copy.deepcopy(self.messages)
        message_tokens = self._estimate_tokens(messages)
        tool_tokens = int(getattr(self, "_tool_tokens", 0) or 0)
        return {"seq": self._gm_seq, "label": label, "turn": self.turn_counter,
                "ts": time.time(), "round": round, "trigger": trigger,
                "message_tokens": message_tokens, "tool_tokens": tool_tokens,
                "tokens": message_tokens + tool_tokens, "messages": messages}

    async def _record_gm_prompt(self, label: str, round=None, trigger=None):
        """Record exactly what the GM is about to be sent (dev only), and stream it live.

        Returns the snapshot so the caller can stamp the provider's real token count once
        the reply lands; `None` when dev mode is off."""
        if not self.dev:
            return None
        snap = self._gm_snapshot(label, round=round, trigger=trigger)
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

    # ── the Device: the cadence, and the jump itself ─────────────

    def _cadence_span(self) -> int | None:
        """Turns until the next jump, or None when the Device does not fire by itself.

        A classic game has no Device, and a Traveller whose Device has been taken has none
        either, so the cadence is off. A complete Device (band 4 is absent) is manual too:
        the Traveller chooses where and when.
        """
        if self.classic or not self.has_device:
            return None
        band = CADENCE_BANDS.get(self.parts_recovered)
        return None if band is None else self._rng.randint(band[0], band[1])

    def _cadence_state(self) -> dict:
        """What the counter shows the player and what warns the GM."""
        if self._jump_at_turn is None:
            return {"parts": self.parts_recovered, "turns_until": None, "warning": False}
        remaining = max(0, self._jump_at_turn - self.turn_counter)
        return {"parts": self.parts_recovered, "turns_until": remaining,
                "warning": remaining <= WARNING_TURNS}

    def _all_other_eras(self) -> list[str]:
        """Every playable era but the one the Traveller is standing in."""
        return [e for e in playable_eras() if e != self.era]

    def _forward_era_ids(self) -> list[str]:
        """The eras strictly ahead of the current one in the ladder (no wrapping).

        The ages are a linear historical run, so the last age has nothing ahead of it --
        the Traveller goes back with "previous", not by wrapping to the first age.
        """
        order = playable_eras()
        if not order or self.era not in order:
            return [e for e in order if e != self.era]
        index = order.index(self.era)
        return order[index + 1:]

    def _previous_era(self) -> str | None:
        """The last era the Traveller visited -- the one before the current in the journey."""
        if len(self.journey) >= 2:
            return self.journey[-2]
        return None

    def _steered_era(self) -> str:
        """Where an automatic jump lands, honouring the pending steering.

        "" is the Device's own drift (any age but this one), "previous" the last age visited,
        "forward" a random age strictly ahead. A steer with no valid target falls back to
        the drift, so a jump never fails.
        """
        steer = str(self._steer or "").strip().lower()
        if steer == "previous":
            prev = self._previous_era()
            if prev and prev != self.era:
                return prev
        elif steer == "forward":
            options = self._forward_era_ids()
            if options:
                return self._rng.choice(options)
        return self._pick_era()

    def _era_options(self, eras: list[str]) -> list[dict]:
        return [{"id": e, "name": device.era_label(e)} for e in eras]

    def _device_state(self) -> dict:
        """The cadence plus everything the Device panel needs to draw its controls."""
        parts = self.parts_recovered
        total = device.part_total()
        live = (not self.classic) and self.has_device
        manual = live and parts >= total
        abilities = {
            "adjust": live and parts >= 1,
            "direction": live and 2 <= parts < total,
            "choose_forward": live and parts >= 3,
            "full": manual,
        }
        state = self._cadence_state()
        remaining = state.get("turns_until")
        state.update({
            "total": total,
            "manual": manual,
            "in_combat": self._in_combat,
            "steer": self._steer,
            "abilities": abilities,
            "adjust": {
                "turns": device.adjust_turns(),
                "used": self._adjusted_jump,
                "can_wait": abilities["adjust"] and not self._adjusted_jump,
                "can_hasten": (abilities["adjust"] and not self._adjusted_jump
                               and remaining is not None and remaining >= 3),
            },
            "previous_era": self._previous_era(),
            "previous_era_name": device.era_label(self._previous_era()) if self._previous_era() else "",
            "forward_eras": self._era_options(self._forward_era_ids()),
            "all_eras": self._era_options(self._all_other_eras()),
        })
        return state

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

    def _pick_era(self) -> str:
        """Where the Device throws the Traveller: any playable era but this one.

        The Compass Rose is missing, so it does not aim."""
        options = [e for e in playable_eras() if e != self.era]
        return self._rng.choice(options) if options else self.era

    def _pick_jump_arrival(self, era: str) -> str:
        """Where a jump lands: one place from the era's pool, drawn uniformly.

        The pool is the deduped union of the era's authored destinations (cities,
        landmarks, regions) and the places this save has visited -- a visited place is one
        entry like any other, with no preference for recency or repeat visits. The list
        never enters the prompt, so a larger pool costs nothing.
        """
        try:
            places = known_scene_places(self.base_dir / OUTPUT_DIR, self.active_name or "", era)
        except Exception:  # noqa: BLE001 - never fail a jump over the arrival point
            places = []
        candidates: list[str] = []
        seen: set[str] = set()
        for phrase in _arrival_candidates(places) + era_arrivals(era):
            key = phrase.lower()
            if key and key not in seen:
                seen.add(key)
                candidates.append(phrase)
        if candidates:
            return self._rng.choice(candidates)
        return pick_arrival(era, rng=self._rng)

    def _reload_era_scene_state(self) -> None:
        """Re-read the places, cast and roles for the era the Traveller is now in."""
        try:
            places = known_scene_places(self.base_dir / OUTPUT_DIR, self.active_name or "",
                                        None if self.classic else self.era)
        except Exception:  # noqa: BLE001 - never fail a session over the place tree
            places = []
        self._primed = places
        self._scene_places = {_scene_key(p["place"]): (p["kingdom"], p["area"]) for p in places}
        try:
            self._current_place = current_scene_place(
                self.base_dir / OUTPUT_DIR, self.active_name or "")
        except Exception:  # noqa: BLE001 - never fail a session over the caption
            self._current_place = None
        try:
            self._cast_names = known_npc_names(self.base_dir / OUTPUT_DIR, self.active_name or "")
        except Exception:  # noqa: BLE001
            self._cast_names = set()
        # Roles are learned from the era's places so the on-stage echo works from the
        # first tool call on. They are never primed.
        self._npc_roles = {}
        for p in places:
            self._remember_roles(p.get("main_npcs"))

    def _caption_text(self) -> str:
        """The scene caption from what the engine knows: the age (era game only) and the
        current place, falling back to the rolled arrival when no place is declared yet (e.g.
        just after a jump). Never time of day or weather -- text mode has no scene tool to carry
        them, so they are dropped."""
        parts: list[str] = []
        if not self.classic and self.era:
            parts.append(str(device.era_label(self.era) or "").strip())
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
                    parts.append(area)
                if place:
                    parts.append(place)
                placed = bool(area or place)
        if not placed:
            arrival = str(self.arrival or "").strip()
            if arrival:
                parts.append(arrival)
        return " · ".join(p for p in parts if p)

    async def _emit_caption(self) -> None:
        """Tell the client the current scene caption (the text-mode Game Master header)."""
        await self._emit({"type": "caption", "text": self._caption_text(),
                          "era": "" if self.classic else str(self.era or ""),
                          "era_name": "" if self.classic else device.era_label(self.era)})

    def _places_body(self) -> str:
        primed = _primed_places(self._primed)
        if self.scene_images:
            return (KNOWN_PLACES_EMPTY if not primed
                    else KNOWN_PLACES_HEADER + "\n" + format_known_places(primed))
        return (KNOWN_PLACES_EMPTY_TEXT if not primed
                else KNOWN_PLACES_HEADER_TEXT + "\n" + format_known_places(primed))

    async def _check_cadence(self) -> None:
        """After a turn: warn once before the jump, then jump when it is due.

        A turn spent fighting is not a turn the Device counts: it pushes the jump one turn
        further out, so the counter holds for the length of a fight and the jump can never
        interrupt one. `_in_combat` is sticky (a whole fight, not only the turns a combat tool
        ran on), so a mid-fight turn that merely narrates still holds the clock. The warning is
        then re-evaluated in this SAME call, because pushing the
        jump out makes the next turn the jump turn -- exactly the turn the warning has to
        precede. A fight landing on the warned turn used to leave the warning a turn stale.
        """
        battle = self._in_combat
        if self._jump_at_turn is None:
            return
        if battle:
            self._jump_at_turn += 1
            # The jump just moved out, so the "last turn of this age" the GM already holds may
            # no longer be the last turn. Re-arm it and let the check below decide -- and drop
            # any memory line already written, because the age is still going.
            self._warned = False
            self._pending_legend = ""
        remaining = self._jump_at_turn - self.turn_counter
        if remaining > 0:
            if remaining == WARNING_TURNS and not self._warned:
                self._warned = True
                # The GM hears it BEFORE the jump turn, so the age can be closed properly. A
                # re-warning REPLACES the earlier one: that text claimed this was the last turn
                # and the fight since made it false, so history holds exactly one claim. The
                # memory ask rides the same turn.
                self.messages = [m for m in self.messages
                                 if not (isinstance(m, dict)
                                         and m.get("content") in (DEVICE_WARNING, LEGEND_ASK))]
                self.messages.append({"role": "system", "content": DEVICE_WARNING})
                self.messages.append({"role": "system", "content": LEGEND_ASK})
                await self._emit({"type": "notice", "title": "The Device", "text": DEVICE_WARNING})
            return
        if battle:
            return  # belt and braces: a jump may never fire on a battle turn
        await self._jump()

    def _era_opening(self) -> str:
        """The lines the GM is handed with an era: where the Traveller is in it, and that
        the kit is their own -- literal in every age. Written once, never
        always-on: it rides the ERA_FILE at awakening and the jump message after that."""
        parts = []
        if self.arrival:
            parts.append(f"You arrive at {self.arrival}.")
        parts.append(CARRY_NOTE)
        return "\n\n".join(parts)

    def _memory_message(self, just_left: str) -> str:
        """The eras' memory as one short block -- the compaction output."""
        lines: list[str] = []
        for era in (just_left, self.era):
            line = self._legends.get(era, "")
            if line and line not in lines:
                lines.append(line)
        return ("WHAT THE AGES REMEMBER\n" + "\n".join(lines)) if lines else ""

    def _compact(self, just_left: str) -> None:
        """The era switch IS the compaction: prefix + what the ages remember, and nothing
        behind it. This is what stops the context growing ~104 tokens per turn at the test
        cadence -- and the thing that saves the tokens is the legend, not a prose summary."""
        head = [self.messages[0], {"role": "system", "content": render_era_index(self.era)}]
        self._era_index_at = 1
        self._places_at = None
        self._places_at = len(head)
        head.append({"role": "system", "content": self._places_body()})
        memory = self._memory_message(just_left)
        if memory:
            head.append({"role": "system", "content": memory})
        self.messages = head

    async def _ensure_legend(self) -> None:
        """Force the age's memory with quiet rounds (never shown as player prose).

        The legend is the only thing that survives the compaction, so it is worth a second
        attempt before giving up -- there is no engine-authored fallback."""
        for _ in range(2):
            if self._pending_legend:
                return
            await self._run_role(LEGEND_RECOVERY_NUDGE, "legend-fix", quiet=True)

    async def _close_and_jump(self, era: str) -> None:
        """A player-driven jump: one closing turn first, so the GM can say what the age keeps.

        An automatic jump is warned ahead and the memory line rides its last turn; a jump the
        Traveller triggers has no such turn, so the engine manufactures one.
        """
        await self._run_turn(DEVICE_CLOSES.format(name=device.era_label(era)), "closing")
        await self._jump(to_era=era)

    async def _jump(self, to_era: str | None = None) -> None:
        """The Device fires: a new era, a rolled arrival, and the age left behind as one
        line. Whatever an era sees, it remembers. `to_era` is the player's chosen
        destination (a recovered part); without it the Device does not aim."""
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
        chosen = str(to_era or "").strip().lower()
        self.era = chosen if chosen in self._all_other_eras() else self._steered_era()
        self.arrival = self._pick_jump_arrival(self.era)
        self._reload_era_scene_state()
        self._current_place = None  # a new age, a new place: the caption resets below
        self._compact(old)
        self._era_started_turn = self.turn_counter
        # The engine DB must agree: the GM's reputation paths and `dump_player_db` read the
        # era from there, and a save writes it back to the `.player`.
        await self._call_tool_text("set_player_field", {"key": "era", "value": self.era})
        await self._call_tool_text("set_player_field", {"key": "arrival", "value": self.arrival})
        # Where the Traveller has stood, for "previous era".
        self.journey = list(self.journey or []) + [self.era]
        await self._call_tool_text("set_player_field", {"key": "journey",
                                                         "value": ",".join(self.journey)})
        if memory:
            await self._emit({"type": "notice", "title": "The Age Remembers", "text": memory})
        # The new age's caption now (era only -- the arrival place is not declared yet).
        await self._emit_caption()
        jump = DEVICE_FIRES.format(old=old, new=self.era, arrival=self.arrival or "somewhere")
        # Hand the new age's scaffold WITH the jump, as the awakening does. Asking the GM to
        # `lookup` it first mixed a tool call with narration (the protocol forbids that), and a
        # model resolved the conflict with a holding line -- "I'll consult the era first." --
        # which stalled the turn with no narration.
        if not self.classic:
            jump = f"{jump}\n\n{render_era_text(self.era)}"
        await self._run_turn(jump, "jump")
        # Rearm AFTER the jump turn: that turn is itself a turn, and counting it keeps the
        # player's actions between jumps equal to the band (5 at the start, 5 again after).
        span = self._cadence_span()
        self._jump_at_turn = None if span is None else self.turn_counter + span
        self._warned = False
        self._adjusted_jump = False
        self._steer = ""
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

    async def _plain_summary(self, prompt: str) -> str:
        """Tool-free, one-off model call for a summary; does not touch history."""
        messages = self.messages + [{"role": "user", "content": prompt}]
        snap = await self._record_gm_prompt("summary")
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

    async def _summarize_timeline(self, target_turn: int):
        """Produce a comprehensive timeline segment covering turns since the last save.

        Returned, never written: the caller writes the whole timeline in one go on Save."""
        if target_turn <= self.last_timeline_turn:
            return None
        start = self.last_timeline_turn + 1
        await self._emit({"type": "notice", "title": "Timeline",
                          "text": f"Summarising turns {start}-{target_turn} for the save..."})
        prompt = TIMELINE_PROMPT.replace("X-Y", f"{start}-{target_turn}")
        text = await self._plain_summary(prompt)
        entry = _clean_pause_tokens(text or "")
        if entry and "**Key Events**" in entry:
            self.last_timeline_turn = target_turn
            await self._emit({"type": "timeline", "entry": entry})
            return entry
        return None

    def _write_timeline(self, entry=None) -> None:
        """Rewrite the save's timeline atomically: header, the ages' legends, the session
        history, and the new summary entry.

        Called on Save only. Legends live in memory (`self._legends`) until here, so an
        unsaved session never touches the file; rebuilding the legend block keeps a save
        idempotent (no duplicate legends)."""
        if not self.timeline_path:
            return
        session_text = load_timeline(self.timeline_path)  # the file with legends stripped
        if not (session_text or entry or self._legends):
            return  # nothing to record yet: do not create an empty timeline
        blocks = ["# Session Timeline"]
        if self._legends:
            blocks.append("\n".join(f"<!-- legend:{era} --> {text}"
                                    for era, text in self._legends.items()))
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
                              "text": "You cannot save in the middle of a fight — the "
                                      "timeline would not survive it."})
            return
        entry = await self._summarize_timeline(self.turn_counter)
        await self._save_to_active(entry)

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
                # after a few resumes, tell it plainly to write the narrative. At awakening
                # the first resume re-anchors the step it must resume at.
                if resumes > MAX_RESUME_ROUNDS:
                    nudge = RESUME_GUARD_NUDGE
                elif origin == "awakening":
                    nudge = AWAKENING_RESUME
                else:
                    nudge = "{{_CONTINUE_EXECUTION}}"
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
        if not quiet and _clean_pause_tokens(content):
            # A narrative has reached the player this turn; never ask for it again. This must
            # test the CLEANED text: the pause token is emitted as *content*, so a bare
            # `{{_NEED_ANOTHER_PROMPT}}` used to mark a turn as narrated that had no prose at
            # all -- which defeated the narrative guarantee and left the opening scene unwritten.
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

            if any(token in (content or "") for token in PAUSE_TOKENS):
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
        if name == PLACE_TOOL and not is_error:
            self._record_place_live(args)
        gm_text = _gm_tool_view(name, text)
        if name == SCENE_TOOL and warning is None:
            for note in (self._undeclared_note(args), self._on_stage_note(args)):
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
                                  "initiative": dict(self._combat_initiative),
                                  "in_combat": self._in_combat})
        if name == SCENE_TOOL and self.scene_images and warning is None and not self._scene_requested_turn:
            self._scene_requested_turn = True
            raw = args.get("place")
            place = [str(p).strip() for p in raw if str(p or "").strip()] if isinstance(raw, list) else []
            kingdom = str(args.get("kingdom") or "")
            area = str(args.get("area") or "")
            if str(args.get("establishing") or "") or str(args.get("seed_change") or ""):
                self._scene_places[_scene_key(place)] = (kingdom, area)
            elif not kingdom and not area:
                kingdom, area = self._scene_places.get(_scene_key(place), ("", ""))
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
            # The place moved: refresh the scene caption.
            await self._emit_caption()

        if name == "update_player_list":
            await self._sync_device_after_inventory(text)

    async def _sync_device_after_inventory(self, text: str) -> None:
        """A gear change can add or remove a Device part; the cadence reads from the items."""
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(payload, dict) and payload.get("key") == "inventory":
            await self._refresh_device()
            # Recover a part and the Device grips harder immediately; take the Device (or
            # reach 4 of 4) and the countdown stops. Going back the other way arms a fresh
            # one. An existing countdown is never reset by a change, only its band.
            span = self._cadence_span()
            if span is None:
                self._jump_at_turn = None
                self._warned = False
            elif self._jump_at_turn is None:
                self._jump_at_turn = self.turn_counter + span
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
        if isinstance(summary, list):
            # One hostile, living, active non-player keeps combat on. Derived here so the save
            # gate and the Device cadence share the server's truth (`_in_active_combat`).
            self._in_combat = any(
                isinstance(e, dict)
                and not e.get("is_player")
                and str(e.get("role") or "hostile").strip().lower() == "hostile"
                and not e.get("killed")
                and str(e.get("status") or "active").strip().lower() == "active"
                for e in summary)
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
        self._remember_roles(npcs)
        return added

    def _collect_declarations(self, args: dict) -> None:
        """Declarations that ride a scene call: the `npcs` list + the place's main NPCs."""
        self._remember_declared(args.get("npcs"))
        main = args.get("main_npcs")
        self._remember_roles(main)
        if isinstance(main, list):
            for entry in main:
                if not isinstance(entry, dict):
                    continue
                name = " ".join(str(entry.get("name") or "").split())
                if name:
                    self._cast_names.add(name.lower())

    def _remember_roles(self, entries) -> None:
        """Remember declared NPC roles, by lowercased name.

        A name with no role is skipped: there is nothing to remind the GM of."""
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split())
            role = " ".join(str(entry.get("role") or "").split())
            if name and role:
                self._npc_roles[name.lower()] = (name, role)

    def _record_place_live(self, args: dict) -> None:
        """Apply a `note_place` declaration to the live session (the tree updates at once)."""
        raw = args.get("place")
        place = [str(p).strip() for p in raw if str(p or "").strip()] if isinstance(raw, list) else []
        if not place:
            return
        era = "" if self.classic else str(self.era or "")
        kingdom = " ".join(str(args.get("kingdom") or "").split())
        area = " ".join(str(args.get("area") or "").split())
        main = args.get("main_npcs") if isinstance(args.get("main_npcs"), list) else []
        cast = args.get("cast") if isinstance(args.get("cast"), list) else []
        self._scene_places[_scene_key(place)] = (kingdom, area)
        self._remember_roles(main)
        self._remember_roles(cast)
        for entry in list(main) + list(cast):
            if isinstance(entry, dict):
                name = " ".join(str(entry.get("name") or "").split())
                if name:
                    self._cast_names.add(name.lower())
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
                                 "main_npcs": people, "used": 0})
        else:
            live["kingdom"] = kingdom or live.get("kingdom", "")
            live["area"] = area or live.get("area", "")
            if people:
                live["main_npcs"] = people
        # The scene caption's place is the one just declared.
        self._current_place = {"era": era, "kingdom": kingdom, "area": area, "place": place}

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

    def _on_stage_note(self, args: dict) -> str:
        """The role of each declared NPC the GM just put on stage.

        Roles are deliberately absent from the priming tree -- they are wanted when a
        character appears, not on every turn -- so they ride the scene result instead,
        which costs nothing in the always-on prefix. Only names the GM itself listed, and
        only ones with a declared role: a one-off extra's role is invented on the spot."""
        characters = args.get("characters")
        if not isinstance(characters, dict) or not characters or not self._npc_roles:
            return ""
        labels: list[str] = []
        seen: set[str] = set()
        for key in characters:
            key_l = " ".join(str(key or "").split()).lower()
            found = self._npc_roles.get(key_l)
            if not found or key_l in seen:
                continue
            seen.add(key_l)
            labels.append(f"{found[0]} ({found[1]})")
        return ("on stage: " + ", ".join(labels)) if labels else ""

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
