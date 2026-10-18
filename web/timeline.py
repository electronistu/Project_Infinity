"""The save's timeline file: legends, STATE pages, the chronicle and turn entries."""

import os
import re

from .places import name_key


# ── the save's timeline ─────────────────────────────────────────────
# One file holds blocks with different lifetimes, each marked so it can be parsed back:
#   `<!-- legend:era -->`   what an age remembers (diegetic, per-era)
#   `<!-- met:era -->`      everyone the Traveller met there, names only, newest recorded first
#   `<!-- state:key -->`     what is TRUE NOW: one page per age. A classic save keeps the
#                           single unkeyed `<!-- state -->`. Rewritten every Save.
#   `<!-- chronicle:A-B -->` old events collapsed into prose, at compaction
#   `## Turns X-Y ...`       recent events, kept verbatim
_TIMELINE_HEADER = "# Session Timeline"
_LEGEND_RE = re.compile(r"(?m)^<!-- legend:([a-z0-9_-]+) -->[ \t]*(.*?)[ \t]*$")
_MET_RE = re.compile(r"(?m)^<!-- met:([a-z0-9_-]+) -->[ \t]*(.*?)[ \t]*$")
_CHRONICLE_RE = re.compile(
    r"(?ms)^<!-- chronicle:(?P<a>\d+)-(?P<b>\d+) -->[ \t]*\n(?P<body>.*?)^<!-- /chronicle -->[ \t]*$")
_STATE_KEYED_RE = re.compile(
    r"(?ms)^<!-- state:(?P<key>[a-z0-9_-]+) -->[ \t]*\n(?P<body>.*?)^<!-- /state:(?P=key) -->[ \t]*$")
_STATE_LEGACY_RE = re.compile(
    r"(?ms)^<!-- state -->[ \t]*\n(?P<body>.*?)^<!-- /state -->[ \t]*$")
# The met register: the names of everyone the Traveller met in an age, names only. The age's
# legend keeps the story; this keeps the roll. The INJECTED block is bounded like the legend,
# the stored roll is larger, so a lossy rewrite cannot silently shrink it.
MAX_MET_CHARS = 1200
MAX_MET_STORE = 400
MAX_MET_NAME_CHARS = 60


def parse_met_names(body) -> list[str]:
    """The names in a `{{met: ...}}` payload or a stored met line: split, clean, bound.

    Names are separated by commas, semicolons or newlines. A chunk that is empty, carries a
    placeholder bracket, or is longer than a name (or than six words) is rejected, so a stray
    sentence never becomes a person."""
    out: list[str] = []
    for chunk in re.split(r"[,;\n]+", str(body or "")):
        name = " ".join(chunk.replace("*", " ").strip(" \t-–—").split())
        if not name or "<" in name or ">" in name:
            continue
        if len(name) > MAX_MET_NAME_CHARS or len(name.split()) > 6:
            continue
        out.append(name)
    return out


def met_key(name) -> str:
    """A met-roll identity: `name_key` with a leading article dropped.

    The roll is append-only, so a new batch may repeat an old person under a drifted spelling
    (`Knife-man` for `the knife-man`). Article-insensitive dedup keeps one entry; the spelling
    already recorded is the one kept. Registry identity (places, cast) keeps `name_key`."""
    key = name_key(name)
    for article in ("the ", "a ", "an "):
        if key.startswith(article):
            return key[len(article):]
    return key


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
    """The kept (recent) session entries: the header, the legends, the met rolls, the STATE
    pages and the chronicle are stripped. This is what `_write_timeline` rebuilds from, and
    what turn numbering reads."""
    text = _LEGEND_RE.sub("", _read_timeline_file(timeline_path))
    text = _MET_RE.sub("", text)
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


def load_met(timeline_path) -> dict:
    """The met register per age: `{era: [names]}` in recorded order, newest first.

    Names only, deduplicated article/case/spacing-insensitively (`met_key`). A stored roll is
    bounded by `MAX_MET_STORE`; the INJECTED block has its own, smaller bound."""
    out: dict = {}
    for era, body in _MET_RE.findall(_read_timeline_file(timeline_path)):
        key = str(era or "").strip().lower()
        if not key:
            continue
        names: list[str] = []
        seen: set[str] = set()
        for name in parse_met_names(body):
            ident = met_key(name)
            if ident and ident not in seen:
                seen.add(ident)
                names.append(name)
        if names:
            out.setdefault(key, names[:MAX_MET_STORE])
    return out


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


MAX_TIMELINE_CHARS = 4500   # the injected body: STATE page + chronicle + kept entries
TIMELINE_MAX_ENTRIES = 6    # always compact past this many entries
