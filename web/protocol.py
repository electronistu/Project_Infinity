import json
import re

from .tool_schema import compact_schema


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
# Declares a companion's durable character sheet (engine-owned, one party). The look in it
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

# Tools the GM never sees: called directly by the session for its own bookkeeping, filtered
# out of the GM's tool list (and out of the measured prefix -- see tools/token_budget.py).
# The `debug_*` tools are the developer's levers (dev mode only); the GM is never offered
# them, and they no-op unless the dice server was started with `--debug`.
ENGINE_ONLY_TOOLS = ("set_player_field", "dump_player_save_state",
                     "debug_set_field", "debug_device", "dump_player_db", "device_jump_reset")

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

# The Device is player-controlled from the first turn: it never fires on its own, so there
# is no cadence, no countdown and no warning. The four parts are the key to the future age.
# The place menu offers each age's last few visited places; there are no preset arrivals.
PLACE_MENU_RECENT = 12

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
    "{arrival}\n\n"
    "You still have everything you carried, exactly as it is: nothing is translated, "
    "nothing is left behind. This age has never seen its like, and people will notice.\n\n"
    "Your companions come with you -- the Device carries everyone travelling with you, exactly "
    "as they were. Whether each already knows what you are and what the Device can do, or is "
    "finding out now, is yours to play: recognition, terror, a debt called in. Improvise.\n\n"
    "Narrate the arrival -- the disorientation, what the place smells and sounds like at "
    "this hour, and whether anyone notices a stranger appear."
)

# Text mode only (no scene call carries the place): a landing must declare its place, reusing the
# known path when it already exists, so the engine pins the arrival to the place the GM used.
DEVICE_DECLARES = (
    "Declare the place you land in once with `note_place` -- if it is already among the KNOWN "
    "PLACES, reuse its exact `kingdom` / `area` / `place` path -- so the landing is recorded."
)

# A short, explicit situation line at the top of every model-facing turn, so the GM never has to
# guess from the timeline whether this is a new game, a resumed session, or a Device jump.
# Runtime text only -- never always-on.
SITUATION_NEW = ("SITUATION: SESSION START — a new Traveller; open the story with the opening "
                 "scene.")
SITUATION_RESUME = ("SITUATION: SESSION RESUMED — the story continues from the last beat; this is "
                    "not a new landing and not a new game.")
SITUATION_JUMP = ("SITUATION: DEVICE JUMP — the Traveller has crossed into {new}; the same story "
                  "continues. This is not a new game and not a session start.")
# Labels a jump's era scaffold so it cannot be read as the session's ERA_FILE.
JUMP_ERA_LABEL = "THE AGE YOU HAVE ENTERED (the era file):"

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
