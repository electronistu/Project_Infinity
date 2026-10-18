import re


# The KNOWN PLACES block: a header + the CURRENT area's place tree (paths + each shown
# regular's identity and state, never the descriptions), the current place marked, and one
# line naming the other areas. Built by `places_feed`, which `tools/token_budget.py` calls as
# well, so the count cannot drift.
# Kept terse on purpose: it sits in the always-on prefix and is rewritten in place on a move.
# The tail of both headers is the one place the on-demand map is announced: the block is
# capped, and this is where the GM finds itself looking when it needs a path it cannot see.
# Said once here -- never again in the `lookup` row (one home, one phrasing).
_LOOKUP_POINTER = "; anything not shown here: `lookup('places')`"
KNOWN_PLACES_HEADER = (
    "KNOWN PLACES (reuse these exact kingdom / area / place paths; reuse a place's "
    "NPCs by NAME and their recorded role / race / class, never their look; a new place "
    "is seeded once with `establishing`" + _LOOKUP_POINTER + "):"
)
# The images-off variant: the same tree, but a new place is declared with `note_place`
# (no seed, no establishing description).
KNOWN_PLACES_HEADER_TEXT = (
    "KNOWN PLACES (reuse these exact kingdom / area / place paths; reuse a place's "
    "NPCs by NAME and their recorded role / race / class, never their look; a new place "
    "is declared once with `note_place`" + _LOOKUP_POINTER + "):"
)

# The place list is scoped to the CURRENT area and capped: the block is a reminder, not the
# record. Past the cap the GM still has `lookup` and the manifest.
PLACE_TREE_CAP = 12
# ... and a place's regulars are capped too, so one busy place cannot blow the ceiling. The
# identity is shown for the first `cap`; `+N` says the rest are in the on-demand record.
NPC_NAMES_PER_PLACE = 2
# The other areas the save has stood in cost one line each, so the GM can name a place in one
# exactly when the story returns there.
AREA_INDEX_CAP = 6

KNOWN_PLACES_EMPTY_TEXT = (
    "KNOWN PLACES: none recorded yet. On entering a place, declare its "
    "`kingdom`, `area` and full `place` path once with `note_place`."
)
KNOWN_PLACES_EMPTY = (
    "KNOWN PLACES: none recorded yet. On entering a place, declare "
    "its `kingdom`, `area` and `place` path and request its seed with "
    "`establishing` (a short, people-free description) together with the "
    "action image."
)
# The block's optional tail: the other areas the save has stood in, each with one exact place
# path to reuse, and the one case where there is no current place -- a landing to choose.
KNOWN_PLACES_OTHER_AREAS = "OTHER AREAS YOU KNOW (declare one of these places to move there):"
# A crowded area (more places than the cap) is sub-scoped by the first path segment -- the
# ward / building every place is filed under -- and the unseen groups plus the other areas
# share one anchor budget.
KNOWN_PLACES_ELSEWHERE = ("OTHER PLACES YOU KNOW (declare one of these exact paths to move "
                          "there):")
KNOWN_PLACES_CHOOSE = ("NO PLACE YET — choose where the Traveller lands, or reuse one of the "
                       "places above, declaring its exact kingdom / area / place path.")


def _slug_segment(text) -> str:
    """One path segment as a slug: lowercase, spacing/punctuation collapsed to dashes."""
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def _scene_key(place) -> tuple[str, ...]:
    """Slug path identifying a place (deepest segment last)."""
    if isinstance(place, str):
        place = [place]
    if not isinstance(place, (list, tuple)):
        return ()
    return tuple(_slug_segment(p) for p in place if str(p or "").strip())


# Articles carry no identity in a place name: `the Western Market` and `Western Market` are
# one place. Live, the model dropped the article off a path it could read exactly, and the
# engine filed that place a second time.
_ARTICLES = ("the", "a", "an")


def _place_identity(segment) -> str:
    """A segment's forgiving identity: the slug with a leading article dropped."""
    slug = _slug_segment(segment)
    for article in _ARTICLES:
        if slug.startswith(article + "-"):
            return slug[len(article) + 1:]
    return slug


def _identity_path(place) -> tuple[str, ...]:
    """A declared path as forgiving identity segments."""
    if isinstance(place, str):
        place = [place]
    if not isinstance(place, (list, tuple)):
        return ()
    return tuple(_place_identity(p) for p in place if str(p or "").strip())


def path_label(place) -> str:
    """A path as one label: `district — room` (the caption and the tool notes use this shape)."""
    if isinstance(place, str):
        return place.strip()
    return " — ".join(str(s).strip() for s in (place or []) if str(s or "").strip())


def _path_of(place: dict) -> list[str]:
    """A registry entry's path, trimmed."""
    return [str(s).strip() for s in (place.get("place") or []) if str(s or "").strip()]


def path_group(path) -> str:
    """The top-level group a path sits in: the first segment's identity, or "".

    The tool contract already mandates at least two entries (`[district/neighbourhood, the
    exact building/room, ...]`), so the first segment is the ward / top-level place a crowd
    can be split by. A one-segment legacy path has no group and stays in the area's own cap.
    """
    path = [str(p).strip() for p in (path or []) if str(p or "").strip()]
    return _place_identity(path[0]) if len(path) >= 2 else ""


def place_group(place: dict) -> str:
    """`path_group` for a registry entry."""
    return path_group(_path_of(place))


def group_identity(label) -> str:
    """A ward's display label as its matching key (`Ward A` == `the ward a`)."""
    return _place_identity(label)


def _place_anchor(place: dict) -> str:
    """One hidden group's newest place as an exact reusable line."""
    path = _path_of(place)
    if len(path) >= 2:
        return "- " + path[0] + ": " + " — ".join(path[1:])
    return "- " + " — ".join(path)


def resolve_place_path(places: list[dict], place, current: dict | None = None
                       ) -> list[str] | None:
    """The registry's own path for a declared one, when the match is unambiguous.

    Case, spacing and punctuation are already forgiven by the slug; this adds the three drifts
    a live model actually produced: a dropped article (`the Western Market` vs `Western
    Market`), a known place appearing as the TAIL of a longer declared path (the model nesting
    a place the save holds under a parent it invented), and -- since a declaration may name
    only where the story IS (`note_place` echoes the place every turn) -- the mirror of that
    drift, a declared path that is the tail of a known longer one (`the guard room` for `the
    princely court / the guard room`).

    A bare leaf can name a place in several settlements, so `current` (the engine's own idea
    of where the story stands) decides when exactly one of the matches lies in that area.
    Returns None when nothing matches, or when two known places match equally -- a genuinely
    new place is never merged into one.
    """
    want = _identity_path(place)
    if not want:
        return None
    known = [(p, _identity_path(p.get("place"))) for p in places if p.get("place")]
    whole = [p for p, keys in known if keys == want]
    if len(whole) == 1:
        return _path_of(whole[0])
    if len(whole) > 1:
        return None
    tails = [p for p, keys in known
             if keys and len(keys) < len(want) and want[-len(keys):] == keys]
    if len(tails) == 1:
        return _path_of(tails[0])
    if len(tails) > 1:
        return None
    heads = [p for p, keys in known
             if keys and len(keys) > len(want) and keys[-len(want):] == want]
    if len(heads) == 1:
        return _path_of(heads[0])
    if len(heads) > 1:
        here = (str((current or {}).get("kingdom") or ""), str((current or {}).get("area") or ""))
        local = [p for p in heads if _in_area(p, here[0], here[1])]
        if len(local) == 1:
            return _path_of(local[0])
    return None


# A character's name as ONE identity key: case, spacing and the model's own separators
# folded (`Master_Guo`, `Master-Guo` and `Master Guo` are one person). Live 2026-10-16 the
# scene call keyed two declared names with an underscore, and the declaration check -- an
# exact `name.lower()` -- told the GM, in the result of the same call that declared them,
# that those names were undeclared: the on-stage echo then stayed silent for them too.
_NAME_SEPARATORS = re.compile(r"[_\-.]+")


def name_key(value) -> str:
    """A declared character name's identity key (case/separators/spacing forgiven)."""
    return " ".join(_NAME_SEPARATORS.sub(" ", str(value or "")).split()).lower()


# A regular's state is the GM's word for what happened to them ("dead", "petrified",
# "gone"). One short word, because it rides the per-turn block; `dead` is the one value the
# engine consumes (it feeds the death index), every other value is the fiction's.
STATE_MAX = 24


def _npc_state(value) -> str:
    """A declared state, trimmed and capped for the block's budget."""
    return " ".join(str(value or "").split())[:STATE_MAX].strip()


def _people_list(value) -> list[dict]:
    """Normalize a declared people list to the seed shape ({name, description, role, race,
    class, state}). The look is kept: a scene call's `main_npcs` carry it (the image prompt
    needs it), while `note_place` carries none and gets an empty description."""
    out: list[dict] = []
    for entry in value or []:
        if not isinstance(entry, dict):
            continue
        name = " ".join(str(entry.get("name") or "").split())
        if name:
            out.append({"name": name,
                        "description": " ".join(str(entry.get("description") or "").split()),
                        "role": " ".join(str(entry.get("role") or "").split()),
                        "race": " ".join(str(entry.get("race") or "").split()),
                        "class": " ".join(str(entry.get("class") or "").split()),
                        "state": _npc_state(entry.get("state"))})
    return out


def _area_order(place: dict):
    """Tree order: kingdom, then area, then the place path."""
    return (str(place.get("kingdom") or "").lower(), str(place.get("area") or "").lower(),
            [s.lower() for s in (place.get("place") or [])])


def _in_area(place: dict, kingdom: str, area: str) -> bool:
    """Whether a place sits in this area. A missing kingdom matches any kingdom."""
    want_area = str(area or "").strip().lower()
    want_kingdom = str(kingdom or "").strip().lower()
    if want_area and str(place.get("area") or "").strip().lower() != want_area:
        return False
    if want_kingdom and str(place.get("kingdom") or "").strip().lower() != want_kingdom:
        return False
    return True


def _places_for_area(places: list[dict], kingdom: str, area: str,
                     cap: int = PLACE_TREE_CAP) -> list[dict]:
    """The current area's places: the `cap` most recently used, back in tree order.

    Per AREA, not per era: a settlement walked all over cannot crowd the place the story
    stands in out of the block the GM reads -- the same bound the player's place picker uses
    (`PLACE_MENU_RECENT`). The record is still the manifest, and `_match_known_place`
    deliberately keeps reading the whole list.
    """
    recent = sorted((p for p in places if _in_area(p, kingdom, area)),
                    key=lambda p: (-int(p.get("used") or 0), *_area_order(p)))
    return sorted(recent[:cap], key=_area_order)


def _area_anchor_entries(places: list[dict], cap: int = AREA_INDEX_CAP
                         ) -> tuple[list[str], int, list[dict]]:
    """Every area the save has stood in, each with its newest place as an exact path to reuse.

    A move into another area needs the place NAMES, not just the area name: given only
    "Wapping", the live model invented a path for a place the save already held. One anchor
    per area (`AREA_INDEX_CAP` of them) restores exact reuse without the whole-era tree.

    Returns the lines, the number of areas the cap hid, and the chosen entries (newest per
    area) so a caller can count what the lines actually name. Past the cap the block SAYS so
    and names the on-demand map: an area the GM cannot see must never become an area it
    forgets (or invents a place in)."""
    newest: dict[str, tuple[int, str, dict]] = {}
    for p in places:
        path = _path_of(p)
        if not path:
            continue
        k = str(p.get("kingdom") or "").strip() or "(unknown kingdom)"
        a = str(p.get("area") or "").strip() or "(unknown settlement)"
        label = f"{k} — {a}"
        used = int(p.get("used") or 0)
        best = newest.get(label)
        if best is None or used > best[0]:
            newest[label] = (used, " — ".join(path), p)
    ordered = sorted(newest.items(), key=lambda kv: (-kv[1][0], kv[0]))
    shown, hidden = ordered[:cap], ordered[cap:]
    lines = [f"- {label}: {anchor}" for label, (_used, anchor, _entry) in shown]
    if hidden:
        lines.append(f"- +{len(hidden)} more areas you have stood in — `lookup('places')` "
                     "lists them.")
    return lines, len(hidden), [entry for _label, (_used, _anchor, entry) in shown]


def _area_anchors(places: list[dict], cap: int = AREA_INDEX_CAP) -> tuple[list[str], int]:
    """`_area_anchor_entries` without the entries (the non-crowded block's shape)."""
    lines, hidden, _entries = _area_anchor_entries(places, cap)
    return lines, hidden


def _other_areas(places: list[dict], kingdom: str, area: str,
                 cap: int = AREA_INDEX_CAP) -> list[str]:
    """`_area_anchors` for the areas other than the one the GM is in."""
    return _area_anchors([p for p in places if not _in_area(p, kingdom, area)], cap)[0]


def _npc_label(npc: dict, dead: set[str], missing: str = "") -> str:
    """One regular as `Name — role, race class (state)`.

    The engine's permanent death index wins; any other state is the GM's word and renders as
    given. `missing` is the fallback for a regular with no recorded identity (the on-demand
    record says `no role recorded`; the per-turn tree stays quiet instead of spending tokens
    on a placeholder)."""
    name = str(npc.get("name") or "").strip()
    if not name:
        return ""
    role = str(npc.get("role") or "").strip()
    people = " ".join(x for x in (str(npc.get("race") or "").strip(),
                                  str(npc.get("class") or "").strip()) if x)
    what = ", ".join(x for x in (role, people) if x)
    if what:
        label = f"{name} — {what}"
    elif missing:
        label = f"{name} — {missing}"
    else:
        label = name
    state = "dead" if name.lower() in dead else str(npc.get("state") or "").strip()
    return f"{label} ({state})" if state else label


def format_known_places(places: list[dict], cap: int = NPC_NAMES_PER_PLACE, here=()) -> str:
    """A kingdom -> area -> place-path tree for the primed block (any depth).

    Each place is followed by its first `cap` regulars as `Name — role, race class (state)`;
    the full establishing description is deliberately omitted (it lives in the manifest, which
    the image pipeline reads) and a look is never shown. A place with more regulars than `cap`
    says so with `+N`: the rest are in `lookup('places')`, which is uncapped. `here` is the
    current place path: the line it names is marked, so a drift is visible to the GM instead
    of silent."""
    here_key = _scene_key(here) if here else ()
    lines: list[str] = []
    last_k = last_a = None
    last_path: list[str] = []
    for p in places:
        path = _path_of(p)
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
        if here_key and _scene_key(path) == here_key:
            lines[-1] += " · ← you are here"
        dead = {str(n).strip().lower() for n in (p.get("dead") or []) if str(n or "").strip()}
        labels = [label for npc in (p.get("main_npcs") or [])
                  if isinstance(npc, dict) and (label := _npc_label(npc, dead))]
        npc_labels = labels[:cap]
        if len(labels) > cap:
            npc_labels.append(f"+{len(labels) - cap}")
        if npc_labels:
            label = "NPC" if len(npc_labels) == 1 else "NPCs"
            lines[-1] += f" · {label}: " + ", ".join(npc_labels)
    return "\n".join(lines)


# ── the on-demand record: `lookup('places')` / `lookup('places: X')` ─────────────
#
# The block above is a REMINDER, not the record: it shows the current area's places (capped
# at the picker's own bound) and one anchor per other area. Everything it cannot hold -- the
# places of a settlement the story is not in, the areas past its anchor cap, a regular's
# role/race/class, which names are dead -- is served on demand from the same live tree the
# block is built from, so the GM never has to invent a path it cannot see. Same registry,
# same spellings: the block and the answer cannot disagree.
#
# The key is strict -- the bare word, or `places: <name>` -- because the same tool serves era
# ids and polity names (rules/tools_player.py). No era id and no polity starts with "places",
# so the two cannot be confused.
PLACES_QUERY = "places"
PLACES_MAP_HEADER = (
    "PLACES YOU KNOW (ask `lookup('places: <settlement>')` for one settlement's places, or "
    "`lookup('places: <kingdom>')` for a whole kingdom):"
)


def places_query(query) -> tuple[str, str] | None:
    """A `lookup` query as `("map", "")` or `("scope", "<name>")`, else None.

    `places` alone is the map; anything after it (a colon or a space, both tolerated) scopes
    the answer to that kingdom or settlement. An empty scope is the map."""
    text = " ".join(str(query or "").split())
    head, _, rest = text.partition(" ")
    if head.rstrip(":").lower() != PLACES_QUERY:
        return None
    arg = " ".join(rest.split()).lstrip(":").strip().strip("\"'").strip()
    return ("scope", arg) if arg else ("map", "")


def _kingdom_label(place: dict) -> str:
    return str(place.get("kingdom") or "").strip() or "(unknown kingdom)"


def _area_label(place: dict) -> str:
    return str(place.get("area") or "").strip() or "(unknown settlement)"


def _used(place: dict) -> int:
    return int(place.get("used") or 0)


def _area_groups(places: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """The registry grouped by (kingdom, area). A place with no path is not a place."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in places:
        if isinstance(p, dict) and _path_of(p):
            groups.setdefault((_kingdom_label(p), _area_label(p)), []).append(p)
    return groups


def _areas_by_recency(groups: dict) -> list[tuple[str, str]]:
    """The areas, the one the story stood in most recently first (name breaks ties)."""
    newest = {k: max(_used(p) for p in items) for k, items in groups.items()}
    return sorted(newest, key=lambda k: (-newest[k], k))


def _regulars(place: dict) -> list[str]:
    """A place's regulars as `Name — role, race class (state)`, in the record.

    The same shape the per-turn tree uses, uncapped: the record is where a regular past the
    tree's cap, or with an identity changed this turn, is read in full."""
    dead = {str(n).strip().lower() for n in (place.get("dead") or []) if str(n or "").strip()}
    return [label for npc in (place.get("main_npcs") or [])
            if isinstance(npc, dict)
            and (label := _npc_label(npc, dead, missing="no role recorded"))]


def _place_lines(places: list[dict], here=(), indent: str = "") -> list[str]:
    """A place's exact path, the current one marked, then one line per regular.

    Tree order, so the nested rooms of one building stay together. Nothing is capped: the
    reason to ask is to see the record."""
    here_key = _scene_key(here) if here else ()
    lines: list[str] = []
    for p in sorted(places, key=_area_order):
        path = _path_of(p)
        line = f"{indent}- {path_label(path)}"
        if here_key and _scene_key(path) == here_key:
            line += " · ← you are here"
        lines.append(line)
        lines.extend(f"{indent}    - NPC: {regular}" for regular in _regulars(p))
    return lines


def _map_lines(groups: dict, current: dict | None) -> list[str]:
    """Every kingdom, its settlements, and how many places each holds."""
    here_area = (str((current or {}).get("kingdom") or "").strip(),
                 str((current or {}).get("area") or "").strip())
    lines: list[str] = []
    for kingdom in sorted({k[0] for k in groups},
                          key=lambda name: (-max(_used(p) for k, items in groups.items()
                                                 if k[0] == name for p in items), name)):
        lines.append(f"- {kingdom}")
        for k in [k for k in _areas_by_recency(groups) if k[0] == kingdom]:
            n = len(groups[k])
            lines.append(f"    - {k[1]}: {n} place{'' if n == 1 else 's'}"
                         + (" · ← you are here" if k == here_area else ""))
    return lines


def _cast_footer(cast) -> list[str]:
    """The place-less recurring people, already formatted as `Name — identity`.

    The block never shows them; the on-demand record is their home. Labels are built by the
    engine (`_serve_places_lookup`) from the live identity map, so the record and the tree use
    the same shape."""
    labels = [str(n).strip() for n in (cast or []) if str(n or "").strip()]
    return [f"OTHER PEOPLE YOU KNOW: {', '.join(labels)}"] if labels else []


def render_places_answer(places: list[dict], current: dict | None, query, cast=()) -> str:
    """The on-demand place record, or "" when the query is not a place query.

    `places` = the map; `places: <name>` = a settlement's places, or a kingdom's settlements
    and their places -- both, labelled, when one name is a kingdom and a settlement here. A
    name that matches nothing returns the map, so a wrong guess teaches the right one instead
    of inventing anything. `cast` is the place-less recurring people, already formatted as
    `Name — role, race class`, shown as a footer."""
    parsed = places_query(query)
    if parsed is None:
        return ""
    kind, arg = parsed
    groups = _area_groups(places)
    if not groups:
        return "\n".join(["PLACES: none recorded yet.", *_cast_footer(cast)])
    if kind == "map" or not arg:
        return "\n".join([PLACES_MAP_HEADER, *_map_lines(groups, current),
                           *_cast_footer(cast)])
    want = _place_identity(arg)
    by_recency = _areas_by_recency(groups)
    kingdom_areas = [k for k in by_recency if _place_identity(k[0]) == want]
    area_keys = [k for k in by_recency if _place_identity(k[1]) == want]
    if not kingdom_areas and not area_keys:
        return "\n".join([f'PLACES: no kingdom or settlement here is called "{arg}" — you know:',
                          *_map_lines(groups, current), *_cast_footer(cast)])
    here_path = [str(x).strip() for x in ((current or {}).get("place") or [])]
    here_area = (str((current or {}).get("kingdom") or "").strip(),
                 str((current or {}).get("area") or "").strip())
    sections: list[str] = []
    if kingdom_areas:
        lines = [f'PLACES IN "{kingdom_areas[0][0]}" — reuse these exact kingdom / area / '
                 "place paths:"]
        for k in kingdom_areas:
            lines.append(f"- {k[1]}" + (" · ← you are here" if k == here_area else ""))
            lines.extend(_place_lines(groups[k], here_path, indent="    "))
        sections.append("\n".join(lines))
    if area_keys:
        lines = []
        for k in area_keys:
            lines.append(f'PLACES IN "{k[1]}" ({k[0]}) — reuse these exact kingdom / area / '
                         "place paths:")
            lines.extend(_place_lines(groups[k], here_path))
        sections.append("\n".join(lines))
    if len(sections) > 1:
        return "\n".join([f'"{arg}" is both a kingdom and a settlement here — both:',
                          *sections, *_cast_footer(cast)])
    return "\n".join([sections[0], *_cast_footer(cast)])


def _crowded_body(places: list[dict], kingdom: str, area: str, path: list[str]) -> list[str]:
    """The block for an area holding more places than the cap.

    The current top-level group (ward / building) is shown first and cannot be crowded out;
    the remaining slots go to the newest places elsewhere in the area. Every group with no
    shown place, and every other area, contributes one exact anchor path; the shared
    `AREA_INDEX_CAP` keeps the section bounded, and the `+N` line names the on-demand record
    for whatever neither the tree nor an anchor names. Nothing is dropped silently.
    """
    here_key = _scene_key(path)
    here_group = path_group(path)
    area_places = [p for p in places if _in_area(p, kingdom, area) and _path_of(p)]

    def rank(p):
        return (-int(p.get("used") or 0), *_area_order(p))

    own = sorted((p for p in area_places if place_group(p) == here_group), key=rank)
    shown = own[:PLACE_TREE_CAP]
    shown_keys = {_scene_key(p.get("place")) for p in shown}
    if here_key and here_key not in shown_keys:
        current = next((p for p in area_places
                        if _scene_key(p.get("place")) == here_key), None)
        if current is not None:
            shown = [current] + [p for p in shown
                                 if _scene_key(p.get("place")) != here_key][:PLACE_TREE_CAP - 1]
            shown_keys.add(here_key)
    for p in sorted((p for p in area_places if _scene_key(p.get("place")) not in shown_keys),
                    key=rank):
        if len(shown) >= PLACE_TREE_CAP:
            break
        shown.append(p)
        shown_keys.add(_scene_key(p.get("place")))
    shown = sorted(shown, key=_area_order)

    named = set(shown_keys)
    anchor_lines, _hidden_areas, anchor_entries = _area_anchor_entries(
        [p for p in places if not _in_area(p, kingdom, area)])
    anchors = anchor_lines[:len(anchor_entries)]
    for p in anchor_entries:
        named.add(_scene_key(p.get("place")))

    shown_groups = {place_group(p) for p in shown}
    hidden_groups: dict[str, dict] = {}
    for p in sorted(area_places, key=rank):
        group = place_group(p)
        if group in shown_groups:
            continue
        hidden_groups.setdefault(group, p)
    for p in hidden_groups.values():
        if len(anchors) >= AREA_INDEX_CAP:
            break
        anchors.append(_place_anchor(p))
        named.add(_scene_key(p.get("place")))

    hidden_places = [p for p in places
                     if _path_of(p) and _scene_key(p.get("place")) not in named]
    body: list[str] = []
    if shown:
        body.append(format_known_places(shown, here=path))
    else:
        body.append(f"- {kingdom or '(unknown kingdom)'}\n"
                    f"    - {area or '(unknown settlement)'}")
    if path and here_key not in shown_keys:
        body.append(f"{' ' * 8}- {path_label(path)} · ← you are here (not recorded yet)")
    if anchors:
        body.append("\n".join([KNOWN_PLACES_ELSEWHERE, *anchors]))
    if hidden_places:
        body.append(f"- +{len(hidden_places)} more places you have stood in — "
                    "`lookup('places')` lists them.")
    return body


def places_feed(places: list[dict], current: dict | None, scene_images: bool) -> str:
    """The KNOWN PLACES block: the place the GM is in, its area's places, and the other areas.

    Scoped to where the GM is, not to the whole era. A place the story stands in can never be
    evicted by a busier settlement, the block stays small on a long save, and the current
    line is marked -- so a GM that has drifted declares the real place instead of narrating on.

    Three shapes: the current area (its places, the marked line, the other areas); no current
    place yet, which is a landing the GM still has to choose (the areas alone); and nothing
    recorded at all.
    """
    header = KNOWN_PLACES_HEADER if scene_images else KNOWN_PLACES_HEADER_TEXT
    if not places:
        return KNOWN_PLACES_EMPTY if scene_images else KNOWN_PLACES_EMPTY_TEXT
    kingdom = str((current or {}).get("kingdom") or "").strip()
    area = str((current or {}).get("area") or "").strip()
    path = [str(p).strip() for p in ((current or {}).get("place") or []) if str(p or "").strip()]
    if not (kingdom or area):
        # A landing the GM still has to choose: every area, each with one exact place to reuse.
        anchors, _hidden = _area_anchors(places)
        return "\n".join([header, *anchors, KNOWN_PLACES_CHOOSE])
    area_places = [p for p in places if _in_area(p, kingdom, area)]
    if len(area_places) > PLACE_TREE_CAP:
        # A city that is an entire era (Chang'an) or a settlement walked all over: split by
        # its top-level places rather than dropping the 13th onward.
        return "\n".join([header, *_crowded_body(places, kingdom, area, path)])
    body: list[str] = []
    in_area = _places_for_area(places, kingdom, area)
    if in_area:
        body.append(format_known_places(in_area, here=path))
    else:
        # The area is known but holds no declared place: show the address it is being filed
        # under, so the GM reuses it.
        body.append(f"- {kingdom or '(unknown kingdom)'}\n"
                    f"    - {area or '(unknown settlement)'}")
    if path and not any(_scene_key(p.get("place")) == _scene_key(path) for p in in_area):
        body.append(f"{' ' * 8}- {path_label(path)} · ← you are here (not recorded yet)")
    others = _other_areas(places, kingdom, area)
    if others:
        body.append("\n".join([KNOWN_PLACES_OTHER_AREAS, *others]))
    return "\n".join([header, *body])
