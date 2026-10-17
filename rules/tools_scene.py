"""Scene MCP tools: the scene-image request, NPC registration and place notes."""

from .app import mcp

@mcp.tool()
def request_scene_image(description: str, kingdom: str = "", area: str = "",
                        place: list[str] | None = None,
                        time_of_day: str = "", weather: str = "",
                        characters: dict[str, str] | None = None,
                        establishing: str = "", main_npcs: list[dict] | None = None,
                        npcs: list[dict] | None = None,
                        seed_change: str = "", mood: str = "") -> dict:
    """Request the turn's storyline illustration.

    WHEN: every narrative turn -- exactly ONE, in the SAME response as the prose; it ENDS the turn.
    FIELDS:
    - description: what HAPPENS -- the protagonist's action and notable transient events. NAME every NPC; never identify anyone by look. Never describe a person's look, the place, its furniture, its light, the time or the weather. You MAY say where the protagonist stands or sits. DO restate anything still visible from earlier (spilled ale, blood, a body, an open door).
    - place: the ordered path of nested places BELOW the settlement, deepest last, at least 2 entries -- [district/neighbourhood, the exact building/room, (any room inside it, ...)]. A different room is a longer path. Reuse the exact path you have used; keep the levels distinct (a district is a `place` entry, never the `area`).
    - kingdom = the realm; area = the settlement (city/town); both only when creating the seed (with `establishing`), else reuse the exact known names.
    - time_of_day/weather: fed straight to the generator (e.g. 'dusk', 'heavy rain'); keep them out of `description`.
    - characters: dict of EVERY on-stage NPC -> their action toward the protagonist. Key = the declared NAME; never repeat a look; exact counts ('three dockhands'), never 'a few'; exclude the protagonist. A one-off extra needs no declaration (a short look in the key is used for that image only).
    - establishing: an empty, people-free, weather-free, timeless description of a NEW place (only when its seed does not exist yet).
    - main_npcs: the place's main NPCs, only when creating the seed, [{name, role, race, class, description}] -- one person per entry; role = their function; race + class = their people and calling (the twelve SRD classes, or a plain word when none fits); description = the stable physical look only.
    - npcs: recurring NPCs declared in this same call, [{name, race, class, description}]; afterwards refer to them by NAME ONLY.
    - seed_change: a PERMANENT change to the place regenerates its seed immediately.
    - mood: a short mood word for the light/atmosphere.

    PLACES: kingdom -> area -> place path (realm -> settlement -> district -> spot -> any nested rooms). Example: kingdom `Eldoria`, area `Eldoria City`, place `["Ropehaven Wharf", "Warehouse Nine", "the counting office"]`. On entering a NEW place (not in KNOWN PLACES), also pass `establishing` so the engine builds the hidden seed (never shown); every action image is drawn FRESH from that seed. A missing seed returns a WARNING -- call again with `establishing`.

    PLAYER: refer to them as 'the protagonist'; say where they are and what they do (facing the action, back to camera is fine), but NEVER describe their appearance -- the portrait is attached automatically. Describe clothing/armour/weapons ONLY from what they actually have equipped (check `_equipment`); never invent a hood, cloak, cowl, hat, helmet, armour or other item.

    Does not change game state; the engine does not wait for the picture.
    """
    return {
        "status": "requested",
        "kingdom": kingdom or "",
        "area": area or "",
        "place": place or [],
        "characters": len(characters) if isinstance(characters, dict) else 0,
        "main_npcs": len(main_npcs) if isinstance(main_npcs, list) else 0,
        "npcs": len(npcs) if isinstance(npcs, list) else 0,
        "seed_requested": bool(establishing or seed_change),
        "note": "The illustration will appear with your narrative.",
    }


@mcp.tool()
def register_npcs(npcs: list[dict]) -> dict:
    """Declare recurring storyline NPCs so the illustrator draws them identically every time.

    WHEN: a recurring character first appears.
    FIELDS:
    - npcs: list of {name, race, class, description}. name = the exact name you will keep using (a person's name or a stable handle like 'the harbourmaster'). race + class = their people and calling -- give BOTH for every declared character; the twelve SRD classes are the vocabulary, or a plain word when none fits. description = the stable PHYSICAL look only (gender, build, distinguishing features, clothing/role-defining gear) -- never a pose, a position or a current action, and never a second person.

    RULES:
    - Declare each recurring character ONCE; afterwards refer by NAME ONLY and never repeat the description.
    - Re-declaring a name updates its description.
    - Use request_scene_image's `npcs` field instead when you also request the illustration in the same call.
    """
    cleaned = []
    if isinstance(npcs, list):
        for entry in npcs:
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split())
            if name:
                cleaned.append(name)
    return {
        "status": "registered",
        "npcs": cleaned,
        "count": len(cleaned),
        "note": "The engine stores these descriptions; use the names alone from now on.",
    }


@mcp.tool()
def note_place(place: list[str], kingdom: str = "", area: str = "",
               main_npcs: list[dict] | None = None,
               cast: list[dict] | None = None) -> dict:
    """Remember a place (and its regulars) in the storyline when images are off.

    WHEN: whenever the story moves the Traveller to a place he is not already in -- a place
    that is new to the story, or a return to one already known -- and again when a place gains
    a regular. Not every turn: only when the story moves. The engine primes the tree (names
    only) each session, and pools these places with the ones it already knows.
    FIELDS:
    - place: ordered path BELOW the settlement, deepest last, at least 2 entries -- [district/neighbourhood, the exact building/room, (any room inside it, ...)]. Reuse the exact path once used.
    - kingdom = realm; area = settlement (city/town). A NEW place needs both; a place the record already holds may omit them (a room's own name is enough) -- the saved address and spelling are kept.
    - main_npcs: the place's OWN regulars, [{name, role, race, class}] -- one person per entry; give race and class for each.
    - cast: recurring people NOT tied to one place (a villain, a companion), [{name, role, race, class}].

    RULES:
    - Names + roles + races + classes only: never a look, a pose or a second person's details.
    - Re-declaring a name updates it; re-declaring a place says the story moved back there;
      reuse names and paths exactly.
    - Does not change game state; the engine remembers the place.
    """
    path = [" ".join(str(p).split()) for p in (place or []) if str(p or "").strip()]

    def _people(value):
        out = []
        for entry in (value or []):
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split())
            if name:
                out.append({"name": name,
                            "role": " ".join(str(entry.get("role") or "").split()),
                            "race": " ".join(str(entry.get("race") or "").split()),
                            "class": " ".join(str(entry.get("class") or "").split())})
        return out

    return {
        "status": "noted",
        "kingdom": " ".join(str(kingdom or "").split()),
        "area": " ".join(str(area or "").split()),
        "place": path,
        "main_npcs": _people(main_npcs),
        "cast": _people(cast),
        "note": "The place is remembered; reuse the exact path and names.",
    }
