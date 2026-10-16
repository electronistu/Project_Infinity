import re


# The KNOWN PLACES block: a header + the CURRENT area's place tree (names + main NPCs, never
# the descriptions), the current place marked, and one line naming the other areas. Built by
# `places_feed`, which `tools/token_budget.py` calls as well, so the count cannot drift.
# Kept terse on purpose: it sits in the always-on prefix and is rewritten in place on a move.
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

# The place list is scoped to the CURRENT area and capped: the block is a reminder, not the
# record. Past the cap the GM still has `lookup` and the manifest.
PLACE_TREE_CAP = 12
# ... and a place's NPC names are capped too, so one busy place cannot blow the ceiling.
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


def resolve_place_path(places: list[dict], place) -> list[str] | None:
    """The registry's own path for a declared one, when the match is unambiguous.

    Case, spacing and punctuation are already forgiven by the slug; this adds the two drifts a
    live model actually produced: a dropped article (`the Western Market` vs `Western Market`),
    and a known place appearing as the TAIL of a longer declared path (the model nesting a
    place the save holds under a parent it invented). Returns None when nothing matches, or
    when two known places match equally -- a genuinely new place is never merged into one.
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


def _area_anchors(places: list[dict], cap: int = AREA_INDEX_CAP) -> list[str]:
    """Every area the save has stood in, each with its newest place as an exact path to reuse.

    A move into another area needs the place NAMES, not just the area name: given only
    "Wapping", the live model invented a path for a place the save already held. One anchor
    per area (`AREA_INDEX_CAP` of them) restores exact reuse without the whole-era tree.
    """
    newest: dict[str, tuple[int, str]] = {}
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
            newest[label] = (used, " — ".join(path))
    ordered = sorted(newest.items(), key=lambda kv: (-kv[1][0], kv[0]))[:cap]
    return [f"- {label}: {anchor}" for label, (_used, anchor) in ordered]


def _other_areas(places: list[dict], kingdom: str, area: str,
                 cap: int = AREA_INDEX_CAP) -> list[str]:
    """`_area_anchors` for the areas other than the one the GM is in."""
    return _area_anchors([p for p in places if not _in_area(p, kingdom, area)], cap)


def format_known_places(places: list[dict], cap: int = NPC_NAMES_PER_PLACE, here=()) -> str:
    """A kingdom -> area -> place-path tree for the primed block (any depth).

    Names + at most `cap` main NPC names per place: the full establishing description is
    deliberately omitted (it lives in the manifest, which the image pipeline reads), and
    the NPC *roles* are dropped -- a role parenthetical costs ~3.5 tokens and cannot fit a
    12-place tree inside the budget. A place with more names than `cap` says so
    with `+N`, so the GM knows the place is not empty. `here` is the current place path: the
    line it names is marked, so a drift is visible to the GM instead of silent."""
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
        return "\n".join([header, *_area_anchors(places), KNOWN_PLACES_CHOOSE])
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
