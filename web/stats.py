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

from .icons import icon_key_for  # noqa: E402

try:  # SRD 5.1 carrying rules live at the repo root (shared with dice_server)
    from carrying import carry_state, weight_for  # noqa: E402
except ImportError:  # pragma: no cover - package-relative fallback
    from ..carrying import carry_state, weight_for  # noqa: E402

try:  # SRD 5.1 equipped-items model (same repo root, shared with dice_server)
    from equipment import equipment_state  # noqa: E402
except ImportError:  # pragma: no cover - package-relative fallback
    from ..equipment import equipment_state  # noqa: E402

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
        out.append({"name": name, "description": str(desc), "icon": icon_key_for("spell", name)})
    return out


def _has_spellbook_item(inventory) -> bool:
    """True when the inventory still holds the character's spellbook.

    A wizard whose spellbook is stolen/lost keeps every spell on the sheet, but
    cannot use them until the item is recovered (see the `unavailable` field).
    """
    for entry in inventory or []:
        name = entry.get("name", "") if isinstance(entry, dict) else entry
        if "spellbook" in str(name).lower():
            return True
    return False


def _merge_prepared(spellbook, prepared):
    """One sheet list: the spellbook, with the prepared entries flagged.

    Prepared spells missing from the book (prepared casters have no spellbook
    at all — Cleric/Druid/Paladin) are appended so they never disappear.
    """
    prepared_names = {e["name"].strip().lower() for e in prepared}
    merged = [{**entry, "prepared": entry["name"].strip().lower() in prepared_names}
              for entry in spellbook]
    listed = {e["name"].strip().lower() for e in merged}
    merged += [{**entry, "prepared": True} for entry in prepared
               if entry["name"].strip().lower() not in listed]
    return merged


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


_CONFIG_CACHE = {}


def _load_config(filename):
    """Load a YAML list from config/ (cached). Returns [] on any failure."""
    if filename in _CONFIG_CACHE:
        return _CONFIG_CACHE[filename]
    data = []
    path = _CONFIG_DIR / filename
    if yaml is not None and path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            if isinstance(loaded, list):
                data = loaded
        except Exception:
            data = []
    _CONFIG_CACHE[filename] = data
    return data


def _index_by_name(entries):
    return {str(e["name"]).lower(): e
            for e in entries if isinstance(e, dict) and e.get("name")}


def _weapon_index():
    return _index_by_name(_load_config("weapons.yml"))


def _weapon_entry(name):
    """(canonical name, description) for a weapon, matched against weapons.yml."""
    idx = _weapon_index()
    key = str(name or "").strip().lower()
    entry = idx.get(key)
    if entry is None and key.endswith("s"):
        entry = idx.get(key[:-1])
    if entry is None:
        return str(name), ""
    desc = f"{entry.get('damage', '')} {entry.get('damage_type', '')}".strip()
    props = entry.get("properties") or []
    if props:
        desc = (desc + ", " if desc else "") + ", ".join(str(p) for p in props)
    if entry.get("category"):
        kind = f"{entry['category']} {'melee' if entry.get('melee') else 'ranged'}"
        desc = f"{desc} ({kind})" if desc else kind
    return str(entry["name"]), desc


def _weapon_entries(names):
    if not isinstance(names, list):
        return []
    out = []
    for name in names:
        if isinstance(name, dict):
            name = name.get("name", "")
        canonical, desc = _weapon_entry(name)
        out.append({"name": canonical, "description": desc, "icon": icon_key_for("weapon", canonical)})
    return out


def _race_index():
    idx = {}
    for race in _load_config("races.yml"):
        if not isinstance(race, dict) or not race.get("name"):
            continue
        idx[str(race["name"]).lower()] = (race, None)
        for sub in race.get("subraces") or []:
            if isinstance(sub, dict) and sub.get("name"):
                idx[str(sub["name"]).lower()] = (race, sub)
    return idx


def _race_traits(race, sub):
    traits = list((race or {}).get("traits") or []) + list((sub or {}).get("traits") or [])
    return [t for t in traits if isinstance(t, dict) and t.get("name")]


def _race_description(name):
    race, sub = _race_index().get(str(name or "").strip().lower(), (None, None))
    if race is None:
        return ""
    lines = [str(race["name"]) + (f" ({sub['name']})" if sub else "")]
    increases = (list(race.get("ability_score_increases") or [])
                 + list((sub or {}).get("ability_score_increases") or []))
    if increases:
        lines.append("Ability increases: " + ", ".join(
            f"+{i.get('value')} {i.get('ability')}" for i in increases if isinstance(i, dict)))
    if race.get("speed"):
        lines.append(f"Speed: {race['speed']} ft")
    languages = list(race.get("languages") or []) + list((sub or {}).get("languages") or [])
    if languages:
        lines.append("Languages: " + ", ".join(str(x) for x in languages))
    for t in _race_traits(race, sub):
        lines.append(f"{t['name']}: {t.get('description', '')}")
    return "\n".join(lines)


def _class_index():
    return _index_by_name(_load_config("classes.yml"))


def _class_description(name):
    entry = _class_index().get(str(name or "").strip().lower())
    if entry is None:
        return ""
    lines = [str(entry["name"])]
    if entry.get("hit_die"):
        lines.append(f"Hit die: d{entry['hit_die']}")
    for f in entry.get("features") or []:
        if isinstance(f, dict) and f.get("name"):
            label = str(f["name"]) + (f" (L{f['level']})" if f.get("level") else "")
            lines.append(f"{label}: {f.get('description', '')}")
    return "\n".join(lines)


def _background_index():
    return _index_by_name(_load_config("backgrounds.yml"))


def _background_description(name):
    entry = _background_index().get(str(name or "").strip().lower())
    if entry is None:
        return ""
    lines = [str(entry["name"])]
    for label, key in (("Skills", "skill_proficiencies"), ("Tools", "tool_proficiencies")):
        vals = entry.get(key) or []
        if vals:
            lines.append(f"{label}: {', '.join(str(v) for v in vals)}")
    if entry.get("languages"):
        lines.append(f"Languages: {entry['languages']}")
    feat = entry.get("feature") or {}
    if isinstance(feat, dict) and feat.get("name"):
        lines.append(f"{feat['name']}: {feat.get('description', '')}")
    return "\n".join(lines)


def _feature_index(class_name=None, race_name=None, background_name=None):
    """Feature name -> description from config; the character's own class/race/background win."""
    idx = {}

    def add(name, desc):
        if name and str(name).lower() not in idx:
            idx[str(name).lower()] = desc or ""

    def override(entries):
        for f in entries:
            if isinstance(f, dict) and f.get("name"):
                idx[str(f["name"]).lower()] = f.get("description") or ""

    for cls in _load_config("classes.yml"):
        for f in cls.get("features") or []:
            if isinstance(f, dict) and f.get("name"):
                add(f["name"], f.get("description"))
    for race in _load_config("races.yml"):
        for t in _race_traits(race, None):
            add(t["name"], t.get("description"))
        for sub in race.get("subraces") or []:
            for t in _race_traits(None, sub):
                add(t["name"], t.get("description"))
    for bg in _load_config("backgrounds.yml"):
        feat = bg.get("feature") or {}
        if isinstance(feat, dict) and feat.get("name"):
            add(feat["name"], feat.get("description"))

    race, sub = _race_index().get(str(race_name or "").strip().lower(), (None, None))
    if race is not None:
        override(_race_traits(race, sub))
    cls = _class_index().get(str(class_name or "").strip().lower())
    if cls is not None:
        override(cls.get("features") or [])
    bg = _background_index().get(str(background_name or "").strip().lower())
    if bg is not None:
        override([bg.get("feature") or {}])
    return idx


def _feature_entries(features, class_name=None, race_name=None, background_name=None):
    if not isinstance(features, list):
        return []
    idx = _feature_index(class_name, race_name, background_name)
    out = []
    for f in features:
        if isinstance(f, dict):
            name = str(f.get("name") or "")
            desc = f.get("description") or ""
        else:
            name = str(f)
            desc = ""
        if not desc:
            desc = idx.get(name.lower(), "")
        out.append({"name": name, "description": str(desc), "icon": icon_key_for("feature", name)})
    return out


def _inventory_entries(items, equip=None):
    """Inventory chips, with equipped entries flagged like prepared spells.

    `equip` is the shared `_equipment` block; each equipped name is tagged with the
    slot it occupies (Main hand / Off hand / Worn) so the sheet can highlight it.
    """
    slots = {}
    for hand in (equip or {}).get("hands", []) or []:
        if isinstance(hand, dict) and hand.get("name"):
            slots[str(hand["name"])] = ("Main hand" if hand.get("slot") == "main_hand"
                                        else "Off hand")
    for worn in (equip or {}).get("worn", []) or []:
        if isinstance(worn, dict) and worn.get("name"):
            slots[str(worn["name"])] = "Worn"
    if (equip or {}).get("armor"):
        slots[str(equip["armor"])] = "Worn"

    out = []
    for it in items:
        declared = None
        base = None
        if isinstance(it, dict):
            name = str(it.get("name") or "")
            desc = it.get("description") or ""
            declared = it.get("weight")
            base = it.get("base")
        else:
            name = str(it)
            desc = ""
        if not desc:
            _canon, wdesc = _weapon_entry(name)
            desc = wdesc
        weight = weight_for(name, declared, base)
        out.append({"name": name, "description": str(desc), "icon": icon_key_for("inventory", name),
                    "weight": weight, "unweighed": weight is None,
                    "equipped": name in slots, "slot": slots.get(name)})
    return out


def _tag_entries(names, category):
    """Plain string lists (skills, armour, tools, languages) -> {name, icon}."""
    if not isinstance(names, list):
        return []
    out = []
    for entry in names:
        label = str(entry.get("name") or "") if isinstance(entry, dict) else str(entry)
        if label:
            out.append({"name": label, "icon": icon_key_for(category, label)})
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


_ABILITY_NAMES = {
    "str": "Strength", "dex": "Dexterity", "con": "Constitution",
    "int": "Intelligence", "wis": "Wisdom", "cha": "Charisma",
}


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
            name = _ABILITY_NAMES[key]
            stats.append({
                "key": key.upper(), "name": name, "value": value,
                "modifier": _modifier(value), "icon": icon_key_for("ability", name),
            })

    spellcasting = None
    spell_raw = g("spellcasting")
    if isinstance(spell_raw, dict):
        spell_slots = spell_raw.get("slots", {})
        spellbook = _spell_entries(spell_raw.get("spellbook", []))
        prepared = _spell_entries(spell_raw.get("spells_prepared", []))
        raw_inventory = g("inventory")
        has_spellbook_item = _has_spellbook_item(
            raw_inventory if isinstance(raw_inventory, list) else [])
        spellcasting = {
            "ability": str(spell_raw.get("ability", "")).capitalize(),
            "ability_icon": icon_key_for("ability", str(spell_raw.get("ability", ""))),
            "dc": spell_raw.get("dc"),
            "dc_icon": "stat/save-dc",
            "attack_modifier": spell_raw.get("attack_modifier"),
            "attack_icon": "stat/spell-attack",
            "cantrips": _spell_entries(spell_raw.get("cantrips", [])),
            "spells_known": _spell_entries(spell_raw.get("spells_known", [])),
            "spells_prepared": prepared,
            # The book itself: prepared entries flagged, so the sheet shows one
            # list instead of two. `has_spellbook` picks the field label;
            # `spellbook_missing` greys the whole list (book item gone).
            "spellbook": _merge_prepared(spellbook, prepared),
            "has_spellbook": bool(spellbook),
            "spellbook_missing": bool(spellbook) and not has_spellbook_item,
            "slots": spell_slots,
            "slot_levels": _slot_levels(spell_slots, g("character_class"), g("level")),
        }

    race_name = g("race")
    class_name = g("character_class")
    background_name = g("background")

    proficiencies = {
        "skills": _tag_entries(g("skills"), "skill"),
        "saves": _tag_entries(g("saves"), "save"),
        "armor": _tag_entries(g("armor_proficiencies"), "armor"),
        "weapons": _weapon_entries(g("weapon_proficiencies")),
        "tools": _tag_entries(g("tool_proficiencies"), "tool"),
        "features": _feature_entries(g("features"), class_name, race_name, background_name),
        "languages": _tag_entries(g("languages"), "language"),
    }

    equip = equipment_state(g)

    inventory = g("inventory")
    if not isinstance(inventory, list):
        inventory = []
    inventory = _inventory_entries(inventory, equip)

    # SRD 5.1 carrying capacity / variant encumbrance, recomputed from the data
    # above (never taken from the dump, which may carry a stale derived copy).
    carry = carry_state(g)

    consumables = g("consumables")
    if not isinstance(consumables, dict):
        consumables = {}
    consumable_icons = {str(k): icon_key_for("consumable", k) for k in consumables}

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
                        "kingdom_icon": icon_key_for("kingdom", category),
                        "faction_icon": icon_key_for("faction", faction),
                        "entries": entries,
                    })

    active_effects = []
    effects = g("active_effects")
    buff_data = g("_active_buff_data")
    thp = g("temporary_hit_points")
    if isinstance(effects, list):
        for raw in effects:
            # Entries may be plain strings or {name, description} dicts (the
            # engine stores the latter when update_player_list gets a colon).
            if isinstance(raw, dict):
                name = str(raw.get("name") or raw.get("spell") or "")
                desc = str(raw.get("description") or "")
            else:
                name = str(raw) if raw is not None else ""
                desc = ""
            rows = []
            if name and isinstance(buff_data, dict) and name in buff_data:
                for entry in buff_data.get(name) or []:
                    if not isinstance(entry, dict):
                        continue
                    field = entry.get("field", "?")
                    if field == "temporary_hit_points" and isinstance(thp, (int, float)):
                        rows.append({"field": field, "value": int(thp)})
                    else:
                        delta = entry.get("delta", 0)
                        rows.append({"field": field, "value": f"{'+' if delta >= 0 else ''}{delta}"})
            active_effects.append({
                "name": name,
                "description": desc,
                "rows": rows,
                "icon": icon_key_for("spell", name),
            })

    return {
        "character": {
            "name": g("name"),
            "race": race_name,
            "character_class": class_name,
            "level": g("level"),
            "gender": g("gender"),
            "gold": g("gold"),
            "xp": g("xp"),
            "level_icon": "stat/level",
            "gold_icon": "stat/gold",
            "xp_icon": "stat/xp",
            "background": background_name,
            "alignment": g("alignment"),
            "race_desc": _race_description(race_name),
            "character_class_desc": _class_description(class_name),
            "background_desc": _background_description(background_name),
            "race_icon": icon_key_for("race", race_name),
            "class_icon": icon_key_for("class", class_name),
            "background_icon": icon_key_for("background", background_name),
            "alignment_icon": icon_key_for("alignment", g("alignment")),
        },
        "combat": {
            "hp_current": g("current_hit_points", "?"),
            "hp_max": g("total_hit_points", "?"),
            "armor_class": g("armor_class", "?"),
            "ac_breakdown": equip.get("ac_breakdown", "") if equip.get("derived_from_equipped") else "",
            "speed": carry["speed"] if carry["speed"] is not None else g("speed", "?"),
            "speed_base": g("speed", "?"),
            "speed_penalty": carry["speed_penalty"],
            "proficiency_bonus": g("proficiency_bonus", "?"),
            "hit_dice_count": g("hit_dice_count", "?"),
            "hit_dice_size": g("hit_dice_size", "?"),
            "temporary_hit_points": thp,
            "hp_icon": "stat/hp",
            "ac_icon": "stat/ac",
            "speed_icon": "stat/speed",
            "proficiency_icon": "stat/proficiency",
            "hit_dice_icon": "stat/hit-dice",
            "temp_hp_icon": "stat/temp-hp",
        },
        "stats": stats,
        "spellcasting": spellcasting,
        "proficiencies": proficiencies,
        "inventory": inventory,
        "consumables": consumables,
        "consumable_icons": consumable_icons,
        "reputation": reputation,
        "active_effects": active_effects,
        "carrying": carry,
        "equipment": equip,
    }
