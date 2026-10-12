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
import shutil
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
    known_scene_places, mark_place_dead,
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


# ── the save's timeline ─────────────────────────────────────────────
# One file holds blocks with different lifetimes, each marked so it can be parsed back:
#   `<!-- legend:era -->`   what an age remembers (diegetic, per-era)
#   `<!-- state:key -->`     what is TRUE NOW: one page per age. A classic save keeps the
#                           single unkeyed `<!-- state -->`. Rewritten every Save.
#   `<!-- chronicle:A-B -->` old events collapsed into prose, at compaction
#   `## Turns X-Y ...`       recent events, kept verbatim
_TIMELINE_HEADER = "# Session Timeline"
_LEGEND_RE = re.compile(r"(?m)^<!-- legend:([a-z0-9_-]+) -->[ \t]*(.*?)[ \t]*$")
_CHRONICLE_RE = re.compile(
    r"(?ms)^<!-- chronicle:(?P<a>\d+)-(?P<b>\d+) -->[ \t]*\n(?P<body>.*?)^<!-- /chronicle -->[ \t]*$")
_STATE_KEYED_RE = re.compile(
    r"(?ms)^<!-- state:(?P<key>[a-z0-9_-]+) -->[ \t]*\n(?P<body>.*?)^<!-- /state:(?P=key) -->[ \t]*$")
_STATE_LEGACY_RE = re.compile(
    r"(?ms)^<!-- state -->[ \t]*\n(?P<body>.*?)^<!-- /state -->[ \t]*$")
# The STATE page vocabulary -- one contract for both games. A page is the GM's own prose;
# the engine enforces the FIELD NAMES and their order, never the content. Companions are NOT
# here: the engine lists their names itself (`_companions_line`), so the GM never writes them.
# `Standing` is gone too: a power's attitude is reputation (engine-owned), a name is the place
# tree's, and anything left is a Hook, a Debt or Canon.
_STATE_FIELDS = ("Goal", "Hooks", "Clocks", "Debts", "Secrets", "Canon", "Unpaid")
# A label the GM reached for that means a canonical field (case-insensitive).
_STATE_SYNONYMS = {
    "goal": "Goal", "goals": "Goal", "objectives": "Goal",
    "threads": "Hooks", "echoes": "Hooks", "left behind": "Hooks",
    "debts": "Debts", "oaths": "Debts", "promises": "Debts",
    "secrets": "Secrets",
    "hooks": "Hooks", "unresolved": "Hooks",
    "clocks": "Clocks", "pressures": "Clocks",
    "canon": "Canon", "truths": "Canon",
    "unpaid": "Unpaid", "foreshadowing": "Unpaid",
}
# Labels that only restate a store the engine already feeds: the whole block is dropped.
_STATE_ENGINE_KEYS = {
    "era", "age", "when", "year", "date", "time", "location", "place", "places",
    "arrival", "journey", "reputation", "device", "inventory", "equipment", "gold",
    "xp", "level", "hp", "hit points", "conditions", "spells", "slots",
    "cast", "characters", "people", "npcs",
    "standing", "regulars", "attitudes", "companions", "companion", "party",
}
_STATE_FIELD_RE = re.compile(
    r"(?m)^\s*(?:\*\*)?([A-Za-z][A-Za-z _-]{0,24}?)(?:\*\*)?\s*:\s*(.*)$")
# The `## Turns …` / legacy `## Rounds …` entry boundary.
_ENTRY_SPLIT_RE = re.compile(r"(?m)^(?=##\s+(?:Rounds?|Turns?)\s+\d)")


def _read_timeline_file(timeline_path) -> str:
    if not timeline_path or not os.path.exists(timeline_path):
        return ""
    try:
        with open(timeline_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def load_timeline(timeline_path):
    """The kept (recent) session entries: the header, the legends, the STATE pages and the
    chronicle are stripped. This is what `_write_timeline` rebuilds from, and what turn
    numbering reads."""
    text = _LEGEND_RE.sub("", _read_timeline_file(timeline_path))
    text = _STATE_KEYED_RE.sub("", text)
    text = _STATE_LEGACY_RE.sub("", text)
    text = _CHRONICLE_RE.sub("", text)
    text = re.sub(r"(?m)^# Session Timeline[ \t]*\n?", "", text, count=1)
    return text.strip()


def _clean_state_page(body: str) -> str:
    """Enforce a STATE page's field names and order.

    The engine-owned labels (`era`, `year`, `location`, `reputation`, `device`, `cast`,
    `standing`, …) copy a store that is injected anyway, so their blocks are dropped; a
    synonym (`threads` -> `Hooks`) is renamed and kept. Blocks are emitted in the contract's
    order, so `_clamp_block` (which keeps the earliest lines) can never drop `Goal` first.
    Content is never rewritten."""
    allowed = {f.lower() for f in _STATE_FIELDS}
    blocks: dict = {}
    current = None
    for line in (body or "").splitlines():
        match = _STATE_FIELD_RE.match(line)
        label = " ".join(match.group(1).split()).lower() if match else ""
        if match and (label in _STATE_SYNONYMS or label in _STATE_ENGINE_KEYS):
            if label in _STATE_ENGINE_KEYS:
                current = None
                continue
            target = _STATE_SYNONYMS[label]
            if target.lower() not in allowed:
                current = None
                continue
            value = match.group(2).rstrip()
            blocks.setdefault(target, []).append(
                f"**{target}**:" + (" " + value if value else ""))
            current = target
        elif current and line.strip():
            blocks[current].append(line.rstrip())
    out: list[str] = []
    for name in _STATE_FIELDS:
        out.extend(blocks.get(name, []))
    return "\n".join(out).strip()


def _state_has_fields(page: str, fields) -> bool:
    """True when a page carries every named field -- the page's core."""
    labels = {m.group(1).strip().lower() for m in _STATE_FIELD_RE.finditer(page or "")}
    return all(str(f).strip().lower() in labels for f in fields)


def load_states(timeline_path) -> dict:
    """Every scoped STATE page in the save: `{key: body}` (one per age).

    Only the age the Traveller stands in is ever put in the prompt, so the ages he is not
    in cost nothing. Each page is cleaned on read, so a page written before the vocabulary
    was enforced is injected correctly and self-heals on the next Save."""
    out: dict = {}
    for match in _STATE_KEYED_RE.finditer(_read_timeline_file(timeline_path)):
        key = match.group("key").strip().lower()
        body = _clean_state_page(match.group("body").strip())
        if key and body:
            out.setdefault(key, body)
    return out


def load_state(timeline_path, key: str = "") -> str:
    """What is true now -- the GM's notebook, rewritten on every Save.

    A `key` reads one age's page; without one, the classic save's single unkeyed block. A
    save made before the pages were scoped has only the unkeyed block: the caller seeds it
    into the age the Traveller is in, and the next Save rewrites it keyed."""
    text = _read_timeline_file(timeline_path)
    want = str(key or "").strip().lower()
    if not want:
        match = _STATE_LEGACY_RE.search(text)
        return _clean_state_page(match.group("body").strip()) if match else ""
    for match in _STATE_KEYED_RE.finditer(text):
        if match.group("key").strip().lower() == want:
            return _clean_state_page(match.group("body").strip())
    return ""


def load_chronicle(timeline_path) -> tuple[int, int, str] | None:
    """The collapsed event history: `(first_turn, last_turn, prose)`, or None."""
    match = _CHRONICLE_RE.search(_read_timeline_file(timeline_path))
    if not match:
        return None
    return int(match.group("a")), int(match.group("b")), match.group("body").strip()


def _extract_states(text: str, keys) -> tuple[dict, str]:
    """Pull one or more scoped STATE pages out of a model reply.

    Returns ``(pages, cleaned_text)``; `pages` holds only the wanted keys that were found and
    valid. A page that merely echoes the placeholder is rejected -- the page already held is
    better than a broken one. EVERY matched block is stripped from the text (wanted or not),
    so a departure page never reaches the client as prose."""
    raw = text or ""
    wanted = {str(k or "").strip().lower() for k in keys}
    wanted.discard("")
    out: dict = {}
    for match in _STATE_KEYED_RE.finditer(raw):
        key = match.group("key").strip().lower()
        if key not in wanted:
            continue
        body = match.group("body").strip()
        if body and "<" not in body:
            page = _clean_state_page(body)
            if page:
                out.setdefault(key, page)
    cleaned = _STATE_KEYED_RE.sub("", raw)
    cleaned = _STATE_LEGACY_RE.sub("", cleaned).strip()
    return out, cleaned


def _extract_state(text: str, key: str = "") -> tuple[str, str]:
    """Pull ONE STATE notebook out of a model reply.

    `key` reads a scoped page (`<!-- state:key -->`); without one, the classic save's single
    unkeyed block. Returns ``(state_body, cleaned_text)``. A state that merely echoes the
    placeholder is rejected -- the state already held is better than a broken one."""
    if key:
        pages, cleaned = _extract_states(text, [key])
        return pages.get(str(key).strip().lower(), ""), cleaned
    raw = text or ""
    match = _STATE_LEGACY_RE.search(raw)
    if not match:
        return "", raw
    body = match.group("body").strip()
    cleaned = _STATE_LEGACY_RE.sub("", raw).strip()
    if not body or "<" in body:
        return "", cleaned
    return _clean_state_page(body), cleaned


def _split_timeline_entries(text: str) -> list[str]:
    """The `## Turns …` entries in a timeline body, in order."""
    return [e.strip() for e in _ENTRY_SPLIT_RE.split(text or "") if e.strip()]


def _entry_turn_range(entry: str) -> tuple[int, int]:
    """`(lowest, highest)` turn in one entry's headings; `(0, 0)` when it has none."""
    lo = hi = 0
    for first, second in _TURN_HEADING_RE.findall(entry or ""):
        a, b = int(first), int(second or first)
        lo = a if lo == 0 else min(lo, a)
        hi = max(hi, b)
    return lo, hi


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
# The session start's resume re-anchors the one step a bare `{{_CONTINUE_EXECUTION}}` leaves
# implicit: after the pause the next beat IS the narrative (the opening scene for a new game,
# the continuation for a resumed one), and it carries the illustration. An empty turn there
# otherwise reads as valid. A runtime message, so it costs no prefix tokens.
AWAKENING_RESUME = (
    "{{_CONTINUE_EXECUTION}} — write the SESSION START narrative now — the opening scene for "
    "a new game, the continuation for a resumed one — in the same response as exactly ONE "
    "`request_scene_image`. The CHARACTER SHEET is already in your context."
)

# Web-local timeline prompt: the database is authoritative, so mechanical changes are NOT
# summarised (the terminal CLI's prompt is gone). The response carries the event entry AND
# the STATE notebook (what is true now, rewritten in place every Save).
TIMELINE_PROMPT = """SYSTEM INSTRUCTION: The player is saving the game. Output TWO blocks covering the turns listed below, in this EXACT order and format. Replace bracketed text with actual content. Keep it concise. Output ONLY these blocks — no extra narration, no tool calls.

## Turns X-Y | [current location] | [in-game time]
**Key Events**:
- [event in one sentence]

<!-- state -->
**Goal**: [what the Traveller is trying to do now, one line]
**Hooks**: [every unresolved thread, one per line; drop resolved ones]
**Clocks**: [what advances if the Traveller does nothing]
**Debts**: [promises, oaths and bargains owed or held]
**Secrets**: [what the Traveller knows that others do not]
**Canon**: [details that must stay true — keep it short]
**Unpaid**: [foreshadowing planted but not yet paid off]
<!-- /state -->

RULES:
- Write current truth only — no "then", no "now freed", no "later".
- Do not restate what the engine already tells you: the date or time, the place, the place tree and its regulars, reputation, the companions the engine lists, HP, inventory.
- Never write a `Companions` line: the engine lists the companions; a person's stance is a Hook, a settled truth Canon, an oath a Debt."""

# The era game's timeline prompt: one page per age, because an age is a perspective. The
# `state:<era>` page holds this age's fields alone; the age the Traveller is not in is on
# disk and never injected. `{era}` is replaced with the age he stands in. Companions are
# listed by the engine, never written here.
TIMELINE_PROMPT_ERA = """SYSTEM INSTRUCTION: The player is saving the game. Output TWO blocks covering the turns listed below, in this EXACT order and format. Replace bracketed text with actual content. Keep it concise. Output ONLY these blocks — no extra narration, no tool calls.

## Turns X-Y | [current location] | [in-game time]
**Key Events**:
- [event in one sentence]

<!-- state:{era} -->
**Goal**: [what the Traveller is trying to do in THIS age, one line]
**Hooks**: [every unresolved thread in this age, one per line; drop resolved ones]
**Clocks**: [what advances in this age if the Traveller does nothing]
**Debts**: [promises, oaths and bargains he owes or holds in THIS age]
**Secrets**: [what only he knows about THIS age]
**Canon**: [details of this age that must stay true — keep it short]
**Unpaid**: [foreshadowing planted here but not yet paid off]
<!-- /state:{era} -->

RULES:
- Write current truth only — no "then", no "now freed", no "later".
- Do not restate what the engine already tells you: the era, the year or time, the place, the place tree and its regulars, reputation, the companions the engine lists, the Device and its parts, HP, inventory.
- Never write a `Companions` line: the engine lists the companions; a person's stance is a Hook, a settled truth Canon, an oath a Debt.
- This page is this age's alone; another age's truth is not written here."""

# The compaction output: the older entries, rewritten as one short in-voice chronicle. The
# STATE block is already current, so it is not folded here -- only events are.
CHRONICLE_PROMPT = """SYSTEM INSTRUCTION: These are earlier session timeline entries from a long-running game, covering turns {a}-{b}. Rewrite them as ONE compact "story so far" in the same voice as the entries but much shorter. Keep the events that still matter; drop resolved detail and anything the story has moved past. Do not invent. Keep it under about 1000 characters, as prose — do NOT include a "## Turns" heading, and do not write a STATE block. Output ONLY the chronicle.

{old}"""

# The injected session memory: the STATE page (current truth) + the EVENTS (chronicle +
# kept entries). A classic save has one notebook; the era game has one page per age, and
# only the current age's is injected.
SESSION_TIMELINE_HEADER = (
    "SESSION_TIMELINE — earlier in this story. Refer to past events when the player asks; "
    "do not replay or re-describe them. The STATE block(s) below are what is TRUE NOW — "
    "honour them.\n\n"
)

# The timeline is bounded by COMPACTION, not by dropping history: once the injected body
# passes the cap (or the entry-count floor), the older entries are rewritten as one
# GM-authored chronicle and the raw entries are discarded. Events decay; the STATE pages
# are rewritten in place and do not grow. Only the current age's page is ever injected, so
# the other ages cost disk, not tokens.
MAX_TIMELINE_CHARS = 4500   # the injected body: STATE page + chronicle + kept entries
TIMELINE_KEEP_ENTRIES = 2   # newest entries kept verbatim at a compaction
TIMELINE_MAX_ENTRIES = 6    # always compact past this many entries
MAX_CHRONICLE_CHARS = 1200  # the event chronicle
MAX_STATE_CHARS = 1200      # one age's STATE page

HELP_TEXT = (
    "/help  - show this help\n"
    "/stats - show the current character sheet\n"
    "/save  - write the character sheet to your .player file (active effects reverted)\n"
    "/quit  - end the session\n\n"
    "Anything else is sent to the Game Master as an action."
)

# Shown when the player tries to save or release the Device while a fight is live. The
# engine ends combat on its own once every hostile is dead, fled or surrendered; this is the
# nudge for the cases it cannot see (the player left, the foes are caged, a parley).
COMBAT_HOLD_MESSAGE = "Combat is not over. If you think it is, inform the Game Master."


def _tool_schema(tool) -> dict:
    params = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": compact_schema(params, tool.name),
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
# Declares a companion's durable character sheet (engine-owned, era-scoped). The look in it
# rides the same image pipeline as a recurring NPC; its stats are filled in when the GM later
# registers the companion by name alone.
COMPANION_TOOL = "register_companion"
# The text-only place/NPC registry (images off): declares a place and its regulars by
# name + role with no image. Hidden when images are on (the scene call records places there).
PLACE_TOOL = "note_place"
# Tools the GM may attach to narrative prose (the Narrative Phase). A round that
# carries prose plus only these tools is the turn's last word: the tool loop must
# NOT run another model round, or the GM re-narrates the whole turn.
NARRATIVE_PHASE_TOOLS = frozenset({SCENE_TOOL})
# Tools whose result may change the combat roster the client shows as tooltips.
COMBAT_TOOLS = frozenset({"register_combatants", "update_combatant", "end_combat",
                          "resolve_attack", "resolve_magic", "modify_exhaustion", "make_death_save"})
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
    """Normalize a `note_place` people list to the seed shape ({name, role, race, class},
    empty look)."""
    out: list[dict] = []
    for entry in value or []:
        if not isinstance(entry, dict):
            continue
        name = " ".join(str(entry.get("name") or "").split())
        if name:
            out.append({"name": name, "description": "",
                        "role": " ".join(str(entry.get("role") or "").split()),
                        "race": " ".join(str(entry.get("race") or "").split()),
                        "class": " ".join(str(entry.get("class") or "").split())})
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
        dead = {str(n).strip().lower() for n in (p.get("dead") or []) if str(n or "").strip()}
        names = [str(npc.get("name") or "").strip() for npc in (p.get("main_npcs") or [])
                 if isinstance(npc, dict)]
        names = [n for n in names if n]
        labels = [f"{n} (dead)" if n.lower() in dead else n for n in names]
        npc_labels.extend(labels[:cap])
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


def _player_companions(player_path: str) -> dict[str, list[dict]]:
    """The companions each world of a save declares: `{era: [{name, race, class, description}]}`.

    The engine injects this in the sheet (never the GM), so the store is the source of truth. A
    sheet's `name` is the display spelling; the lowercased key is the fallback. The description
    (the stable look) rides along so the GM keeps the same person across saves and ages."""
    try:
        with open(player_path, "r", encoding="utf-8") as f:
            stored = (json.load(f) or {}).get("companions")
    except (OSError, ValueError, TypeError):
        return {}
    out: dict[str, list[dict]] = {}
    if not isinstance(stored, dict):
        return out
    for era, bucket in stored.items():
        if not isinstance(bucket, dict):
            continue
        people = []
        for key, sheet in bucket.items():
            sheet = sheet if isinstance(sheet, dict) else {}
            name = " ".join(str(sheet.get("name") or key or "").split())
            if name:
                people.append({
                    "name": name,
                    "race": " ".join(str(sheet.get("race") or "").split()),
                    "class": " ".join(str(sheet.get("class") or "").split()),
                    "description": " ".join(str(sheet.get("description") or "").split()),
                })
        if people:
            out[str(era).strip().lower()] = people
    return out


def _sheet_body(db: dict) -> str:
    """The lean CHARACTER SHEET block the engine injects (never the GM's `dump_player_db`).

    It carries the fields the GM *reads* -- identity, vitals, gear, magic, standing -- and the
    key-sets it addresses a write with (`inventory` names, `reputation` polities/factions,
    prepared spells). The combat math the engine owns (`stats`, `saves`, `skills`,
    `*_proficiencies`, `_passive`, `armor_class`, hit dice, `_active_buff_data`, the
    `_carrying`/`_equipment` internals) is deliberately absent: the GM passes ability and skill
    NAMES to `perform_check`, never numbers. Names only -- quantities live in `consumables`."""
    if not isinstance(db, dict):
        return ""

    def _labels(items) -> str:
        out = []
        for it in items or []:
            name = it.get("name") if isinstance(it, dict) else it
            name = " ".join(str(name or "").split())
            if name:
                out.append(name)
        return ", ".join(out)

    def _j(value) -> str:
        return json.dumps(value, ensure_ascii=True)

    carrying = db.get("_carrying") if isinstance(db.get("_carrying"), dict) else {}
    status = str(carrying.get("status") or "").strip()
    speed = f"speed {db.get('speed')}" + (f" ({status})" if status else "")
    equipped = db.get("equipped") if isinstance(db.get("equipped"), dict) else {}
    lines = [
        (f"CHARACTER SHEET — {db.get('name')} | {db.get('race')} {db.get('character_class')} "
         f"{db.get('level')} | {db.get('background')}, {db.get('alignment')}, {db.get('gender')}, "
         f"{db.get('age')}"),
        (f"HP {db.get('current_hit_points')}/{db.get('total_hit_points')} "
         f"temp {db.get('temporary_hit_points')} | {db.get('gold')} gp | {speed} | "
         f"languages: {', '.join(str(x) for x in (db.get('languages') or []))}"),
        (f"conditions: {_labels(db.get('conditions')) or 'none'} | "
         f"concentration: {_labels([db.get('concentration')]) or 'none'} | "
         f"effects: {_labels(db.get('active_effects')) or 'none'}"),
        f"features: {_labels(db.get('features'))}",
        f"inventory: {_labels(db.get('inventory'))}",
        (f"equipped: worn {_labels(equipped.get('worn')) or 'none'} | "
         f"hands {_labels(equipped.get('hands')) or 'none'} | "
         f"consumables: {_j(db.get('consumables') or {})}"),
        f"reputation: {_j(db.get('reputation') or {})}",
    ]
    return "\n".join(line for line in lines if line)


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
                     "debug_set_field", "debug_device", "dump_player_db")

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

# The Device's cadence, keyed by how many of its five parts are back: more parts, a longer
# wait. It only matters while the Device fires on its own -- the Mainspring's charge stops
# the forced jumps entirely (`_cadence_span`). A four-part Device WITHOUT the Mainspring is
# possible (the Vernier and three originals), so band 4 is a real interval.
CADENCE_BANDS = {0: (5, 5), 1: (7, 7), 2: (11, 11), 3: (13, 13), 4: (17, 17)}
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

# Asked on the same closing turn, and it must come BEFORE the memory marker (which stays
# last): the age's own STATE page. Once the age is left no Save can ever happen there again,
# so departure is the only moment a visit's final truth can be captured. Best-effort -- a
# miss leaves the page already held, and the next Save in that age rewrites it anyway.
ERA_STATE_ASK = (
    "Before that, write this age's STATE page exactly as in a save reply -- a "
    "<!-- state:{era} --> … <!-- /state:{era} --> block with this age's fields, scoped to "
    "THIS age. It comes BEFORE the {{{{_REMEMBERS}}}} marker, which stays last."
)

# Asked ONCE, tool-free, when a Save produced a state page with no core AND there is no page
# on disk to keep -- the one case where silence would mean the notebook is silently absent.
# Unlike the legend's quiet round (which runs at a jump, just before the history is thrown
# away), this call never touches `self.messages`, so a Save cannot leave the nudge in the
# live conversation.
STATE_RECOVERY_NUDGE = (
    "SYSTEM: this save's STATE page for `{scope}` is missing its required field(s): {fields}. "
    "Rewrite ONLY that page, from the material below, in the usual field format and wrapped "
    "in <!-- state:{scope} --> … <!-- /state:{scope} -->. Output nothing else -- no prose, "
    "no tool calls.\n\n{reply}"
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


# `lookup` is the one generic read-only reference tool: era lore in the time-travel game, and
# a tool's on-demand field reference (``ref:TOOL``) in BOTH games -- so it is offered in classic
# too, where its era branches simply have no other era to reach.
LOOKUP_TOOL = "lookup"


def _hidden_tools(scene_images: bool, mode: str = MODE_TIME_TRAVELER) -> set[str]:
    """The tools this session must neither offer nor execute.

    One source, two callers: `filter_tools` (the offer) and `_execute_tool` (the guard), so
    a hallucinated call to a tool the GM was never offered cannot run."""
    hidden = set(ENGINE_ONLY_TOOLS)
    if not scene_images:
        hidden |= {SCENE_TOOL, NPC_TOOL}
    else:
        hidden.add(PLACE_TOOL)
    return hidden


def filter_tools(tools: list[dict], scene_images: bool,
                 mode: str = MODE_TIME_TRAVELER) -> list[dict]:
    """What the GM is offered: hide the tools this game cannot use, and always hide the
    session's own bookkeeping tools."""
    hidden = _hidden_tools(scene_images, mode)
    return [t for t in tools
            if (t.get("function") or {}).get("name") not in hidden]


# ── What the GM is shown of a tool result ─────────────────────────────────
#
# Tool results carry only what the GM does not already hold: the event/delta, the exact
# mechanics, and errors that explain what happened. The base sheet is already in context
# (dump_player_db), so state snapshots (`equipment`, the carrying breakdown, a full inventory
# list) are never re-sent. The FULL result still goes to the client (the tool_result event) for
# debugging; only the copy appended to the GM conversation is trimmed.
#
# Every GM-offered tool has an EXPLICIT decision -- KEEP, DROP or FULL below. There is no
# fail-open default: a tool in none of the three is a bug, and a coverage test fails the build
# for it (web/tests/test_tool_projection.py).
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
    "register_combatants": ("success", "initiative_order", "registry_summary", "companions"),
    "end_combat": ("success",),
    "update_combatant": ("success", "name", "is_player", "hp", "ac", "conditions",
                         "exhaustion", "blocked_conditions", "already_present", "note"),
    "modify_exhaustion": ("success", "exhaustion", "old", "delta", "effects", "max_hp"),
    "make_death_save": ("success", "roll", "successes", "failures", "outcome", "hp"),
    # Acknowledgements: the GM just made the call, so only the outcome is new.
    "request_scene_image": ("status", "note"),
    "register_npcs": ("status", "count", "note"),
    "register_companion": ("success", "status", "name", "era", "count", "limit", "note"),
    "note_place": ("status", "kingdom", "area", "place", "main_npcs", "cast", "note"),
}
# Added on failure so the GM can explain and act on a refused/wrong call.
_GM_VIEW_ERROR_EXTRA = (
    "error", "reason", "gm_instruction", "turn_lost", "action_consumed",
    "current_items", "held", "occupied", "worn", "slot", "available_keys",
    "attuned", "attunement_slots_free", "conflicting_item", "prerequisite")

# Deny-lists: keep everything EXCEPT these. For a broad mechanics result, naming what to drop
# is safer than naming what to keep.
_GM_VIEW_DROP: dict[str, tuple[str, ...]] = {
    # A mechanics tool embeds the FULL result of the calls it made -- `slot_result` and
    # `xp_result` (whole `modify_player_numeric` results) and `hp_change` (a whole HP-change
    # result). The scalars the GM needs already ride the top level (`slot_consumed`,
    # `slot_level_used`, `xp_awarded`, `damage_total`, `targets[].remaining_hp`).
    "resolve_magic": ("slot_result", "xp_result", "hp_change"),
    "resolve_attack": ("slot_result", "xp_result", "hp_change"),
}

# Intentional FULL pass-throughs, each with its reason. Naming them is what makes coverage
# testable; none of these is a default.
_GM_VIEW_FULL: dict[str, str] = {
    "lookup": "plain text (era lore / a tool reference); there is no JSON to project",
    "perform_check": "one small mechanics result; nothing stateful to strip",
    "roll_dice": "one small magnitude roll",
}

# Always dropped, at ANY depth: the engine's own mechanics disclosure, and the state the GM
# already holds. Recursive because a tool can embed another tool's result.
_GM_VIEW_ALWAYS_DROP = ("narrative_format", "equipment", "current_list", "registry_summary",
                        "sheets", "difficulty", "era", "arrival")
# `carrying` is reduced to the one-line state that drives disadvantage.
_GM_VIEW_CARRYING = ("status", "speed_penalty", "carried", "capacity")


def _gm_strip(value):
    """Recursively drop the always-drop keys, at any depth.

    A mechanics tool embeds other tools' results (`slot_result`, `hp_change`), so a
    top-level-only pop used to leak a nested `narrative_format` -- the engine's own mechanics
    disclosure -- straight to the GM."""
    if isinstance(value, dict):
        return {k: _gm_strip(v) for k, v in value.items() if k not in _GM_VIEW_ALWAYS_DROP}
    if isinstance(value, list):
        return [_gm_strip(v) for v in value]
    return value


def _gm_tool_view(name: str, text: str) -> str:
    """The trimmed result the GM sees, as JSON (falls back to `text` when not a dict).

    Every GM-offered tool is classified: KEEP (only these keys), DROP (everything but these) or
    FULL (whole, with a stated reason). A name in none of them is a bug, not a free pass -- the
    payload is still stripped of the always-drop keys rather than dumped raw, and the coverage
    test (web/tests/test_tool_projection.py) fails the build for a new tool."""
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
    elif name in _GM_VIEW_DROP:
        drop = set(_GM_VIEW_DROP[name])
        if str(payload.get("attack_type") or "").strip().lower() == "automatic":
            # An automatic multi-projectile spell needs no per-dart list: `targets[].darts`
            # already carries every dart's damage. An attack-roll one keeps it -- the rolls and
            # AC are the only record of those attacks.
            drop.add("per_projectile")
        view = {k: v for k, v in payload.items() if k not in drop}
    else:  # _GM_VIEW_FULL, or (a bug) unclassified: fail closed, never dump the raw payload
        view = dict(payload)
    view = _gm_strip(view)
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


def _clamp_block(text: str, limit: int) -> str:
    """Hard-bound a multi-line block to `limit` characters, at a line boundary.

    Unlike `_trim_legend` this keeps line structure (the STATE notebook is a list of
    fields, not one paragraph) and, on overflow, keeps the EARLIEST lines -- the prompt
    orders the fields by importance (Goal and Hooks first)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    out: list[str] = []
    total = 0
    for line in text.splitlines():
        if total + len(line) + 1 > limit:
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out) or text[:limit]


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
        # The Device and its parts are real inventory items (engine-seeded at creation). The
        # count sets the cadence; the recovered SET decides each part's own power (one part =
        # one power -- see device.POWERS). `has_device` False means the Device itself is gone
        # and the cadence is off.
        self.has_device = False
        self.parts_recovered = 0
        # {ability id: held}, e.g. {"charge": True, "release": False, ...}.
        self.device_powers: dict[str, bool] = {}
        # Whether the Mainspring's charge is engaged (the Device holds; no forced jumps). The
        # player may let it run, so a partially repaired Device can never be stranded.
        self._hold: bool = True
        # The eras the Traveller has stood in, oldest first: "previous" means the one before
        # the current, which the timeline legends cannot give (a return does not rewrite one).
        self.journey: list[str] = []
        # The pending aim for the next jump: "" (drift), "previous", "forward", or an era id
        # the Regulator named. Set without firing; consumed and cleared by the next jump.
        self._steer: str = ""
        # The pending place for the next jump (the Vernier), or ""; honoured only when the
        # landing era has it among the places this save has visited.
        self._aim_place: str = ""
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
        # The current-truth notebook(s) and the collapsed event chronicle: all live in memory
        # until Save, like the legends, so an unsaved session never touches the file. A classic
        # save has one `_state`; the era game keeps one page per age, and a page captured on
        # the closing turn waits in `_pending_era_state` until the jump.
        self._state: str = ""
        self._era_states: dict[str, str] = {}
        # The engine-injected lean CHARACTER SHEET block (see `_sheet_body`), rebuilt at session
        # build and at every jump. The GM never fetches it (`dump_player_db` is engine-only).
        self._sheet_block: str = ""
        # The companions the engine lists itself (derived, never GM-written): `{era: [name]}`,
        # read from the save at start and updated as `register_companion` runs.
        self._companions: dict[str, list[str]] = {}
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
        # The companions the engine lists itself: the save's own store, plus any declared
        # this session. Never the GM's to write (see `_companions_line`).
        self._companions = _player_companions(self.player_path)
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
                            place: str = "", value=None, delta=None) -> None:
        await self._cmd_q.put({"type": "device", "action": action,
                               "era": era, "direction": direction, "place": place,
                               "value": value, "delta": delta})

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

                    # The character sheet, engine-injected: the GM never fetches it, so it is
                    # always in context (and re-injected at every jump -- see `_compact`). Lean:
                    # only the fields the GM narrates from and the key-sets it addresses writes
                    # with, never the combat math.
                    self._sheet_block = await self._fetch_sheet()
                    if self._sheet_block:
                        self.messages.append({"role": "system", "content": self._sheet_block})
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
        """A player-driven Device action: aim the next jump, or release the held one."""
        action = str(cmd.get("action") or "").strip().lower()
        if self.classic or not self.has_device:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "There is no Device to command."})
            return
        if action == "aim":
            await self._device_aim(cmd)
        elif action == "steer":
            await self._device_steer(cmd.get("direction"))
        elif action == "release":
            await self._device_release(cmd)
        elif action == "hold":
            await self._device_hold(cmd.get("value"))
        else:
            await self._emit({"type": "error", "message": f"unknown Device action: {action}"})

    def _aim_allowed(self, era: str, place: str = "") -> bool:
        """Whether the aim may name this age.

        The Regulator names ANY other playable age -- ahead or behind; there is no dead end at
        the last age. Without it, the Vernier can still name an age through a place the save has
        already stood in (the place carries its age)."""
        era = str(era or "").strip().lower()
        if not era or era == str(self.era or "").strip().lower():
            return False
        if era not in playable_eras():
            return False
        if self.device_powers.get(device.ERA):
            return True
        wanted = " ".join(str(place or "").split())
        return bool(self.device_powers.get(device.PLACE) and wanted
                    and wanted in self._visited_pool(era))

    async def _device_aim(self, cmd: dict) -> None:
        """Aim the NEXT jump without firing it (the Regulator; the place rides the Vernier).

        The Regulator names any other age. With only the Vernier, a place the save has already
        stood in names its age. Sending the active aim again clears it, so the Device drifts."""
        if not (self.device_powers.get(device.ERA) or self.device_powers.get(device.PLACE)):
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "The Device cannot be aimed yet."})
            return
        era = str(cmd.get("era") or "").strip().lower()
        place = self._clean_aim_place(cmd.get("place"))
        if not era:
            await self._emit({"type": "error", "message": "no era to aim at"})
            return
        if not self._aim_allowed(era, place):
            await self._device_refuse()
            return
        self._steer = "" if era == self._steer else era
        self._aim_place = place
        if self._steer:
            text = f"The Device is aimed at {device.era_label(era)}"
            text += f", {self._aim_place}." if self._aim_place else "."
        else:
            text = "The Device drifts again — the next jump goes where it likes."
        await self._emit({"type": "notice", "title": "The Device", "text": text})
        await self._emit({"type": "cadence", **self._device_state()})

    def _clean_aim_place(self, place) -> str:
        """The aimed place (the Vernier), or '' without it. Validated at jump time, because a
        drift or a 'random forward' jump does not know its era until it lands."""
        if not self.device_powers.get(device.PLACE):
            return ""
        return " ".join(str(place or "").split())

    async def _device_release(self, cmd: dict) -> None:
        """Release the held Device: fire the jump now (the Escapement on the wound Mainspring).

        The Mainspring winds the Device and holds it; the Escapement lets the charge out --
        both are needed. A deliberate jump is refused in combat. The aim (Regulator, Compass
        Rose) chooses the age and the Vernier the place; with nothing aimed, the Device drifts.
        """
        if not (self.device_powers.get(device.CHARGE)
                and self.device_powers.get(device.RELEASE)):
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "The Device is not wound for release yet."})
            return
        if self._in_combat:
            await self._emit({"type": "notice", "title": "The Device",
                              "text": COMBAT_HOLD_MESSAGE})
            return
        era = str(cmd.get("era") or "").strip().lower()
        place = self._clean_aim_place(cmd.get("place"))
        if era in ("previous", "forward"):
            if self.device_powers.get(device.DIRECTION):
                self._steer = era
        elif era:
            if not self._aim_allowed(era, place):
                await self._device_refuse()
                return
            self._steer = era
        if "place" in cmd:
            self._aim_place = place
        await self._close_and_jump(self._steered_era())

    async def _device_hold(self, value) -> None:
        """Hold the wound Device (stop its forced jumps), or let it run again.

        The Mainspring's charge holds it. Letting it go re-arms the countdown from the current
        turn, so a partially repaired Device is never stranded."""
        if not self.device_powers.get(device.CHARGE):
            await self._emit({"type": "notice", "title": "The Device",
                              "text": "The Device cannot be held yet."})
            return
        want = True if value is None else bool(value)
        self._hold = want
        if want:
            self._jump_at_turn = None
            self._warned = False
            self.messages = [m for m in self.messages
                             if not (isinstance(m, dict)
                                     and m.get("content") in (DEVICE_WARNING, LEGEND_ASK))]
            text = "The Device is held — it will not fire until you release it."
        else:
            span = self._cadence_span()
            self._jump_at_turn = None if span is None else self.turn_counter + span
            self._warned = False
            text = "The Device is let go — it will fire when it likes again."
        await self._emit({"type": "notice", "title": "The Device", "text": text})
        await self._emit({"type": "cadence", **self._device_state()})

    async def _device_steer(self, direction) -> None:
        """The Compass Rose's coarse aim: "" (drift), "previous" or "forward" (a random age).

        Not a jump, so it is allowed in combat -- the jump it aims will not fire until the
        fight is over. Sending the active direction toggles it off.
        """
        if not self.device_powers.get(device.DIRECTION):
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
        self._aim_place = ""
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
        device_touched = False  # a Device / inventory edit must re-derive the countdown
        cadence_staged = False  # an explicit countdown is the more specific instruction
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
            elif kind == "cadence":
                self._dev_set_cadence(raw.get("turns"), raw.get("steer"))
                results.append({"kind": "cadence", "turns": raw.get("turns"),
                                "steer": self._steer})
                remaining = self._cadence_state().get("turns_until")
                notes.append(f"Device countdown set to {remaining} turns")
                cadence_staged = True
            elif kind == "jump":
                era = str(raw.get("era") or "").strip().lower()
                notes.append(f"Device jumped to {device.era_label(era) if era else 'a new age'}")
                results.append({"kind": "jump", "era": era})
                await self._dev_force_jump(era)
            else:
                results.append({"kind": kind, "error": "unknown dev op"})
        if db_touched:
            await self._refresh_device()
        # A Device edit must re-derive the countdown, or a Device completed in the panel keeps
        # firing on the band it was armed with. An explicit cadence op wins.
        if device_touched and not cadence_staged:
            self._rearm_cadence()
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

    # ── the Device: the cadence, and the jump itself ─────────────

    def _cadence_span(self) -> int | None:
        """Turns until the next jump, or None when the Device does not fire by itself.

        A classic game has no Device, and a Traveller whose Device has been taken has none
        either. The Mainspring's charge stops the forced jumps entirely: while it is held the
        Device waits for the Escapement's release, so there is no countdown.
        """
        if self.classic or not self.has_device:
            return None
        if self.device_powers.get(device.CHARGE) and self._hold:
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
        """Where the next jump lands, honouring the pending aim.

        "" is the Device's own drift (any age but this one); "previous" the last age visited;
        "forward" a random age strictly ahead; any other value is a specific era id the
        Regulator named. An aim with no valid target falls back to the drift, so a jump
        never fails.
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
        elif steer and steer in playable_eras() and steer != self.era:
            return steer
        return self._pick_era()

    def _era_options(self, eras: list[str]) -> list[dict]:
        return [{"id": e, "name": device.era_label(e)} for e in eras]

    def _device_state(self) -> dict:
        """The cadence plus everything the Device panel needs to draw its controls."""
        total = device.part_total()
        live = (not self.classic) and self.has_device
        pw = self.device_powers if live else {}
        has_charge = live and bool(pw.get(device.CHARGE))
        # A fully-assembled Device has no cadence band, so it never forces a jump: the
        # Mainspring's hold toggle would change nothing and is not offered (`manual`); its
        # charge is still held (the Escapement needs it).
        manual = live and CADENCE_BANDS.get(self.parts_recovered) is None
        holding = has_charge and self._hold
        can_release = has_charge and bool(pw.get(device.RELEASE))
        abilities = {
            "charge": has_charge,                       # the Device can be held / let go
            "release": can_release,                     # fire the held jump now
            "direction": live and bool(pw.get(device.DIRECTION)),
            "era": live and bool(pw.get(device.ERA)),
            "place": live and bool(pw.get(device.PLACE)),
        }
        state = self._cadence_state()
        state.update({
            "total": total,
            "manual": manual,
            "holding": holding,
            "in_combat": self._in_combat,
            "steer": self._steer,
            "aim_place": self._aim_place,
            "abilities": abilities,
            "previous_era": self._previous_era(),
            "previous_era_name": device.era_label(self._previous_era()) if self._previous_era() else "",
            "forward_eras": self._era_options(self._forward_era_ids()),
            "era_options": self._era_options(self._all_other_eras()),
            "era_names": {e: device.era_label(e) for e in playable_eras()},
            "all_eras": self._era_options(self._all_other_eras()),
            "places": self._place_options() if abilities["place"] else {},
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
        self.device_powers = device.powers(inventory)
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

    def _visited_pool(self, era: str) -> list[str]:
        """The places this save has already stood in, in an era -- the Vernier's whole menu.

        Read from the scene manifest (memory or disk); it never enters the prompt."""
        try:
            places = known_scene_places(self.base_dir / OUTPUT_DIR, self.active_name or "", era)
        except Exception:  # noqa: BLE001 - never fail a jump over the arrival point
            places = []
        return _arrival_candidates(places)

    def _place_options(self) -> dict:
        """{era id: [visited place phrase]} -- the Vernier's menu, per era."""
        return {era: self._visited_pool(era) for era in playable_eras()}

    def _pick_jump_arrival(self, era: str) -> str:
        """Where a jump lands: a visited place or one of the era's authored destinations.

        The pool is the deduped union of the places this save has visited and the era's
        authored destinations (cities, landmarks, regions), drawn uniformly -- a visited
        place is one entry like any other, with no preference for recency or repeat visits.
        The list never enters the prompt, so a larger pool costs nothing.
        """
        candidates: list[str] = []
        seen: set[str] = set()
        for phrase in self._visited_pool(era) + era_arrivals(era):
            key = phrase.lower()
            if key and key not in seen:
                seen.add(key)
                candidates.append(phrase)
        if candidates:
            return self._rng.choice(candidates)
        return pick_arrival(era, rng=self._rng)

    def _chosen_arrival(self, era: str, wanted: str = "") -> str:
        """The landing: a chosen place (the Vernier) when the save has visited it, else a draw."""
        for candidate in (wanted, self._aim_place):
            place = " ".join(str(candidate or "").split())
            if place and place in self._visited_pool(era):
                return place
        return self._pick_jump_arrival(era)

    def _reload_era_scene_state(self) -> None:
        """Re-read the places, cast and roles for the era the Traveller is now in."""
        try:
            places = known_scene_places(self.base_dir / OUTPUT_DIR, self.active_name or "",
                                        None if self.classic else self.era)
        except Exception:  # noqa: BLE001 - never fail a session over the place tree
            places = []
        self._primed = places
        self._dead_perm = {}
        for p in places:
            self._dead_perm[(str(p.get("era") or ""), _scene_key(p.get("place")))] = {
                str(n).strip().lower() for n in (p.get("dead") or []) if str(n or "").strip()}
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
        # Identity is learned from the era's places so the on-stage echo works from the
        # first tool call on. It is never primed.
        self._npc_people = {}
        for p in places:
            self._remember_people(p.get("main_npcs"))
        self._sync_dead_display()

    def _sync_dead_display(self) -> None:
        """Merge the manifest's permanent dead names with this visit's provisional ones
        into each primed place, for `format_known_places`."""
        for p in self._primed:
            key = (str(p.get("era") or ""), _scene_key(p.get("place")))
            dead = set(self._dead_perm.get(key, set())) | self._dead_provisional.get(key, set())
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
            self._pending_era_state = None
        remaining = self._jump_at_turn - self.turn_counter
        if remaining > 0:
            if remaining == WARNING_TURNS and not self._warned:
                self._warned = True
                # The GM hears it BEFORE the jump turn, so the age can be closed properly. A
                # re-warning REPLACES the earlier one: that text claimed this was the last turn
                # and the fight since made it false, so history holds exactly one claim. The
                # memory ask and the STATE ask ride the same turn.
                state_ask = ERA_STATE_ASK.format(era=self.era)
                self.messages = [m for m in self.messages
                                 if not (isinstance(m, dict)
                                         and m.get("content") in (DEVICE_WARNING, LEGEND_ASK,
                                                                 state_ask))]
                self.messages.append({"role": "system", "content": DEVICE_WARNING})
                self.messages.append({"role": "system", "content": LEGEND_ASK})
                self.messages.append({"role": "system", "content": state_ask})
                await self._emit({"type": "notice", "title": "The Device", "text": DEVICE_WARNING})
            return
        if battle:
            return  # belt and braces: a jump may never fire on a battle turn
        await self._jump()

    def _era_opening(self, resumed: bool = False) -> str:
        """The lines the GM is handed with an era: where the Traveller is in it, and that
        the kit is their own -- literal in every age. Written once, never always-on: it rides
        the ERA_FILE at session start and the jump message after that. A RESUMED session must
        not read as a new landing, or the GM skips the session-start sheet."""
        parts = []
        if resumed:
            at = f" You are already at {self.arrival}." if self.arrival else ""
            parts.append("SESSION RESUMED — the story continues from where it left off; this "
                         "is not a new landing and you do not open a new scene." + at)
        elif self.arrival:
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
        companions = self._companions_block(self.era)
        return f"{block}\n{companions}" if companions else block

    def _compact(self, just_left: str) -> None:
        """The era switch IS the compaction: prefix + what the ages remember + what is true
        now, and nothing behind it. This is what stops the context growing ~104 tokens per turn
        at the test cadence -- and the thing that saves the tokens is the legend, not a prose
        summary. The STATE pages are re-injected here: a return to an age must be handed what
        the Traveller was doing there, and compaction used to drop the notebook entirely. The
        character sheet is re-injected too (it is engine-owned and would otherwise be lost)."""
        head = [self.messages[0], {"role": "system", "content": render_era_index(self.era)}]
        self._era_index_at = 1
        self._places_at = None
        self._places_at = len(head)
        head.append({"role": "system", "content": self._places_body()})
        memory = self._memory_message(just_left)
        if memory:
            head.append({"role": "system", "content": memory})
        state = self._state_body()
        if state:
            head.append({"role": "system", "content": state})
        if self._sheet_block:
            head.append({"role": "system", "content": self._sheet_block})
        self.messages = head

    async def _ensure_legend(self) -> None:
        """Force the age's memory with quiet rounds (never shown as player prose).

        The legend is the only thing that survives the compaction, so it is worth a second
        attempt before giving up -- there is no engine-authored fallback."""
        for _ in range(2):
            if self._pending_legend:
                return
            await self._run_role(LEGEND_RECOVERY_NUDGE, "legend-fix", quiet=True)

    async def _close_and_jump(self, era: str, arrival: str = "") -> None:
        """A player-driven jump: one closing turn first, so the GM can say what the age keeps.

        An automatic jump is warned ahead and the memory line rides its last turn; a jump the
        Traveller triggers has no such turn, so the engine manufactures one. `arrival` is the
        place the Vernier aimed ("" for the usual draw). The STATE ask comes first, because the
        memory marker must stay the last thing in the turn.
        """
        ask = (ERA_STATE_ASK.format(era=self.era) + "\n\n"
               + DEVICE_CLOSES.format(name=device.era_label(era)))
        await self._run_turn(ask, "closing")
        await self._jump(to_era=era, arrival=arrival)

    async def _jump(self, to_era: str | None = None, arrival: str = "") -> None:
        """The Device fires: a new era, an aimed (or rolled) arrival, and the age left behind
        as one line. Whatever an era sees, it remembers. `to_era` is the destination the player
        aimed; without it the Device does not aim. `arrival` is a place the Vernier picked."""
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
        self.era = chosen if chosen in self._all_other_eras() else self._steered_era()
        self.arrival = self._chosen_arrival(self.era, arrival)
        self._reload_era_scene_state()
        self._current_place = None  # a new age, a new place: the caption resets below
        # `_compact` throws the sheet away with the rest, so rebuild it from the live DB first.
        self._sheet_block = await self._fetch_sheet()
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
        self._steer = ""
        self._aim_place = ""
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
        text = _clean_pause_tokens(await self._plain_summary(prompt) or "")
        reply = text
        if self.classic:
            state, text = _extract_state(reply)
            if state:
                self._state = _clamp_block(state, MAX_STATE_CHARS)
        else:
            pages, text = _extract_states(reply, [self.era])
            await self._apply_state_page(self.era, pages.get(self.era, ""), reply, "Goal")
        entry = text.strip()
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
        text = _clean_pause_tokens(await self._plain_call(
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

    def _capture_state_pages(self, text: str) -> str:
        """Capture a STATE page the GM wrote into a narrative turn and strip it, so it can
        never reach the client. Only the departure ask writes one there: the age being left."""
        if self.classic:
            return text
        pages, cleaned = _extract_states(text, [self.era])
        if pages.get(self.era):
            self._pending_era_state = (self.era,
                                       _clamp_block(pages[self.era], MAX_STATE_CHARS))
        return cleaned

    def _commit_departure_state(self, old: str) -> None:
        """Store the page captured on the closing turn for the age being left. A miss keeps
        the page already held (the next Save in that age would rewrite it anyway)."""
        if self._pending_era_state and self._pending_era_state[0] == old:
            page = (self._pending_era_state[1] or "").strip()
            if page:
                self._era_states[old] = _clamp_block(page, MAX_STATE_CHARS)
        self._pending_era_state = None

    def _companions_block(self, era: str) -> str:
        """The companions of this world, for the sheet: name, identity and the stable look.

        A companion cannot travel in time, so the block is era-scoped. The description (the
        look) rides along so the GM keeps the same person across saves and ages. Combat data
        (stats, attacks, AC, HP) is never shown -- the engine fills the block in a fight."""
        lines = []
        for p in self._companions.get(era) or []:
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
        return _clean_pause_tokens(await self._plain_call(
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
        if len(candidate) <= MAX_TIMELINE_CHARS and pending_count <= TIMELINE_MAX_ENTRIES:
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
        if not (session_text or entry or self._legends or self._state
                or self._era_states or self._chronicle):
            return  # nothing to record yet: do not create an empty timeline
        blocks = [_TIMELINE_HEADER]
        if self._legends:
            blocks.append("\n".join(f"<!-- legend:{era} --> {text}"
                                    for era, text in self._legends.items()))
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
        # A departure STATE page rides the closing turn the same way, and is stripped here
        # rather than at the jump so it can never reach the client.
        content = self._capture_state_pages(content)
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
            self._track_companion(args, text)
        if name == PLACE_TOOL and not is_error:
            self._record_place_live(args)
        gm_text = _gm_tool_view(name, text)
        notes: list[str] = []
        if name == SCENE_TOOL and warning is None:
            notes = [self._undeclared_note(args), self._on_stage_note(args)]
        if name in (SCENE_TOOL, NPC_TOOL, PLACE_TOOL):
            notes.append(self._missing_identity_note(args))
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
            self._rearm_cadence()
            await self._emit({"type": "cadence", **self._device_state()})

    def _rearm_cadence(self) -> None:
        """Re-derive the countdown from the live part set, without resetting a ticking one.

        A complete Device (all five parts, no cadence band), a held Mainspring, or a Device
        that is gone has no cadence at all, so an armed countdown is cleared -- this is what
        makes a Device finished in the developer panel stop firing on its own. A change that
        merely re-bands an already ticking countdown leaves it alone, and a Device that starts
        ticking again (the parts were stripped) arms a fresh one from the current turn."""
        span = self._cadence_span()
        if span is None:
            self._jump_at_turn = None
            self._warned = False
        elif self._jump_at_turn is None:
            self._jump_at_turn = self.turn_counter + span

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
            # Main NPCs of the current place: a name at 0 HP is marked dead for this visit.
            self._track_main_npc_deaths(summary)
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

    def _track_companion(self, args: dict, result_text: str) -> None:
        """Keep the engine's own companion list in step with `register_companion`.

        Parsed from the tool result, not the arguments, so a refused declaration (the era's
        cap) never lists a companion the store did not keep. The identity (`race`/`class`) and
        the look (`description`) come from the declare arguments, for the injected sheet."""
        try:
            result = json.loads(result_text) if result_text else {}
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(result, dict):
            return
        era = str(result.get("era") or "").strip().lower()
        name = " ".join(str(result.get("name") or "").split())
        if not name:
            return
        low = name.lower()
        people = self._companions.get(era, [])
        if str(result.get("status") or "") in ("forgotten", "absent"):
            self._companions[era] = [p for p in people if str(p.get("name") or "").lower() != low]
        elif result.get("success"):
            sheet = args.get("sheet") if isinstance(args.get("sheet"), dict) else {}
            entry = {"name": name,
                     "race": " ".join(str(sheet.get("race") or "").split()),
                     "class": " ".join(str(sheet.get("class") or "").split()),
                     "description": " ".join(str(sheet.get("description") or "").split())}
            kept = [p for p in people if str(p.get("name") or "").lower() != low]
            kept.append(entry)
            self._companions[era] = kept

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
            self._cast_names.add(name.lower())
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
                    self._cast_names.add(name.lower())

    def _remember_people(self, entries) -> None:
        """Remember declared NPC identity (role, race, class), by lowercased name.

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
                self._npc_people[name.lower()] = (name, role, race, klass)

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
        self._remember_people(main)
        self._remember_people(cast)
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
                                 "main_npcs": people, "dead": [], "used": 0})
        else:
            live["kingdom"] = kingdom or live.get("kingdom", "")
            live["area"] = area or live.get("area", "")
            if people:
                live["main_npcs"] = people
                # A re-declaration must not wipe liveness: keep the dead names still declared.
                declared = {str(npc.get("name") or "").strip().lower() for npc in people}
                live["dead"] = [n for n in (live.get("dead") or []) if str(n).lower() in declared]
                self._dead_perm[(era, key)] = {n for n in self._dead_perm.get((era, key), set())
                                               if n in declared}
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
            key_l = " ".join(str(key or "").split()).lower()
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
