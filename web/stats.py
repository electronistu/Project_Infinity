"""Structured character-sheet data for the web client.

Mirrors the fields rendered by `display.format_stats` but returns plain data
(dicts/lists) so any front-end can render it. `display.py` is left untouched.
"""

import json
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_SPELL_DB = None


def _load_spells():
    """Spell metadata from config/spells.yml, keyed by lowercased name (cached)."""
    global _SPELL_DB
    if _SPELL_DB is not None:
        return _SPELL_DB
    _SPELL_DB = {}
    path = _CONFIG_DIR / "spells.yml"
    if yaml is not None and path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except Exception:
            data = []
        for spell in data:
            if isinstance(spell, dict) and spell.get("name"):
                _SPELL_DB[str(spell["name"]).lower()] = spell
    return _SPELL_DB


def _spell_entries(seq):
    """Normalize a spell list to [{name, description}] so the UI can render chips.

    Accepts entries as plain strings or dicts. Description precedence:
    the entry's own `description`, else config/spells.yml, else "".
    """
    spells = _load_spells()
    out = []
    if not isinstance(seq, list):
        return out
    for entry in seq:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("spell") or ""
            desc = entry.get("description") or ""
            name = str(name)
        else:
            name = str(entry)
            desc = ""
        if not desc:
            meta = spells.get(name.lower())
            if isinstance(meta, dict):
                desc = meta.get("description") or ""
        out.append({"name": name, "description": str(desc)})
    return out


def _max_spell_slots(character_class, level):
    """Max spell slots per level for a class/level, from level_up's tables."""
    try:
        from level_up import CASTER_TYPE_MAP, SLOT_TABLES
    except Exception:  # pragma: no cover - level_up may be off sys.path
        return {}
    caster = CASTER_TYPE_MAP.get(str(character_class or ""))
    if not caster:
        return {}
    table = SLOT_TABLES.get(caster) or {}
    try:
        level = int(level)
    except (TypeError, ValueError):
        return {}
    return table.get(level, {}) or {}


def _slot_levels(slots, character_class, level):
    """Per-level {level, remaining, max} for the sheet's pip display."""
    slots = slots if isinstance(slots, dict) else {}
    max_slots = _max_spell_slots(character_class, level)
    levels = set()
    for key in slots:
        try:
            levels.add(int(key))
        except (TypeError, ValueError):
            continue
    levels.update(int(k) for k in max_slots)
    out = []
    for lvl in sorted(levels):
        remaining = int(slots.get(str(lvl), 0) or 0)
        maximum = int(max_slots.get(lvl, 0) or 0)
        if maximum <= 0:
            maximum = remaining  # unknown class/level: treat remaining as the max
        if maximum <= 0:
            continue
        out.append({"level": lvl, "remaining": max(0, min(remaining, maximum)), "max": maximum})
    return out


def _parse(val):
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


def _modifier(score):
    try:
        mod = (int(score) - 10) // 2
    except (TypeError, ValueError):
        return None
    return f"+{mod}" if mod >= 0 else str(mod)


def build_stats(db_data: dict) -> dict:
    """Turn a `dump_player_db` payload into a structured character sheet."""
    if not isinstance(db_data, dict):
        return {}

    def g(key, default=""):
        return _parse(db_data.get(key, default))

    stats_raw = g("stats")
    stats = []
    if isinstance(stats_raw, dict):
        for key in ("str", "dex", "con", "int", "wis", "cha"):
            value = stats_raw.get(key, "?")
            stats.append({"key": key.upper(), "value": value, "modifier": _modifier(value)})

    spellcasting = None
    spell_raw = g("spellcasting")
    if isinstance(spell_raw, dict):
        spell_slots = spell_raw.get("slots", {})
        spellcasting = {
            "ability": str(spell_raw.get("ability", "")).capitalize(),
            "dc": spell_raw.get("dc"),
            "attack_modifier": spell_raw.get("attack_modifier"),
            "cantrips": _spell_entries(spell_raw.get("cantrips", [])),
            "spells_known": _spell_entries(spell_raw.get("spells_known", [])),
            "spells_prepared": _spell_entries(spell_raw.get("spells_prepared", [])),
            "spellbook": _spell_entries(spell_raw.get("spellbook", [])),
            "slots": spell_slots,
            "slot_levels": _slot_levels(spell_slots, g("character_class"), g("level")),
        }

    proficiencies = {
        "skills": g("skills") if isinstance(g("skills"), list) else [],
        "saves": g("saves") if isinstance(g("saves"), list) else [],
        "armor": g("armor_proficiencies") if isinstance(g("armor_proficiencies"), list) else [],
        "weapons": g("weapon_proficiencies") if isinstance(g("weapon_proficiencies"), list) else [],
        "tools": g("tool_proficiencies") if isinstance(g("tool_proficiencies"), list) else [],
        "features": g("features") if isinstance(g("features"), list) else [],
        "languages": g("languages") if isinstance(g("languages"), list) else [],
    }

    inventory = g("inventory")
    if not isinstance(inventory, list):
        inventory = []

    consumables = g("consumables")
    if not isinstance(consumables, dict):
        consumables = {}

    reputation = []
    rep_raw = g("reputation")
    if isinstance(rep_raw, dict):
        for category, factions in rep_raw.items():
            if not isinstance(factions, dict):
                continue
            for faction, entries in factions.items():
                if isinstance(entries, list) and entries:
                    reputation.append({
                        "category": str(category).title(),
                        "faction": str(faction).title(),
                        "entries": entries,
                    })

    active_effects = []
    effects = g("active_effects")
    buff_data = g("_active_buff_data")
    thp = g("temporary_hit_points")
    if isinstance(effects, list):
        for spell_name in effects:
            rows = []
            if isinstance(buff_data, dict) and spell_name in buff_data:
                for entry in buff_data[spell_name]:
                    field = entry.get("field", "?")
                    if field == "temporary_hit_points" and isinstance(thp, (int, float)):
                        rows.append({"field": field, "value": int(thp)})
                    else:
                        delta = entry.get("delta", 0)
                        rows.append({"field": field, "value": f"{'+' if delta >= 0 else ''}{delta}"})
            active_effects.append({"name": spell_name, "rows": rows})

    return {
        "character": {
            "name": g("name"),
            "race": g("race"),
            "character_class": g("character_class"),
            "level": g("level"),
            "gold": g("gold"),
            "xp": g("xp"),
            "background": g("background"),
            "alignment": g("alignment"),
        },
        "combat": {
            "hp_current": g("current_hit_points", "?"),
            "hp_max": g("total_hit_points", "?"),
            "armor_class": g("armor_class", "?"),
            "speed": g("speed", "?"),
            "proficiency_bonus": g("proficiency_bonus", "?"),
            "hit_dice_count": g("hit_dice_count", "?"),
            "hit_dice_size": g("hit_dice_size", "?"),
            "temporary_hit_points": thp,
        },
        "stats": stats,
        "spellcasting": spellcasting,
        "proficiencies": proficiencies,
        "inventory": inventory,
        "consumables": consumables,
        "reputation": reputation,
        "active_effects": active_effects,
    }
