import re


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
