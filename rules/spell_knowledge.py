"""Spell knowledge: the spellbook, prepared spells, castable lists and slot validation.

Which spells the player knows and can cast, the prepared-spell limits, and the slot-availability
check `modify_player_numeric` runs before spending a slot.
"""

import json

from .consts import PREPARED_CASTER_ABILITIES
from .db import _db_val, _effective_stats


def get_max_prepared_spells(cursor) -> int | None:
    cursor.execute("SELECT value FROM player WHERE key = ?", ("character_class",))
    row = cursor.fetchone()
    if not row:
        return None

    char_class = row[0]
    stat_key = PREPARED_CASTER_ABILITIES.get(char_class)
    if stat_key is None:
        return None

    cursor.execute("SELECT value FROM player WHERE key = ?", ("level",))
    level_row = cursor.fetchone()
    level = int(level_row[0]) if level_row else 1

    stats = _effective_stats(cursor)
    stat_val = int(stats.get(stat_key, 10))

    modifier = (stat_val - 10) // 2
    return max(modifier + level, 1)


def _has_spellbook_item(cursor) -> bool:
    """True when the character's spellbook is (still) in their inventory."""
    inventory = _db_val(cursor, "inventory", []) or []
    if not isinstance(inventory, list):
        return False
    for entry in inventory:
        name = entry.get("name", "") if isinstance(entry, dict) else entry
        if "spellbook" in str(name).lower():
            return True
    return False


def _spell_list_names(entries) -> list[str]:
    """Normalise a spell list that may hold strings or `{name}` dicts."""
    out = []
    for s in entries or []:
        name = s.get("name") if isinstance(s, dict) else s
        name = " ".join(str(name or "").split())
        if name:
            out.append(name)
    return out


def _dedupe_names(names) -> list[str]:
    seen: dict[str, str] = {}
    for n in names:
        seen.setdefault(n.lower(), n)
    return [seen[k] for k in sorted(seen)]


def _castable_spells(cursor) -> dict:
    """The player's castable spells, for the refusal that names the character's real options.

    `spells` is the leveled set (prepared / known / always-prepared); `ritual_spells` is the
    wizard-style spellbook (castable only as a ritual). Names are display-case, deduped."""
    sc = _db_val(cursor, "spellcasting", {}) or {}
    if not isinstance(sc, dict):
        return {"cantrips": [], "spells": [], "ritual_spells": []}
    leveled = (_spell_list_names(sc.get("spells_prepared"))
               + _spell_list_names(sc.get("spells_known"))
               + _spell_list_names(sc.get("always_prepared")))
    return {
        "cantrips": _dedupe_names(_spell_list_names(sc.get("cantrips"))),
        "spells": _dedupe_names(leveled),
        "ritual_spells": _dedupe_names(_spell_list_names(sc.get("spellbook"))),
    }


def _player_knows_spell(known: dict, spell_key: str, sp_level: int, ritual: bool = False) -> bool:
    """True when the player's sheet carries this spell. A cantrip is checked against
    `cantrips`; a leveled spell against prepared/known/always-prepared, plus the spellbook
    when it is cast as a ritual. Case-insensitive."""
    key = str(spell_key or "").strip().lower()
    if not key:
        return False
    if sp_level == 0:
        return key in {n.lower() for n in known.get("cantrips") or []}
    pool = {n.lower() for n in known.get("spells") or []}
    if ritual:
        pool |= {n.lower() for n in known.get("ritual_spells") or []}
    return key in pool


def _needs_spellbook(cursor) -> bool:
    """True for a caster whose spell list lives in a spellbook (wizard-style).

    Prepared casters (Cleric/Druid/Paladin) have no spellbook list, so they are
    never gated on the item.
    """
    sc = _db_val(cursor, "spellcasting", {}) or {}
    return bool(sc.get("spellbook")) if isinstance(sc, dict) else False


def build_prepared_spells_info(cursor) -> dict | None:
    max_spells = get_max_prepared_spells(cursor)
    if max_spells is None:
        return None

    cursor.execute("SELECT value FROM player WHERE key = ?", ("spellcasting",))
    sc_row = cursor.fetchone()
    if not sc_row:
        return None
    sc = json.loads(sc_row[0])
    current_prepared = sc.get("spells_prepared", [])
    current_count = len(current_prepared)
    available = max_spells - current_count

    spell_names = []
    for s in current_prepared:
        if isinstance(s, dict):
            spell_names.append(s.get("name", str(s)))
        else:
            spell_names.append(str(s))

    info = {
        "current_count": current_count,
        "max_count": max_spells,
        "available_slots": available,
        "formula": "spellcasting_ability_modifier + level",
    }

    if available <= 0:
        info["at_capacity"] = True
        info["current_spells"] = spell_names
        info["reason"] = f"Maximum prepared spells reached ({max_spells}). Remove a spell first before adding a new one."
    else:
        info["at_capacity"] = False

    return info


def _validate_spell_slot(cursor, slot_key, delta):
    if delta >= 0:
        return None

    cursor.execute("SELECT value FROM player WHERE key = ?", ("spellcasting",))
    row = cursor.fetchone()
    if not row:
        return None

    sc = json.loads(row[0])
    slots = sc.get("slots", {})
    slot_level = slot_key.split(".")[-1]

    if slot_level in slots:
        current_uses = int(slots[slot_level])
        if current_uses <= 0:
            available = {f"lv{k}": v for k, v in slots.items() if int(v) > 0}
            return {
                "error": f"No level {slot_level} spell slots remaining.",
                "available_slots": available if available else "No spell slots available.",
                "hint": "The character has no uses of this slot level left. They must take a long rest to recover spell slots, or cast using a higher-level slot."
            }
    return None
