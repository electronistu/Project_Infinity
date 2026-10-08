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
    from equipment import (armor_entry, bonus_suppressed_reason, equipment_state,
                           item_effects, properties_for, weapon_entry)  # noqa: E402
except ImportError:  # pragma: no cover - package-relative fallback
    from ..equipment import (armor_entry, bonus_suppressed_reason, equipment_state,
                             item_effects, properties_for, weapon_entry)  # noqa: E402

try:  # SRD 5.1 skill -> ability map (same repo root, shared with dice_server)
    import skills as skills_mod  # noqa: E402
except ImportError:  # pragma: no cover - package-relative fallback
    from .. import skills as skills_mod  # noqa: E402

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


_ABILITY_LABELS = {"str": "STR", "dex": "DEX", "con": "CON",
                   "int": "INT", "wis": "WIS", "cha": "CHA"}


def _signed(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"+{number}" if number >= 0 else str(number)


def _effect_line(effect):
    """One human-readable line for a declared item effect (flat fields normalize to these)."""
    etype = str(effect.get("type") or "").strip().lower()
    value = effect.get("value")
    ability = _ABILITY_LABELS.get(str(effect.get("ability") or "").lower(), "")
    if etype == "ac_bonus":
        return f"{_signed(value)} AC"
    if etype == "attack_bonus":
        return f"{_signed(value)} to attack rolls"
    if etype == "damage_bonus":
        return f"{_signed(value)} to damage rolls"
    if etype == "save_bonus":
        return f"{_signed(value)} to saving throws"
    if etype == "check_bonus":
        return f"{_signed(value)} to ability checks"
    if etype == "proficiency_bonus":
        return f"{_signed(value)} to proficiency bonus"
    if etype == "spell_attack_bonus":
        return f"{_signed(value)} to spell attacks"
    if etype == "spell_dc_bonus":
        return f"{_signed(value)} spell save DC"
    if etype == "initiative":
        return f"{_signed(value)} to initiative"
    if etype == "ability_set" and ability:
        return f"{ability} becomes {value}"
    if etype == "ability_bonus" and ability:
        cap = effect.get("max")
        return f"{_signed(value)} {ability}" + (f" (max {cap})" if cap else "")
    if etype == "skill_bonus":
        skill = effect.get("skill") or effect.get("name")
        return f"{_signed(value)} to {skill}" if skill else f"{_signed(value)} to skill checks"
    if etype == "damage_resistance":
        return f"resistance to {value}" if value else "damage resistance"
    if etype == "damage_immunity":
        return f"immunity to {value}" if value else "damage immunity"
    if etype == "condition_immunity":
        return f"immune to {value}" if value else "condition immunity"
    if etype == "speed_grant":
        return f"speed {value} ft" if value else "speed granted"
    pretty = etype.replace("_", " ").strip()
    return f"{pretty} {value}".strip() if value not in (None, True, False, "") else pretty


def _weapon_stat_line(name, base, declared):
    """A weapon's stat line: declared fields win, the archetype fills the gaps."""
    arche = weapon_entry(name, base) or {}
    damage = declared.get("damage_dice") or arche.get("damage") or ""
    damage_type = declared.get("damage_type") or arche.get("damage_type") or ""
    head = f"{damage} {damage_type}".strip()
    parts = [head] if head else []
    parts += [str(p) for p in properties_for(name, base, declared)]
    line = ", ".join(parts)
    if arche.get("category"):
        kind = f"{arche['category']} {'melee' if arche.get('melee') else 'ranged'}"
        line = f"{line} ({kind})" if line else kind
    return line


def _armor_stat_line(name, base, declared):
    """Armour / shield stat line: declared fields win, the archetype fills the gaps."""
    arche = armor_entry(name, base) or {}
    shield = bool(arche.get("shield"))
    ac = declared.get("ac")
    if ac is None:
        ac = arche.get("ac")
    parts = []
    if ac is not None:
        parts.append(f"+{int(ac)} AC" if shield else f"AC {int(ac)}")
    if not shield:
        dex_cap = declared.get("dex_cap")
        if dex_cap is None:
            dex_cap = arche.get("dex_cap")
        if dex_cap is not None:
            parts.append("no DEX bonus" if int(dex_cap) == 0 else f"DEX cap +{int(dex_cap)}")
    strength_req = declared.get("strength_req")
    if strength_req is None:
        strength_req = arche.get("strength_req")
    if strength_req:
        parts.append(f"Str {int(strength_req)}")
    if arche.get("stealth_disadvantage"):
        parts.append("stealth disadvantage")
    if arche.get("type") and not shield:
        parts.append(str(arche["type"]))
    return ", ".join(parts)


def _item_stat_lines(name, declared, items, attuned_names, equipped_names, is_equipped):
    """Stat lines for one inventory entry (weapon / armour / worn magic).

    Declared GM fields win over the config archetype. Magic bonuses are listed even when
    inactive, with a qualifier (while wielded / while worn / while attuned / pair incomplete).
    """
    base = declared.get("base")
    weapon = weapon_entry(name, base)
    armor = armor_entry(name, base)
    is_weapon = bool(weapon) or bool(declared.get("damage_dice") or declared.get("damage_type")
                                     or declared.get("properties"))
    is_armor = bool(armor) or declared.get("ac") is not None

    lines = []
    if is_weapon:
        line = _weapon_stat_line(name, base, declared)
    elif is_armor:
        line = _armor_stat_line(name, base, declared)
    else:
        line = ""
    if line:
        lines.append(line)

    rendered = [r for r in (_effect_line(e) for e in item_effects(declared)) if r]
    if rendered:
        reason = bonus_suppressed_reason(name, items, attuned_names, equipped_names)
        if reason == "pair_incomplete":
            qualifier = "when both halves are worn"
        elif reason == "not_attuned":
            qualifier = "while attuned"
        elif not is_equipped:
            qualifier = "while wielded" if is_weapon else "while worn"
        else:
            qualifier = ""
        bonus = ", ".join(rendered)
        if declared.get("kind") and not (is_weapon or is_armor):
            bonus = f"{declared['kind']}: {bonus}"
        if qualifier:
            bonus = f"{bonus} ({qualifier})"
        lines.append(bonus)
    return lines


def _inventory_entries(items, equip=None):
    """Inventory chips, with equipped entries flagged like prepared spells.

    `equip` is the shared `_equipment` block; each equipped name is tagged with the
    slot it occupies (Main hand / Off hand / Worn) so the sheet can highlight it.
    The tooltip carries the GM's flavour text plus every declared stat line (weapon,
    armour, worn magic), with the config archetype filling any gap.
    """
    slots = {}
    equipped_names = []
    for hand in (equip or {}).get("hands", []) or []:
        if isinstance(hand, dict) and hand.get("name"):
            slots[str(hand["name"])] = ("Main hand" if hand.get("slot") == "main_hand"
                                        else "Off hand")
            equipped_names.append(str(hand["name"]))
    for worn in (equip or {}).get("worn", []) or []:
        if isinstance(worn, dict) and worn.get("name"):
            slots[str(worn["name"])] = "Worn"
            equipped_names.append(str(worn["name"]))
    if (equip or {}).get("armor"):
        slots[str(equip["armor"])] = "Worn"
        equipped_names.append(str(equip["armor"]))
    attuned_names = [str(n) for n in ((equip or {}).get("attuned") or [])]

    out = []
    for it in items:
        declared = {}
        base = None
        if isinstance(it, dict):
            name = str(it.get("name") or "")
            declared = it
            base = it.get("base")
            flavour = str(it.get("description") or "").strip()
        else:
            name = str(it)
            flavour = ""
        stat_lines = _item_stat_lines(name, declared, items, attuned_names, equipped_names,
                                      name in slots)
        description = "\n".join(p for p in [flavour] + stat_lines if p)
        weight = weight_for(name, declared.get("weight"), base)
        out.append({"name": name, "description": description,
                    "icon": icon_key_for("inventory", name),
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

    equip = equipment_state(g)

    stats_raw = g("stats")
    scores = equip.get("effective_stats") if isinstance(equip, dict) else None
    if not isinstance(scores, dict):
        scores = stats_raw if isinstance(stats_raw, dict) else {}
    score_sources = equip.get("score_sources") if isinstance(equip, dict) else {}
    if not isinstance(score_sources, dict):
        score_sources = {}
    stats = []
    if isinstance(stats_raw, dict):
        for key in ("str", "dex", "con", "int", "wis", "cha"):
            base_value = stats_raw.get(key, "?")
            value = scores.get(key, base_value)
            name = _ABILITY_NAMES[key]
            entry = {
                "key": key.upper(), "name": name, "value": value,
                "modifier": _modifier(value), "icon": icon_key_for("ability", name),
            }
            # A worn/wielded score item changed this score — show the source so the
            # sheet (and the GM reading it) never contradicts the engine.
            if value != base_value:
                entry["base_value"] = base_value
                entry["source"] = ", ".join(score_sources.get(key, [])) or None
            stats.append(entry)

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

    save_prof = {str(s).strip().lower()[:3] for s in (g("saves") or [])}
    prof_bonus = int(g("proficiency_bonus", 2) or 2) + int(equip.get("proficiency_bonus_mod") or 0)
    item_save_bonus = int(equip.get("save_bonus") or 0)
    saves = []
    for key in ("str", "dex", "con", "int", "wis", "cha"):
        try:
            mod = (int(scores.get(key, 10)) - 10) // 2
        except (TypeError, ValueError):
            mod = 0
        proficient = key in save_prof
        saves.append({
            "key": key.upper(), "name": _ABILITY_NAMES[key], "modifier": mod,
            "proficient": proficient, "prof_bonus": prof_bonus if proficient else 0,
            "item_bonus": item_save_bonus,
            "total": mod + (prof_bonus if proficient else 0) + item_save_bonus,
            "icon": icon_key_for("save", _ABILITY_NAMES[key]),
        })

    # SRD 5.1 derived skill modifiers: ability + proficiency/Expertise/Jack of All Trades + the flat
    # item check bonus (scoped skill_bonus needs a context, so it is engine-side only).
    def _names(val):
        out = []
        for e in (val or []):
            n = e.get("name") if isinstance(e, dict) else e
            if n:
                out.append(str(n))
        return out

    proficient_skills = {skills_mod.normalize(n) for n in _names(g("skills"))}
    expertise_skills = {skills_mod.normalize(n) for n in _names(g("expertise"))}
    has_joat = any("jack of all trades" in skills_mod.normalize(n) for n in _names(g("features")))
    flat_check = int(equip.get("check_bonus") or 0) if isinstance(equip, dict) else 0
    derived_skills = []
    for skill_name, ability_key in skills_mod.SKILL_ABILITIES.items():
        key = skills_mod.normalize(skill_name)
        try:
            base = (int(scores.get(ability_key, 10)) - 10) // 2
        except (TypeError, ValueError):
            base = 0
        proficient = key in proficient_skills
        expertise = key in expertise_skills
        if expertise:
            part = 2 * prof_bonus
        elif proficient:
            part = prof_bonus
        elif has_joat:
            part = prof_bonus // 2
        else:
            part = 0
        derived_skills.append({
            "name": skill_name, "ability": ability_key.upper(), "modifier": base + part + flat_check,
            "proficient": proficient, "expertise": expertise, "prof_bonus": part,
            "item_bonus": flat_check, "icon": icon_key_for("skill", skill_name),
        })
    by_skill = {d["name"]: d for d in derived_skills}
    passive = {name.lower(): 10 + by_skill[name]["modifier"]
               for name in ("Perception", "Investigation", "Insight")}

    inventory = g("inventory")
    if not isinstance(inventory, list):
        inventory = []
    inventory = _inventory_entries(inventory, equip)

    # SRD 5.1 carrying capacity / variant encumbrance, recomputed from the data
    # above (never taken from the dump, which may carry a stale derived copy).
    carry = carry_state(g)
    speed = equip.get("speed_state") or {}
    if speed:
        if speed.get("ignore_encumbrance"):
            carry["speed_penalty"] = 0
            carry["speed"] = speed.get("walk")
        elif speed.get("walk") is not None:
            carry["speed"] = max(0, int(speed["walk"]) - int(carry.get("speed_penalty") or 0))
        carry["speed_base"] = speed.get("base", g("speed", "?"))
        carry["speeds"] = speed.get("modes", {})

    # SRD 5.1 exhaustion: speed halved at level 2, 0 at level 5.
    try:
        exh_level = max(0, min(6, int(g("exhaustion", 0) or 0)))
    except (TypeError, ValueError):
        exh_level = 0
    exh_mult = 0.0 if exh_level >= 5 else (0.5 if exh_level >= 2 else 1.0)
    if exh_mult != 1.0 and carry.get("speed") is not None:
        carry["speed"] = int(carry["speed"] * exh_mult)
        carry["speeds"] = {k: int(v * exh_mult) for k, v in (carry.get("speeds") or {}).items()}
    conc_raw = g("concentration")
    concentration = conc_raw.get("spell") if isinstance(conc_raw, dict) else None

    consumables = g("consumables")
    if not isinstance(consumables, dict):
        consumables = {}
    consumable_icons = {str(k): icon_key_for("consumable", k) for k in consumables}

    # Reputation is stored era-scoped: the sheet is the live character, so it
    # shows the era it is standing in, never another age's standing.
    reputation = []
    rep_raw = g("reputation")
    # A classic save carries no era and keeps its flat reputation map; the era game's
    # map is namespaced by era and only the era being played is shown.
    if not isinstance(rep_raw, dict):
        rep_scope = None
    elif str(g("era") or "").strip():
        rep_scope = rep_raw.get(g("era"))
    else:
        rep_scope = rep_raw
    if isinstance(rep_scope, dict):
        for category, factions in rep_scope.items():
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
            "speed_base": carry.get("speed_base", g("speed", "?")),
            "speeds": carry.get("speeds", {}),
            "resistances": (equip.get("defenses") or {}).get("damage_resistances", []),
            "immunities": (equip.get("defenses") or {}).get("damage_immunities", []),
            "condition_immunities": (equip.get("defenses") or {}).get("condition_immunities", []),
            "speed_penalty": carry["speed_penalty"],
            "proficiency_bonus": g("proficiency_bonus", "?"),
            "hit_dice_count": g("hit_dice_count", "?"),
            "hit_dice_size": g("hit_dice_size", "?"),
            "temporary_hit_points": thp,
            "exhaustion": exh_level,
            "concentration": concentration,
            "death_saves": {
                "successes": int(g("death_save_successes", 0) or 0),
                "failures": int(g("death_save_failures", 0) or 0),
            },
            "hp_icon": "stat/hp",
            "ac_icon": "stat/ac",
            "speed_icon": "stat/speed",
            "proficiency_icon": "stat/proficiency",
            "hit_dice_icon": "stat/hit-dice",
            "temp_hp_icon": "stat/temp-hp",
        },
        "stats": stats,
        "saves": saves,
        "skills": derived_skills,
        "passive": passive,
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
