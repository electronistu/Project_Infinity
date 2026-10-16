import json

from .eras import START_ERA, era_ids
from .protocol import MODE_CLASSIC, MODE_TIME_TRAVELER, MODES


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


def _player_arrival(player_path: str, era: str) -> str:
    """The saved arrival string -- the last place the GM declared -- or '' for a fresh save."""
    try:
        with open(player_path, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("arrival", "") or "").strip()
    except (OSError, ValueError, TypeError):
        return ""


def _split_journey(value) -> list[str]:
    """The eras a save has stood in, oldest first (stored as a comma-joined string)."""
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    return [p.strip().lower() for p in str(value or "").split(",") if p.strip()]


def _jump_reset_note(raw: str) -> str:
    """A one-line note on what a Device crossing ended and what it left, from the reset tool."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    gone = [str(x) for x in list(data.get("effects_cleared") or [])
            + list(data.get("conditions_cleared") or [])]
    left = [str(x) for x in list(data.get("effects_kept") or [])
            + list(data.get("conditions_kept") or [])]
    bits = []
    if gone:
        bits.append("The crossing ends the spells on you: " + ", ".join(gone) + ".")
    if left:
        bits.append("What was laid on you remains: " + ", ".join(left) + ".")
    return " ".join(bits)
