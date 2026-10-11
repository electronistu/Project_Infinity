import math
import random
import sys
import json
import sqlite3
import os
import re
from mcp.server.fastmcp import FastMCP
from level_up import apply_level_up, CASTER_TYPE_MAP, SLOT_TABLES, FULL_CASTER_SPELL_SLOTS, WARLOCK_SPELL_SLOTS, ABILITY_TO_STAT

import carrying  # local SRD 5.1 carrying capacity / encumbrance rules
import device  # local Text Time Traveler: the Device and its four parts (closed vocabulary)
import equipment  # local SRD 5.1 equipped-items model (armour, hands, derived AC)
import skills  # local SRD 5.1 skill -> ability map + normalisation

try:
    import yaml
except ImportError:
    yaml = None

mcp = FastMCP("InfinityRolls", log_level="WARNING")

DB_CONNECTION = None

# Developer mode: the `debug_*` tools exist only to let the engine edit live state from
# the UI. They are off unless the server was started with `--debug` (the engine passes it
# when the session was created with the dev flag). They are never offered to the GM.
DEBUG_MODE = False

_COMBAT_REGISTRY: dict[str, dict] = {}

XP_THRESHOLDS = [
    (2, 300), (3, 900), (4, 2700), (5, 6500),
    (6, 14000), (7, 23000), (8, 33000), (9, 48000),
    (10, 64000), (11, 85000), (12, 100000), (13, 120000),
    (14, 145000), (15, 175000), (16, 210000), (17, 255000),
    (18, 305000), (19, 360000), (20, 400000),
]

KNOWN_CASTER_CLASSES = {"Bard", "Sorcerer", "Warlock", "Ranger"}
PREPARED_CASTER_CLASSES = {"Cleric", "Druid", "Paladin"}
SRD_CLASSES = {"Barbarian", "Bard", "Cleric", "Druid", "Fighter", "Monk", "Paladin",
               "Ranger", "Rogue", "Sorcerer", "Warlock", "Wizard"}

CR_XP_TABLE = {
    0: 10, 0.125: 25, 0.25: 50, 0.5: 100,
    1: 200, 2: 450, 3: 700, 4: 1100,
    5: 1800, 6: 2300, 7: 2900, 8: 3900,
    9: 5000, 10: 5900, 11: 7200, 12: 8400,
    13: 10000, 14: 11500, 15: 13000, 16: 15000,
    17: 18000, 18: 20000, 19: 22000, 20: 25000,
    21: 33000, 22: 41000, 23: 50000, 24: 62000,
    25: 75000, 26: 90000, 27: 105000, 28: 120000,
    29: 135000, 30: 155000,
}


_SPELLS_DB = None


def _load_spells() -> dict:
    global _SPELLS_DB
    if _SPELLS_DB is not None:
        return _SPELLS_DB
    if yaml is None:
        _SPELLS_DB = {}
        return _SPELLS_DB
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "spells.yml")
    if not os.path.exists(config_path):
        _SPELLS_DB = {}
        return _SPELLS_DB
    with open(config_path, "r", encoding="utf-8") as f:
        spells_list = yaml.safe_load(f)
    if spells_list is None:
        _SPELLS_DB = {}
    else:
        _SPELLS_DB = {}
        for s in spells_list:
            _SPELLS_DB[s["name"].lower()] = s
    return _SPELLS_DB


def _parse_higher_levels(hl_str: str) -> tuple | None:
    if not hl_str or not hl_str.startswith("+"):
        return None
    dice_match = re.match(r"\+(\d+)d(\d+)(?:\+(\d+))?", hl_str)
    if dice_match:
        num = int(dice_match.group(1))
        size = int(dice_match.group(2))
        flat = int(dice_match.group(3)) if dice_match.group(3) else 0
        return ("dice", num, size, flat)
    flat_match = re.match(r"\+(\d+)", hl_str)
    if flat_match:
        return ("flat", int(flat_match.group(1)), 0, 0)
    return None


def _compute_spell_damage(spell: dict, character_level: int, slot_level: int | None) -> tuple:
    base_dice_str = spell.get("damage_dice", "0d0")
    base_mod = spell.get("damage_modifier", 0)
    native_level = spell.get("level", 0)

    is_cantrip = native_level == 0
    cantrip_scales = spell.get("cantrip_scaling", False)
    scale_dice_str = spell.get("cantrip_scale_dice", None)

    dice_str = base_dice_str
    modifier = base_mod

    if is_cantrip and cantrip_scales and scale_dice_str:
        if character_level >= 17:
            extra = 3
        elif character_level >= 11:
            extra = 2
        elif character_level >= 5:
            extra = 1
        else:
            extra = 0
        if extra > 0:
            dice_str = _multiply_dice_notation(base_dice_str, extra + 1)
    elif slot_level is not None and slot_level > native_level and not is_cantrip:
        hl_str = spell.get("higher_levels", None)
        if hl_str:
            levels_above = slot_level - native_level
            parsed = _parse_higher_levels(hl_str)
            if parsed:
                kind, num, size, flat = parsed
                if kind == "dice":
                    extra_dice_str = f"{num * levels_above}d{size}"
                    dice_str = _combine_dice(base_dice_str, extra_dice_str)
                    modifier += flat * levels_above
                elif kind == "flat":
                    dice_str = base_dice_str
                    modifier += num * levels_above

    extra_dice_str = spell.get("extra_damage_dice", None)
    if extra_dice_str and slot_level is not None and slot_level > native_level and not is_cantrip:
        ehl_str = spell.get("extra_higher_levels", None)
        if ehl_str:
            levels_above = slot_level - native_level
            eparsed = _parse_higher_levels(ehl_str)
            if eparsed:
                ekind, enum, esize, eflat = eparsed
                if ekind == "dice":
                    extra_dice_str = _combine_dice(extra_dice_str, f"{enum * levels_above}d{esize}")

    return dice_str, modifier, extra_dice_str


def _multiply_dice_notation(dice_str: str, multiplier: int) -> str:
    if multiplier <= 1:
        return dice_str
    parts = dice_str.lower().split("d")
    if len(parts) != 2:
        return dice_str
    try:
        num = int(parts[0]) if parts[0] else 1
        size = int(parts[1])
        return f"{num * multiplier}d{size}"
    except (ValueError, TypeError):
        return dice_str


def _combine_dice(d1: str, d2: str) -> str:
    if not d2:
        return d1
    p1 = d1.lower().split("d")
    p2 = d2.lower().split("d")
    if len(p1) == 2 and len(p2) == 2:
        try:
            n1 = int(p1[0]) if p1[0] else 1
            s1 = int(p1[1])
            n2 = int(p2[0]) if p2[0] else 1
            s2 = int(p2[1])
            if s1 == s2:
                return f"{n1 + n2}d{s1}"
        except (ValueError, TypeError):
            pass
    return f"{d1}+{d2}"


def get_level_for_xp(xp: int) -> int:
    level = 1
    for lvl, threshold in XP_THRESHOLDS:
        if xp >= threshold:
            level = lvl
        else:
            break
    return level


def init_player_db(player_file_path: str) -> str:
    global DB_CONNECTION
    try:
        with open(player_file_path, 'r', encoding="utf-8") as f:
            data = json.load(f)

        DB_CONNECTION = sqlite3.connect(":memory:")
        cursor = DB_CONNECTION.cursor()

        cursor.execute("CREATE TABLE player (key TEXT PRIMARY KEY, value TEXT)")

        for key, value in data.items():
            if isinstance(value, (dict, list)):
                cursor.execute("INSERT INTO player (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            elif isinstance(value, str):
                cursor.execute("INSERT INTO player (key, value) VALUES (?, ?)", (key, value))
            else:
                cursor.execute("INSERT INTO player (key, value) VALUES (?, ?)", (key, json.dumps(value)))

        DB_CONNECTION.commit()

        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("active_effects", "[]"))
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("_active_buff_data", "{}"))
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("temporary_hit_points", "0"))
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("capacity_multiplier", "1"))
        DB_CONNECTION.commit()

        # A character file that records what is equipped gets its armour class from
        # that set (the Forge writes the first consistent pair). Payloads without an
        # `equipped` value keep whatever `armor_class` they carry.
        if isinstance(data.get("equipped"), dict):
            _recompute_armor_class(cursor)
            DB_CONNECTION.commit()

        return f"Database initialized with player data from {player_file_path}."
    except Exception as e:
        return f"Failed to initialize database: {str(e)}"


def _db_val(cursor, key, default=None):
    cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
    row = cursor.fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return row[0]


# ── carrying capacity / encumbrance (SRD 5.1 variant) ──────────────────────

def _carry_get(cursor):
    """A `get(key, default)` reader over the player DB, as carrying.py expects."""
    return lambda key, default=None: _db_val(cursor, key, default)


def _item_effect_state(cursor) -> dict:
    """Aggregated item-effect channels for the player (scores, save/check/prof/spell bonuses).

    The single source of truth is `equipment.effect_state`, so the engine and the sheet agree.
    """
    if cursor is None:
        return {}
    return equipment.effect_state(_carry_get(cursor))


def _effective_stats(cursor) -> dict:
    """The player's six ability scores with worn/wielded score items applied (SRD 5.1)."""
    state = _item_effect_state(cursor)
    return dict(state.get("scores") or {})


def _save_proficiencies(cursor) -> set:
    """Lowercased 3-letter ability keys the player is proficient at saving with."""
    return {str(s).strip().lower()[:3] for s in (_db_val(cursor, "saves", []) or [])}


def _effective_prof_bonus(cursor) -> int:
    """The player's proficiency bonus including item grants (Ioun Stone, Mastery)."""
    base = int(_db_val(cursor, "proficiency_bonus", 2) or 2)
    return base + int(_item_effect_state(cursor).get("proficiency_bonus_mod") or 0)


def _derived_save(cursor, ability, situational: int = 0, against=None, damage=None,
                  condition=None) -> dict:
    """The player's full saving-throw modifier (SRD 5.1), engine-computed.

    ability modifier (effective) + proficiency bonus (if proficient with the save) + worn /
    attuned item save bonuses + a caller-supplied situational bonus or penalty. Scoped item
    advantage (`save_advantage` with `vs_spells`/`vs_damage:`/`vs_condition:`) is returned too.
    """
    key = str(ability or "").strip().lower()[:3]
    state = _item_effect_state(cursor)
    scores = state.get("scores") or {}
    ability_mod = _ability_mod(scores.get(key, scores.get("str", 10)))
    prof_bonus = int(_db_val(cursor, "proficiency_bonus", 2) or 2) \
        + int(state.get("proficiency_bonus_mod") or 0)
    proficient = key in _save_proficiencies(cursor)
    item_bonus = int(state.get("save_bonus") or 0)
    advantage, advantage_sources = equipment.save_advantage_for(
        _carry_get(cursor), against=against, damage=damage, condition=condition)
    return {
        "ability": key,
        "ability_modifier": ability_mod,
        "proficiency_bonus": prof_bonus if proficient else 0,
        "proficient": proficient,
        "item_bonus": item_bonus,
        "item_sources": list(state.get("save_bonus_sources") or []),
        "situational": int(situational or 0),
        "advantage": advantage,
        "advantage_sources": advantage_sources,
        "modifier": ability_mod + (prof_bonus if proficient else 0) + item_bonus
                    + int(situational or 0),
    }


def _list_names(value) -> list[str]:
    """Names from a player list whose entries may be strings or {name, ...} dicts."""
    out = []
    for entry in (value or []):
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("skill") or ""
        else:
            name = entry
        name = str(name or "").strip()
        if name:
            out.append(name)
    return out


def _player_skill_names(cursor) -> set:
    """Normalized names of the player's proficient skills (SRD 5.1)."""
    return {skills.normalize(n) for n in _list_names(_db_val(cursor, "skills", []))}


def _player_expertise(cursor) -> set:
    """Normalized names of the player's Expertise skills (double proficiency)."""
    return {skills.normalize(n) for n in _list_names(_db_val(cursor, "expertise", []))}


def _has_feature(cursor, needle) -> bool:
    """True when the player's feature list contains `needle` (case-insensitive substring)."""
    target = skills.normalize(needle)
    return any(target in skills.normalize(n) for n in _list_names(_db_val(cursor, "features", [])))


def _derived_check(cursor, check_name, ability=None, context=None, situational: int = 0) -> dict:
    """The player's full ability/skill check modifier (SRD 5.1), engine-computed.

    Ability modifier (effective) + proficiency bonus, doubled for Expertise or halved (rounded
    down) for Jack of All Trades on a check that is not already proficient + worn/attuned item
    `check_bonus`/`skill_bonus` + a caller-supplied situational bonus. `skill` is None for a plain
    ability check (or an unrecognised check name).
    """
    skill = skills.skill_name(check_name)
    ab = str(ability or "").strip().lower()[:3] or (skills.SKILL_ABILITIES.get(skill, "") if skill else "")
    scores = _effective_stats(cursor)
    ability_mod = _ability_mod(scores.get(ab, 10)) if ab else 0
    prof_bonus = _effective_prof_bonus(cursor)
    proficient = bool(skill) and skills.normalize(skill) in _player_skill_names(cursor)
    expertise = bool(skill) and skills.normalize(skill) in _player_expertise(cursor)
    joat = (not proficient) and _has_feature(cursor, "jack of all trades")
    if expertise:
        prof_part = 2 * prof_bonus
    elif proficient:
        prof_part = prof_bonus
    elif joat:
        prof_part = prof_bonus // 2
    else:
        prof_part = 0
    item_bonus, item_sources = equipment.check_bonus_for(_carry_get(cursor), check_name, context)
    return {
        "skill": skill,
        "ability": ab,
        "ability_modifier": ability_mod,
        "proficiency_bonus": prof_part,
        "proficient": proficient,
        "expertise": expertise,
        "jack_of_all_trades": joat,
        "item_bonus": item_bonus,
        "item_sources": list(item_sources or []),
        "situational": int(situational or 0),
        "modifier": ability_mod + prof_part + item_bonus + int(situational or 0),
    }


def _passive_scores(cursor) -> dict:
    """SRD 5.1 passive scores (10 + the derived modifier) for the three passive skills."""
    return {skill.lower(): 10 + _derived_check(cursor, skill)["modifier"]
            for skill in ("Perception", "Investigation", "Insight")}


def _carry_block(cursor) -> dict:
    """Derived carrying state (never persisted: stripped before saving). Uses effective STR."""
    scores = _effective_stats(cursor)

    def get(key, default=None):
        if key == "stats":
            return scores
        return _db_val(cursor, key, default)

    state = carrying.carry_state(get)
    # Passive item speed (Boots of Striding and Springing set a 30 ft floor and ignore
    # encumbrance/heavy-armour reduction). Active speed items are spell-style buffs, not here.
    speed = equipment.speed_state(get)
    if speed["ignore_encumbrance"]:
        state["speed_penalty"] = 0
        state["speed"] = speed["walk"]
    else:
        state["speed"] = max(0, speed["walk"] - int(state.get("speed_penalty") or 0))
    state["speed_base"] = speed["base"]
    state["speeds"] = speed["modes"]
    # SRD 5.1 exhaustion: speed halved at level 2, 0 at level 5.
    exh = _exhaustion_effects(_exhaustion_level(cursor))
    if exh["speed_multiplier"] != 1.0:
        state["speed"] = int(state["speed"] * exh["speed_multiplier"])
        state["speeds"] = {k: int(v * exh["speed_multiplier"])
                           for k, v in (state.get("speeds") or {}).items()}
        state["exhaustion_speed"] = exh["level"]
    return state


# ── equipped items / derived armour class (SRD 5.1) ───────────────────────────

def _equipment_block(cursor) -> dict:
    """Derived equipped-items state (never persisted: stripped before saving)."""
    return equipment.equipment_state(_carry_get(cursor))


def _recompute_armor_class(cursor) -> int | None:
    """Recompute everything derived from the equipped set and effective scores (SRD 5.1).

    Called after every equip/unequip/attune/level change: sets `armor_class` from the equipped set,
    recomputes the spell save DC / spell attack from the effective spellcasting ability, and
    reconciles the CON-score contribution to max HP. No-op (for each part) without an `equipped`
    or `spellcasting` value.
    """
    ac = None
    if isinstance(_db_val(cursor, "equipped", None), dict):
        ac = _equipment_block(cursor)["base_ac"]
        _db_set(cursor, "armor_class", ac)
    _recompute_spellcasting(cursor)
    _recompute_hit_points(cursor)
    return ac


def _recompute_spellcasting(cursor) -> dict | None:
    """Recompute the player's spell save DC and spell attack from the effective sheet.

    SRD 5.1: DC = 8 + proficiency bonus + spellcasting ability modifier (+ item spell-DC bonus);
    attack = proficiency bonus + ability modifier (+ item spell-attack bonus).
    """
    sc = _db_val(cursor, "spellcasting", None)
    if not isinstance(sc, dict) or not sc:
        return None
    stat_key = ABILITY_TO_STAT.get(str(sc.get("ability") or "").lower())
    if not stat_key:
        return None
    scores = _effective_stats(cursor)
    mod = _ability_mod(scores.get(stat_key, 10))
    prof = _effective_prof_bonus(cursor)
    state = _item_effect_state(cursor)
    dc = 8 + prof + mod + int(state.get("spell_dc_bonus") or 0)
    attack = prof + mod + int(state.get("spell_attack_bonus") or 0)
    if sc.get("dc") != dc or sc.get("attack_modifier") != attack:
        sc["dc"] = dc
        sc["attack_modifier"] = attack
        _db_set(cursor, "spellcasting", sc)
    return {"dc": dc, "attack_modifier": attack}


def _recompute_hit_points(cursor) -> int | None:
    """Apply the effective-CON contribution to max HP (SRD 5.1: max changes retroactively).

    Reconciles the stored `item_hp_delta` (the HP added by worn/wielded CON score items) with the
    current effective CON, adjusting max and current HP by the difference (`Δcon_mod x level`).
    Idempotent; no-op without an `equipped` value.
    """
    if not isinstance(_db_val(cursor, "equipped", None), dict):
        _recompute_exhaustion_hp(cursor)
        return None
    base = _db_val(cursor, "stats", {}) or {}
    if isinstance(base, str):
        base = json.loads(base)
    effective = _effective_stats(cursor)
    level = int(_db_val(cursor, "level", 1) or 1)
    base_mod = _ability_mod(base.get("con", 10))
    eff_mod = _ability_mod(effective.get("con", base.get("con", 10)))
    hp_per_level = int(_item_effect_state(cursor).get("hp_per_level") or 0)
    new_delta = (eff_mod - base_mod) * level + hp_per_level * level
    old_delta = int(_db_val(cursor, "item_hp_delta", 0) or 0)
    diff = new_delta - old_delta
    total = int(_db_val(cursor, "total_hit_points", 1) or 1)
    if diff:
        total = max(1, total + diff)
        current = int(_db_val(cursor, "current_hit_points", 0) or 0)
        # Never revive: a larger CON changes the maximum; a downed character stays down.
        if current > 0:
            current = min(max(0, current + diff), total)
        _db_set(cursor, "total_hit_points", total)
        _db_set(cursor, "current_hit_points", current)
    _db_set(cursor, "item_hp_delta", new_delta)
    _recompute_exhaustion_hp(cursor)
    return int(_db_val(cursor, "total_hit_points", total) or total)


def _recompute_exhaustion_hp(cursor) -> int:
    """SRD 5.1 exhaustion: at level 4 the HP maximum is halved.

    Reconciled through a persisted `exhaustion_hp_delta` (the same shape as `item_hp_delta`), so
    recovering below level 4 restores the full maximum automatically. Never revives a downed
    character. Idempotent; safe with no equipped set.
    """
    level = _exhaustion_level(cursor)
    total = int(_db_val(cursor, "total_hit_points", 1) or 1)
    old_penalty = int(_db_val(cursor, "exhaustion_hp_delta", 0) or 0)
    base = total - old_penalty  # the maximum without the exhaustion penalty
    target = max(1, int(base * (0.5 if level >= 4 else 1.0)))
    new_penalty = target - base
    if new_penalty != old_penalty:
        current = int(_db_val(cursor, "current_hit_points", 0) or 0)
        _db_set(cursor, "total_hit_points", str(target))
        if current > 0:
            _db_set(cursor, "current_hit_points", str(min(current, target)))
        _db_set(cursor, "exhaustion_hp_delta", str(new_penalty))
        DB_CONNECTION.commit()
    return target


def _clear_equipped(cursor, name) -> bool:
    """Drop an item from the equipped set (it left the inventory). True if changed.

    Keeps `equipped` from dangling on a name that is no longer carried, and recomputes
    the armour class from what remains.
    """
    equipped = _db_val(cursor, "equipped", None)
    if not isinstance(equipped, dict):
        return False
    changed = False
    if equipped.get("armor") == name:
        equipped["armor"] = None
        changed = True
    hands = equipped.get("hands")
    if isinstance(hands, list):
        hands = (list(hands) + [None, None])[:2]
        for index, held in enumerate(hands):
            if held == name:
                hands[index] = None
                changed = True
        equipped["hands"] = hands
    worn = equipped.get("worn")
    if isinstance(worn, list) and name in worn:
        equipped["worn"] = [w for w in worn if w != name]
        changed = True
    if changed:
        _db_set(cursor, "equipped", equipped)
        _recompute_armor_class(cursor)
    return changed


def _rename_equipped(cursor, old, new) -> bool:
    """Follow a renamed inventory item through the equipped set and attunement (True if changed).

    A rename must not orphan the item from how it is worn/wielded or from its attunement.
    """
    changed = False
    equipped = _db_val(cursor, "equipped", None)
    if isinstance(equipped, dict):
        if equipped.get("armor") == old:
            equipped["armor"] = new
            changed = True
        hands = equipped.get("hands")
        if isinstance(hands, list):
            hands = (list(hands) + [None, None])[:2]
            for index, held in enumerate(hands):
                if held == old:
                    hands[index] = new
                    changed = True
            equipped["hands"] = hands
        worn = equipped.get("worn")
        if isinstance(worn, list) and old in worn:
            equipped["worn"] = [new if w == old else w for w in worn]
            changed = True
        if changed:
            _db_set(cursor, "equipped", equipped)
            _recompute_armor_class(cursor)
    attuned = _db_val(cursor, "attuned", None)
    if isinstance(attuned, list) and old in attuned:
        _db_set(cursor, "attuned", [new if a == old else a for a in attuned])
        changed = True
    return changed


WORN_KINDS = {"cloak", "boots", "gloves", "gauntlets", "bracers", "headwear", "ring", "amulet",
              "belt"}

# SRD 5.1 "Multiple Items of the Same Kind": a character can't normally wear more than one pair
# of footwear, one pair of gloves or gauntlets, one pair of bracers, one item of headwear, or one
# cloak. Rings, amulets, necklaces and belts are NOT on that list (only the attunement rule "one
# copy of an item" applies to them).
WORN_EXCLUSIVE = {
    "boots": "footwear",
    "gloves": "hands",
    "gauntlets": "hands",
    "bracers": "arms",
    "cloak": "shoulders",
    "headwear": "head",
}


def _worn_group(declared) -> str | None:
    """The SRD "same kind" wearing group for an item (None when unconstrained)."""
    kind = str((declared or {}).get("kind") or "").strip().lower()
    return WORN_EXCLUSIVE.get(kind)


def _inventory_entry(inventory, name) -> dict:
    """The inventory entry (dict) an item name points at, else {}."""
    return next((e for e in (inventory or [])
                 if isinstance(e, dict) and e.get("name") == name), {})


def _is_worn_slot(item, declared) -> bool:
    """True when equipping should put the item in the `worn` container rather than a hand.

    5e has no slots, but clothing, cloaks, boots, gloves, bracers, headwear, rings and amulets are
    worn rather than held — they need somewhere to live so attunement and pairing rules have
    something to bind to.
    """
    if equipment.is_weapon(item) or equipment.is_shield(item) or equipment.is_armor(item):
        return False
    kind = str((declared or {}).get("kind") or "").strip().lower()
    return kind in WORN_KINDS or equipment.is_clothing(item)


def _reject_item(error: str, reason: str, item: str, nudge: str, **extra) -> dict:
    """A refused inventory add: the GM is told exactly how to call the tool correctly."""
    result = {"success": False, "error": error, "item": item, "reason": reason,
              "gm_instruction": nudge}
    result.update(extra)
    return result


def _device_add_entry(cursor, name: str, desc: str, weight):
    """The Device vocabulary, enforced: an add is either engine-owned or refused.

    - The Device itself is engine-owned. It is seeded with the character; a second one
      (however it is spelled) is refused. If it was removed in play, the GM may restore it
      and the engine writes the description.
    - A part is accepted only by its exact canonical name; the engine supplies the
      description and stamps the age it was recovered in. Never a numeric edit -- the
      inventory entry is the only record of where a part came from.

    Returns ``{"entry": {...}}`` for an accepted device add, ``{"reject": {...}}`` for a
    refusal, or None when the name is not a Device thing at all.
    """
    era = str(_db_val(cursor, "era", "") or "").strip().lower()
    inventory = _db_val(cursor, "inventory", []) or []
    if device.is_device(name):
        if device.has_device(inventory):
            return {"reject": _reject_item(
                "device_engine_owned",
                f"'{device.device_name()}' is created with the Traveller and owned by the engine.",
                name,
                "Do not add or rename the Device -- it is already in the inventory. To recover "
                "a part, add it by its exact name, e.g. update_player_list(key='inventory', "
                "item='Escapement', action='add').")}
        # Removed in play (the GM decides): restoring it is allowed, engine-described.
        return {"entry": device.device_entry()}
    part = device.canonical_part(name)
    if part:
        if not era:
            return {"reject": _reject_item(
                "device_not_in_this_game",
                f"'{part}' is a part of the Device, which exists only in the Time Traveler game.",
                part,
                "This is a classic world -- there is no Device and no parts. Do not add one.")}
        if device.entry_for_part(inventory, part) is not None:
            return {"reject": _reject_item(
                "already_exists", f"'{part}' is already in the inventory.", part,
                "A part can only be recovered once.")}
        return {"entry": device.part_entry(part, era)}
    return None


def _validate_item_add(name: str, declared: dict, weight) -> dict | None:
    """Reject a combat-relevant inventory add that is missing required stats (SRD 5.1).

    The engine derives attack/damage/AC from the declared fields, so a weapon or armour that is
    neither a known SRD archetype nor explicitly statted cannot be used. Returns None when the add
    is acceptable.
    """
    base = declared.get("base")
    known_archetype = bool(base) and (equipment.is_weapon(base) or equipment.is_armor(base)
                                      or equipment.is_shield(base))
    has_stats = bool(declared.get("damage_dice") or declared.get("ac"))
    has_bonus = bool(equipment.item_effects(declared))
    kind = str(declared.get("kind") or "").strip().lower()

    # 1. An unknown `base` with nothing else to derive from is unusable. A declared bonus or kind
    #    is enough for a homebrew worn item (e.g. base='Ring of Protection', kind='ring').
    if base and not known_archetype and not has_stats and not has_bonus:
        return _reject_item(
            "unknown_base_no_stats",
            f"Unknown base archetype '{base}' and no explicit stats — the engine cannot derive "
            f"hands, weight or combat stats from it.", name,
            f"Re-add with a real SRD base (e.g. base='Dagger'), or declare the stats: "
            f"update_player_list(key='inventory', item='{name}', action='add', "
            f"damage_dice='1d4', damage_type='piercing', properties=['Finesse']).",
            unknown_base=base)

    # 2. Weapon intent (dice/type/properties) with no damage dice and no resolvable archetype.
    if (declared.get("damage_type") or declared.get("properties")) and not declared.get("damage_dice"):
        wpn = equipment.weapon_entry(name, base) or {}
        if not wpn.get("damage"):
            return _reject_item(
                "weapon_stats_missing",
                f"No damage is known for '{name}' — declare damage_dice or a known weapon base.",
                name,
                f"Re-add as a weapon: update_player_list(key='inventory', item='{name}', "
                f"action='add', base='Dagger', damage_dice='1d4', damage_type='piercing', "
                f"properties=['Finesse','Light']).")

    # 3. A worn magic item (bonus or paired) with no `kind` would be held, not worn.
    worn_intent = has_bonus or bool(declared.get("pair"))
    if worn_intent and not kind:
        resolves_worn = (equipment.is_weapon(name, base) or equipment.is_armor(name, base)
                         or equipment.is_shield(name, base) or equipment.is_clothing(name, base))
        if not resolves_worn:
            return _reject_item(
                "worn_kind_missing",
                f"'{name}' grants a magic bonus but has no `kind`, so it would be held rather "
                f"than worn and its bonus would not apply.", name,
                f"Declare the slot: update_player_list(key='inventory', item='{name}', "
                f"action='add', kind='ring' (or 'amulet', 'cloak', 'boots', 'gloves', "
                f"'gauntlets', 'bracers', 'headwear', 'belt'), save_bonus=<n>).")
    return None


def _in_active_combat() -> bool:
    """True while the combat registry holds a living, hostile, active non-player.

    Donning or doffing armour takes minutes, so it cannot happen mid-fight; shields take one
    action and are always allowed. Combat is over when the last hostile has dropped to 0 HP
    (``killed``), or fled or surrendered (an explicit ``status``). Allies and neutrals never
    keep a fight alive.
    """
    for entry in (_COMBAT_REGISTRY or {}).values():
        if not isinstance(entry, dict) or entry.get("is_player"):
            continue
        if str(entry.get("role") or "hostile").strip().lower() != "hostile":
            continue
        if entry.get("killed"):
            continue
        if str(entry.get("status") or "active").strip().lower() != "active":
            continue
        return True
    return False


def _blocked_action(cursor, error: str, reason: str, item: str | None = None,
                    narrative: str = "") -> dict:
    """A refused player action (SRD 5.1 equipped items): the action is spent.

    The engine does not track whose turn it is, so the turn loss is signalled here for the Game
    Master to apply: tell the player why the action failed and move on. No dice are rolled.
    """
    return {
        "success": False,
        "error": error,
        "reason": reason,
        "item": item,
        "turn_lost": True,
        "action_consumed": True,
        "gm_instruction": ("Tell the player the action is wasted and why — the turn is spent. "
                           "Move on to the next combatant."),
        "equipment": _equipment_block(cursor),
        "narrative_format": narrative or f"Action wasted — {reason}",
    }


def _action_failure(error: str, narrative_format: str, **fields) -> dict:
    """A refused player action that costs nothing (no slot, no turn spent).

    The GM gets the `error` (and any extra fields); the engine composes the player-facing
    `narrative_format` line into the turn's Mechanics block. Tool-shape / GM-correctable errors
    (a missing parameter, an invalid notation, an unknown registry name) must NOT use this —
    they stay GM-only.
    """
    return {"success": False, "error": error, "narrative_format": narrative_format, **fields}


def _ability_mod(score) -> int:
    try:
        return (int(score) - 10) // 2
    except (TypeError, ValueError):
        return 0


def _is_light_melee(name, inventory) -> bool:
    """True for a light melee weapon (SRD two-weapon fighting needs one in each hand)."""
    entry = next((e for e in (inventory or []) if isinstance(e, dict) and e.get("name") == name), {})
    base = entry.get("base")
    wpn = equipment.weapon_entry(name, base) or {}
    props = [str(p).lower() for p in equipment.properties_for(name, base, entry)]
    return bool(wpn.get("melee", True)) and any(p.startswith("light") for p in props)


def _worn_item_bonuses(cursor, inventory):
    """Sum worn items' attack/damage bonuses (SRD 5.1), by attunement/pair gating.

    A thin projection of the general effect model (`equipment.effect_state`); returns
    (attack, damage, sources).
    """
    state = _item_effect_state(cursor)
    attack = int(state.get("worn_attack_bonus") or 0)
    damage = int(state.get("worn_damage_bonus") or 0)
    sources = list(state.get("worn_attack_sources") or [])
    for src in state.get("worn_damage_sources") or []:
        if src not in sources:
            sources.append(src)
    return attack, damage, sources


def _derive_weapon_attack(cursor, weapon: str, inventory: list, hands_free: int = 1,
                          off_hand: bool = False) -> dict:
    """Derived attack/damage for a player's equipped weapon (SRD 5.1).

    The item's declared stats (damage_dice/damage_type/attack_bonus/damage_bonus) win over its SRD
    archetype (`base` or its own name). Ability: Finesse -> the better of STR/DEX, otherwise a melee
    weapon uses STR and a ranged one DEX. Proficiency adds the proficiency bonus (and a warning
    when the character is not proficient, SRD: no bonus). A Versatile weapon uses its two-handed die
    while the other hand is free, and an off-hand (two-weapon fighting) attack takes no ability
    modifier to damage unless that modifier is negative. Magic bonuses apply only while the item is
    attuned / its pair is complete.
    """
    entry = next((e for e in inventory if isinstance(e, dict) and e.get("name") == weapon), {})
    base = entry.get("base")
    wpn = equipment.weapon_entry(weapon, base) or {}
    props = [str(p) for p in equipment.properties_for(weapon, base, entry)]
    for eff in equipment.item_effects(entry):
        if eff.get("type") == "weapon_property" and eff.get("property"):
            props.append(str(eff["property"]))
    low = [p.lower() for p in props]
    melee = bool(wpn.get("melee", True))
    stats = _effective_stats(cursor)
    str_mod = _ability_mod(stats.get("str", 10))
    dex_mod = _ability_mod(stats.get("dex", 10))
    if any("finesse" in p for p in low):
        ability, ability_mod = (("dexterity", dex_mod) if dex_mod >= str_mod else ("strength", str_mod))
    elif melee:
        ability, ability_mod = "strength", str_mod
    else:
        ability, ability_mod = "dexterity", dex_mod

    proficiencies = {str(p).strip().lower() for p in (_db_val(cursor, "weapon_proficiencies", []) or [])}
    for grant in (_item_effect_state(cursor).get("granted_proficiencies") or []):
        if str(grant.get("category") or "") in ("weapon", "weapons"):
            proficiencies.add(str(grant.get("value") or "").lower())
    category = str(wpn.get("category") or "").lower()
    # Proficiency is a fact about the weapon's KIND, not its display name: a magic or
    # reflavoured weapon ("Dagger +1", "Storm's Edge" base Longsword) still matches its
    # base (or category) proficiency.
    base_name = str(base or weapon).strip().lower()
    proficient = (not proficiencies
                  or f"{category} weapons" in proficiencies
                  or base_name in proficiencies
                  or weapon.strip().lower() in proficiencies)
    prof_bonus = _effective_prof_bonus(cursor)

    # Magic bonuses: attunement / paired-item gating, from the weapon itself and any worn item.
    equipped = _db_val(cursor, "equipped", {}) or {}
    worn = [str(w) for w in (equipped.get("worn") or []) if w]
    equipped_names = ([equipped.get("armor")] if equipped.get("armor") else []) \
        + [h for h in (equipped.get("hands") or []) if h] + worn
    suppressed = equipment.bonus_suppressed_reason(weapon, inventory,
                                                   _db_val(cursor, "attuned", []) or [],
                                                   equipped_names)
    weapon_effects = equipment.item_effects(entry)
    attack_bonus = sum(int(e.get("value") or 0) for e in weapon_effects
                       if e.get("type") == "attack_bonus") if suppressed is None else 0
    damage_bonus = sum(int(e.get("value") or 0) for e in weapon_effects
                       if e.get("type") == "damage_bonus") if suppressed is None else 0
    worn_attack, worn_damage, worn_sources = _worn_item_bonuses(cursor, inventory)

    # Damage dice: a declared value wins; a Versatile weapon uses its two-handed die with a free hand.
    declared_dice = entry.get("damage_dice")
    damage_dice = declared_dice or wpn.get("damage")
    versatile_die = None
    for prop in props:
        if prop.lower().startswith("versatile"):
            match = re.search(r"(\d+d\d+)", prop)
            if match:
                versatile_die = match.group(1)
    if not declared_dice and versatile_die and hands_free >= 1:
        damage_dice = versatile_die

    damage_modifier = ability_mod + damage_bonus + worn_damage
    if off_hand:
        # Two-weapon fighting: no ability modifier on the bonus attack's damage unless negative.
        damage_modifier = min(ability_mod, 0) + damage_bonus + worn_damage

    return {
        "attack_modifier": ability_mod + (prof_bonus if proficient else 0) + attack_bonus + worn_attack,
        "damage_dice": str(damage_dice) if damage_dice else None,
        "damage_modifier": damage_modifier,
        "damage_type": entry.get("damage_type") or wpn.get("damage_type") or "",
        "ability": ability,
        "ability_modifier": ability_mod,
        "proficient": proficient,
        "melee": melee,
        "properties": props,
        "two_handed": any("two-handed" in p for p in low),
        "light_melee": melee and any(p.startswith("light") for p in low),
        "versatile_die": versatile_die,
        "bonus_suppressed": suppressed,
        "item_bonus": {
            "attack": attack_bonus + worn_attack,
            "damage": damage_bonus + worn_damage,
            "sources": ([weapon] if (attack_bonus or damage_bonus) else []) + worn_sources,
        },
    }


def _covers_material(cursor, held_names) -> bool:
    """True when a held item covers a spell's material components (focus or component pouch).

    A Cleric's or Paladin's holy symbol can be borne on a shield, so their shield counts as a focus.
    """
    if any(equipment.is_focus(name) for name in held_names or []):
        return True
    character_class = str(_db_val(cursor, "character_class", "") or "")
    if character_class in ("Cleric", "Paladin"):
        return any(equipment.is_shield(name) for name in held_names or [])
    return False


_SPELL_COMPONENTS = None


def _spell_components() -> dict:
    """{normalized spell name: 'VSM'} from config/components.yml (cached).

    The SRD 5.1 components per spell. A spell missing from the table is treated as needing a
    hand (V,S,M) and the Game Master can override per cast with components='...'.
    """
    global _SPELL_COMPONENTS
    if _SPELL_COMPONENTS is None:
        table = {}
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "config", "components.yml")
            if yaml is not None and os.path.exists(path):
                with open(path, encoding="utf-8") as handle:
                    for entry in yaml.safe_load(handle) or []:
                        if isinstance(entry, dict) and entry.get("name"):
                            table[carrying.normalize(entry["name"])] = \
                                str(entry.get("components") or "").upper()
        except Exception:  # noqa: BLE001 - a missing/!broken catalog means "no components known"
            table = {}
        _SPELL_COMPONENTS = table
    return _SPELL_COMPONENTS


def _roll_d20(advantage: bool = False, disadvantage: bool = False):
    """5e d20 with advantage/disadvantage, which cancel each other out.

    Returns ``(d20, rolls, mode, cancelled)``: `rolls` is the pair for the
    narrative, `mode` is "advantage"/"disadvantage"/None.
    """
    if advantage and disadvantage:
        return random.randint(1, 20), None, None, True
    if advantage or disadvantage:
        r1, r2 = random.randint(1, 20), random.randint(1, 20)
        if advantage:
            return max(r1, r2), [r1, r2], "advantage", False
        return min(r1, r2), [r1, r2], "disadvantage", False
    return random.randint(1, 20), None, None, False


def _is_player_actor(cursor, actor) -> bool:
    """True when `actor` refers to the player (the '{player_name}' placeholder or their name)."""
    if actor == "{player_name}":
        return True
    if DB_CONNECTION is None or cursor is None:
        return False
    name = _db_val(cursor, "name", None)
    return bool(name) and actor == name


def _encumbrance(cursor, is_player: bool, ability=None):
    """(disadvantage?, sources) from heavy encumbrance for a player actor.

    SRD 5.1 variant: heavily encumbered imposes disadvantage on attack rolls, on
    ability checks and on saving throws that use Strength, Dexterity or
    Constitution. Non-player actors are never affected.
    """
    if not is_player or DB_CONNECTION is None:
        return False, []
    return carrying.heavy_encumbrance(_carry_get(cursor), ability)


def _armor_disadvantage(cursor, is_player: bool, ability=None):
    """(disadvantage?, sources) from wearing armour/shields the player is not proficient with.

    SRD 5.1: disadvantage on any STR or DEX ability check, saving throw or attack roll
    (spellcasting is refused separately). Non-player actors are never affected.
    """
    if not is_player or DB_CONNECTION is None:
        return False, []
    state = _equipment_block(cursor)
    if state.get("armor_proficient", True):
        return False, []
    if ability is not None:
        key = str(ability).strip().lower()[:3]
        if key not in ("str", "dex"):
            return False, []
    sources = state.get("proficiency_sources") or ["not proficient with worn armour"]
    return True, sources


def _roll_disadvantage(cursor, is_player: bool, ability=None):
    """(disadvantage?, sources) merging every source: heavy encumbrance and non-proficient armour."""
    heavy, heavy_sources = _encumbrance(cursor, is_player, ability)
    armor, armor_sources = _armor_disadvantage(cursor, is_player, ability)
    sources = []
    for source in [*heavy_sources, *armor_sources]:
        if source not in sources:
            sources.append(source)
    return bool(heavy or armor), sources


def _encumbrance_note(result: dict, sources: list, cancelled: bool = False) -> None:
    """Disclose where the disadvantage came from (and an eaten advantage)."""
    if sources:
        result["disadvantage_sources"] = list(sources)
    if cancelled:
        result["advantage_cancelled"] = True


def _db_set(cursor, key, value):
    if isinstance(value, (dict, list)):
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, json.dumps(value)))
    else:
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, str(value)))


def _player_name(cursor):
    return _db_val(cursor, "name", "Player")


def _target_label(cursor, target_name, is_player_target=False) -> str:
    """Display name of an action's target (SRD mechanics lines).

    A real name is used as-is; `{player_name}` or an empty name for a player target resolves to the
    sheet name; an empty non-player target yields "" so callers can omit the suffix.
    """
    name = str(target_name or "").strip()
    if name and name != "{player_name}":
        return name
    if is_player_target or name == "{player_name}":
        if cursor is not None:
            return str(_db_val(cursor, "name", "") or "").strip() or "the player"
        return "the player"
    return ""


def _target_suffix(cursor, target_name, is_player_target=False) -> str:
    """` → Name` for a mechanics line, or '' when the target is unknown."""
    label = _target_label(cursor, target_name, is_player_target)
    return f" \u2192 {label}" if label else ""


def _hp_status_tag(current, total):
    if total <= 0:
        return "Unknown"
    if current == 0:
        return "Unconscious"
    ratio = current / total
    if ratio < 0.25:
        return "Critical"
    elif ratio < 0.50:
        return "Bloodied"
    elif ratio < 0.75:
        return "Wounded"
    else:
        return "Healthy"


def _format_hp_status(current, total):
    tag = _hp_status_tag(current, total)
    pct = round((current / total) * 100) if total > 0 else 0
    return f"HP: {current}/{total} ({tag} {pct}%)"


def _parse_and_roll_dice(dice_notation):
    try:
        if isinstance(dice_notation, (int, float)):
            return 0, [], int(dice_notation)
        s = str(dice_notation).strip().lower()
        if s.isdigit():
            return 0, [], int(s)
        parts = s.split('d')
        if len(parts) != 2:
            return None, [], 0
        num_dice = int(parts[0]) if parts[0] else 1
        die_size = int(parts[1])
        if num_dice == 0 or die_size == 0:
            return 0, [], 0
        if num_dice < 0 or die_size < 0:
            return None, [], 0
        rolls = [random.randint(1, die_size) for _ in range(num_dice)]
        return die_size, rolls, sum(rolls)
    except (ValueError, TypeError):
        return None, [], 0


def _caster_spellcasting_mod(cursor) -> int:
    """The caster's spellcasting ability modifier from the player DB (0 if unknown)."""
    if cursor is None:
        return 0
    sc = _db_val(cursor, "spellcasting", {}) or {}
    if isinstance(sc, str):
        try:
            sc = json.loads(sc)
        except (json.JSONDecodeError, TypeError):
            sc = {}
    stat_key = ABILITY_TO_STAT.get(str(sc.get("ability", "")).lower())
    if not stat_key:
        return 0
    stats = _effective_stats(cursor)
    return _ability_mod(stats.get(stat_key, 10))


# ── exhaustion levels (SRD 5.1) ───────────────────────────────────────────

EXHAUSTION_MAX = 6


def _exhaustion_effects(level) -> dict:
    """The SRD 5.1 exhaustion table for a level 0-6."""
    try:
        level = max(0, min(EXHAUSTION_MAX, int(level or 0)))
    except (TypeError, ValueError):
        level = 0
    return {
        "level": level,
        "check_disadvantage": level >= 1,
        "speed_multiplier": 0.0 if level >= 5 else (0.5 if level >= 2 else 1.0),
        "attack_disadvantage": level >= 3,
        "save_disadvantage": level >= 3,
        "hp_multiplier": 0.5 if level >= 4 else 1.0,
        "dead": level >= 6,
    }


def _exhaustion_level(cursor) -> int:
    """The player's exhaustion level (0-6), from the numeric sheet field."""
    if cursor is None:
        return 0
    try:
        return max(0, min(EXHAUSTION_MAX, int(_db_val(cursor, "exhaustion", 0) or 0)))
    except (TypeError, ValueError):
        return 0


def _exhaustion_level_for(cursor, name, is_player=False) -> int:
    """Exhaustion level for a player or a registered NPC."""
    if is_player:
        return _exhaustion_level(cursor)
    entry = _COMBAT_REGISTRY.get(name) or {}
    try:
        return max(0, min(EXHAUSTION_MAX, int(entry.get("exhaustion") or 0)))
    except (TypeError, ValueError):
        return 0


def _apply_exhaustion_change(cursor, delta, reason=None) -> dict:
    """Change the player's exhaustion level and reconcile derived state (HP max, speed)."""
    old = _exhaustion_level(cursor)
    new = max(0, min(EXHAUSTION_MAX, old + int(delta or 0)))
    _db_set(cursor, "exhaustion", str(new))
    DB_CONNECTION.commit()
    if new >= EXHAUSTION_MAX:
        _db_set(cursor, "current_hit_points", "0")
        _end_concentration(cursor, reason="exhaustion 6")
        DB_CONNECTION.commit()
    max_hp = _recompute_exhaustion_hp(cursor)
    return {"old": old, "new": new, "delta": new - old, "max_hp": max_hp,
            "effects": _exhaustion_effects(new), "reason": reason}


# ── concentration (SRD 5.1) ───────────────────────────────────────────────

def _get_concentration(cursor) -> dict | None:
    if cursor is None:
        return None
    conc = _db_val(cursor, "concentration", None)
    if isinstance(conc, str):
        try:
            conc = json.loads(conc)
        except (json.JSONDecodeError, TypeError):
            conc = None
    return conc if isinstance(conc, dict) and conc.get("spell") else None


def _remove_active_effect(cursor, name) -> dict:
    """Revert and delete one active spell effect; shared by update_player_list and concentration."""
    buff_data_raw = _db_val(cursor, "_active_buff_data", {})
    if isinstance(buff_data_raw, str):
        buff_data_raw = json.loads(buff_data_raw)
    if not isinstance(buff_data_raw, dict) or name not in buff_data_raw:
        return {}
    reverted = {}
    for entry in buff_data_raw[name]:
        if not isinstance(entry, dict):
            continue
        if entry.get("delta") is not None:
            modify_player_numeric(key=entry["field"], delta=-entry["delta"])
            reverted[entry["field"]] = {"delta": -entry["delta"]}
        else:
            reverted.setdefault(entry.get("field") or entry.get("kind"), []).append(
                entry.get("value"))
    del buff_data_raw[name]
    _db_set(cursor, "_active_buff_data", buff_data_raw)
    effects_list = _db_val(cursor, "active_effects", []) or []
    if isinstance(effects_list, str):
        effects_list = json.loads(effects_list)
    _db_set(cursor, "active_effects", [e for e in effects_list if e != name])
    DB_CONNECTION.commit()
    return reverted


def _end_concentration(cursor, reason="") -> dict | None:
    """End the player's concentration, reverting its active effect. Returns what ended."""
    conc = _get_concentration(cursor)
    if not conc:
        return None
    spell = conc.get("spell")
    reverted = _remove_active_effect(cursor, spell)
    _db_set(cursor, "concentration", {})
    DB_CONNECTION.commit()
    return {"spell": spell, "reverted": reverted, "reason": reason}


def _set_concentration(cursor, spell) -> dict | None:
    """Begin concentrating on `spell`, ending any previous concentration first."""
    ended = _end_concentration(cursor, reason=f"replaced by {spell}")
    _db_set(cursor, "concentration", {"spell": spell})
    DB_CONNECTION.commit()
    return ended


def _concentration_save(cursor, damage, source="damage") -> dict | None:
    """SRD 5.1: CON save (DC 10 or half the damage) when a concentrating player takes damage."""
    conc = _get_concentration(cursor)
    if not conc or damage <= 0:
        return None
    spell = conc.get("spell")
    dc = max(10, int(damage) // 2)
    save_detail = _derived_save(cursor, "con")
    cond_dis, _auto_fail, _ = _condition_save_effects(_player_conditions(cursor), "con")
    exh_dis = _exhaustion_effects(_exhaustion_level(cursor))["save_disadvantage"]
    roll, rolls, _mode, _ = _roll_d20(
        advantage=bool(save_detail.get("advantage")), disadvantage=(cond_dis or exh_dis))
    total = roll + save_detail["modifier"]
    success = total >= dc
    result = {
        "spell": spell,
        "dc": dc,
        "roll": roll,
        "modifier": save_detail["modifier"],
        "total": total,
        "success": success,
        "source": source,
    }
    if rolls:
        result["rolls"] = rolls
    if not success:
        result["broken"] = _end_concentration(cursor, reason=f"failed CON save (DC {dc})")
    return result


# ── death saving throws (SRD 5.1) ─────────────────────────────────────────

def _reset_death_saves(cursor):
    _db_set(cursor, "death_save_successes", "0")
    _db_set(cursor, "death_save_failures", "0")
    DB_CONNECTION.commit()


def _clear_death_saves(cursor):
    if int(_db_val(cursor, "death_save_successes", 0) or 0) or \
            int(_db_val(cursor, "death_save_failures", 0) or 0):
        _reset_death_saves(cursor)


def _add_death_save_failure(cursor, count=1) -> int:
    failures = min(3, int(_db_val(cursor, "death_save_failures", 0) or 0) + count)
    _db_set(cursor, "death_save_failures", str(failures))
    DB_CONNECTION.commit()
    return failures


def _apply_hp_change(cursor, delta):
    current_hp = int(_db_val(cursor, "current_hit_points", 0))
    total_hp = int(_db_val(cursor, "total_hit_points", 1))
    thp = int(_db_val(cursor, "temporary_hit_points", 0))

    thp_absorbed = 0
    effect_expired = ""
    if delta < 0 and thp > 0:
        damage = abs(delta)
        if thp >= damage:
            thp_absorbed = damage
            new_thp = thp - damage
            _db_set(cursor, "temporary_hit_points", str(new_thp))
            DB_CONNECTION.commit()
            if new_thp == 0:
                effect_expired = _cleanup_thp_effects(cursor)
            result = {
                "success": True,
                "key": "current_hit_points",
                "old_value": current_hp,
                "new_value": current_hp,
                "delta": 0,
                "hp_status": _format_hp_status(current_hp, total_hp),
                "max_hp": total_hp,
                "temporary_hit_points": {"old": thp, "new": new_thp, "absorbed": damage},
                "message": f"{damage} damage absorbed by temporary HP ({new_thp} THP remaining). {effect_expired}{_format_hp_status(current_hp, total_hp)}",
            }
            return result
        else:
            thp_absorbed = thp
            _db_set(cursor, "temporary_hit_points", "0")
            DB_CONNECTION.commit()
            effect_expired = _cleanup_thp_effects(cursor)
            delta = -(damage - thp)

    new_val = current_hp + delta

    clamped = False
    original_delta = delta
    if new_val > total_hp:
        new_val = total_hp
        clamped = True
    if new_val < 0:
        new_val = 0
        clamped = True

    _db_set(cursor, "current_hit_points", str(new_val))
    DB_CONNECTION.commit()

    result = {
        "success": True,
        "key": "current_hit_points",
        "old_value": current_hp,
        "new_value": new_val,
        "delta": original_delta,
        "hp_status": _format_hp_status(new_val, total_hp),
        "max_hp": total_hp,
    }
    if new_val > current_hp:
        result["healing_applied"] = new_val - current_hp
    if thp_absorbed > 0:
        result["temporary_hit_points"] = {"old": thp, "new": 0, "absorbed": thp_absorbed}

    # SRD 5.1 hooks on the player damage path: healing any real HP ends death saves; damage while
    # concentrating forces a CON save; massive leftover damage is instant death; damage at 0 HP
    # is a death-save failure.
    real_damage = -original_delta if original_delta < 0 else 0
    if new_val > current_hp and current_hp == 0:
        _clear_death_saves(cursor)

    if real_damage > 0:
        conc = _concentration_save(cursor, real_damage)
        if conc is not None:
            result["concentration_check"] = conc
            if not conc["success"]:
                result["concentration_broken"] = conc.get("broken")
        if new_val == 0 and current_hp > 0:
            overshoot = max(0, -(current_hp + original_delta))
            if overshoot >= total_hp:
                _end_concentration(cursor, reason="instant death")
                result["status"] = "Dead"
                result["dead"] = True
                result["instant_death"] = True
                result["message"] = (
                    f"Massive damage! {result['hp_status']}. Instant death — "
                    f"the leftover damage meets or exceeds the HP maximum ({total_hp}).")
            else:
                _reset_death_saves(cursor)
                _end_concentration(cursor, reason="unconscious")
                result["clamped"] = True
                result["status"] = "Unconscious"
                result["death_saves"] = True
                result["death_save_failures"] = 0
                result["message"] = f"HP has reached 0. {result['hp_status']}. Begin death saves."
        elif new_val == 0 and current_hp == 0:
            failures = _add_death_save_failure(cursor, count=1)
            result["status"] = "Unconscious"
            result["death_saves"] = True
            result["death_save_failures"] = failures
            if failures >= 3:
                _end_concentration(cursor, reason="death")
                result["status"] = "Dead"
                result["dead"] = True
                result["message"] = (f"Damage at 0 HP — third death-save failure. "
                                     f"{result['hp_status']}. The character is dead.")
            else:
                result["message"] = (f"Damage while at 0 HP — one death-save failure "
                                     f"({failures}/3). {result['hp_status']}.")
        elif clamped:
            result["clamped"] = True
            result["message"] = f"Value was clamped. {result['hp_status']}."
        else:
            result["message"] = result["hp_status"]
    elif clamped:
        result["clamped"] = True
        if new_val == 0 and current_hp > 0:
            result["status"] = "Unconscious"
            result["death_saves"] = True
            result["message"] = f"HP has reached 0. {result['hp_status']}. Begin death saves."
        elif new_val == total_hp and delta > 0:
            result["message"] = f"HP restored to maximum. {result['hp_status']}."
        else:
            result["message"] = f"Value was clamped. {result['hp_status']}."
    elif total_hp > 0 and new_val == total_hp:
        result["message"] = f"Fully healed. {result['hp_status']}."
    else:
        result["message"] = result["hp_status"]

    if thp_absorbed > 0:
        result["message"] = f"{thp_absorbed} damage absorbed by temporary HP. {effect_expired}" + result["message"]

    return result


def _cleanup_thp_effects(cursor):
    buff_data_raw = _db_val(cursor, "_active_buff_data", {})
    if isinstance(buff_data_raw, str):
        buff_data_raw = json.loads(buff_data_raw)

    effects_list = _db_val(cursor, "active_effects", [])
    if isinstance(effects_list, str):
        effects_list = json.loads(effects_list)

    removed = []
    for spell_name in list(buff_data_raw.keys()):
        for entry in buff_data_raw[spell_name]:
            if entry.get("field") == "temporary_hit_points":
                del buff_data_raw[spell_name]
                if spell_name in effects_list:
                    effects_list.remove(spell_name)
                removed.append(spell_name)
                break

    if removed:
        _db_set(cursor, "_active_buff_data", buff_data_raw)
        _db_set(cursor, "active_effects", effects_list)
        DB_CONNECTION.commit()
        names = ", ".join(removed)
        return f"{names} has expired — temporary HP depleted. "
    return ""


def get_nested_value(data, path):
    parts = path.split('.')
    for part in parts:
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list):
            try:
                idx = int(part)
                if 0 <= idx < len(data):
                    data = data[idx]
                else:
                    return None
            except ValueError:
                return None
        else:
            return None
    return data


def set_nested_value(data, path, value):
    parts = path.split('.')
    for i in range(len(parts) - 1):
        part = parts[i]
        if isinstance(data, dict):
            data = data.setdefault(part, {})
        elif isinstance(data, list):
            try:
                idx = int(part)
                if 0 <= idx < len(data):
                    data = data[idx]
                else:
                    return data
            except ValueError:
                return data
        else:
            return data

    last_part = parts[-1]
    if isinstance(data, dict):
        data[last_part] = value
    elif isinstance(data, list):
        try:
            idx = int(last_part)
            if 0 <= idx < len(data):
                data[idx] = value
            else:
                if idx == len(data):
                    data.append(value)
        except ValueError:
            pass
    return data


PREPARED_CASTER_ABILITIES = {
    "Wizard": "int",
    "Cleric": "wis",
    "Druid": "wis",
    "Paladin": "cha",
}


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


@mcp.tool()
def modify_player_numeric(key: str, delta: int) -> dict:
    """Add to (or subtract from) a numeric player field, by dotted path.

    WHEN: gold, HP, XP, slots, consumables or capacity change.
    FIELDS:
    - key: dotted path, e.g. 'gold', 'spellcasting.slots.1', 'consumables.Bolts'.
    - delta: integer, negative to decrement.

    RULES:
    - current_hit_points: clamped to [0, total_hit_points]; at 0 returns death_saves and Unconscious.
    - spellcasting.slots.N: availability validated; available slots returned on error.
    - consumables.ITEM: auto-created at 0; auto-removed with DEPLETION at 0 or below (never negative).
    - xp: crossing a threshold auto-applies level, proficiency, hit dice, HP, slots, DC and attack modifier. Class features, cantrips/spells known, ASIs and subclass features stay manual.
    - capacity_multiplier: scales carrying capacity (2 for Bull's Strength, back to 1 when it ends; a long rest resets it).

    EX:
    modify_player_numeric(key='gold', delta=-10)
    modify_player_numeric(key='xp', delta=50)
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized.", "key": key}
    try:
        cursor = DB_CONNECTION.cursor()

        if key == "current_hit_points":
            return _apply_hp_change(cursor, delta)

        if key == "temporary_hit_points":
            current_val = int(_db_val(cursor, "temporary_hit_points", 0))
            new_val = max(current_val + delta, 0)
            _db_set(cursor, "temporary_hit_points", str(new_val))
            DB_CONNECTION.commit()
            result = {
                "success": True,
                "key": key,
                "old_value": current_val,
                "new_value": new_val,
                "delta": delta,
            }
            if new_val == 0 and current_val > 0:
                result["message"] = "Temporary HP depleted."
            else:
                result["message"] = f"Temporary HP: {new_val}"
            if current_val + delta < 0:
                result["clamped"] = True
            return result

        if key.startswith("spellcasting.slots."):
            slot_validation = _validate_spell_slot(cursor, key, delta)
            if slot_validation:
                return {
                    "success": False,
                    "error": slot_validation["error"],
                    "key": key,
                    "delta": delta,
                    "available_slots": slot_validation["available_slots"],
                    "hint": slot_validation["hint"],
                }

        if '.' in key:
            root_key = key.split('.')[0]
            cursor.execute("SELECT value FROM player WHERE key = ?", (root_key,))
            row = cursor.fetchone()

            auto_init_root = False
            if not row:
                if root_key == "consumables":
                    data = {}
                    auto_init_root = True
                else:
                    cursor.execute("SELECT key FROM player")
                    available = [r[0] for r in cursor.fetchall()]
                    return {"success": False, "error": f"Root key '{root_key}' not found.", "available_keys": available, "key": key}
            else:
                data = json.loads(row[0])

            path_in_obj = key[len(root_key)+1:]
            current_val = get_nested_value(data, path_in_obj)

            if current_val is None:
                if key.startswith("spellcasting.slots."):
                    current_val = 0
                elif key.startswith("consumables."):
                    current_val = 0
                else:
                    return {"success": False, "error": f"Key '{key}' not found in database.", "available_nested_keys": list(data.keys()), "key": key}

            current_val = int(current_val)
            new_val = current_val + delta
            set_nested_value(data, path_in_obj, new_val)

            if key.startswith("consumables.") and new_val <= 0:
                consumable_name = path_in_obj
                if new_val < 0:
                    data[consumable_name] = 0
                    cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
                    DB_CONNECTION.commit()
                    return {
                        "success": True,
                        "key": key,
                        "old_value": current_val,
                        "new_value": 0,
                        "delta": delta,
                        "clamped": True,
                        "message": f"Consumable '{consumable_name}' cannot go below 0. Set to 0.",
                        "narrative_format": f"Consumable '{consumable_name}' set to 0 (cannot go below 0).",
                    }
                del data[consumable_name]
                cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
                DB_CONNECTION.commit()
                return {
                    "success": True,
                    "key": key,
                    "old_value": current_val,
                    "new_value": 0,
                    "delta": delta,
                    "item_depleted": True,
                    "depleted_item": consumable_name,
                    "message": f"DEPLETED — {consumable_name} has been used up and removed from consumables.",
                    "narrative_format": f"DEPLETED — {consumable_name} has been used up and removed from consumables.",
                }

            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
        else:
            cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("SELECT key FROM player")
                available = [r[0] for r in cursor.fetchall()]
                return {"success": False, "error": f"Key '{key}' not found in database.", "available_keys": available, "key": key}

            current_val = int(row[0])
            new_val = current_val + delta
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, str(new_val)))

        DB_CONNECTION.commit()
        result = {
            "success": True,
            "key": key,
            "old_value": current_val,
            "new_value": new_val,
            "delta": delta,
        }
        if key.startswith("spellcasting.slots."):
            cursor.execute("SELECT value FROM player WHERE key = ?", ("spellcasting",))
            sc_row = cursor.fetchone()
            if sc_row:
                sc_data = json.loads(sc_row[0])
                result["remaining_slots"] = {f"lv{k}": v for k, v in sc_data.get("slots", {}).items()}
        if key == "xp":
            cursor.execute("SELECT value FROM player WHERE key = ?", ("level",))
            level_row = cursor.fetchone()
            if level_row:
                current_level = int(level_row[0])
                new_level = get_level_for_xp(new_val)
                if new_level > current_level:
                    cursor.execute("SELECT key, value FROM player")
                    all_rows = cursor.fetchall()
                    player_data = {row[0]: row[1] for row in all_rows}
                    character_class = player_data.get('character_class', '')
                    changes, summary = apply_level_up(character_class, current_level, new_level, player_data)
                    for db_key, db_value in changes.items():
                        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (db_key, db_value))
                    DB_CONNECTION.commit()
                    _recompute_armor_class(cursor)
                    DB_CONNECTION.commit()

                    total_hp = int(changes.get('total_hit_points', _db_val(cursor, 'total_hit_points', 0)))
                    current_hp = int(changes.get('current_hit_points', _db_val(cursor, 'current_hit_points', 0)))

                    result["level_up"] = True
                    result["old_level"] = current_level
                    result["new_level"] = new_level
                    result["level_up_changes"] = summary
                    result["level_up_summary"] = f"LEVEL UP! Level {current_level} → {new_level}. " + "; ".join(summary)
                    result["hp_status"] = _format_hp_status(current_hp, total_hp)
                    result["message"] = (
                        f"LEVEL UP! Level {current_level} → {new_level}. "
                        f"Changes: {', '.join(summary)}. "
                        f"You MUST still apply manually: class features, cantrips/spells, "
                        f"ability score improvements (at levels 4/8/12/16/19), and subclass features."
                    )
        result["narrative_format"] = (result.get("level_up_summary")
                                      or f"{key}: {current_val} → {new_val} ({delta:+d})")
        return result
    except Exception as e:
        return {"success": False, "error": f"Error modifying numeric value: {str(e)}", "key": key}


_DEFAULT_REPUTATION_FACTION = "others"
# The faction slot the engine used before 2026-10-08; still accepted, folded to `others`.
_LEGACY_REPUTATION_FACTIONS = ("misc",)


def _era_reputation_scope(era):
    """The empty, era-scoped reputation shape a fresh record gets: `{polity: {faction: []}}`.

    `web.eras` is the one source, so a scope created here is indistinguishable from one
    created at character creation. An import failure is not fatal: the era still gets its
    `others` bucket and the GM's first write lands there.
    """
    name = str(era or "").strip()
    try:
        from web.eras import era_reputation_seed
    except Exception:  # pragma: no cover - the engine always ships web/eras.py
        return {"others": {}}
    seed = era_reputation_seed(name)
    scope = seed.get(name)
    return scope if isinstance(scope, dict) else {"others": {}}


def _ensure_reputation_scope(data, era):
    """The era's writable scope, created on demand from the same seed a new record gets.

    A save that has an era but no scope for it is exactly what a **jump** used to leave
    behind -- the scope was only ever created at character creation -- so every reputation
    write in every era after the first was refused, for good. The scope is now created where
    it is *required*.

    This is not a migration: nothing is rewritten and an unfamiliar top-level key is left
    alone. What the save already holds wins over the seed; a scope that exists but looks
    empty (nothing but `others`, or only keys that are not this era's polities) is merged
    **under** the seed, so a half-written scope heals instead of shadowing its own era.
    """
    name = str(era or "").strip()
    existing = data.get(name)
    seed = _era_reputation_scope(name)
    scope = existing if isinstance(existing, dict) else {}
    if seed and not (set(seed) & set(scope)):
        merged = dict(seed)
        merged.update(scope)
        scope = merged
    data[name] = scope
    return scope


def _reputation_error(key, data, era):
    """A refusal the GM can act on: the era, its real polities, and the shape to write.

    The old refusal listed the era keys of the whole map -- which is not what the GM writes
    against. It now names the era's own polities and their factions.
    """
    known: dict = {}
    scope = data.get(str(era or "").strip()) if isinstance(data, dict) else None
    if isinstance(scope, dict):
        for name, bucket in scope.items():
            known[str(name)] = sorted(bucket) if isinstance(bucket, dict) else []
    return {
        "success": False,
        "error": f"Key '{key}' not found or is not a list.",
        "key": key,
        "era": str(era or ""),
        "known_polities": known,
        "hint": ("Reputation is era-scoped and the engine adds the era: write "
                 "reputation.<polity>[.<faction>]. Pick a polity from known_polities (or "
                 "'others' for people who belong to no polity), then a faction under it."),
    }


def _resolve_reputation_bucket(data, path_in_obj, era, create=True):
    """Resolve a `reputation.<polity>[.<faction>]` path to a writable list.

    Reputation is stored **era-scoped** -- `{era: {polity: {faction: [entries]}}}` -- so
    standing earned in one era is never offered in another, and never lost when the
    Traveler leaves. The GM still writes the short path: the era is the one the save is in,
    and the caller passes it in.

    The era's scope is created on demand (`_ensure_reputation_scope`): a jump, or any path
    that forgets to seed, must not leave an era unwritable. A missing faction under an
    *existing* polity is auto-created when `create` (an `add`); targeting a polity alone, or
    the legacy `misc`, uses the `others` bucket. An unknown polity is never created, so a
    typo still fails.

    Returns `(list_or_None, path_in_obj)`, and the path is relative to **`data`** -- the era
    is part of it -- so the caller writes at the level the resolver actually resolved.
    Returning a scope-relative path here used to make every add create a duplicate bucket at
    the top of the map.
    """
    if not isinstance(data, dict):
        return None, path_in_obj
    name = str(era or "").strip()
    if not name:
        return None, path_in_obj
    scope = _ensure_reputation_scope(data, name)
    parts = path_in_obj.split('.')
    kingdom = parts[0] if parts else ''
    if not kingdom or kingdom not in scope or not isinstance(scope[kingdom], dict):
        return None, path_in_obj
    if len(parts) == 1:
        parts = [kingdom, _DEFAULT_REPUTATION_FACTION]
    if len(parts) != 2 or not parts[1]:
        return None, path_in_obj
    faction = parts[1]
    if faction in _LEGACY_REPUTATION_FACTIONS:
        faction = _DEFAULT_REPUTATION_FACTION
    bucket = scope[kingdom].get(faction)
    if bucket is None:
        if not create:
            return None, path_in_obj
        bucket = []
        scope[kingdom][faction] = bucket
    elif not isinstance(bucket, list):
        return None, path_in_obj
    return bucket, f"{name}.{kingdom}.{faction}"


@mcp.tool()
def update_player_list(key: str, item: str, action: str, weight: float | None = None,
                       base: str | None = None,
                       damage_dice: str | None = None,
                       damage_type: str | None = None,
                       properties: list[str] | None = None,
                       ac: int | None = None,
                       dex_cap: int | None = None,
                       strength_req: int | None = None,
                       ac_bonus: int | None = None,
                       attack_bonus: int | None = None,
                       damage_bonus: int | None = None,
                       attunement: bool | None = None,
                       attunement_by: str | None = None,
                       kind: str | None = None,
                       pair: bool | None = None,
                       save_bonus: int | None = None,
                       check_bonus: int | None = None,
                       proficiency_bonus: int | None = None,
                       spell_attack_bonus: int | None = None,
                       spell_dc_bonus: int | None = None,
                       set_str: int | None = None, set_dex: int | None = None,
                       set_con: int | None = None, set_int: int | None = None,
                       set_wis: int | None = None, set_cha: int | None = None,
                       str_bonus: int | None = None, dex_bonus: int | None = None,
                       con_bonus: int | None = None, int_bonus: int | None = None,
                       wis_bonus: int | None = None, cha_bonus: int | None = None,
                       str_bonus_max: int | None = None, dex_bonus_max: int | None = None,
                       con_bonus_max: int | None = None, int_bonus_max: int | None = None,
                       wis_bonus_max: int | None = None, cha_bonus_max: int | None = None,
                       effects: list[dict] | None = None,
                       appearance: str | None = None,
                       description: str | None = None,
                       new_name: str | None = None) -> dict:
    """Add, remove or edit one player-list entry (inventory, spells, reputation, active effects).

    WHEN: gear/items/spells/reputation/effects change.
    FIELDS (only the non-obvious; declare a weapon's/armour's stats WITH the add):
    - key: dotted list path, e.g. 'inventory', 'spellcasting.spells_known', 'reputation.KINGDOM.FACTION'.
    - item: add -> 'Name' or 'Name: Description'; remove -> name only.
    - action: 'add' | 'remove' | 'update'.
    - description: the item's flavour/state text -- on add AND update; preferred over the inline 'Name: Description' form. The NAME is identity; transient state (sealed/opened, lit, half-full, emptied, broken) belongs here.
    - base: the SRD archetype the item is built on (e.g. base='Dagger'). Set it on EVERY weapon and suit of armour so properties/hands/weight/AC resolve even when the name is reflavoured.
    - damage_dice/damage_type/properties: a weapon's stats. ac/dex_cap/strength_req: an armour's stats.
    - weight: pounds, only for items the SRD weight catalog does not know (magic items, loot, homebrew).
    - ac_bonus/attack_bonus/damage_bonus: magic bonuses, declared explicitly (never parsed from a '+1' name); they apply only while attuned (and, for a pair, only while both halves are worn).
    - save_bonus/check_bonus: a flat bonus to ALL saves / ALL checks -- the engine applies it; never add it yourself.
    - set_*: set an ability score as a floor. *_bonus (with *_bonus_max): raise an ability, optionally capped.
    - proficiency_bonus / spell_attack_bonus / spell_dc_bonus: item bonuses to those.
    - effects: typed list for anything the flat fields cannot express, {type, ..., when}. Types: ability_set, ability_bonus (+max), save_bonus, save_advantage, check_bonus, skill_bonus, initiative, ac_bonus, ac_set ({base, plus_dex}), damage_resistance, damage_immunity, condition_immunity, attack_bonus/damage_bonus (+scope), spell_attack_bonus, spell_dc_bonus, weapon_property, hp_per_level, hit_die_healing_multiplier, regeneration, speed, speed_grant, grant_proficiency. when: no_armor, no_shield, no_armor_or_shield, while_holding, requires_items (items=[...]), vs_spells, vs_damage:<type>, vs_condition:<name>. Anything the engine cannot apply is echoed as 'unmodelled_effects'.
    - attunement: True when the item's magic needs attunement; attunement_by names a prerequisite (class, spellcaster, creature type, alignment). Bonuses declared WITHOUT attunement apply immediately.
    - kind: the worn slot -- armor, cloak, boots, gloves, gauntlets, bracers, headwear, ring, amulet, belt, other. Required for a worn magic item so it is donned, not held.
    - pair: True when this entry is ONE HALF of a paired item. Add both halves sharing the same `base`; a single half grants no bonus.
    - appearance: (active_effects adds) the look the character takes on; the image uses it INSTEAD of the portrait, and a known covering disguise also replaces the depicted clothing/armour/weapons.
    - new_name: (update only) rename in place; declared stats are kept.

    RULES:
    - Declare `base` + the damage/ac fields + `properties` + `kind` + any bonuses in the SAME add: the engine derives attacks, damage, AC, saves, ability scores and attunement from them, and a declared value wins over the catalog. A weapon with no damage and no known base cannot attack.
    - Prepared casters: capacity enforced (max = spellcasting mod + level).
    - Removing an active_effect auto-reverts its stat deltas.
    - Consumables: NEVER here -- use modify_player_numeric(key='consumables.ITEM', delta=N).
    - Reputation: lowercase, no apostrophes; a missing faction under a known polity is created; a bare 'reputation.POLITY' writes to 'others'; unknown polities are rejected.
    - Removing a worn/wielded inventory item auto-unequips it; a replacement is NOT auto-equipped.
    - Every inventory change returns a 'carrying' block; an unknown item added without weight counts 0 lb and returns 'unweighed_item'.

    EX:
    update_player_list(key='inventory', item='Dagger: a rusty blade', action='add', base='Dagger', damage_dice='1d4', damage_type='piercing', properties=['Finesse','Light','Thrown (range 20/60)'])
    update_player_list(key='inventory', item='Ring of Warding: a band of cold silver', action='add', base='Ring of Protection', kind='ring', ac_bonus=1, save_bonus=1, attunement=True)
    update_player_list(key='inventory', item="the stranger's letter", action='update', description="a folded scrap, the grey seal broken")
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized.", "key": key}
    try:
        cursor = DB_CONNECTION.cursor()

        if '.' in key:
            root_key = key.split('.')[0]
            cursor.execute("SELECT value FROM player WHERE key = ?", (root_key,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("SELECT key FROM player")
                available = [r[0] for r in cursor.fetchall()]
                return {"success": False, "error": f"Root key '{root_key}' not found.", "available_keys": available, "key": key}

            data = json.loads(row[0])
            path_in_obj = key[len(root_key)+1:]

            era_now = str(_db_val(cursor, "era", "") or "").strip()
            if root_key == "reputation" and era_now:
                # Reputation is era-scoped *in the era game*: resolve through the era FIRST
                # there, so a flat map cannot be written to by accident -- the generic nested
                # path would happily find it. A save with no era is the classic game and keeps
                # the flat map it has always had. Every action resolves here, not just `add`:
                # `update` and `remove` used to fall through to the raw short path and fail.
                current_list, path_in_obj = _resolve_reputation_bucket(
                    data, path_in_obj, era_now, create=(action == "add"))
                if current_list is None:
                    return _reputation_error(key, data, era_now)
            else:
                current_list = get_nested_value(data, path_in_obj)
                if current_list is None or not isinstance(current_list, list):
                    return {"success": False, "error": f"Key '{key}' not found or is not a list.", "available_nested_keys": list(data.keys()), "key": key}
        else:
            cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("SELECT key FROM player")
                available = [r[0] for r in cursor.fetchall()]
                return {"success": False, "error": f"Key '{key}' not found in database.", "available_keys": available, "key": key}
            try:
                current_list = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                current_list = [row[0]]

        is_prepared_spells = (key == "spellcasting.spells_prepared")
        added_name = None
        removed_equipped = False
        device_owned = False
        # Encumbrance is reported only when this change TIPS the character into it (SRD 5.1
        # variant) — never the default "unencumbered" line.
        before_carry_status = None
        if key == "inventory":
            try:
                before_carry_status = (_carry_block(cursor) or {}).get("status")
            except Exception:  # noqa: BLE001 - never fail a list write over reporting
                before_carry_status = None

        if action == "add":
            name = item
            desc = ""
            if ":" in item:
                name, desc = [p.strip() for p in item.split(":", 1)]
            # An explicit `description=` wins over the inline 'Name: Description' form
            # (the GM usually passes the field rather than embedding it in `item`).
            if isinstance(description, str) and description.strip():
                desc = description.strip()
            added_name = name

            new_entry = {"name": name, "description": desc} if (desc or ":" in item) else name
            declared = {}
            if key == "inventory":
                # The Device and its four parts: a closed, engine-owned vocabulary. A part is
                # accepted only by its exact name and gets the engine description + the age it
                # was recovered in; the Device itself may only be restored, never invented.
                device_add = _device_add_entry(cursor, name, desc, weight)
                if device_add is not None:
                    if device_add.get("reject"):
                        return device_add["reject"]
                    new_entry = device_add["entry"]
                    device_owned = True
                    name = str(new_entry.get("name") or name)
                    added_name = name
                else:
                    if weight is not None:
                        declared["weight"] = float(weight)
                    for field, value in (("base", base), ("damage_dice", damage_dice),
                                         ("damage_type", damage_type), ("properties", properties),
                                         ("ac", ac), ("dex_cap", dex_cap),
                                         ("strength_req", strength_req), ("ac_bonus", ac_bonus),
                                         ("attack_bonus", attack_bonus), ("damage_bonus", damage_bonus),
                                         ("attunement", attunement), ("attunement_by", attunement_by),
                                         ("kind", kind), ("pair", pair),
                                         ("save_bonus", save_bonus), ("check_bonus", check_bonus),
                                         ("proficiency_bonus", proficiency_bonus),
                                         ("spell_attack_bonus", spell_attack_bonus),
                                         ("spell_dc_bonus", spell_dc_bonus),
                                         ("set_str", set_str), ("set_dex", set_dex),
                                         ("set_con", set_con), ("set_int", set_int),
                                         ("set_wis", set_wis), ("set_cha", set_cha),
                                         ("str_bonus", str_bonus), ("dex_bonus", dex_bonus),
                                         ("con_bonus", con_bonus), ("int_bonus", int_bonus),
                                         ("wis_bonus", wis_bonus), ("cha_bonus", cha_bonus),
                                         ("str_bonus_max", str_bonus_max),
                                         ("dex_bonus_max", dex_bonus_max),
                                         ("con_bonus_max", con_bonus_max),
                                         ("int_bonus_max", int_bonus_max),
                                         ("wis_bonus_max", wis_bonus_max),
                                         ("cha_bonus_max", cha_bonus_max)):
                        if value is not None:
                            declared[field] = value
                    if effects is not None:
                        declared["effects"] = effects
            if declared:
                new_entry = {"name": name, "description": desc, **declared}

            if appearance and key == "active_effects":
                entry = new_entry if isinstance(new_entry, dict) else {"name": name}
                entry["appearance"] = str(appearance).strip()
                new_entry = entry

            if key == "inventory" and not device_owned:
                rejection = _validate_item_add(name, declared, weight)
                if rejection is not None:
                    return rejection

            exists = any((isinstance(e, dict) and e.get("name") == name) or e == name for e in current_list)
            if exists:
                available = []
                for e in current_list:
                    if isinstance(e, dict):
                        available.append(e.get("name", str(e)))
                    else:
                        available.append(str(e))
                result = {"success": False, "error": "already_exists", "key": key, "item": name,
                          "action": action, "current_items": available}
                if is_prepared_spells:
                    info = build_prepared_spells_info(cursor)
                    if info is not None:
                        result["spells_prepared_info"] = info
                return result

            if is_prepared_spells:
                info = build_prepared_spells_info(cursor)
                if info is not None and info.get("at_capacity"):
                    info["reason"] = f"Cannot add '{name}'. Maximum prepared spells reached ({info['max_count']}). Remove a spell first before adding a new one."
                    return {"success": False, "error": "spells_prepared_at_capacity", "key": key, "item": name, "action": action, "spells_prepared_info": info}

            current_list.append(new_entry)

        elif action == "remove":
            found = False
            for i, e in enumerate(current_list):
                if (isinstance(e, dict) and e.get("name") == item) or e == item:
                    current_list.pop(i)
                    found = True
                    break
            if not found:
                available = []
                for e in current_list:
                    if isinstance(e, dict):
                        available.append(e.get("name", str(e)))
                    else:
                        available.append(str(e))
                return {"success": False, "error": "not_found", "key": key, "item": item,
                        "action": action, "current_items": available}

            if key == "inventory":
                # The item left the pack, so it cannot stay worn or wielded.
                removed_equipped = _clear_equipped(cursor, item)

            if key == "active_effects":
                reverted = _remove_active_effect(cursor, item)
                conc = _get_concentration(cursor)
                if conc and conc.get("spell") == item:
                    _db_set(cursor, "concentration", {})
                # Persist the removal of the effect from the list (the early
                # return below would otherwise skip the normal list write).
                cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                               (key, json.dumps(current_list)))
                DB_CONNECTION.commit()
                result_early = {
                    "success": True,
                    "key": key,
                    "item": item,
                    "action": action,
                    "current_list": [e.get("name", str(e)) if isinstance(e, dict) else str(e) for e in current_list],
                    "reverted": reverted,
                    "narrative_format": f"Removed active effect '{item}' and reverted its bonuses.",
                }
                return result_early
        elif action == "update":
            index = None
            for i, e in enumerate(current_list):
                if (isinstance(e, dict) and e.get("name") == item) or e == item:
                    index = i
                    break
            if index is None:
                available = [e.get("name", str(e)) if isinstance(e, dict) else str(e)
                             for e in current_list]
                return {"success": False, "error": "not_found", "key": key, "item": item,
                        "action": action, "current_items": available}
            entry = current_list[index]
            if key == "inventory" and device.is_device_item(
                    str(entry.get("name")) if isinstance(entry, dict) else str(entry)):
                return {"success": False, "error": "device_engine_owned", "key": key,
                        "item": item, "action": action,
                        "reason": "The Device and its parts are engine-owned; their description and state are written by the engine.",
                        "gm_instruction": ("Do not edit the Device or a part. Remove one with "
                                           "action='remove' if the fiction takes it away.")}
            new_desc = description if isinstance(description, str) else None
            rename = new_name.strip() if isinstance(new_name, str) and new_name.strip() else None
            if new_desc is None and rename is None:
                return {"success": False, "error": "nothing_to_update", "key": key, "item": item,
                        "action": action,
                        "reason": "Pass description= and/or new_name= to change the entry."}
            if rename and any(
                    (isinstance(e, dict) and e.get("name") == rename) or e == rename
                    for i, e in enumerate(current_list) if i != index):
                available = [e.get("name", str(e)) if isinstance(e, dict) else str(e)
                             for e in current_list]
                return {"success": False, "error": "already_exists", "key": key, "item": rename,
                        "action": action, "current_items": available}
            if isinstance(entry, dict):
                updated = dict(entry)
                if rename:
                    updated["name"] = rename
                if new_desc is not None:
                    updated["description"] = new_desc
            else:
                entry_name = rename or str(entry)
                updated = ({"name": entry_name, "description": new_desc}
                           if new_desc is not None else entry_name)
            current_list[index] = updated
            if key == "inventory" and rename and rename != item:
                _rename_equipped(cursor, item, rename)
            updated_label = rename or (entry.get("name") if isinstance(entry, dict) else str(entry))
        else:
            return {"success": False,
                    "error": "Invalid action. Use 'add', 'remove' or 'update'.",
                    "key": key, "action": action}

        if '.' in key:
            set_nested_value(data, path_in_obj, current_list)
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
        else:
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, json.dumps(current_list)))

        DB_CONNECTION.commit()

        display_list = []
        for e in current_list:
            if isinstance(e, dict):
                display_list.append(e.get("name", str(e)))
            else:
                display_list.append(str(e))

        result = {
            "success": True,
            "key": key,
            "item": item,
            "action": action,
            "current_list": display_list,
        }
        if device_owned and added_name:
            # The Device vocabulary is canonical: tell the GM the exact name it now holds.
            result["item"] = added_name

        if is_prepared_spells:
            info = build_prepared_spells_info(cursor)
            if info is not None:
                result["spells_prepared_info"] = info

        if key == "inventory":
            result["carrying"] = _carry_block(cursor)
            result["equipment"] = _equipment_block(cursor)
            if action == "remove" and removed_equipped:
                result["unequipped"] = item
                result["note"] = (f"{item} was worn or wielded, so it was unequipped when it left the "
                                  f"inventory. If you replaced it with a new item, call equip_item "
                                  f"on the replacement — it is not auto-equipped.")
            added_base = None
            added_entry = next((e for e in current_list
                                 if isinstance(e, dict) and e.get("name") == (added_name or item)), None)
            if isinstance(added_entry, dict):
                added_base = added_entry.get("base")
            if (action == "add" and weight is None and added_name and not device_owned
                    and carrying.weight_for(added_name, base=added_base) is None):
                result["unweighed_item"] = added_name
                result["warning"] = (
                    f"'{added_name}' is not in the SRD weight catalog and no weight was given, so it "
                    f"counts as 0 lb. Re-add it with weight=<pounds> so carrying capacity stays accurate."
                )

        if action == "update":
            narrative = f"Updated {key}: {updated_label}"
            if new_desc is not None:
                narrative += f": {new_desc}"
            narrative += "."
        else:
            verb = "Removed from" if action == "remove" else "Added to"
            narrative = f"{verb} {key}: {item}."
        if result.get("carrying"):
            status = result["carrying"].get("status")
            if status and status != "unencumbered" and status != before_carry_status:
                narrative += f" Encumbrance: {status}."
        if result.get("unequipped"):
            narrative += f" {item} was worn or wielded, so it was unequipped."
        result["narrative_format"] = narrative

        return result
    except Exception as e:
        return {"success": False, "error": f"Error updating list: {str(e)}", "key": key}


def _ac_note(before, after) -> str:
    """The ' — AC x → y' suffix for a tool narrative, only when the AC actually changed."""
    return f" — AC {before} → {after}" if before != after else ""


@mcp.tool()
def equip_item(item: str, action: str = "equip", slot: str | None = None,
               replace: bool = False, instant: bool = False) -> dict:
    """Equip or unequip something the character is carrying.

    WHEN: worn/wielded gear changes. The item must already be in the inventory (use update_player_list first) -- this tool only moves it.
    FIELDS:
    - item: the inventory name exactly.
    - action: 'equip' (default) | 'unequip'.
    - slot: 'main_hand' | 'off_hand' | 'armor' | 'worn'. Omit to let the engine choose (armour -> armour slot; anything else -> a free hand). Cloaks, boots, gloves, gauntlets, bracers, headwear, rings and amulets go to 'worn'; clothing is worn automatically.
    - replace: True stows whatever occupied the slot/hand instead of refusing (the stowed item stays in the inventory).
    - instant: allow an armour change in combat (otherwise refused; use only for magic or a GM call). Shields take one action and are always allowed.

    RULES:
    - 5e has NO equipment slots -- only two hands and one suit of armour. A shield is held in a hand; a two-handed weapon needs BOTH hands (equip it only with the other hand free, or replace=True).
    - Equipping recomputes AC from the equipped set and returns before->after + time_cost. Only one suit of armour and one shield benefit a creature.
    - Unequipping never drops the item.
    - The result carries the derived 'equipment' block (hands, hands_free, base_ac, ac_breakdown, warnings).
    - The in-combat refusal checks the registry: hostiles all dead = no longer in combat.

    EX:
    equip_item(item='Chain Mail')
    equip_item(item='Greatsword', replace=True)
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        cursor = DB_CONNECTION.cursor()
        action = str(action or "equip").strip().lower()
        if action not in ("equip", "unequip"):
            return {"success": False, "error": "invalid_action", "action": action,
                    "reason": "Use action='equip' or action='unequip'."}

        inventory = _db_val(cursor, "inventory", []) or []
        names = [(e.get("name") if isinstance(e, dict) else str(e)) for e in inventory]
        if item not in names:
            return {"success": False, "error": "not_in_inventory", "item": item,
                    "reason": f"'{item}' is not in the inventory — add it with "
                              f"update_player_list first.",
                    "current_items": names}
        declared = next((e for e in inventory if isinstance(e, dict) and e.get("name") == item), {})
        base = declared.get("base")
        if device.is_device_item(item) or (isinstance(declared, dict)
                                           and (declared.get("device") or declared.get("device_part"))):
            return {"success": False, "error": "device_not_equippable", "item": item,
                    "reason": "The Device is not gear — it cannot be equipped, wielded or worn.",
                    "gm_instruction": "The Device and its parts are carried, never equipped. Leave them in the inventory."}

        equipped = _db_val(cursor, "equipped", None)
        if not isinstance(equipped, dict):
            equipped = {"armor": None, "hands": [None, None]}
        hands = equipped.get("hands")
        hands = (list(hands) + [None, None])[:2] if isinstance(hands, list) else [None, None]
        worn_raw = equipped.get("worn")
        worn = [str(w) for w in worn_raw if w] if isinstance(worn_raw, list) else []
        before = int(_db_val(cursor, "armor_class", 10) or 10)
        is_armor_item = equipment.is_armor(item, base)
        is_shield_item = equipment.is_shield(item, base)

        # SRD 5.1 "Getting Into and Out of Armor": minutes for armour, one action for a shield.
        time_cost = equipment.don_time(item, base, doff=(action == "unequip"))
        if is_armor_item and not instant and _in_active_combat():
            verb = "doff" if action == "unequip" else "don"
            return _blocked_action(
                cursor, "cannot_change_armor_in_combat",
                f"{verb}ning {item} takes {time_cost} — impossible in the middle of a fight.",
                item,
                f"{item} was not {verb}ned — it takes {time_cost} and the party is in combat. "
                f"The action is spent.")

        if action == "unequip":
            where = None
            if equipped.get("armor") == item:
                equipped["armor"] = None
                where = "worn"
            if item in worn:
                worn = [w for w in worn if w != item]
                where = where or "worn"
            for index, held in enumerate(hands):
                if held == item:
                    hands[index] = None
                    where = where or ("main hand" if index == 0 else "off hand")
            if where is None:
                return {"success": False, "error": "not_equipped", "item": item,
                        "reason": f"'{item}' is not currently worn or wielded.",
                        "equipment": _equipment_block(cursor)}
            equipped["hands"] = hands
            equipped["worn"] = worn
            _db_set(cursor, "equipped", equipped)
            after = _recompute_armor_class(cursor)
            block = _equipment_block(cursor)
            label = "stowed" if where == "worn" else f"hand freed ({where})"
            return {
                "success": True, "action": "unequip", "item": item,
                "equipped": equipped, "armor_class_before": before, "armor_class_after": after,
                "equipment": block, "warnings": block.get("warnings", []),
                "time_cost": time_cost,
                "narrative_format": f"{item} {label}{_ac_note(before, after)}"
                                    + (f" ({time_cost})" if time_cost else ""),
            }

        target = (slot or "").strip().lower() or None
        if target not in (None, "main_hand", "off_hand", "armor", "worn"):
            return {"success": False, "error": "invalid_slot", "slot": slot,
                    "reason": "Use slot='main_hand', 'off_hand', 'armor' or 'worn' (or omit it)."}

        is_armor = is_armor_item
        if is_armor:
            if target not in (None, "armor"):
                return {"success": False, "error": "invalid_slot", "slot": slot,
                        "reason": f"'{item}' is armour and can only go in the armour slot."}
            if equipped.get("armor") == item:
                return {"success": True, "action": "equip", "item": item, "already_equipped": True,
                        "equipped": equipped, "armor_class_before": before, "armor_class_after": before,
                        "equipment": _equipment_block(cursor), "narrative_format":
                        f"{item} is already worn"}
            if equipped.get("armor") and not replace:
                return {"success": False, "error": "armor_slot_occupied", "item": item,
                        "worn": equipped.get("armor"),
                        "reason": f"{equipped.get('armor')} is already worn — unequip it first, or "
                                  f"repeat with replace=True.",
                        "equipment": _equipment_block(cursor)}
            equipped["armor"] = item
            equipped["hands"] = [None if h == item else h for h in hands]
        else:
            if target == "armor":
                return {"success": False, "error": "invalid_slot", "slot": slot,
                        "reason": f"'{item}' is not armour — use slot='main_hand', 'off_hand' or 'worn'."}
            if item in hands:
                return {"success": True, "action": "equip", "item": item, "already_equipped": True,
                        "equipped": equipped, "armor_class_before": before, "armor_class_after": before,
                        "equipment": _equipment_block(cursor), "narrative_format":
                        f"{item} is already held"}
            if target == "worn" or (target is None and _is_worn_slot(item, declared)):
                if item in worn:
                    return {"success": True, "action": "equip", "item": item,
                            "already_equipped": True, "equipped": equipped,
                            "armor_class_before": before, "armor_class_after": before,
                            "equipment": _equipment_block(cursor),
                            "narrative_format": f"{item} is already worn"}
                # SRD 5.1 "Multiple Items of the Same Kind": one pair of footwear, gloves/gauntlets,
                # bracers, headwear, cloak. Rings and amulets are unrestricted.
                group = _worn_group(declared)
                if group:
                    conflict = next((w for w in worn
                                     if _worn_group(_inventory_entry(inventory, w)) == group), None)
                    if conflict and not replace:
                        return {"success": False, "error": "worn_slot_taken", "item": item,
                                "worn": conflict,
                                "reason": f"{conflict} is already worn in the {group} slot — unequip "
                                          f"it first, or repeat with replace=True.",
                                "gm_instruction": f"Re-call equip_item(item='{item}', "
                                                  f"replace=True) to stow {conflict}, or unequip "
                                                  f"{conflict} first.",
                                "equipment": _equipment_block(cursor)}
                    if conflict:
                        worn = [w for w in worn if w != conflict]
                worn.append(item)
                equipped["worn"] = worn
                equipped["hands"] = hands
            else:
                # A shield prefers the off hand, a weapon the main hand; otherwise the first free one.
                order = (1, 0) if equipment.is_shield(item, base) else (0, 1)
                if target == "main_hand":
                    index = 0
                elif target == "off_hand":
                    index = 1
                else:
                    index = next((i for i in order if hands[i] is None), None)
                    if index is None and not replace:
                        return {"success": False, "error": "no_free_hand", "item": item,
                                "held": [h for h in hands if h],
                                "reason": "Both hands are busy — stow or drop something first, or "
                                          "repeat with replace=True.",
                                "equipment": _equipment_block(cursor)}
                    if index is None:
                        index = order[0]
                other = 1 - index
                if equipment.hands_used([item, None], inventory) == 2:
                    if hands[other] and not replace:
                        return {"success": False, "error": "two_handed_needs_both_hands", "item": item,
                                "occupied": hands[other],
                                "reason": f"{item} needs both hands to attack with, but {hands[other]} "
                                          f"is in the other hand — free it first, or repeat with "
                                          f"replace=True.",
                                "equipment": _equipment_block(cursor)}
                    hands[other] = None
                hands[index] = item
                equipped["hands"] = hands

        _db_set(cursor, "equipped", equipped)
        after = _recompute_armor_class(cursor)
        block = _equipment_block(cursor)
        where = "worn" if (is_armor or item in worn) else ("main hand" if hands[0] == item else "off hand")
        return {
            "success": True, "action": "equip", "item": item,
            "equipped": equipped, "armor_class_before": before, "armor_class_after": after,
            "equipment": block, "warnings": block.get("warnings", []),
            "time_cost": time_cost,
            "narrative_format": f"{item} equipped ({where}){_ac_note(before, after)}"
                                + (f" ({time_cost})" if time_cost else ""),
        }
    except Exception as e:
        return {"success": False, "error": f"Error equipping item: {str(e)}", "item": item}


@mcp.tool()
def attune_item(item: str, action: str = "attune", instant: bool = False) -> dict:
    """Attune to (or break attunement with) a magic item.

    WHEN: after a rest, for an item whose magic needs attunement.
    FIELDS:
    - item: the inventory name exactly.
    - action: 'attune' (default) | 'unattune'.
    - instant: allow it in combat (otherwise refused -- attuning normally takes a short rest).

    RULES:
    - At most THREE attuned magic items, at most one copy of each. Fails if the item's declared `attunement_by` prerequisite (class, spellcaster, creature type, alignment) is unmet.
    - Only items declared attunement=True can be attuned; their ac_bonus/attack_bonus/damage_bonus apply only while attuned (before that the 'equipment' block warns 'not_attuned').
    - The item must be in the inventory first.

    EX:
    attune_item(item='Voidmail')
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        cursor = DB_CONNECTION.cursor()
        action = str(action or "attune").strip().lower()
        if action not in ("attune", "unattune"):
            return {"success": False, "error": "invalid_action", "action": action,
                    "reason": "Use action='attune' or action='unattune'."}

        inventory = _db_val(cursor, "inventory", []) or []
        names = [(e.get("name") if isinstance(e, dict) else str(e)) for e in inventory]
        if item not in names:
            return {"success": False, "error": "not_in_inventory", "item": item,
                    "reason": f"'{item}' is not in the inventory.", "current_items": names}
        declared = next((e for e in inventory if isinstance(e, dict) and e.get("name") == item), {})
        if device.is_device_item(item) or (isinstance(declared, dict)
                                           and (declared.get("device") or declared.get("device_part"))):
            return {"success": False, "error": "device_not_attunable", "item": item,
                    "reason": "The Device is not a magic item — it cannot be attuned.",
                    "gm_instruction": "The Device and its parts are carried, never attuned."}
        attuned = _db_val(cursor, "attuned", []) or []
        attuned = [str(a) for a in attuned] if isinstance(attuned, list) else []

        def _identity(name):
            entry = next((e for e in inventory if isinstance(e, dict) and e.get("name") == name), {})
            return str(entry.get("base") or name).strip().lower()

        def _prereq_met(prereq):
            """SRD 5.1 prerequisites the sheet can answer; anything else is a GM ruling."""
            text = str(prereq or "").strip().lower()
            if not text:
                return True
            klass = str(_db_val(cursor, "character_class", "") or "").strip().lower()
            race = str(_db_val(cursor, "race", "") or "").strip().lower()
            alignment = str(_db_val(cursor, "alignment", "") or "").strip().lower()
            if klass and klass in text:
                return True
            if race and race in text:
                return True
            if "spellcaster" in text or "spellcasting" in text:
                return bool(_db_val(cursor, "spellcasting", None))
            named_class = next((c for c in SRD_CLASSES if c.lower() in text), None)
            if named_class:
                return named_class.lower() == klass
            for term in ("lawful", "chaotic", "neutral", "good", "evil"):
                if term in text:
                    return term in alignment
            return True

        def _fail(error, reason, **extra):
            result = {"success": False, "error": error, "item": item, "reason": reason,
                      "attuned": attuned, "attunement_slots_free": max(0, 3 - len(attuned))}
            result.update(extra)
            return result

        if action == "unattune":
            if item not in attuned:
                return _fail("not_attuned", f"'{item}' is not attuned.")
            attuned.remove(item)
            _db_set(cursor, "attuned", attuned)
            _recompute_armor_class(cursor)
            block = _equipment_block(cursor)
            return {"success": True, "action": "unattune", "item": item, "attuned": attuned,
                    "attunement_slots_free": max(0, 3 - len(attuned)), "equipment": block,
                    "warnings": block.get("warnings", []),
                    "narrative_format": f"Attunement with {item} ends — "
                                        f"{max(0, 3 - len(attuned))} slot(s) free"}

        if not declared.get("attunement"):
            return _fail("attunement_not_required",
                         f"'{item}' does not require attunement (declare attunement=True to change that).")
        if item in attuned:
            return {"success": True, "action": "attune", "item": item, "already_attuned": True,
                    "attuned": attuned, "attunement_slots_free": max(0, 3 - len(attuned)),
                    "equipment": _equipment_block(cursor),
                    "narrative_format": f"Already attuned to {item}"}
        if not instant and _in_active_combat():
            return _fail("requires_short_rest",
                         f"Attuning to {item} requires a short rest — not possible during a fight.")
        if len(attuned) >= 3:
            return _fail("attunement_full",
                         "Already attuned to three magic items — break one attunement first.")
        prereq = declared.get("attunement_by")
        if prereq and not _prereq_met(prereq):
            return _fail("attunement_prerequisite_unmet",
                         f"'{item}' requires attunement {prereq} — this character does not qualify.",
                         prerequisite=prereq)
        # SRD 5.1: "a creature can't attune to more than one copy of an item."
        ident = _identity(item)
        clash = next((a for a in attuned if _identity(a) == ident), None)
        if clash is not None:
            return _fail("attunement_duplicate_item",
                         f"Already attuned to another copy of {declared.get('base') or item} "
                         f"({clash}).", conflicting_item=clash)
        attuned.append(item)
        _db_set(cursor, "attuned", attuned)
        _recompute_armor_class(cursor)
        block = _equipment_block(cursor)
        return {"success": True, "action": "attune", "item": item, "attuned": attuned,
                "attunement_slots_free": max(0, 3 - len(attuned)), "equipment": block,
                "warnings": block.get("warnings", []),
                "narrative_format": f"Attuned to {item} — "
                                    f"{max(0, 3 - len(attuned))} slot(s) free"}
    except Exception as e:
        return {"success": False, "error": f"Error attuning item: {str(e)}", "item": item}


@mcp.tool()
def lookup(query: str) -> str:
    """Era lore on demand (read-only).

    WHEN: you need the era again, one polity's factions, or another era from the ERA INDEX.
    - query: 'here' = the era you are in; an era id = that era; a polity name = just that polity.
    """
    try:
        from web.eras import lookup as _lookup

        current = ""
        if DB_CONNECTION is not None:
            current = str(_db_val(DB_CONNECTION.cursor(), "era", "") or "")
        return _lookup(query, current)
    except Exception as e:  # noqa: BLE001
        return f"lookup failed: {type(e).__name__}: {e}"


@mcp.tool()
def set_player_field(key: str, value: str) -> dict:
    """Set one stored player field (session bookkeeping)."""
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        cursor = DB_CONNECTION.cursor()
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                       (str(key), json.dumps(str(value))))
        DB_CONNECTION.commit()
        return {"success": True, "key": str(key), "value": str(value)}
    except Exception as e:
        return {"success": False, "error": f"Error setting '{key}': {str(e)}"}


@mcp.tool()
def debug_set_field(key: str, value: str) -> dict:
    """DEBUG ONLY -- set one stored player field to an arbitrary JSON value.

    Unlike `set_player_field`, which stringifies everything, this writes the value as real
    JSON, so dicts, lists, numbers, booleans and null all survive. The derived blocks are
    recomputed afterwards (armour class, spell save DC / attack, max HP). Enabled only when
    the engine started this server with `--debug`.
    """
    global DB_CONNECTION
    if not DEBUG_MODE:
        return {"success": False, "error": "developer mode is not enabled."}
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = value
    try:
        cursor = DB_CONNECTION.cursor()
        key = str(key)
        if '.' in key:
            root_key, _, path_in_obj = key.partition('.')
            cursor.execute("SELECT value FROM player WHERE key = ?", (root_key,))
            row = cursor.fetchone()
            data = json.loads(row[0]) if row else {}
            set_nested_value(data, path_in_obj, parsed)
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                           (root_key, json.dumps(data)))
        else:
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                           (key, json.dumps(parsed)))
        DB_CONNECTION.commit()
        ac = _recompute_armor_class(cursor)
        DB_CONNECTION.commit()
        return {"success": True, "key": key, "value": parsed,
                "armor_class": ac if ac is not None else _db_val(cursor, "armor_class", None)}
    except Exception as e:
        return {"success": False, "error": f"Error setting '{key}': {str(e)}"}


@mcp.tool()
def debug_device(parts: list[str] | None = None, present: bool = True) -> dict:
    """DEBUG ONLY -- set the Device's recovered parts directly.

    Replaces every Device / part inventory entry: keeps the Device when `present`, then adds
    each named part (canonical names, in config order) stamped with the current era. This
    bypasses the GM's add guards on purpose -- it is the developer's lever. Enabled only
    when the engine started this server with `--debug`.
    """
    global DB_CONNECTION
    if not DEBUG_MODE:
        return {"success": False, "error": "developer mode is not enabled."}
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        cursor = DB_CONNECTION.cursor()
        era = str(_db_val(cursor, "era", "") or "").strip().lower()
        inventory = _db_val(cursor, "inventory", []) or []
        if not isinstance(inventory, list):
            inventory = []
        kept = [e for e in inventory
                if not (isinstance(e, dict) and (e.get("device") or e.get("device_part")))]
        if present:
            kept.append(device.device_entry())
        wanted = {device.canonical_part(p) for p in (parts or []) if device.canonical_part(p)}
        recovered = []
        for name in device.part_names():
            if name in wanted:
                kept.append(device.part_entry(name, era or "egypt"))
                recovered.append(name)
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                       ("inventory", json.dumps(kept)))
        DB_CONNECTION.commit()
        _recompute_armor_class(cursor)
        DB_CONNECTION.commit()
        return {"success": True, "present": bool(present), "parts": recovered,
                "total": device.part_total()}
    except Exception as e:
        return {"success": False, "error": f"Error setting the Device: {str(e)}"}


@mcp.tool()
def dump_player_save_state() -> dict:
    """The whole player row map, unreduced. For the engine's save, never for the GM.

    `dump_player_db` is the GM's view: it reduces `reputation` to the era the save is in. A
    save built from that view writes a **flat** map -- the `{era: ...}` wrapper and every
    other era's standing are lost, and the reloaded save can never record reputation again.
    This returns the rows verbatim, derived blocks omitted (they are recomputed).
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"error": "Database not initialized."}
    cursor = DB_CONNECTION.cursor()
    cursor.execute("SELECT * FROM player")
    result: dict = {}
    for key, value in cursor.fetchall():
        try:
            result[key] = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            result[key] = value
    return result


@mcp.tool()
def dump_player_db() -> dict:
    """Full dump of the in-memory player database.

    WHEN: a state refresh is needed (e.g. at awakening).
    NOTE: the derived `_carrying` (capacity/encumbrance/speed), `_equipment` (worn armour, hands, AC breakdown) and `_passive` (passive Perception/Investigation/Insight = 10 + modifier) blocks are computed, never saved.
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"error": "Database not initialized."}

    try:
        cursor = DB_CONNECTION.cursor()
        cursor.execute("SELECT * FROM player")
        rows = cursor.fetchall()

        if not rows:
            return {}

        result = {}
        for key, value in rows:
            try:
                result[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                result[key] = value

        result["_carrying"] = _carry_block(cursor)
        result["_equipment"] = _equipment_block(cursor)
        result["_passive"] = _passive_scores(cursor)
        # Reputation is stored era-scoped; the GM is only ever offered the era it is in.
        # The saved map keeps every era, so nothing is lost by leaving -- which is why the
        # SAVE must never be built from this view (see `dump_player_save_state`).
        rep = result.get("reputation")
        era = str(_db_val(cursor, "era", "") or "")
        if isinstance(rep, dict) and era:
            scoped = rep.get(era)
            # Never another age's standing -- but never nothing at all either: an era whose
            # scope has not been written to yet still shows its real polities, so the GM has
            # a shape to write against instead of inventing one.
            result["reputation"] = (scoped if isinstance(scoped, dict)
                                    else _era_reputation_scope(era))

        return result
    except Exception as e:
        return {"error": f"Error dumping database: {str(e)}"}


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
    - description: what HAPPENS -- the protagonist's action and notable transient events. NAME every NPC; never identify anyone by look. Never describe a person's look, the place, its furniture, its light, the time or the weather (the seed carries the place, time_of_day/weather carry the light, a declared NPC's look comes from the registry). You MAY say where the protagonist stands or sits. DO restate anything still visible from earlier (spilled ale, blood, a body, an open door).
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
def note_place(kingdom: str, area: str, place: list[str],
               main_npcs: list[dict] | None = None,
               cast: list[dict] | None = None) -> dict:
    """Remember a place (and its regulars) in the storyline when images are off.

    WHEN: once, on entering a genuinely NEW place -- not every turn. The engine primes the
    tree (names only) each session, and pools these places with the era's own for a jump.
    FIELDS:
    - place: ordered path BELOW the settlement, deepest last, at least 2 entries -- [district/neighbourhood, the exact building/room, (any room inside it, ...)]. Reuse the exact path once used.
    - kingdom = realm; area = settlement (city/town).
    - main_npcs: the place's OWN regulars, [{name, role, race, class}] -- one person per entry; give race and class for each.
    - cast: recurring people NOT tied to one place (a villain, a companion), [{name, role, race, class}].

    RULES:
    - Names + roles + races + classes only: never a look, a pose or a second person's details.
    - Re-declaring a name updates it; reuse names and paths exactly.
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


@mcp.tool()
def rest(rest_type: str, prepared_spells: list[str] | None = None) -> dict:
    """Apply a short or long rest; all numeric changes are auto-applied.

    WHEN: the player rests.
    FIELDS:
    - rest_type: 'short' | 'long'.
    - prepared_spells: (long rest only) full replacement list of prepared spell names; validated against capacity (Wizards against the spellbook).

    RULES:
    - Short rest: auto-spends hit dice until HP is full or none remain. Warlocks restore Pact Magic; Wizards auto-apply Arcane Recovery (ceil(level/2) combined slot levels, lowest first, not 6th+).
    - Long rest: full HP, regain max(level//2, 1) hit dice (capped at level), all slots restored, active effects cleared with deltas reverted, one exhaustion level removed.
    - Long rest is rejected at 0 HP.
    - Returns hints for class features needing manual recharge.

    EX:
    rest(rest_type='short')
    rest(rest_type='long', prepared_spells=['Magic Missile', 'Shield', 'Mage Armor', 'Burning Hands'])
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}

    if rest_type not in ("short", "long"):
        return {"success": False, "error": "rest_type must be 'short' or 'long'."}

    try:
        cursor = DB_CONNECTION.cursor()

        char_class = _db_val(cursor, "character_class", "")
        level = int(_db_val(cursor, "level", 1))
        hd_count = int(_db_val(cursor, "hit_dice_count", 0))
        hd_size = int(_db_val(cursor, "hit_dice_size", 8))
        total_hp = int(_db_val(cursor, "total_hit_points", 1))
        current_hp = int(_db_val(cursor, "current_hit_points", 0))

        con_mod = _ability_mod(_effective_stats(cursor).get("con", 10))

        sc_raw = _db_val(cursor, "spellcasting", {})
        if isinstance(sc_raw, str):
            sc_raw = json.loads(sc_raw)
        sc = dict(sc_raw) if sc_raw else {}

        buff_data = _db_val(cursor, "_active_buff_data", {})
        if isinstance(buff_data, str):
            buff_data = json.loads(buff_data)
        effects_list = _db_val(cursor, "active_effects", [])
        if isinstance(effects_list, str):
            effects_list = json.loads(effects_list)

        result = {"success": True, "rest_type": rest_type}
        changes = {}
        hints = []

        caster_type = CASTER_TYPE_MAP.get(char_class)
        slot_table = None
        if caster_type:
            slot_table = SLOT_TABLES[caster_type].get(level, {})

        if rest_type == "short":
            dice_spent = 0
            total_healing = 0
            missing_hp = total_hp - current_hp
            # Periapt of Wound Closure (and similar): double the Hit-Die healing (SRD 5.1).
            hd_multiplier = float(_item_effect_state(cursor).get("hit_die_healing_multiplier") or 1)

            while missing_hp > 0 and dice_spent < hd_count:
                roll = random.randint(1, hd_size)
                healing = int(max(roll + con_mod, 0) * hd_multiplier)
                total_healing += healing
                missing_hp -= healing
                dice_spent += 1

            new_hd = hd_count - dice_spent

            if dice_spent > 0:
                hp_result = _apply_hp_change(cursor, total_healing)
                _db_set(cursor, "hit_dice_count", str(new_hd))
                DB_CONNECTION.commit()
                changes["hp"] = {"old": current_hp, "new": hp_result["new_value"],
                                 "healed": hp_result["new_value"] - current_hp,
                                 "healing_applied": hp_result["new_value"] - current_hp,
                                 "max_hp": total_hp,
                                 "dice_spent": dice_spent,
                                 "status": hp_result["hp_status"]}
                changes["hit_dice"] = {"old": hd_count, "new": new_hd, "spent": dice_spent}
            else:
                changes["hp"] = {"old": current_hp, "new": current_hp,
                                 "healed": 0, "healing_applied": 0, "max_hp": total_hp,
                                 "dice_spent": 0,
                                 "status": _format_hp_status(current_hp, total_hp)}
                changes["hit_dice"] = {"old": hd_count, "new": hd_count, "spent": 0}

            recovered = {}

            if caster_type == "warlock" and slot_table:
                sc["slots"] = {str(k): v for k, v in slot_table.items()}
                _db_set(cursor, "spellcasting", sc)
                DB_CONNECTION.commit()
                changes["slots_restored"] = {str(k): v for k, v in slot_table.items()}

            if char_class == "Wizard" and caster_type == "full":
                max_slots = FULL_CASTER_SPELL_SLOTS.get(level, {})
                current_slots = sc.get("slots", {})
                budget = math.ceil(level / 2)
                recovered = {}
                budget_used = 0

                for slot_lvl in range(1, 6):
                    slot_key = str(slot_lvl)
                    slot_cost = slot_lvl
                    curr = int(current_slots.get(slot_key, 0))
                    max_s = max_slots.get(slot_lvl, 0)
                    while curr < max_s and budget_used + slot_cost <= budget:
                        curr += 1
                        budget_used += slot_cost
                    if slot_key in current_slots:
                        recovered_amount = curr - int(current_slots.get(slot_key, 0))
                    else:
                        recovered_amount = curr
                    if recovered_amount > 0:
                        recovered[slot_key] = recovered_amount
                        sc["slots"] = sc.get("slots", {})
                        sc["slots"][slot_key] = curr

                if recovered:
                    _db_set(cursor, "spellcasting", sc)
                    DB_CONNECTION.commit()
                    changes["arcane_recovery"] = {
                        "recovered": recovered,
                        "budget_used": budget_used,
                        "budget_total": budget,
                    }

            narrative_parts = [f"Short Rest complete."]
            if dice_spent > 0:
                narrative_parts.append(f"Spent {dice_spent} hit die(s) → healed {total_healing} HP ({new_hd}/{level} remaining).")
            else:
                narrative_parts.append("HP already at maximum — no hit dice spent.")
            if caster_type == "warlock" and slot_table:
                narrative_parts.append("Pact Magic slots restored.")
            if recovered:
                arc_parts = ", ".join(f"Lv{k}: +{v}" for k, v in recovered.items())
                narrative_parts.append(f"Arcane Recovery: {arc_parts} (budget {budget_used}/{budget}).")

            hints.append("Wizard: Arcane Recovery has been used for this rest period (once per long rest)." if char_class == "Wizard" else None)
            hints.append("Fighter: Second Wind and Action Surge recharge on short/long rest." if char_class == "Fighter" else None)
            hints.append("Cleric: Channel Divinity recharges on short/long rest." if char_class == "Cleric" else None)
            hints.append("Warlock: Pact Magic slots recharge on short rest." if char_class == "Warlock" else None)
            hints.append("Bard: Bardic Inspiration recharges on short rest (level 5+)." if char_class == "Bard" and level >= 5 else None)
            hints.append("Monk: Ki points recharge on short rest (level 2+)." if char_class == "Monk" and level >= 2 else None)
            hints.append("Druid: Wild Shape uses recharge on short rest." if char_class == "Druid" and level >= 2 else None)
            hints.append("Paladin: Channel Divinity recharges on short/long rest." if char_class == "Paladin" and level >= 3 else None)
            hints = [h for h in hints if h is not None]

        elif rest_type == "long":
            if current_hp <= 0:
                return _action_failure(
                    "Cannot benefit from a long rest with 0 HP. The character must be stabilized first.",
                    "Long rest — impossible while at 0 HP")

            _db_set(cursor, "current_hit_points", str(total_hp))
            changes["hp"] = {"old": current_hp, "new": total_hp,
                             "healing_applied": total_hp - current_hp, "max_hp": total_hp,
                             "status": _format_hp_status(total_hp, total_hp)}

            hd_regained = max(level // 2, 1)
            new_hd = min(hd_count + hd_regained, level)
            _db_set(cursor, "hit_dice_count", str(new_hd))
            DB_CONNECTION.commit()
            changes["hit_dice"] = {"old": hd_count, "new": new_hd, "regained": hd_regained}

            if slot_table:
                sc["slots"] = {str(k): v for k, v in slot_table.items()}
                _db_set(cursor, "spellcasting", sc)
                DB_CONNECTION.commit()
                changes["slots_restored"] = {str(k): v for k, v in slot_table.items()}
                if caster_type == "warlock":
                    changes["slots_restored"] = {"pact_magic": {str(k): v for k, v in slot_table.items()}}

            effects_cleared = []
            for spell_name in list(buff_data.keys()):
                entries = buff_data[spell_name]
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("delta") is not None:
                        modify_player_numeric(key=entry["field"], delta=-entry["delta"])
                effects_cleared.append(spell_name)
            _db_set(cursor, "active_effects", [])
            _db_set(cursor, "_active_buff_data", {})
            _db_set(cursor, "concentration", {})
            _clear_death_saves(cursor)
            DB_CONNECTION.commit()
            if effects_cleared:
                changes["effects_cleared"] = effects_cleared

            # SRD 5.1: a long rest removes one level of exhaustion.
            if _exhaustion_level(cursor) > 0:
                exh_change = _apply_exhaustion_change(cursor, -1, reason="long rest")
                changes["exhaustion"] = {"old": exh_change["old"], "new": exh_change["new"]}

            # A long rest ends capacity-changing effects (e.g. enhance ability: Bull's Strength).
            previous_multiplier = _db_val(cursor, "capacity_multiplier", 1)
            if str(previous_multiplier) not in ("1", "1.0"):
                _db_set(cursor, "capacity_multiplier", 1)
                DB_CONNECTION.commit()
                changes["capacity_multiplier"] = {"old": previous_multiplier, "new": 1}

            if prepared_spells is not None:
                if char_class in PREPARED_CASTER_CLASSES or char_class == "Wizard":
                    max_spells = get_max_prepared_spells(cursor)
                    if _needs_spellbook(cursor) and not _has_spellbook_item(cursor):
                        changes["prepared_spells_error"] = {
                            "error": "Spellbook missing.",
                            "hint": "The character's spellbook is not in their inventory; "
                                    "no spells can be prepared until it is recovered.",
                        }
                    elif max_spells is not None and len(prepared_spells) > max_spells:
                        changes["prepared_spells_error"] = {
                            "error": "Too many prepared spells.",
                            "provided": len(prepared_spells),
                            "max": max_spells,
                            "formula": "spellcasting_ability_modifier + level",
                        }
                    elif char_class == "Wizard":
                        spellbook = sc.get("spellbook", [])
                        spellbook_names = set()
                        for s in spellbook:
                            if isinstance(s, dict):
                                spellbook_names.add(s.get("name", ""))
                            else:
                                spellbook_names.add(str(s))
                        not_in_book = [s for s in prepared_spells if s not in spellbook_names]
                        if not_in_book:
                            changes["prepared_spells_error"] = {
                                "error": "Spells not in spellbook.",
                                "not_in_spellbook": not_in_book,
                                "hint": "Wizards can only prepare spells from their spellbook.",
                            }
                        else:
                            old_prepared = [s.get("name", str(s)) if isinstance(s, dict) else str(s)
                                            for s in sc.get("spells_prepared", [])]
                            sc["spells_prepared"] = [{"name": s} for s in prepared_spells]
                            _db_set(cursor, "spellcasting", sc)
                            DB_CONNECTION.commit()
                            changes["prepared_spells"] = {
                                "old": old_prepared,
                                "new": list(prepared_spells),
                                "count": len(prepared_spells),
                                "max": max_spells if max_spells is not None else 0,
                            }
                    else:
                        old_prepared = [s.get("name", str(s)) if isinstance(s, dict) else str(s)
                                        for s in sc.get("spells_prepared", [])]
                        sc["spells_prepared"] = [{"name": s} for s in prepared_spells]
                        _db_set(cursor, "spellcasting", sc)
                        DB_CONNECTION.commit()
                        changes["prepared_spells"] = {
                            "old": old_prepared,
                            "new": list(prepared_spells),
                            "count": len(prepared_spells),
                            "max": max_spells if max_spells is not None else 0,
                        }
                else:
                    changes["prepared_spells_error"] = {
                        "error": f"{char_class} is not a prepared caster.",
                        "hint": f"{char_class} uses spells_known (cannot change on long rest).",
                    }

            narrative_parts = ["Long Rest complete. HP fully restored."]
            narrative_parts.append(f"Hit Dice: {new_hd}/{level} (+{hd_regained} regained).")
            if slot_table:
                narrative_parts.append("All spell slots restored.")
            if effects_cleared:
                narrative_parts.append(f"Active effects cleared: {', '.join(effects_cleared)}.")
            if prepared_spells is not None and "prepared_spells" in changes:
                narrative_parts.append(f"Prepared spells updated ({len(prepared_spells)}/{changes['prepared_spells']['max']}).")

            hints.append("Wizard: Arcane Recovery available (once per long rest) on next short rest." if char_class == "Wizard" else None)
            hints.append("Fighter: Second Wind and Action Surge recharge on short/long rest." if char_class == "Fighter" else None)
            hints.append("Cleric: Channel Divinity recharges on short/long rest." if char_class == "Cleric" else None)
            hints.append("Warlock: Pact Magic slots recharge on short rest." if char_class == "Warlock" else None)
            hints.append("Bard: Bardic Inspiration recharges on short rest (level 5+)." if char_class == "Bard" and level >= 5 else None)
            hints.append("Monk: Ki points recharge on short rest (level 2+)." if char_class == "Monk" and level >= 2 else None)
            hints.append("Druid: Wild Shape uses recharge on short/long rest." if char_class == "Druid" and level >= 2 else None)
            hints.append("Paladin: Channel Divinity recharges on short/long rest." if char_class == "Paladin" and level >= 3 else None)
            hints.append("Carrying capacity is back to normal (a capacity-changing effect ended)." if "capacity_multiplier" in changes else None)
            hints.append("No more than one long rest per 24 hours." if True else None)
            hints = [h for h in hints if h is not None]

        result["changes"] = changes
        result["hints"] = hints
        result["narrative_format"] = " ".join(narrative_parts)
        return result

    except Exception as e:
        return {"success": False, "error": f"Error applying rest: {str(e)}"}


@mcp.tool()
def modify_exhaustion(delta: int, reason: str | None = None) -> dict:
    """Add or remove exhaustion levels on the player (0-6).

    WHEN: exertion, a hazard or magic causes or clears exhaustion.
    FIELDS:
    - delta: signed number of levels. reason: an optional short note.

    RULES:
    - The engine applies the full level table and re-derives max HP, speed and roll disadvantage; never hand-apply the penalties.
    - A long rest removes one level automatically; use this tool for every other source.
    - Level 6 is death.

    EX:
    modify_exhaustion(delta=1, reason='forced march')
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    cursor = DB_CONNECTION.cursor()
    change = _apply_exhaustion_change(cursor, delta, reason=reason)
    effects = change["effects"]
    parts = [f"Exhaustion {change['old']} \u2192 {change['new']}"]
    if reason:
        parts.append(f"({reason})")
    if effects["dead"]:
        parts.append("\u2014 level 6: the character is dead.")
    elif effects["level"]:
        bits = []
        if effects["check_disadvantage"]:
            bits.append("disadvantage on ability checks")
        if effects["speed_multiplier"] == 0.5:
            bits.append("speed halved")
        elif effects["speed_multiplier"] == 0.0:
            bits.append("speed 0")
        if effects["attack_disadvantage"]:
            bits.append("disadvantage on attack rolls and saves")
        if effects["hp_multiplier"] == 0.5:
            bits.append(f"HP maximum halved ({change['max_hp']})")
        if bits:
            parts.append("\u2014 " + ", ".join(bits))
    return {
        "success": True,
        "exhaustion": change["new"],
        "old": change["old"],
        "delta": change["delta"],
        "effects": effects,
        "max_hp": change["max_hp"],
        "narrative_format": " ".join(parts),
    }


@mcp.tool()
def make_death_save() -> dict:
    """Roll a death saving throw for a player at 0 HP.

    WHEN: the start of the player's turn while at 0 HP.
    RULES:
    - Three successes stabilise (counters clear, still 0 HP); three failures kill. A natural 20 regains 1 HP and clears the counters; a natural 1 counts as two failures.
    - Healing any real HP clears the counters, and damage at 0 HP adds a failure automatically -- so call this only at the start of the turn, at 0 HP.

    EX:
    make_death_save()
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    cursor = DB_CONNECTION.cursor()
    current_hp = int(_db_val(cursor, "current_hit_points", 0) or 0)
    if current_hp > 0:
        return {"success": False, "error": "not_dying",
                "reason": "The character is not at 0 HP \u2014 no death save is needed."}
    if int(_db_val(cursor, "death_save_failures", 0) or 0) >= 3:
        return {"success": False, "error": "already_dead",
                "reason": "The character already has three death-save failures."}
    successes = int(_db_val(cursor, "death_save_successes", 0) or 0)
    failures = int(_db_val(cursor, "death_save_failures", 0) or 0)
    roll = random.randint(1, 20)
    if roll == 20:
        hp = _apply_hp_change(cursor, 1)
        _clear_death_saves(cursor)
        return {"success": True, "roll": 20, "outcome": "revived", "hp": hp["new_value"],
                "narrative_format":
                "Death save: natural 20 \u2014 the character regains 1 HP and is conscious."}
    if roll == 1:
        failures = min(3, failures + 2)
    elif roll >= 10:
        successes = min(3, successes + 1)
    else:
        failures = min(3, failures + 1)
    _db_set(cursor, "death_save_successes", str(successes))
    _db_set(cursor, "death_save_failures", str(failures))
    DB_CONNECTION.commit()
    if failures >= 3:
        _end_concentration(cursor, reason="death")
        outcome = "dead"
        narrative = f"Death save: {roll} \u2014 third failure. The character is dead."
    elif successes >= 3:
        _reset_death_saves(cursor)
        outcome = "stable"
        narrative = f"Death save: {roll} \u2014 third success. The character is stable."
    else:
        outcome = "success" if roll >= 10 else "failure"
        narrative = f"Death save: {roll} \u2014 {successes} successes, {failures} failures."
    return {"success": True, "roll": roll, "successes": successes, "failures": failures,
            "outcome": outcome, "narrative_format": narrative}


@mcp.tool()
def roll_dice(dice_notation: str, modifier: int = 0, actor: str = "{player_name}") -> dict:
    """Roll dice for damage, healing, loot quantity, or any random magnitude.

    WHEN: a pure magnitude roll. For success/failure use perform_check.
    FIELDS:
    - dice_notation: dice only (e.g. '3d4') -- no modifiers.
    - modifier: flat bonus/penalty on the total. actor: who rolls.

    EX:
    roll_dice(actor='Senna', dice_notation='3d4', modifier=3)
    """
    try:
        if actor == "{player_name}" and DB_CONNECTION is not None:
            actor = _db_val(DB_CONNECTION.cursor(), "name", "Player")

        parts = dice_notation.lower().split('d')
        if len(parts) != 2:
            return {"error": "Invalid dice notation. Use format 'XdY' (e.g., '2d6')."}

        num_dice = int(parts[0]) if parts[0] else 1
        die_size = int(parts[1])

        if num_dice <= 0 or die_size <= 0:
            return {"error": "Number of dice and die size must be positive integers."}

        rolls = [random.randint(1, die_size) for _ in range(num_dice)]
        total = sum(rolls) + modifier

        rolls_str = " + ".join(str(r) for r in rolls)
        if modifier != 0:
            narrative = f"{actor} {dice_notation}: {total} ({rolls_str} + {modifier})"
        else:
            narrative = f"{actor} {dice_notation}: {total} ({rolls_str})"

        return {
            "actor": actor,
            "notation": dice_notation,
            "rolls": rolls,
            "modifier": modifier,
            "total": total,
            "narrative_format": narrative,
        }
    except ValueError:
        return {"error": "Invalid dice notation. Please provide integers (e.g., '2d6')."}


@mcp.tool()
def perform_check(modifier: int | None = None, dc: int = 0, check_name: str = "Check",
                  actor: str = "{player_name}", ability: str | None = None, grapple: bool = False,
                  save: bool = False, situational_modifier: int | None = None,
                  context: str | None = None, against: str | None = None,
                  damage: str | None = None, condition: str | None = None) -> dict:
    """Perform a skill check or saving throw (d20 + modifier vs DC).

    WHEN: the success/failure of a save or ability/skill check. Weapon attacks -> resolve_attack; spells -> resolve_magic.
    FIELDS:
    - modifier: only for an NPC, or a custom player check with no ability and no known skill (e.g. 'Luck'). IGNORED for a player check/save the engine can derive.
    - dc: the DC to beat. check_name: a label (e.g. 'Athletics', 'Dexterity save'). actor: who acts.
    - ability: 'str' | 'dex' | 'con' | 'int' | 'wis' | 'cha'; REQUIRED for a player save (save=True).
    - save: True for a SAVING THROW -- the engine computes the player's modifier (effective ability + proficiency + item bonuses) and adds situational_modifier; do NOT pass modifier.
    - situational_modifier: a situational bonus/penalty on top of the derived modifier (cover, a flat Bless total).
    - context: for item skill_bonus effects with a context:<tag> predicate.
    - against / damage / condition: scope a player save for item save_advantage effects.
    - grapple: True for a GRAPPLE attempt; refused, action spent, when both hands are full. Do NOT set it to escape a grapple.

    RULES:
    - Player checks/saves are engine-derived: effective ability + proficiency (doubled for Expertise, halved for Jack of All Trades) + item bonuses. Never compute them yourself.
    - Heavily encumbered or unproficient armour: STR/DEX/CON checks roll with disadvantage; the result carries 'disadvantage_sources' and both dice.
    - Grapple with no free hand is refused with turn_lost=true (no roll).

    EX:
    perform_check(actor='Thorin', dc=15, check_name='Athletics', ability='str')
    perform_check(actor='Thorin', dc=15, check_name='Dexterity save', ability='dex', save=True)
    """
    cursor = DB_CONNECTION.cursor() if DB_CONNECTION is not None else None
    is_player = _is_player_actor(cursor, actor)
    if is_player and cursor is not None:
        actor = _db_val(cursor, "name", "Player")

    # SRD 5.1: a grapple needs a free hand — refuse before rolling anything.
    if grapple and is_player and cursor is not None:
        state = _equipment_block(cursor)
        if state.get("derived_from_equipped") and state.get("hands_free", 2) == 0:
            return _blocked_action(
                cursor, "no_free_hand",
                "Both hands are occupied — a grapple needs at least one free hand.",
                None, "Grapple failed — both hands are busy. The action is spent.")

    disadvantage, encumbrance_sources = _roll_disadvantage(cursor, is_player, ability)
    cond_disadvantage, cond_sources = _condition_check_disadvantage(
        _conditions_for(cursor, actor, is_player=is_player), ability)
    encumbrance_sources = [*encumbrance_sources, *cond_sources]
    disadvantage = disadvantage or cond_disadvantage

    # SRD 5.1 exhaustion: level 1 → disadvantage on ability checks, level 3 → on saving throws.
    exh = _exhaustion_effects(_exhaustion_level_for(cursor, actor, is_player=is_player))
    if exh["save_disadvantage"] if save else exh["check_disadvantage"]:
        disadvantage = True
        encumbrance_sources = [*encumbrance_sources, f"exhaustion {exh['level']}"]

    save_detail = None
    check_detail = None
    advantage = False
    check_bonus_sources = []
    if is_player and save and cursor is not None:
        save_detail = _derived_save(cursor, ability, situational_modifier or 0,
                                    against=against, damage=damage, condition=condition)
        modifier = save_detail["modifier"]
        advantage = bool(save_detail.get("advantage"))
    elif is_player and cursor is not None:
        # Full Option B: the engine derives ability/skill checks (ability + proficiency /
        # Expertise / Jack of All Trades + item bonuses). The passed `modifier` is only the
        # fallback for a custom check with no ability and no known skill (e.g. 'Luck').
        if skills.skill_name(check_name) or ability:
            check_detail = _derived_check(cursor, check_name, ability, context,
                                          situational_modifier or 0)
            modifier = check_detail["modifier"]
        else:
            modifier = int(modifier or 0) + int(situational_modifier or 0)
            check_bonus, check_bonus_sources = equipment.check_bonus_for(
                _carry_get(cursor), check_name, context)
            modifier += check_bonus
    else:
        modifier = int(modifier or 0)

    dice_bonus = 0
    dice_sources = []
    if is_player and cursor is not None:
        field = "saving_throws" if save else "ability_checks"
        for dice in equipment.active_dice(_carry_get(cursor), field):
            raw = str(dice.get("value") or "").strip()
            sign = -1 if raw.startswith("-") else 1
            _, _, rolled = _parse_and_roll_dice(raw.lstrip("+-"))
            if rolled:
                dice_bonus += sign * rolled
                dice_sources.append(f"{raw} ({dice.get('spell')})")

    roll, rolls, roll_mode, _cancelled = _roll_d20(advantage=advantage, disadvantage=disadvantage)
    total = roll + modifier + dice_bonus

    if roll == 20:
        result = "Critical Success"
    elif roll == 1:
        result = "Critical Failure"
    elif total >= dc:
        result = "Success"
    else:
        result = "Failure"

    narrative = f"{actor} {check_name}: {total} vs DC {dc} ({result}) ({roll} + {modifier})"
    if dice_bonus:
        narrative += f" {'+' if dice_bonus >= 0 else '-'}{abs(dice_bonus)} dice"
    if roll_mode:
        narrative += f" [{roll_mode}]"
    if save_detail is not None:
        bits = []
        if save_detail["ability_modifier"]:
            bits.append(f"{save_detail['ability'].upper()} {save_detail['ability_modifier']:+d}")
        if save_detail["proficiency_bonus"]:
            bits.append(f"prof {save_detail['proficiency_bonus']:+d}")
        if save_detail["item_bonus"]:
            bits.append(f"item {save_detail['item_bonus']:+d}")
        if save_detail["situational"]:
            bits.append(f"situational {save_detail['situational']:+d}")
        if bits:
            narrative += " [" + ", ".join(bits) + "]"
    if check_detail is not None:
        bits = []
        if check_detail["ability"] and check_detail["ability_modifier"]:
            bits.append(f"{check_detail['ability'].upper()} {check_detail['ability_modifier']:+d}")
        if check_detail["proficiency_bonus"]:
            tag = ("expertise" if check_detail["expertise"]
                   else "JoAT" if check_detail["jack_of_all_trades"] else "prof")
            bits.append(f"{tag} {check_detail['proficiency_bonus']:+d}")
        if check_detail["item_bonus"]:
            bits.append(f"item {check_detail['item_bonus']:+d}")
        if check_detail["situational"]:
            bits.append(f"situational {check_detail['situational']:+d}")
        if bits:
            narrative += " [" + ", ".join(bits) + "]"

    response = {
        "actor": actor,
        "check_name": check_name,
        "base_roll": roll,
        "modifier": modifier,
        "total": total,
        "dc_to_beat": dc,
        "outcome": result,
        "narrative_format": narrative,
    }
    if save_detail is not None:
        response["save"] = save_detail
        if save_detail.get("advantage"):
            response["advantage_sources"] = save_detail.get("advantage_sources")
    elif check_detail is not None:
        response["check"] = check_detail
        if check_detail["item_sources"]:
            response["item_bonus_sources"] = check_detail["item_sources"]
    elif check_bonus_sources:
        response["item_bonus_sources"] = check_bonus_sources
    if dice_bonus:
        response["dice_bonus"] = dice_bonus
        response["dice_sources"] = dice_sources
    if rolls:
        response[f"{roll_mode}_rolls"] = rolls
    _encumbrance_note(response, encumbrance_sources)

    return response


def _registry_hp(target_name: str) -> int | None:
    entry = _COMBAT_REGISTRY.get(target_name)
    if entry:
        return entry["current_hp"]
    return None


def _registry_ac(target_name: str) -> int | None:
    entry = _COMBAT_REGISTRY.get(target_name)
    if entry:
        return entry["ac"]
    return None


def _registry_update_hp(target_name: str, new_hp: int):
    entry = _COMBAT_REGISTRY.get(target_name)
    if entry:
        entry["current_hp"] = new_hp


def _registry_kill(target_name: str):
    entry = _COMBAT_REGISTRY.get(target_name)
    if entry:
        entry["killed"] = True


def _registry_cr(target_name: str) -> float | None:
    entry = _COMBAT_REGISTRY.get(target_name)
    if entry:
        return entry.get("challenge_rating")
    return None


def _registry_max_hp(target_name: str) -> int:
    entry = _COMBAT_REGISTRY.get(target_name)
    if entry:
        return entry["max_hp"]
    return 0


def _resolve_target_hp(target_name, is_player, cursor, override_current=None, override_max=None):
    """(current_hp, max_hp, warning) for a healing target.

    Precedence: the GM's override, the combat registry (NPCs), the player's sheet, then any
    value the action carried. The ceiling is never silently taken from the current HP unless
    nothing else is known, and the caller is told when that happens.
    """
    current = override_current
    maximum = override_max

    if is_player:
        if cursor is not None:
            if current is None:
                current = int(_db_val(cursor, "current_hit_points", 0))
            if maximum is None:
                maximum = int(_db_val(cursor, "total_hit_points", 1))
    else:
        entry = _COMBAT_REGISTRY.get(target_name) if target_name else None
        if entry is not None:
            if current is None:
                current = entry.get("current_hp")
            if maximum is None:
                maximum = entry.get("max_hp")

    if current is None and target_name:
        current = _registry_hp(target_name)
    if maximum is None and target_name:
        maximum = _registry_max_hp(target_name) or None

    warning = None
    if current is None:
        current = 0
        warning = f"Unknown current HP for {target_name or 'the target'}."
    if not maximum:
        maximum = int(current)
        if warning is None:
            warning = (f"Unknown max HP for {target_name or 'the target'}; pass target_max_hp "
                       f"if it is wounded.")
    current = int(current)
    maximum = max(int(maximum), 1)
    if current > maximum:
        current = maximum
        warning = warning or (f"{target_name or 'Target'} is above its declared max HP "
                              f"({maximum}); using the declared ceiling.")
    return current, maximum, warning


def _heal_target(target_name, amount, cursor, *, is_player=False,
                 override_current=None, override_max=None, full=False):
    """Apply healing to one target, capped at its maximum HP.

    Returns `{name, healing_total, healing_applied, remaining_hp, max_hp}` plus `hp_change`
    (player) and/or `warning`. `full=True` restores to the ceiling regardless of `amount`.
    """
    current, maximum, warning = _resolve_target_hp(
        target_name, is_player, cursor, override_current, override_max)
    rolled = (maximum - current) if full else int(amount)
    applied = max(0, min(int(rolled), maximum - current))

    entry = {
        "name": target_name,
        "healing_total": int(rolled),
        "healing_applied": applied,
        "remaining_hp": current + applied,
        "max_hp": maximum,
    }
    if warning:
        entry["warning"] = warning

    if is_player:
        if cursor is not None:
            if applied > 0:
                hp_result = _apply_hp_change(cursor, applied)
                entry["hp_change"] = hp_result
                entry["remaining_hp"] = hp_result["new_value"]
        else:
            entry["warning"] = entry.get("warning") or "No player database; healing was not applied."
    elif target_name:
        if _registry_hp(target_name) is None:
            entry["warning"] = entry.get("warning") or (
                f"{target_name} is not in the combat registry, so the healing was not written "
                f"to any combatant.")
        else:
            _registry_update_hp(target_name, entry["remaining_hp"])
    return entry


# ── stat blocks: saves, attacks, conditions, damage types ────────────────────

ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")

# SRD conditions -> the roll effects the engine applies. `attackers_*_melee/ranged` need to know
# whether the attack is made at range; `resist_all` is petrified's resistance to all damage.
CONDITION_EFFECTS = {
    "blinded": {"self_attack_disadvantage": True, "attackers_advantage": True},
    "frightened": {"self_attack_disadvantage": True, "self_check_disadvantage": True},
    "invisible": {"self_attack_advantage": True, "attackers_disadvantage": True},
    "paralyzed": {"attackers_advantage": True, "auto_fail_str_dex_saves": True, "melee_crit": True},
    "petrified": {"attackers_advantage": True, "auto_fail_str_dex_saves": True,
                  "melee_crit": True, "resist_all": True},
    "stunned": {"attackers_advantage": True, "auto_fail_str_dex_saves": True},
    "unconscious": {"attackers_advantage": True, "auto_fail_str_dex_saves": True,
                    "melee_crit": True},
    "prone": {"self_attack_disadvantage": True, "attackers_advantage_melee": True,
              "attackers_disadvantage_ranged": True},
    "restrained": {"self_attack_disadvantage": True, "self_dex_save_disadvantage": True,
                   "attackers_advantage": True},
    "poisoned": {"self_attack_disadvantage": True, "self_check_disadvantage": True},
    "grappled": {},
    "incapacitated": {},
    "exhaustion": {},  # tracked as the numeric `exhaustion` level; see _exhaustion_effects
}

# SRD 5.1: concentration ends if you are incapacitated or killed.
_INCAPACITATING_CONDITIONS = {"incapacitated", "paralyzed", "petrified", "stunned",
                            "unconscious"}


def _registry_save(target_name: str, ability) -> int | None:
    """Save modifier for a registry target from its per-ability `saves`."""
    entry = _COMBAT_REGISTRY.get(target_name)
    if not entry:
        return None
    key = str(ability or "").strip().lower()[:3]
    saves = entry.get("saves")
    if isinstance(saves, dict) and key in saves:
        try:
            return int(saves[key])
        except (TypeError, ValueError):
            pass
    return None


def _registry_conditions(name) -> set:
    entry = _COMBAT_REGISTRY.get(name) or {}
    return {str(c).strip().lower() for c in (entry.get("conditions") or [])}


def _player_conditions(cursor) -> set:
    if cursor is None:
        return set()
    return {str(c).strip().lower() for c in (_db_val(cursor, "conditions", []) or [])}


def _conditions_for(cursor, name, is_player=False) -> set:
    if is_player:
        return _player_conditions(cursor)
    return _registry_conditions(name)


def _is_player_name(cursor, name) -> bool:
    """True when `name` is the player (registry is_player flag, else the save's name)."""
    entry = _COMBAT_REGISTRY.get(name)
    if isinstance(entry, dict) and entry.get("is_player"):
        return True
    if cursor is None or not name:
        return False
    return (str(name).strip().lower()
            == str(_db_val(cursor, "name", "") or "").strip().lower())


def _combatant_hp(cursor, name):
    """(current, max) HP for a combatant, from the player sheet or the registry."""
    if _is_player_name(cursor, name):
        cur = int(_db_val(cursor, "current_hit_points", 0) or 0)
        mx = int(_db_val(cursor, "total_hit_points", cur) or cur)
        return cur, mx
    reg = _registry_hp(name)
    if reg is None:
        return None, None
    return reg, (_registry_max_hp(name) or reg)


def _apply_combat_condition(cursor, name, condition):
    """Add an SRD condition to a registered combatant (or the player's sheet).

    Respects a registry NPC's `condition_immunities`. Returns `(status, reason)` with
    status one of 'added' / 'present' / 'immune' / 'not_registered'.
    """
    cond = str(condition or "").strip().lower()
    if not cond or not name:
        return "not_registered", None
    entry = _COMBAT_REGISTRY.get(name)
    if entry is None:
        if cursor is not None and _is_player_name(cursor, name):
            current = _player_conditions(cursor)
            if cond in current:
                return "present", None
            if cond in (_player_defenses(cursor).get("condition_immunities") or []):
                return "immune", f"immune to {cond}"
            _db_set(cursor, "conditions", sorted(current | {cond}))
            DB_CONNECTION.commit()
            if cond in _INCAPACITATING_CONDITIONS:
                _end_concentration(cursor, reason=cond)
            return "added", None
        return "not_registered", None
    if entry.get("is_player"):
        if cursor is None:
            return "not_registered", None
        current = _player_conditions(cursor)
        if cond in current:
            return "present", None
        if cond in (_player_defenses(cursor).get("condition_immunities") or []):
            return "immune", f"immune to {cond}"
        current = current | {cond}
        _db_set(cursor, "conditions", sorted(current))
        entry["conditions"] = sorted(current)
        DB_CONNECTION.commit()
        if cond in _INCAPACITATING_CONDITIONS:
            _end_concentration(cursor, reason=cond)
        return "added", None
    immunities = {str(c).strip().lower() for c in (entry.get("condition_immunities") or [])}
    if cond in immunities:
        return "immune", None
    current = {str(c).strip().lower() for c in (entry.get("conditions") or [])}
    if cond in current:
        return "present", None
    entry["conditions"] = sorted(current | {cond})
    return "added", None


def _condition_attack_effects(attacker_conditions, target_conditions, ranged=False):
    """(advantage?, disadvantage?, force_crit?, sources) from conditions on both sides."""
    advantage = disadvantage = force_crit = False
    sources = []
    for cond in sorted(attacker_conditions or []):
        eff = CONDITION_EFFECTS.get(cond, {})
        if eff.get("self_attack_advantage"):
            advantage = True
            sources.append(f"{cond} (attacker)")
        if eff.get("self_attack_disadvantage"):
            disadvantage = True
            sources.append(f"{cond} (attacker)")
    for cond in sorted(target_conditions or []):
        eff = CONDITION_EFFECTS.get(cond, {})
        if eff.get("attackers_advantage"):
            advantage = True
            sources.append(f"{cond} (target)")
        if eff.get("attackers_advantage_melee") and not ranged:
            advantage = True
            sources.append(f"{cond} (target)")
        if eff.get("attackers_disadvantage"):
            disadvantage = True
            sources.append(f"{cond} (target)")
        if eff.get("attackers_disadvantage_ranged") and ranged:
            disadvantage = True
            sources.append(f"{cond} (target, ranged)")
        if eff.get("melee_crit") and not ranged:
            force_crit = True
            sources.append(f"{cond} (crit)")
    return advantage, disadvantage, force_crit, sources


def _condition_save_effects(conditions, ability):
    """(disadvantage?, auto_fail?, sources) for a saving throw."""
    disadvantage = auto_fail = False
    sources = []
    for cond in sorted(conditions or []):
        eff = CONDITION_EFFECTS.get(cond, {})
        key = str(ability or "").strip().lower()[:3]
        if eff.get("auto_fail_str_dex_saves") and key in ("str", "dex"):
            auto_fail = True
            sources.append(f"{cond} (auto-fail)")
        if eff.get("self_dex_save_disadvantage") and key == "dex":
            disadvantage = True
            sources.append(f"{cond}")
    return disadvantage, auto_fail, sources


def _condition_check_disadvantage(conditions, ability=None):
    """(disadvantage?, sources) for an ability check (poisoned/frightened, restrained DEX)."""
    disadvantage = False
    sources = []
    key = str(ability or "").strip().lower()[:3]
    for cond in sorted(conditions or []):
        eff = CONDITION_EFFECTS.get(cond, {})
        if eff.get("self_check_disadvantage"):
            disadvantage = True
            sources.append(cond)
        if eff.get("self_dex_save_disadvantage") and key == "dex":
            disadvantage = True
            sources.append(cond)
    return disadvantage, sources


def _apply_damage_modifiers(entry, damage, damage_type) -> tuple[int, str | None]:
    """SRD 5.1 resistances/immunities/vulnerabilities applied to incoming damage.

    immunity -> 0, resistance -> half (round down), vulnerability -> double; resistance and
    vulnerability together cancel. Petrified resists all damage. Returns (damage, note).
    """
    if entry is None or damage <= 0 or not damage_type:
        return damage, None
    dtype = str(damage_type).strip().lower()
    immunities = [str(x).strip().lower() for x in (entry.get("damage_immunities") or [])]
    resistances = [str(x).strip().lower() for x in (entry.get("damage_resistances") or [])]
    vulnerabilities = [str(x).strip().lower() for x in (entry.get("damage_vulnerabilities") or [])]
    if "all" in immunities or dtype in immunities:
        return 0, f"immune to {dtype}"
    if any(CONDITION_EFFECTS.get(c, {}).get("resist_all") for c in (entry.get("conditions") or [])):
        resistances = [*resistances, dtype]
    factor = 1.0
    if "all" in resistances or dtype in resistances:
        factor *= 0.5
    if "all" in vulnerabilities or dtype in vulnerabilities:
        factor *= 2.0
    if factor == 1.0:
        return damage, None
    return int(damage * factor), f"{damage} -> {int(damage * factor)} ({dtype}: x{factor:g})"


def _player_defenses(cursor) -> dict:
    """The player's damage/condition defenses from equipped items AND active spell effects."""
    if cursor is None:
        return {"damage_resistances": [], "damage_immunities": [], "damage_vulnerabilities": [],
                "condition_immunities": [], "sources": []}
    return equipment.defense_state(_carry_get(cursor))


def _apply_player_damage_modifiers(cursor, damage, damage_type) -> tuple[int, str | None]:
    """SRD 5.1 resistances/immunities/vulnerabilities applied to damage the PLAYER takes.

    Mirrors `_apply_damage_modifiers` but sources the defenses from the player's sheet (items +
    active spells) and respects petrified's resistance to all damage.
    """
    if cursor is None or damage <= 0 or not damage_type:
        return damage, None
    defenses = _player_defenses(cursor)
    dtype = str(damage_type).strip().lower()
    immunities = list(defenses.get("damage_immunities") or [])
    resistances = list(defenses.get("damage_resistances") or [])
    vulnerabilities = list(defenses.get("damage_vulnerabilities") or [])
    if "all" in immunities or dtype in immunities:
        return 0, f"player immune to {dtype} ({defenses.get('sources')})"
    if any(CONDITION_EFFECTS.get(c, {}).get("resist_all") for c in _player_conditions(cursor)):
        resistances.append(dtype)
    factor = 1.0
    if "all" in resistances or dtype in resistances:
        factor *= 0.5
    if "all" in vulnerabilities or dtype in vulnerabilities:
        factor *= 2.0
    if factor == 1.0:
        return damage, None
    return int(damage * factor), f"player {damage} -> {int(damage * factor)} ({dtype}: x{factor:g})"


def _derive_npc_attack(entry, attack_name) -> dict | None:
    """The declared attack entry whose name matches (case-insensitive)."""
    wanted = str(attack_name or "").strip().lower()
    for attack in (entry or {}).get("attacks") or []:
        if isinstance(attack, dict) and str(attack.get("name") or "").strip().lower() == wanted:
            return attack
    return None


def _npc_attack_is_ranged(attack) -> bool:
    if not isinstance(attack, dict):
        return False
    props = [p.lower() for p in _string_list(attack.get("properties"))]
    if any("two-handed" in p for p in props):
        pass  # a two-handed melee weapon is still melee — range/reach decides
    if attack.get("range"):
        return True
    return any(k in " ".join(props) for k in ("ammunition", "ranged"))


def _string_list(value) -> list[str]:
    """Coerce a declaration to a list of strings.

    A model may send a scalar where a list is expected (e.g.
    `traits: "Sneak Attack 2d6."` instead of `["Sneak Attack 2d6."]`).
    Iterating the bare string would split it into characters.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _normalize_attacks(value) -> list[dict]:
    """A declared `attacks` value as a list of dicts, with string-list fields coerced.

    Accepts a single dict as well as a list, and normalizes each attack's
    `properties` so a bare string is never iterated character by character.
    """
    if isinstance(value, dict):
        value = [value]
    elif not isinstance(value, (list, tuple)):
        return []
    attacks = []
    for a in value:
        if not isinstance(a, dict):
            continue
        a = dict(a)
        a["properties"] = _string_list(a.get("properties"))
        attacks.append(a)
    return attacks


def _normalize_combatant(c, add_to_existing=False) -> dict:
    """Normalize a declared stat block into a registry entry."""
    max_hp = int(c.get("max_hp", c["hp"]))
    saves = c.get("saves") if isinstance(c.get("saves"), dict) else {}
    normalized = {
        "current_hp": int(c["hp"]),
        "max_hp": max_hp,
        "ac": int(c["ac"]),
        "saves": {k: int(v) for k, v in saves.items()
                  if k in ABILITY_KEYS and isinstance(v, (int, float))},
        "challenge_rating": c.get("challenge_rating"),
        "initiative_modifier": 0 if add_to_existing else int(c.get("initiative_modifier", 0) or 0),
        "initiative_advantage": bool(c.get("initiative_advantage", False)),
        "role": str(c.get("role") or "hostile").strip().lower(),
        "speed": c.get("speed"),
        "damage_resistances": [s.lower() for s in _string_list(c.get("damage_resistances"))],
        "damage_immunities": [s.lower() for s in _string_list(c.get("damage_immunities"))],
        "damage_vulnerabilities": [s.lower() for s in _string_list(c.get("damage_vulnerabilities"))],
        "condition_immunities": [s.lower() for s in _string_list(c.get("condition_immunities"))],
        "attacks": _normalize_attacks(c.get("attacks")),
        "multiattack": c.get("multiattack", 1),
        "spellcasting": c.get("spellcasting") if isinstance(c.get("spellcasting"), dict) else None,
        "traits": _string_list(c.get("traits")),
        "conditions": [s.lower() for s in _string_list(c.get("conditions"))],
        "exhaustion": max(0, min(EXHAUSTION_MAX, int(c.get("exhaustion") or 0))),
        "initiative_roll": 0,
        "initiative_total": 0,
        "is_player": False,
        "killed": False,
        "status": "active",
    }
    return normalized


def _player_registry_entry(cursor) -> dict:
    """The player's registry entry, with saves/speed/spellcasting derived from their sheet."""
    name = _db_val(cursor, "name", "Player")
    scores = _effective_stats(cursor)
    mods = {k: _ability_mod(scores.get(k, 10)) for k in ABILITY_KEYS}
    proficient = _save_proficiencies(cursor)
    prof_bonus = _effective_prof_bonus(cursor)
    state = _item_effect_state(cursor)
    item_save = int(state.get("save_bonus") or 0)
    saves = {k: mods[k] + (prof_bonus if k in proficient else 0) + item_save
             for k in ABILITY_KEYS}
    defense = _player_defenses(cursor)
    speed_state = equipment.speed_state(_carry_get(cursor))
    exh = _exhaustion_effects(_exhaustion_level(cursor))
    speed_walk = int(speed_state["walk"] * exh["speed_multiplier"])
    speeds = {k: int(v * exh["speed_multiplier"])
              for k, v in (speed_state["modes"] or {}).items()}
    conditions = sorted(_player_conditions(cursor))
    if exh["level"] > 0 and "exhaustion" not in conditions:
        conditions = sorted([*conditions, "exhaustion"])
    spell_raw = _db_val(cursor, "spellcasting", {}) or {}
    spellcasting = None
    if isinstance(spell_raw, dict) and spell_raw.get("dc") is not None:
        spellcasting = {"ability": spell_raw.get("ability"), "save_dc": spell_raw.get("dc"),
                        "attack_modifier": spell_raw.get("attack_modifier")}
    return {
        "name": name,
        "current_hp": int(_db_val(cursor, "current_hit_points", 1)),
        "max_hp": int(_db_val(cursor, "total_hit_points", 1)),
        "ac": int(_db_val(cursor, "armor_class", 10)),
        "saves": saves,
        "challenge_rating": None,
        "initiative_modifier": mods["dex"] + int(state.get("initiative_bonus") or 0),
        "initiative_advantage": bool(state.get("initiative_advantage")),
        "role": "ally",
        "speed": speed_walk,
        "speeds": speeds,
        "damage_resistances": list(defense.get("damage_resistances") or []),
        "damage_immunities": list(defense.get("damage_immunities") or []),
        "damage_vulnerabilities": list(defense.get("damage_vulnerabilities") or []),
        "condition_immunities": list(defense.get("condition_immunities") or []),
        "attacks": [],
        "multiattack": 1,
        "spellcasting": spellcasting,
        "traits": [],
        "conditions": conditions,
        "exhaustion": exh["level"],
        "initiative_roll": 0,
        "initiative_total": 0,
        "is_player": True,
        "killed": False,
        "status": "active",
    }


def _fmt_attack(a) -> str:
    """One attack as a readable line: 'Dagger +4, 1d4+2 piercing (Finesse, reach 5)'."""
    name = str(a.get("name") or "attack")
    bonus = a.get("attack_bonus")
    hit = f"{int(bonus):+d}" if isinstance(bonus, (int, float)) else ""
    dmg = str(a.get("damage_dice") or a.get("damage") or "").strip()
    mod = a.get("damage_modifier")
    if isinstance(mod, (int, float)) and mod:
        dmg += f"{int(mod):+d}"
    dtype = str(a.get("damage_type") or "").strip()
    reach = a.get("reach")
    props = _string_list(a.get("properties"))
    if reach:
        props.append(f"reach {reach}")
    detail = ", ".join(p for p in (f"{dmg} {dtype}".strip(), *props) if p)
    head = " ".join(p for p in (name, hit) if p)
    return f"{head}, {detail}" if detail else head


def _combatant_sheet_lines(name, entry) -> list[str]:
    """The full declared sheet of a registry NPC, for the client's name tooltip (never the player)."""
    lines = [f"{name} — {entry.get('role') or 'hostile'}"]
    vitals = [f"HP {entry['current_hp']}/{entry['max_hp']}", f"AC {entry['ac']}"]
    if entry.get("speed") is not None:
        vitals.append(f"Speed {entry['speed']}")
    if entry.get("challenge_rating") is not None:
        vitals.append(f"CR {entry['challenge_rating']}")
    lines.append("  " + "  ".join(vitals))
    saves = entry.get("saves") or {}
    if saves:
        lines.append("  Saves: " + ", ".join(f"{k.upper()} {int(v):+d}" for k, v in saves.items()))
    attacks = entry.get("attacks") or []
    if attacks:
        lines.append("  Attacks: " + "; ".join(_fmt_attack(a) for a in attacks))
    if int(entry.get("multiattack") or 1) > 1:
        lines.append(f"  Multiattack: {entry['multiattack']}")
    sc = entry.get("spellcasting")
    if isinstance(sc, dict) and sc:
        lines.append("  Spellcasting: " + json.dumps(sc, ensure_ascii=True))
    traits = entry.get("traits") or []
    if traits:
        lines.append("  Traits: " + "; ".join(str(t) for t in traits))
    for label, key in (("Resistances", "damage_resistances"), ("Immunities", "damage_immunities"),
                       ("Vulnerabilities", "damage_vulnerabilities"),
                       ("Condition immunities", "condition_immunities")):
        vals = entry.get(key) or []
        if vals:
            lines.append(f"  {label}: " + ", ".join(str(v) for v in vals))
    if entry.get("conditions"):
        lines.append("  Conditions: " + ", ".join(str(c) for c in entry["conditions"]))
    if int(entry.get("exhaustion") or 0) > 0:
        lines.append(f"  Exhaustion: {int(entry['exhaustion'])}")
    status = str(entry.get("status") or "active").strip().lower()
    if status != "active":
        lines.append(f"  Status: {status}")
    return lines


def _registry_summary_list() -> list[dict]:
    """The compact roster the engine forwards to the client for the combatant tooltips."""
    out: list[dict] = []
    for rname, entry in _COMBAT_REGISTRY.items():
        summary = {
            "name": rname,
            "hp": f"{entry['current_hp']}/{entry['max_hp']}",
            "ac": entry["ac"],
            "initiative": entry["initiative_total"],
            "is_player": entry.get("is_player", False),
            "role": entry.get("role", "hostile"),
            "killed": bool(entry.get("killed", False)),
            "status": str(entry.get("status") or "active"),
        }
        if entry.get("speed") is not None:
            summary["speed"] = entry["speed"]
        if entry.get("challenge_rating") is not None:
            summary["cr"] = entry["challenge_rating"]
        if entry.get("conditions"):
            summary["conditions"] = entry["conditions"]
        out.append(summary)
    return out


@mcp.tool()
def register_combatants(combatants: list[dict], add_to_existing: bool = False) -> dict:
    """Register every combatant for a battle and roll initiative; the player is auto-registered.

    WHEN: a fight starts -- call this FIRST, before ANY resolve_attack/resolve_magic.
    FIELDS (combatants: list of dicts; required name, hp, ac):
    - name; hp; max_hp (defaults to hp); ac; initiative_modifier (DEX mod; required unless add_to_existing); initiative_advantage; challenge_rating (XP); role 'hostile' | 'ally' | 'neutral' (default hostile); speed.
    - saves {str, dex, con, int, wis, cha}; damage_resistances/damage_immunities/damage_vulnerabilities (list; 'all' allowed); condition_immunities.
    - attacks: list of {name, base?, attack_bonus, damage_dice, damage_modifier?, damage_type?, properties?, reach?, range?} -- resolve_attack(actor, attack) uses these.
    - multiattack (int or str; reported, not enforced); spellcasting {ability, save_dc, attack_modifier}; conditions; exhaustion (0-6); traits (a list of strings, e.g. ["Sneak Attack 2d6."]).
    - add_to_existing (default False): add arrivals without wiping the registry or re-rolling initiative.

    RULES:
    - Declare the FULL stat block -- the engine derives attacks/saves/AC/damage from it and you never repeat those values; estimate only when a creature truly has no block.
    - resolve_attack/resolve_magic auto-look up HP/AC/CR/saves, so you need not pass them each call; HP carries forward.
    - Re-calling without add_to_existing overwrites the registry -- that is how a fight ends and a new one begins (there is no end-combat call).
    - Declared conditions drive advantage/disadvantage, auto failures and crits automatically.

    EX:
    register_combatants(combatants=[{"name":"Goblin","hp":7,"ac":15,"initiative_modifier":2,"challenge_rating":0.25,"role":"hostile","attacks":[{"name":"Scimitar","attack_bonus":4,"damage_dice":"1d6","damage_modifier":2,"damage_type":"slashing"}]}])
    """
    global _COMBAT_REGISTRY, DB_CONNECTION

    if not add_to_existing:
        _COMBAT_REGISTRY = {}

    initiative_results = []

    if not add_to_existing and DB_CONNECTION is not None:
        cursor = DB_CONNECTION.cursor()
        player = _player_registry_entry(cursor)
        player_name = player["name"]
        player_init_mod = player["initiative_modifier"]
        _COMBAT_REGISTRY[player_name] = player

        player_d20, player_rolls, _, _ = _roll_d20(advantage=player.get("initiative_advantage", False))
        player_init_total = player_d20 + player_init_mod
        _COMBAT_REGISTRY[player_name]["initiative_roll"] = player_d20
        _COMBAT_REGISTRY[player_name]["initiative_total"] = player_init_total
        initiative_results.append({
            "name": player_name,
            "roll": player_d20,
            "modifier": player_init_mod,
            "total": player_init_total,
            "is_player": True,
        })

    for c in combatants:
        name = c["name"]
        entry = _normalize_combatant(c, add_to_existing=add_to_existing)
        init_mod = entry["initiative_modifier"]
        _COMBAT_REGISTRY[name] = entry

        if not add_to_existing:
            if entry.get("initiative_advantage"):
                d20 = max(random.randint(1, 20), random.randint(1, 20))
            else:
                d20 = random.randint(1, 20)
            init_total = d20 + init_mod
            _COMBAT_REGISTRY[name]["initiative_roll"] = d20
            _COMBAT_REGISTRY[name]["initiative_total"] = init_total
            initiative_results.append({
                "name": name,
                "roll": d20,
                "modifier": init_mod,
                "total": init_total,
                "is_player": False,
            })

    registry_summary = _registry_summary_list()
    total = len(_COMBAT_REGISTRY)

    new_npc_names = [c["name"] for c in combatants
                     if not (_COMBAT_REGISTRY.get(c["name"], {}).get("is_player"))]
    sheets = []
    for nname in new_npc_names:
        entry = _COMBAT_REGISTRY.get(nname)
        if entry is not None:
            sheets.append({"name": nname, "lines": _combatant_sheet_lines(nname, entry)})

    if add_to_existing:
        added_names = [c["name"] for c in combatants]
        return {
            "success": True,
            "registry_summary": registry_summary,
            "sheets": sheets,
            "narrative_format": (f"Combatants registered ({total} total). "
                                 f"Added to existing registry: {', '.join(added_names)}"),
        }

    initiative_results.sort(key=lambda r: (-r["total"], r["name"]))
    order = [r["name"] for r in initiative_results]

    return {
        "success": True,
        "initiative": initiative_results,
        "initiative_order": order,
        "registry_summary": registry_summary,
        "sheets": sheets,
        "narrative_format": f"Combatants registered ({total} total). Initiative Order",
    }


@mcp.tool()
def update_combatant(name: str, conditions_add: list[str] | None = None,
                     conditions_remove: list[str] | None = None,
                     hp_delta: int | None = None, max_hp: int | None = None,
                     ac: int | None = None, exhaustion_delta: int | None = None,
                     status: str | None = None) -> dict:
    """Change a registered combatant mid-fight: conditions, exhaustion, and (NPC) HP/defence.

    WHEN: a condition the fiction causes (a shove -> prone), a condition ends, a correction is needed, or an NPC's HP/AC changes.
    FIELDS:
    - name: the combatant exactly as registered.
    - conditions_add / conditions_remove: adding an immune condition is refused (in 'blocked_conditions'); a condition a TOOL already applied returns 'already_present' and changes nothing.
    - exhaustion_delta: add/remove exhaustion levels 0-6 (player: updates the sheet and re-derives max HP; NPC: updates the registry entry).
    - hp_delta: (NPCs only) signed, clamped to [0, max_hp], sets 'killed' at 0. max_hp / ac: (NPCs only) correct the declared values.
    - status: (NPCs only) 'active' | 'fled' | 'surrendered' -- a hostile that leaves the fight stops counting as in combat; 'active' returns it.

    RULES:
    - Conditions drive the engine automatically (advantage/disadvantage, auto-failed saves, melee crits vs helpless, petrified resists all damage).
    - The player's conditions live on their sheet; use modify_player_numeric for player HP.

    EX:
    update_combatant(name='Goblin', conditions_add=['prone'])
    update_combatant(name='{player_name}', exhaustion_delta=1)
    """
    global DB_CONNECTION
    entry = _COMBAT_REGISTRY.get(name)
    if entry is None:
        return {"success": False, "error": "not_registered", "name": name,
                "reason": f"'{name}' is not in the combat registry.",
                "combatants": list(_COMBAT_REGISTRY)}
    is_player = bool(entry.get("is_player"))
    new_status = None if status is None else str(status).strip().lower()
    if new_status is not None and new_status not in ("active", "fled", "surrendered"):
        return {"success": False, "error": "invalid_status", "name": name,
                "reason": f"status must be 'active', 'fled' or 'surrendered', not '{status}'.",
                "narrative_format": f"{name}: refused — '{status}' is not a combat status."}
    notes = []
    blocked = []
    already_present = []

    if is_player:
        if DB_CONNECTION is None:
            return {"success": False, "error": "Database not initialized."}
        cursor = DB_CONNECTION.cursor()
        current = _player_conditions(cursor)
        immunities = set()
    else:
        current = {str(c).strip().lower() for c in (entry.get("conditions") or [])}
        immunities = {str(c).strip().lower() for c in (entry.get("condition_immunities") or [])}

    # Exhaustion is a level, not a boolean condition.
    cond_add = [str(c).strip().lower() for c in (conditions_add or []) if str(c).strip()]
    cond_remove = [str(c).strip().lower() for c in (conditions_remove or [])]
    add_levels = int(exhaustion_delta or 0)
    if is_player:
        if add_levels:
            _apply_exhaustion_change(cursor, add_levels, reason="update_combatant")
        entry["exhaustion"] = _exhaustion_level(cursor)
        current = _player_conditions(cursor)
    else:
        entry["exhaustion"] = max(0, min(EXHAUSTION_MAX,
            int(entry.get("exhaustion") or 0) + add_levels))
    # `exhaustion` is a level, not a condition: adjust it with exhaustion_delta.
    cond_add = [c for c in cond_add if c != "exhaustion"]
    cond_remove = [c for c in cond_remove if c != "exhaustion"]

    for key in cond_add:
        if key in immunities:
            blocked.append({"condition": key, "reason": "immune"})
            continue
        if key in current:
            already_present.append(key)
        current.add(key)
    for key in cond_remove:
        current.discard(key)
    current = sorted(current)

    if is_player:
        cursor = DB_CONNECTION.cursor()
        _db_set(cursor, "conditions", current)
        entry["conditions"] = current
    else:
        if entry["exhaustion"] > 0:
            current = sorted(set(current) | {"exhaustion"})
        else:
            current = sorted(set(current) - {"exhaustion"})
        entry["conditions"] = current

    if hp_delta is not None or max_hp is not None or ac is not None:
        if is_player:
            notes.append("Player HP/AC are on the sheet — use modify_player_numeric("
                         "key='current_hit_points'|'armor_class', delta=…).")
        else:
            if max_hp is not None:
                entry["max_hp"] = max(1, int(max_hp))
                entry["current_hp"] = min(entry["current_hp"], entry["max_hp"])
            if ac is not None:
                entry["ac"] = int(ac)
            if hp_delta is not None:
                new_hp = max(0, min(entry["max_hp"], entry["current_hp"] + int(hp_delta)))
                entry["current_hp"] = new_hp
                if new_hp == 0:
                    entry["killed"] = True
                elif int(hp_delta) > 0:
                    entry["killed"] = False

    # `status` is the explicit out-of-combat mark for a hostile that fled or surrendered.
    status_note = None
    if new_status is not None:
        if is_player:
            notes.append("Player status is not tracked — conditions and HP live on the sheet.")
        else:
            old_status = str(entry.get("status") or "active").strip().lower()
            entry["status"] = new_status
            if new_status != old_status:
                status_note = {"active": "back in the fight",
                               "fled": "fled the fight",
                               "surrendered": "surrendered"}[new_status]

    # Vitals appear only when this call actually changed them; otherwise the client tooltip
    # carries the live HP/AC. The player's AC is never shown here (ac= is ignored for the player).
    bits = []
    if hp_delta is not None and not is_player:
        bits.append(f"{entry['current_hp']}/{entry['max_hp']} HP")
    if ac is not None and not is_player:
        bits.append(f"AC {entry['ac']}")
    narrative = f"{name}: {', '.join(bits)}" if bits else name
    if status_note:
        narrative += f" — {status_note}"
    if current:
        narrative += f" — {', '.join(current)}"
    if blocked:
        narrative += f" (immune to {', '.join(b['condition'] for b in blocked)})"
    if already_present:
        narrative += f" ({', '.join(already_present)} already present — no change)"
    return {
        "success": True, "name": name, "is_player": is_player,
        "hp": f"{entry['current_hp']}/{entry['max_hp']}", "ac": entry["ac"],
        "conditions": current, "exhaustion": int(entry.get("exhaustion") or 0),
        "status": str(entry.get("status") or "active"),
        "blocked_conditions": blocked,
        "already_present": already_present,
        "note": " ".join(notes) or None,
        "registry_summary": _registry_summary_list(),
        "narrative_format": narrative,
    }


@mcp.tool()
def resolve_attack(
    actor: str,
    attack_modifier: int | None = None,
    target_ac: int | None = None,
    damage_dice: str | None = None,
    damage_modifier: int | None = None,
    target_name: str = "",
    target_current_hp: int | None = None,
    challenge_rating: float | None = None,
    extra_damage_dice: str = "",
    extra_damage_modifier: int = 0,
    is_npc_attack: bool = False,
    is_npc_vs_npc: bool = False,
    advantage: bool = False,
    force_crit: bool = False,
    weapon: str | None = None,
    off_hand: bool = False,
    attack: str | None = None,
    damage_type: str | None = None,
) -> dict:
    """Resolve a weapon/unarmed attack: roll, damage, HP, kills, XP.

    WHEN: any weapon or unarmed attack. Spells -> resolve_magic; checks -> perform_check.
    FIELDS:
    - actor: the attacker's name.
    - weapon: (player) the item's exact inventory name; the engine derives modifier/dice/type from it, its SRD archetype (or `base`) and the character's stats. If it is not in hand the attack is REFUSED (no roll; action spent).
    - attack: (NPC) a declared attack name in its registry entry; the engine derives bonus/dice/type. Refused if the actor is unregistered or the attack undeclared.
    - damage_type: for resistances; auto-filled from weapon/attack when omitted.
    - off_hand: True for the bonus-action attack of two-weapon fighting (both hands must hold a different light melee weapon; no ability modifier unless negative).
    - target_ac: omit for a registry target (uses its AC). target_name/target_current_hp/challenge_rating: registry lookup (HP/CR auto if omitted).
    - attack_modifier/damage_dice/damage_modifier: explicit values (omit when passing weapon; an explicit value wins).
    - extra_damage_dice: bonus dice, NOT doubled on a crit. extra_damage_modifier: flat extra.
    - is_npc_attack: NPC->player, damage auto-applied, no slot. is_npc_vs_npc: NPC->NPC, no player HP, no auto XP.
    - advantage; force_crit.

    RULES:
    - Registry HP updates after each hit, so sequential hits use the reduced HP.
    - XP is auto-awarded on kill (unless is_npc_vs_npc).
    - A refused attack returns success=false with error 'item_not_equipped' | 'item_not_carried' | 'weapon_stats_unknown', turn_lost=true, gm_instruction.
    - A Versatile weapon uses its two-handed die while the other hand is free; a one-handed Ammunition weapon cannot fire while the other hand holds something (error 'cannot_reload').
    - Conditions drive the roll (advantage/disadvantage, melee crits vs helpless); damage to a registered NPC is adjusted by its resistances/immunities/vulnerabilities ('damage_modified').
    - Unproficient armour/shield -> attack at disadvantage ('disadvantage_sources').

    EX:
    resolve_attack(actor='{player_name}', weapon='Longsword', target_ac=13, target_name='Goblin', target_current_hp=12, challenge_rating=0.5)
    resolve_attack(actor='Goblin', attack='Scimitar', target_name='{player_name}', is_npc_attack=True)
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}

    try:
        cursor = DB_CONNECTION.cursor()

        is_player_attacker = (not is_npc_attack and not is_npc_vs_npc) and _is_player_actor(cursor, actor)
        if is_player_attacker and DB_CONNECTION is not None:
            actor = _db_val(cursor, "name", "Player")

        # ── derived equipped weapon (SRD 5.1) ───────────────────────────────
        weapon_info = None
        equipment_warnings = []
        if weapon and is_player_attacker:
            inventory = _db_val(cursor, "inventory", []) or []
            carried = [(e.get("name") if isinstance(e, dict) else str(e)) for e in inventory]
            if weapon not in carried:
                return _blocked_action(
                    cursor, "item_not_carried", f"{weapon} is not in your inventory.", weapon,
                    f"Attack wasted — {weapon} is not carried. The action is spent.")
            state = _equipment_block(cursor)
            in_hands = [h.get("name") for h in state.get("hands", [])]
            if weapon not in in_hands:
                held = ", ".join(h for h in in_hands if h) or "nothing"
                return _blocked_action(
                    cursor, "item_not_equipped",
                    f"{weapon} is not equipped (holding: {held}).", weapon,
                    f"Attack wasted — {weapon} is not equipped (holding: {held}). The action is spent.")
            weapon_info = _derive_weapon_attack(cursor, weapon, inventory,
                                                state.get("hands_free", 1), off_hand)
            equipment_warnings = state.get("warnings", [])
            other_held = next((h.get("name") for h in state.get("hands", [])
                               if h.get("name") and h.get("name") != weapon), None)

            # SRD 5.1 ammunition: a one-handed loading weapon needs a free hand to reload.
            if (any("ammunition" in str(p).lower() for p in weapon_info["properties"])
                    and not weapon_info["two_handed"] and state.get("hands_free", 2) == 0):
                return _blocked_action(
                    cursor, "cannot_reload",
                    f"{weapon} needs a free hand to load, but {other_held} occupies it.", weapon,
                    f"Attack wasted — {weapon} cannot be reloaded while {other_held} is held. "
                    f"The action is spent.")

            # SRD 5.1 two-weapon fighting: a different light melee weapon in the other hand.
            if off_hand:
                if other_held is None:
                    return _blocked_action(
                        cursor, "off_hand_needs_another_weapon",
                        "Two-weapon fighting needs a light melee weapon in the other hand.", weapon,
                        "Off-hand attack wasted — the other hand is empty. The action is spent.")
                if not (_is_light_melee(weapon, inventory)
                        and _is_light_melee(other_held, inventory)):
                    return _blocked_action(
                        cursor, "two_weapon_requires_light_melee",
                        f"Two-weapon fighting needs a light melee weapon in each hand "
                        f"({weapon} / {other_held}).", weapon,
                        f"Off-hand attack wasted — {weapon} and {other_held} are not both light "
                        f"melee weapons. The action is spent.")

        attack_info = None
        multiattack = None
        if attack and not is_player_attacker:
            entry = _COMBAT_REGISTRY.get(actor)
            if entry is None:
                return {"success": False, "error": "actor_not_registered",
                        "reason": f"'{actor}' is not in the combat registry — call "
                                  f"register_combatants first."}
            attack_info = _derive_npc_attack(entry, attack)
            if attack_info is None:
                return {"success": False, "error": "attack_not_declared", "actor": actor,
                        "reason": f"'{actor}' has no declared attack named '{attack}'.",
                        "declared_attacks": [a.get("name") for a in (entry.get("attacks") or [])]}
            if attack_modifier is None:
                attack_modifier = attack_info.get("attack_bonus")
            if damage_dice is None:
                damage_dice = attack_info.get("damage_dice")
            if damage_modifier is None:
                damage_modifier = int(attack_info.get("damage_modifier") or 0)
            if damage_type is None:
                damage_type = attack_info.get("damage_type")
            base = attack_info.get("base")
            if base and (not damage_dice or not damage_type):
                archetype = equipment.weapon_entry(base) or {}
                damage_dice = damage_dice or archetype.get("damage")
                damage_type = damage_type or archetype.get("damage_type")
            multiattack = entry.get("multiattack")

        if attack_modifier is None and weapon_info is not None:
            attack_modifier = weapon_info["attack_modifier"]
        if damage_dice is None and weapon_info is not None:
            damage_dice = weapon_info["damage_dice"]
        if damage_modifier is None:
            damage_modifier = weapon_info["damage_modifier"] if weapon_info else 0
        if damage_type is None and weapon_info is not None:
            damage_type = weapon_info.get("damage_type")
        if attack_modifier is None:
            return {"success": False, "error": "attack_modifier_required",
                    "reason": "Pass attack_modifier, or weapon=/attack= to let the engine derive it."}
        if not damage_dice:
            if weapon and is_player_attacker:
                return _blocked_action(
                    cursor, "weapon_stats_unknown",
                    f"No damage is known for {weapon} — declare damage_dice (or a base archetype) "
                    f"with update_player_list.", weapon,
                    f"Attack wasted — {weapon} has no known damage. The action is spent.")
            return {"success": False, "error": "damage_dice_required",
                    "reason": "Pass damage_dice, or weapon=/attack= for a known weapon."}

        # A registered target's AC comes from its stat block when the caller omits it.
        if target_ac is None:
            target_ac = _registry_ac(target_name) if target_name else None
        if target_ac is None:
            target_ac = 10

        # SRD 5.1 variant: heavy encumbrance (and armour proficiency) gives disadvantage on
        # attack rolls; conditions on either side drive it too.
        ranged = False
        if weapon_info is not None:
            ranged = not weapon_info.get("melee", True)
        elif attack_info is not None:
            ranged = _npc_attack_is_ranged(attack_info)
        target_is_player = bool(is_npc_attack)
        attacker_conditions = _conditions_for(cursor, actor, is_player=is_player_attacker)
        target_conditions = _conditions_for(cursor, target_name, is_player=target_is_player)
        cond_adv, cond_dis, cond_crit, cond_sources = _condition_attack_effects(
            attacker_conditions, target_conditions, ranged)
        if cond_crit:
            force_crit = True
        enc_disadvantage, encumbrance_sources = _roll_disadvantage(cursor, is_player_attacker)
        encumbrance_sources = [*encumbrance_sources, *cond_sources]
        exh = _exhaustion_effects(_exhaustion_level_for(
            cursor, actor, is_player=is_player_attacker))
        if exh["attack_disadvantage"]:
            enc_disadvantage = True
            encumbrance_sources = [*encumbrance_sources, f"exhaustion {exh['level']}"]
        d20, advantage_rolls, roll_mode, advantage_cancelled = _roll_d20(
            advantage=advantage or cond_adv, disadvantage=enc_disadvantage or cond_dis)
        die_label = f"{min(advantage_rolls)} / {max(advantage_rolls)} → " if advantage_rolls else ""

        attack_dice_bonus = 0
        if is_player_attacker and cursor is not None:
            for dice in equipment.active_dice(_carry_get(cursor), "attack_rolls"):
                raw = str(dice.get("value") or "").strip()
                sign = -1 if raw.startswith("-") else 1
                _, _, rolled = _parse_and_roll_dice(raw.lstrip("+-"))
                if rolled:
                    attack_dice_bonus += sign * rolled
        total_attack = d20 + attack_modifier + attack_dice_bonus

        is_natural_1 = (d20 == 1)
        is_natural_20 = (d20 == 20)
        is_forced_crit = False

        if is_natural_1:
            outcome = "Critical Failure"
            is_crit = False
        elif is_natural_20:
            outcome = "Critical Success"
            is_crit = True
        elif total_attack >= target_ac:
            if force_crit:
                outcome = "Critical Success"
                is_crit = True
                is_forced_crit = True
            else:
                outcome = "Success"
                is_crit = False
        else:
            outcome = "Failure"
            is_crit = False

        adv_label = f" ({roll_mode.capitalize()})" if roll_mode else ""

        narrative_parts = []
        attack_label = ""
        if weapon and is_player_attacker:
            attack_label = f" with {weapon}"
        elif attack_info is not None:
            attack_label = f" with {attack}"
        narrative_parts.append(
            f"{actor} Attack{attack_label}{adv_label}{_target_suffix(cursor, target_name, bool(is_npc_attack))}: {die_label}{total_attack} vs AC {target_ac} ({outcome}) ({d20} + {attack_modifier})"
        )

        result = {
            "success": True,
            "actor": actor,
            "target_name": target_name,
            "attack_roll": d20,
            "attack_modifier": attack_modifier,
            "total_attack": total_attack,
            "target_ac": target_ac,
            "outcome": outcome,
            "is_crit": is_crit,
            "is_natural_20": is_natural_20,
            "advantage": advantage,
            "force_crit": force_crit,
        }
        if advantage_rolls:
            result[f"{roll_mode}_rolls"] = advantage_rolls
        result["disadvantage"] = bool(enc_disadvantage)
        if weapon_info is not None:
            result["used_item"] = weapon
            result["attack_ability"] = weapon_info["ability"]
            result["proficient"] = weapon_info["proficient"]
            result["damage_modifier"] = damage_modifier
            result["damage_dice"] = damage_dice
            if off_hand:
                result["two_weapon"] = True
                result["bonus_action_consumed"] = True
                result["off_hand_weapon"] = weapon
            if weapon_info.get("bonus_suppressed"):
                result["magic_bonus_suppressed"] = weapon_info["bonus_suppressed"]
            if weapon_info.get("damage_type"):
                result["damage_type"] = weapon_info["damage_type"]
            if weapon_info.get("item_bonus"):
                result["item_bonus"] = weapon_info["item_bonus"]
            if equipment_warnings:
                result["equipment_warnings"] = equipment_warnings
        _encumbrance_note(result, encumbrance_sources, advantage_cancelled)
        if is_forced_crit:
            result["forced_crit"] = True
        if attack_info is not None:
            result["used_attack"] = attack
            result["damage_dice"] = damage_dice
            result["damage_type"] = damage_type
            result["multiattack"] = multiattack
        if attacker_conditions or target_conditions:
            result["conditions"] = {"attacker": sorted(attacker_conditions),
                                    "target": sorted(target_conditions)}

        if outcome in ("Failure", "Critical Failure"):
            result["damage_total"] = 0
            result["target_killed"] = None
            if is_npc_vs_npc:
                result["npc_vs_npc"] = True
            result["narrative_format"] = "\n".join(narrative_parts)
            result["registry_summary"] = _registry_summary_list()
            return result

        primary_die_size, primary_rolls, primary_sum = _parse_and_roll_dice(damage_dice)
        if primary_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation: '{damage_dice}'. Use format 'XdY' (e.g., '2d6')."}

        if outcome == "Critical Success":
            crit_rolls = [random.randint(1, primary_die_size) for _ in range(len(primary_rolls))]
            primary_damage = sum(primary_rolls) + sum(crit_rolls) + damage_modifier
            crit_rolls_str = " + ".join(str(r) for r in crit_rolls)
            if damage_modifier != 0:
                narrative_parts.append(
                    f"{actor} Damage: {primary_damage} ({' + '.join(str(r) for r in primary_rolls)} + {crit_rolls_str} + {damage_modifier}) [CRIT]"
                )
            else:
                narrative_parts.append(
                    f"{actor} Damage: {primary_damage} ({' + '.join(str(r) for r in primary_rolls)} + {crit_rolls_str}) [CRIT]"
                )
            result["crit_damage_rolls"] = crit_rolls
        else:
            primary_damage = sum(primary_rolls) + damage_modifier
            if damage_modifier != 0:
                narrative_parts.append(
                    f"{actor} Damage: {primary_damage} ({' + '.join(str(r) for r in primary_rolls)} + {damage_modifier})"
                )
            else:
                narrative_parts.append(
                    f"{actor} Damage: {primary_damage} ({' + '.join(str(r) for r in primary_rolls)})"
                )

        extra_damage = 0
        extra_rolls = []
        if extra_damage_dice:
            extra_die_size, extra_base_rolls, extra_base_sum = _parse_and_roll_dice(extra_damage_dice)
            if extra_die_size is None:
                return {"success": False, "error": f"Invalid extra_damage_dice notation: '{extra_damage_dice}'. Use format 'XdY' (e.g., '1d6')."}
            extra_rolls = extra_base_rolls

            # Bug 2 fix: extra dice are NOT doubled on crit
            extra_damage = sum(extra_base_rolls) + extra_damage_modifier
            crit_tag = " [NO CRIT]" if outcome == "Critical Success" else ""
            if extra_damage_modifier != 0:
                narrative_parts.append(
                    f"{actor} Extra Damage: {extra_damage} ({' + '.join(str(r) for r in extra_base_rolls)} + {extra_damage_modifier}){crit_tag}"
                )
            else:
                narrative_parts.append(
                    f"{actor} Extra Damage: {extra_damage} ({' + '.join(str(r) for r in extra_base_rolls)}){crit_tag}"
                )

            result["extra_damage"] = extra_damage
            result["extra_damage_rolls"] = extra_rolls
            result["extra_damage_modifier"] = extra_damage_modifier

        damage_dice_bonus = 0
        if is_player_attacker and cursor is not None:
            for dice in equipment.active_dice(_carry_get(cursor), "damage_rolls"):
                raw = str(dice.get("value") or "").strip()
                sign = -1 if raw.startswith("-") else 1
                _, _, rolled = _parse_and_roll_dice(raw.lstrip("+-"))
                if rolled:
                    damage_dice_bonus += sign * rolled
        total_damage = primary_damage + extra_damage + damage_dice_bonus
        result["damage_dice_bonus"] = damage_dice_bonus

        # SRD 5.1 resistances/immunities/vulnerabilities of a registered NPC target.
        target_entry = _COMBAT_REGISTRY.get(target_name) if target_name else None
        if not is_npc_attack and target_entry is not None:
            total_damage, dmg_note = _apply_damage_modifiers(target_entry, total_damage, damage_type)
            if dmg_note:
                result["damage_modified"] = dmg_note
                narrative_parts.append(f"Damage adjusted: {dmg_note} ({target_name})")

        result["damage_total"] = total_damage
        result["primary_damage"] = primary_damage
        result["primary_damage_rolls"] = primary_rolls
        result["damage_modifier"] = damage_modifier
        result["primary_die_size"] = primary_die_size
        if damage_type:
            result["damage_type"] = damage_type
        if multiattack is not None:
            result["multiattack"] = multiattack
        if attacker_conditions or target_conditions:
            result["conditions"] = {"attacker": sorted(attacker_conditions),
                                    "target": sorted(target_conditions)}

        if is_npc_attack and total_damage > 0:
            total_damage, dmg_note = _apply_player_damage_modifiers(cursor, total_damage, damage_type)
            if dmg_note:
                result["damage_modified"] = dmg_note
                narrative_parts.append(f"Damage adjusted: {dmg_note}")
            result["damage_total"] = total_damage
            hp_result = _apply_hp_change(cursor, -total_damage)
            result["hp_change"] = hp_result
            target_remaining = hp_result["new_value"]
        elif is_npc_vs_npc:
            result["npc_vs_npc"] = True
            from_registry = False
            if target_current_hp is None and target_name:
                target_current_hp = _registry_hp(target_name)
                if target_current_hp is not None:
                    from_registry = True
            if challenge_rating is None and target_name:
                challenge_rating = _registry_cr(target_name)
            if target_current_hp is not None and total_damage > 0:
                target_remaining = target_current_hp - total_damage
                result["target_remaining_hp"] = target_remaining

                if target_name and from_registry:
                    _registry_update_hp(target_name, max(target_remaining, 0))

                if target_remaining <= 0:
                    result["target_killed"] = True
                    if target_name and from_registry:
                        _registry_kill(target_name)
                    max_hp = _registry_max_hp(target_name)
                    display_max = f"/{max_hp}" if max_hp else ""
                    narrative_parts.append(f"{target_name} HP: 0{display_max} (KILLED)")
                else:
                    result["target_killed"] = False
            else:
                result["target_killed"] = None
        elif not is_npc_attack and not is_npc_vs_npc:
            from_registry = False
            if target_current_hp is None and target_name:
                target_current_hp = _registry_hp(target_name)
                if target_current_hp is not None:
                    from_registry = True
            if challenge_rating is None and target_name:
                challenge_rating = _registry_cr(target_name)
            if target_current_hp is not None:
                target_remaining = target_current_hp - total_damage
                result["target_remaining_hp"] = target_remaining

                if target_name and from_registry:
                    _registry_update_hp(target_name, max(target_remaining, 0))

                if target_remaining <= 0:
                    result["target_killed"] = True
                    if target_name and from_registry:
                        _registry_kill(target_name)
                    max_hp = _registry_max_hp(target_name)
                    display_max = f"/{max_hp}" if max_hp else ""
                    narrative_parts.append(f"{target_name} HP: 0{display_max} (KILLED)")
                else:
                    result["target_killed"] = False

                if result.get("target_killed") and challenge_rating is not None:
                    xp_awarded = CR_XP_TABLE.get(challenge_rating, 0)
                    if xp_awarded > 0:
                        xp_result = modify_player_numeric(key="xp", delta=xp_awarded)
                        result["xp_awarded"] = xp_awarded
                        result["challenge_rating"] = challenge_rating
                        result["xp_result"] = xp_result
                        narrative_parts.append(f"XP Awarded: {xp_awarded}")
                        if xp_result.get("level_up"):
                            narrative_parts.append(xp_result["level_up_summary"])
            else:
                result["target_killed"] = None
        else:
            result["target_killed"] = None

        result["narrative_format"] = "\n".join(narrative_parts)
        result["registry_summary"] = _registry_summary_list()

        return result

    except Exception as e:
        return {"success": False, "error": f"Error resolving attack: {str(e)}"}


def _ordinal(n: int) -> str:
    if 11 <= n <= 13:
        return f"{n}th"
    if n % 10 == 1:
        return f"{n}st"
    if n % 10 == 2:
        return f"{n}nd"
    if n % 10 == 3:
        return f"{n}rd"
    return f"{n}th"


def _resolve_hp_temporary(value, cursor):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        dice_match = re.match(r'^(\d+)d(\d+)(?:\+(\d+))?$', value)
        if dice_match:
            num = int(dice_match.group(1))
            size = int(dice_match.group(2))
            flat = int(dice_match.group(3)) if dice_match.group(3) else 0
            rolls = [random.randint(1, size) for _ in range(num)]
            return sum(rolls) + flat
    return None


def _apply_thp(cursor, spell_name, amount):
    old_thp = int(_db_val(cursor, "temporary_hit_points", 0))
    _db_set(cursor, "temporary_hit_points", str(amount))
    DB_CONNECTION.commit()

    buff_data_raw = _db_val(cursor, "_active_buff_data", {})
    if isinstance(buff_data_raw, str):
        buff_data_raw = json.loads(buff_data_raw)
    if spell_name not in buff_data_raw:
        buff_data_raw[spell_name] = []
    buff_data_raw[spell_name].append({"field": "temporary_hit_points", "delta": amount})
    _db_set(cursor, "_active_buff_data", buff_data_raw)

    effects_list = _db_val(cursor, "active_effects", [])
    if isinstance(effects_list, str):
        effects_list = json.loads(effects_list)
    if spell_name not in effects_list:
        effects_list.append(spell_name)
    _db_set(cursor, "active_effects", effects_list)

    DB_CONNECTION.commit()
    return {"field": "temporary_hit_points", "new": amount, "old": old_thp}


def _apply_active_buff(cursor, spell_name, field, delta):
    buff_data_raw = _db_val(cursor, "_active_buff_data", {})
    if isinstance(buff_data_raw, str):
        buff_data_raw = json.loads(buff_data_raw)

    if spell_name in buff_data_raw:
        return {
            "error": "already_active",
            "spell_name": spell_name,
            "hint": (
                f"{spell_name} is already active. Remove it first via "
                f"update_player_list(key='active_effects', item='{spell_name}', action='remove') "
                "before recasting."
            )
        }

    old_val = int(_db_val(cursor, field, 0))
    num_result = modify_player_numeric(key=field, delta=delta)

    if field not in buff_data_raw.get(spell_name, []):
        if spell_name not in buff_data_raw:
            buff_data_raw[spell_name] = []
        buff_data_raw[spell_name].append({"field": field, "delta": delta})

    _db_set(cursor, "_active_buff_data", buff_data_raw)

    effects_list = _db_val(cursor, "active_effects", [])
    if isinstance(effects_list, str):
        effects_list = json.loads(effects_list)
    if spell_name not in effects_list:
        effects_list.append(spell_name)
    _db_set(cursor, "active_effects", effects_list)

    DB_CONNECTION.commit()

    return {
        "field": field,
        "type": "delta",
        "delta": delta,
        "old": old_val,
        "new": num_result.get("new_value", old_val + delta),
    }


def _store_active_effect(cursor, spell_name, kind, field, value):
    """Record a NON-numeric active spell effect (resistance, dice, condition immunity).

    Typed entries live alongside the numeric deltas in `_active_buff_data` and are consumed by
    `equipment.defense_state` and the dice helpers. Duplicate (kind, field, value) entries for the
    same spell are skipped.
    """
    buff_data_raw = _db_val(cursor, "_active_buff_data", {}) or {}
    if isinstance(buff_data_raw, str):
        buff_data_raw = json.loads(buff_data_raw)
    entry = {"kind": kind, "field": field, "value": value}
    entries = buff_data_raw.get(spell_name)
    if not isinstance(entries, list):
        entries = []
    if entry not in entries:
        entries = [e for e in entries if isinstance(e, dict)] + [entry]
    buff_data_raw[spell_name] = entries
    _db_set(cursor, "_active_buff_data", buff_data_raw)
    effects_list = _db_val(cursor, "active_effects", []) or []
    if isinstance(effects_list, str):
        effects_list = json.loads(effects_list)
    if spell_name not in effects_list:
        effects_list.append(spell_name)
    _db_set(cursor, "active_effects", effects_list)
    DB_CONNECTION.commit()
    return {"field": field, "type": kind, "value": value}


def _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc, is_npc_vs_npc=False):
    buffs_applied = {}
    active_names = None

    if sp_buffs and DB_CONNECTION is not None and not is_npc:
        cursor = DB_CONNECTION.cursor()
        stats_dict = _effective_stats(cursor)

        for field, value in sp_buffs.items():
            applied = None

            if isinstance(value, str) and value.startswith("+"):
                try:
                    delta = int(value[1:].strip())
                    if field == "armor_class":
                        applied = _apply_active_buff(cursor, result.get("spell_name", ""), field, delta)
                    elif field == "speed":
                        applied = _apply_active_buff(cursor, result.get("spell_name", ""), field, delta)
                except ValueError:
                    pass

            elif field == "hp_temporary":
                thp_amount = _resolve_hp_temporary(value, cursor)
                if thp_amount is not None:
                    applied = _apply_thp(cursor, result.get("spell_name", ""), thp_amount)
                    narrative_parts.append(f"{result.get('actor', '?')} {result.get('spell_name', '?')} Temporary HP: {thp_amount}")

            elif isinstance(value, str) and "+" in value and any(mod in value.upper() for mod in ["DEX", "STR", "CON", "INT", "WIS", "CHA"]):
                if field == "armor_class":
                    for stat_name, key in [("DEX", "dex"), ("STR", "str"), ("CON", "con"),
                                            ("INT", "int"), ("WIS", "wis"), ("CHA", "cha")]:
                        if stat_name in value.upper():
                            parts = value.upper().split("+")
                            base = int(parts[0].strip())
                            stat_mod = (int(stats_dict.get(key, 10)) - 10) // 2
                            target_ac = base + stat_mod
                            current_ac = int(_db_val(cursor, "armor_class", 10))
                            delta = target_ac - current_ac
                            if delta != 0:
                                applied = _apply_active_buff(
                                    cursor, result.get("spell_name", ""), field, delta)

            if applied is None and field not in ("armor_class", "hp_temporary") \
                    and "error" not in buffs_applied:
                kind = "note"
                if field in ("damage_resistance", "resistance"):
                    kind = "resistance"
                elif field in ("damage_immunity", "immunity"):
                    kind = "immunity"
                elif field in ("damage_vulnerability", "vulnerability"):
                    kind = "vulnerability"
                elif field == "condition_immunity":
                    kind = "condition_immunity"
                elif field in ("saving_throws", "ability_checks", "attack_rolls", "damage_rolls"):
                    kind = "dice"
                applied = _store_active_effect(
                    cursor, result.get("spell_name", ""), kind, field, value)

            if applied:
                if "error" in applied:
                    buffs_applied["error"] = applied
                else:
                    buffs_applied[field] = applied
            elif field not in buffs_applied and "error" not in buffs_applied:
                pass

        buff_data_raw = _db_val(cursor, "_active_buff_data", {})
        if isinstance(buff_data_raw, str):
            buff_data_raw = json.loads(buff_data_raw)
        active_names = list(buff_data_raw.keys())

        if active_names:
            result["active_effects"] = active_names

    if sp_duration and sp_duration != "Instantaneous":
        result["duration"] = sp_duration
        if sp_buffs:
            result["buffs"] = sp_buffs
        # SRD 5.1: track concentration for a player so damage can force a CON save.
        if sp_requires_concentration and not is_npc and DB_CONNECTION is not None:
            conc_spell = result.get("spell_name", "")
            replaced = _set_concentration(DB_CONNECTION.cursor(), conc_spell)
            if replaced:
                result["concentration_replaced"] = replaced
            if conc_spell:
                result["concentration"] = conc_spell
        conc_tag = " (Requires Concentration)" if sp_requires_concentration else ""

        if buffs_applied and "error" not in buffs_applied:
            applied_parts = []
            for field, info in buffs_applied.items():
                if isinstance(info, dict) and "old" in info:
                    applied_parts.append(f"{field} ({info['old']} -> {info['new']})")
            applied_str = ", ".join(applied_parts) if applied_parts else ""
            revert_str = ""
            if active_names:
                revert_str = (
                    "Remove via: update_player_list(key='active_effects', item='"
                    + "', item='".join(active_names)
                    + "', action='remove') when the duration expires. "
                )
            result["duration_reminder"] = (
                f"[GM REMINDER: Duration = {sp_duration}{conc_tag}. "
                f"Applied: {applied_str}. "
                f"{revert_str}"
                f"The GM must inform the player of both the buff application "
                f"and its expiration in narration.]"
            )
        elif buffs_applied and "error" in buffs_applied:
            err = buffs_applied["error"]
            result["duration_reminder"] = (
                f"[GM REMINDER: {err.get('hint', err.get('error', ''))}]"
            )
        else:
            if is_npc and not is_npc_vs_npc:
                result["duration_reminder"] = (
                    f"[GM REMINDER: Duration = {sp_duration}{conc_tag}. "
                    "The GM must manually remove this effect from the player sheet via "
                    "tool calls and inform the player when the duration expires in narration.]"
                )

    if buffs_applied:
        result["buffs_applied"] = buffs_applied

    # A condition a spell applied is written onto the combatant here, so the GM never has to
    # re-declare it with update_combatant (update_combatant stays available to remove it later).
    cond = result.get("condition")
    cond_targets = result.get("condition_targets")
    if cond and isinstance(cond_targets, list) and cond_targets:
        ccur = DB_CONNECTION.cursor() if DB_CONNECTION is not None else None
        applied, refused = [], []
        for tname in cond_targets:
            status, _reason = _apply_combat_condition(ccur, tname, cond)
            if status == "added":
                applied.append(tname)
            elif status in ("immune", "not_registered"):
                refused.append(tname)
        if applied:
            result["conditions_applied"] = applied
            dur = result.get("condition_duration") or "until ended"
            who = ", ".join(applied)
            result["condition_reminder"] = (
                f"[GM: {who} — {cond} ({dur}). Remove with "
                f"update_combatant(name=..., conditions_remove=['{cond}']) when it ends.]")
        if refused:
            result["conditions_refused"] = refused

    result["narrative_format"] = "\n".join(narrative_parts)
    result["registry_summary"] = _registry_summary_list()
    return result


def _resolve_projectile_spell(
    *, actor, spell_name, spell, count, attack_type, damage_dice, damage_modifier,
    damage_type, targets, target_name, target_current_hp, target_ac, challenge_rating,
    is_npc_attack, is_npc_vs_npc, cursor, spell_attack_modifier, advantage, force_crit,
    result, narrative_parts, disadvantage=False,
):
    """Resolve a multi-projectile spell (Magic Missile / Scorching Ray / Eldritch Blast).

    Each projectile is rolled separately. `attack_type == "automatic"` projectiles
    never roll to hit and all strike simultaneously; attack-roll projectiles each
    make their own attack roll. `targets=[{name, darts}]` splits the projectiles
    (darts must total `count`); without `darts` the spell hits a single target.

    Returns the finished result dict, or an error dict (`success: False`).
    """
    # ── 1. Build the (target, darts) distribution ──
    entries = []
    if targets:
        for t in targets:
            name = t.get("name", "Unknown")
            chp = t.get("current_hp")
            if chp is None:
                chp = _registry_hp(name)
            ac = t.get("ac")
            if ac is None:
                ac = _registry_ac(name)
            if ac is None:
                ac = target_ac
            cr = t.get("challenge_rating")
            if cr is None:
                cr = _registry_cr(name)
            entries.append({
                "name": name, "darts": t.get("darts"),
                "current_hp": chp if chp is not None else 0,
                "ac": ac, "cr": cr, "is_player": bool(t.get("is_player", False)),
            })
        if all(e["darts"] is None for e in entries):
            if len(entries) == 1:
                entries[0]["darts"] = count
            else:
                return {"success": False, "spell_name": spell_name,
                        "error": f"{spell_name} has {count} projectiles — pass 'darts' per target to split them.",
                        "expected_projectiles": count}
        elif any(e["darts"] is None for e in entries):
            return {"success": False, "spell_name": spell_name,
                    "error": "'darts' must be provided on every target, or on none.",
                    "expected_projectiles": count}
        else:
            assigned = sum(int(e["darts"]) for e in entries)
            if assigned != count:
                return {"success": False, "spell_name": spell_name,
                        "error": f"Projectiles assigned ({assigned}) must equal the spell's count ({count}).",
                        "expected_projectiles": count}
    else:
        is_player_target = bool(is_npc_attack)
        name = target_name or ("Player" if is_player_target else "")
        if is_player_target:
            chp = target_current_hp
            if chp is None and cursor:
                chp = int(_db_val(cursor, "current_hit_points", 0))
        else:
            chp = target_current_hp if target_current_hp is not None else _registry_hp(name)
        cr = challenge_rating if challenge_rating is not None else _registry_cr(name)
        entries = [{
            "name": name, "darts": count,
            "current_hp": chp if chp is not None else 0,
            "ac": target_ac if target_ac is not None else _registry_ac(name),
            "cr": cr, "is_player": is_player_target,
        }]

    # ── 2. Roll every projectile before applying anything (simultaneous) ──
    total_damage = 0
    per_dart = []
    per_target = {}
    for e in entries:
        bucket = per_target.setdefault(e["name"], {"damage": 0, "darts": []})
        for _ in range(int(e["darts"])):
            detail = {"target": e["name"], "hit": True, "crit": False}
            if attack_type == "attack_roll":
                d20, dart_rolls, dart_mode, _dart_cancelled = _roll_d20(
                    advantage=advantage, disadvantage=disadvantage)
                if dart_rolls:
                    detail[f"{dart_mode}_rolls"] = dart_rolls
                total_attack = d20 + spell_attack_modifier
                ac = e["ac"] if e["ac"] is not None else 0
                nat20 = d20 == 20
                nat1 = d20 == 1
                detail.update({"attack_roll": d20, "total_attack": total_attack, "ac": ac})
                detail["crit"] = nat20 or (force_crit and not nat1)
                detail["hit"] = nat20 or (not nat1 and total_attack >= ac)
                if not detail["hit"]:
                    per_dart.append(detail)
                    continue
            die, rolls, raw = _parse_and_roll_dice(damage_dice)
            if die is None:
                return {"success": False, "spell_name": spell_name,
                        "error": f"Invalid damage_dice notation: '{damage_dice}'."}
            dmg = raw + damage_modifier
            detail["rolls"] = rolls
            if detail.get("crit"):
                crit_rolls = [random.randint(1, die) for _ in range(len(rolls))]
                dmg += sum(crit_rolls)
                detail["crit_rolls"] = crit_rolls
            detail["damage"] = dmg
            total_damage += dmg
            bucket["damage"] += dmg
            bucket["darts"].append(dmg)
            per_dart.append(detail)

    # ── 3. Apply damage simultaneously, then resolve kills/XP ──
    total_xp = 0
    killed_count = 0
    target_results = []
    hp_lines = []
    for e in entries:
        name = e["name"]
        dealt = per_target[name]["damage"]
        if e["is_player"]:
            tr = {"name": name, "damage": dealt, "darts": per_target[name]["darts"]}
            if dealt > 0 and cursor:
                dealt, dmg_note = _apply_player_damage_modifiers(cursor, dealt, damage_type)
                if dmg_note:
                    tr["damage_modified"] = dmg_note
                    tr["damage"] = dealt
                hp_result = _apply_hp_change(cursor, -dealt)
                tr["hp_change"] = hp_result
            target_results.append(tr)
            continue
        chp = e["current_hp"]
        remaining = chp - dealt
        killed = chp > 0 and remaining <= 0
        if name:
            _registry_update_hp(name, max(remaining, 0))
        tr = {"name": name, "damage": dealt, "darts": per_target[name]["darts"],
              "remaining_hp": max(remaining, 0), "killed": killed}
        max_hp = _registry_max_hp(name) if name else 0
        display_max = f"/{max_hp}" if max_hp else ""
        if killed:
            killed_count += 1
            if name:
                _registry_kill(name)
            hp_lines.append(f"{name} HP: 0{display_max} (KILLED)")
            if e["cr"] is not None:
                xp = CR_XP_TABLE.get(e["cr"], 0)
                if xp > 0:
                    total_xp += xp
                    tr["xp_awarded"] = xp
        target_results.append(tr)

    # ── 4. Breakdown narrative ──
    label = "rays" if attack_type == "attack_roll" else "darts"
    if attack_type == "attack_roll":
        bits = []
        for i, d in enumerate(per_dart, start=1):
            if d["hit"]:
                tag = " [CRIT]" if d.get("crit") else ""
                bits.append(f"ray {i}: {d['total_attack']} vs AC {d['ac']} (hit){tag} {d['damage']} {damage_type}".strip())
            else:
                bits.append(f"ray {i}: {d['total_attack']} vs AC {d['ac']} (miss)")
        narrative_parts.append(f"{actor} {spell_name}{_target_suffix(cursor, target_name, bool(is_npc_attack))}: {total_damage} — {count} {label} — " + " | ".join(bits))
    elif len(entries) == 1:
        vals = per_target[entries[0]["name"]]["darts"]
        narrative_parts.append(f"{actor} {spell_name}{_target_suffix(cursor, target_name, bool(is_npc_attack))}: {total_damage} — {count} darts: " + ", ".join(str(v) for v in vals))
    else:
        parts = []
        for e in entries:
            vals = per_target[e["name"]]["darts"]
            plural = "dart" if len(vals) == 1 else "darts"
            parts.append(f"{e['name']}: {sum(vals)} ({len(vals)} {plural}: " + ", ".join(str(v) for v in vals) + ")")
        narrative_parts.append(f"{actor} {spell_name}: {total_damage} — " + ", ".join(parts))

    narrative_parts.extend(hp_lines)

    if total_xp > 0 and not is_npc_attack and not is_npc_vs_npc and cursor:
        xp_result = modify_player_numeric(key="xp", delta=total_xp)
        result["xp_awarded"] = total_xp
        result["xp_result"] = xp_result
        narrative_parts.append(f"Total XP Awarded: {total_xp}")
        if xp_result.get("level_up"):
            narrative_parts.append(xp_result["level_up_summary"])

    result.update({
        "projectiles": count,
        "per_projectile": per_dart,
        "damage_total": total_damage,
        "damage_type": damage_type,
        "targets": target_results,
        "killed_count": killed_count,
    })
    return result


@mcp.tool()
def resolve_magic(
    spell_name: str,
    actor: str = "{player_name}",
    spell_attack_modifier: int | None = None,
    spell_save_dc: int | None = None,
    target_ac: int | None = None,
    target_name: str = "",
    target_current_hp: int | None = None,
    target_max_hp: int | None = None,
    challenge_rating: float | None = None,
    target_save_modifier: int | None = None,
    player_situational_modifier: int | None = None,
    slot_level: int | None = None,
    is_npc_attack: bool = False,
    is_scroll: bool = False,
    attack_type: str | None = None,
    save_type: str | None = None,
    save_half: bool = True,
    damage_dice: str | None = None,
    damage_modifier: int = 0,
    damage_type: str | None = None,
    cantrip_scaling: bool = False,
    higher_levels: str | None = None,
    healing: bool = False,
    aoe: bool = False,
    ritual: bool = False,
    is_npc_vs_npc: bool = False,
    caster_level: int | None = None,
    advantage: bool = False,
    force_crit: bool = False,
    targets: list[dict] | None = None,
    components: str | None = None,
) -> dict:
    """Resolve a full spell: slot, attack/save, damage/heal, HP, kills, XP.

    WHEN: any spell is cast. Weapon attacks -> resolve_attack; checks -> perform_check.
    FIELDS:
    - spell_name: from config/spells.yml; a custom spell also needs attack_type + damage_dice.
    - actor: the caster's name (player name, or an NPC name).
    - attack_type: 'attack_roll' | 'saving_throw' | 'automatic'. save_type: the save ability. save_half: half damage on a successful save (default True).
    - spell_attack_modifier / spell_save_dc: omit for a registered NPC caster -- the engine uses its declared `spellcasting` block.
    - target_ac: required for attack_roll. target_name/target_current_hp/target_max_hp: registry lookup; pass HP for a wounded target (a healing ceiling comes from the registry or the sheet).
    - challenge_rating: CR for XP (auto-looked up if omitted). target_save_modifier: save bonus for a single-target save (omit for a registry target). player_situational_modifier: situational add-on to the player's save when is_npc_attack=True (never pass the base modifier).
    - slot_level: upcast level (defaults to the spell's level). is_scroll: no slot consumed, above-level scrolls need a check. is_npc_attack: NPC->player, damage auto-applied, no slot. is_npc_vs_npc: NPC->NPC, no player HP, no auto XP. caster_level: for NPC-vs-NPC cantrip scaling.
    - damage_dice/damage_modifier/damage_type: custom damage (needed for unknown spells). cantrip_scaling: auto-scale at 5/11/17. higher_levels: upcast string (e.g. '+1d6'). healing: heals instead. aoe: area. ritual: no slot. advantage/force_crit: attack_roll only. components: override (else config/components.yml; unknown = V,S,M).
    - targets: multi-target list. Shapes by type -- HP pool {name, current_hp}; save {name, current_hp, save_modifier, challenge_rating} (add {is_player: True} to apply to the player); projectiles {name, darts} (darts must total the count; omit to send them all at one target).

    RULES:
    1. The slot is validated BEFORE dice; empty slots are returned. Cantrips, rituals, scrolls and NPC attacks consume none.
    2. Duplicate active buffs are rejected before slot use.
    3. Multi-target AoE: damage rolled ONCE, an individual save each, one slot; HP via the registry; XP summed from all kills.
    4. HP-pool spells: targets ascending by HP, pool drained in order.
    5. Extra damage dice are NOT doubled on a crit.
    6. Player temp HP drains before real HP (is_npc_attack). Player resistance/immunity/vulnerability from worn items AND active spells is applied automatically -- pass the RESOLVED damage type.
    7. Multi-projectile: each resolves separately; automatic spells strike simultaneously, attack-roll spells roll per projectile.
    8. A player spell with somatic/material components is REFUSED with both hands full (success=false, error='both_hands_occupied', turn_lost=true; no slot). Components come from config/components.yml else components=; unknown = V,S,M.
    9. A player in armour they are not proficient with cannot cast ANY spell (refused, error='armor_not_proficient').
    10. Healing is capped at the target's max; every healing result declares max_hp/remaining_hp (healing_total = rolled, healing_applied = landed). Temp HP is a separate pool that may exceed the max.

    EX:
    resolve_magic(spell_name='Fireball', actor='{player_name}', spell_save_dc=15, targets=[{"name":"Goblin","current_hp":7,"save_modifier":2,"challenge_rating":0.25}])
    resolve_magic(spell_name='Fire Bolt', actor='{player_name}', spell_attack_modifier=6, target_ac=14, target_name='Orc', target_current_hp=18, challenge_rating=0.5)
    """
    global DB_CONNECTION
    spells_db = _load_spells()

    if actor == "{player_name}" and DB_CONNECTION is not None:
        cursor = DB_CONNECTION.cursor()
        actor = _db_val(cursor, "name", "Player")
    else:
        cursor = DB_CONNECTION.cursor() if DB_CONNECTION is not None else None

    # SRD 5.1 variant: a heavily encumbered player rolls spell attacks with
    # disadvantage (cancelling a GM-granted advantage).
    is_player_caster = not is_npc_attack and not is_npc_vs_npc
    # DC / attack bonus: a registered NPC caster supplies its own; the player's comes from the sheet.
    if is_player_caster and cursor is not None:
        spell_raw = _db_val(cursor, "spellcasting", {}) or {}
        if isinstance(spell_raw, dict):
            if spell_save_dc is None and spell_raw.get("dc") is not None:
                spell_save_dc = int(spell_raw["dc"])
            if spell_attack_modifier is None and spell_raw.get("attack_modifier") is not None:
                spell_attack_modifier = int(spell_raw["attack_modifier"])
    else:
        npc_spell = (_COMBAT_REGISTRY.get(actor) or {}).get("spellcasting") or {}
        if spell_save_dc is None and npc_spell.get("save_dc") is not None:
            spell_save_dc = int(npc_spell["save_dc"])
        if spell_attack_modifier is None and npc_spell.get("attack_modifier") is not None:
            spell_attack_modifier = int(npc_spell["attack_modifier"])
    spell_save_dc = int(spell_save_dc or 0)
    spell_attack_modifier = int(spell_attack_modifier or 0)
    enc_disadvantage, encumbrance_sources = _roll_disadvantage(cursor, is_player_caster)
    if is_player_caster:
        exh_att = _exhaustion_effects(_exhaustion_level(cursor))
        if exh_att["attack_disadvantage"]:
            enc_disadvantage = True
            encumbrance_sources = [*encumbrance_sources, f"exhaustion {exh_att['level']}"]

    # ── armour proficiency, then free-hand check (SRD 5.1) ───────────────────
    if (cursor is not None and is_player_caster and not is_scroll
            and _is_player_actor(cursor, actor)):
        comps = (components or _spell_components().get(carrying.normalize(spell_name)) or "VSM").upper()
        state = _equipment_block(cursor)
        # No spellcasting at all while wearing armour you are not proficient with.
        if not state.get("armor_proficient", True):
            blocked = _blocked_action(
                cursor, "armor_not_proficient",
                "You can't cast spells while wearing armour you lack proficiency with "
                f"({', '.join(state.get('proficiency_sources') or [])}).", spell_name,
                f"{spell_name} failed — {actor} wears armour they are not proficient with, so no "
                f"spell can be cast. The action is spent.")
            blocked["spell"] = spell_name
            blocked["note"] = "No spell slot is consumed."
            return blocked
        if (("S" in comps or "M" in comps) and state.get("derived_from_equipped")
                and state.get("hands_free_casting", 2) == 0):
            # A held focus or component pouch covers M but not S (a hand is still needed).
            held = [h.get("name") for h in state.get("hands", []) if h.get("name")]
            focus_ok = "M" in comps and "S" not in comps and _covers_material(cursor, held)
            if not focus_ok:
                held_label = ", ".join(held) or "both hands"
                blocked = _blocked_action(
                    cursor, "both_hands_occupied",
                    f"Both hands are occupied ({held_label}) — {spell_name} needs a free hand for its "
                    f"{'somatic' if 'S' in comps else 'material'} components.",
                    spell_name,
                    f"{spell_name} failed — both hands are busy ({held_label}), so the spell cannot "
                    f"be cast. The action is spent.")
                blocked["spell"] = spell_name
                blocked["components"] = comps
                blocked["note"] = "No spell slot is consumed."
                return blocked

    spell_key = spell_name.lower().strip()
    spell = spells_db.get(spell_key)

    if spell:
        sp_attack_type = spell.get("attack_type", "attack_roll")
        sp_damage_dice = spell.get("damage_dice", "0d0")
        sp_damage_modifier = spell.get("damage_modifier", 0)
        sp_damage_type = spell.get("damage_type", "")
        sp_save_type = spell.get("save_type", None)
        sp_save_half = spell.get("save_half", True)
        sp_healing = spell.get("healing", False)
        sp_flat_healing = spell.get("flat_healing", False)
        sp_aoe = spell.get("aoe", False)
        sp_cantrip_scaling = spell.get("cantrip_scaling", False)
        sp_higher_levels = spell.get("higher_levels", None)
        sp_level = spell.get("level", 0)
        sp_no_damage = spell.get("no_damage", False)
        sp_instant_kill = spell.get("instant_kill", False)
        sp_instant_kill_threshold = spell.get("instant_kill_hp_threshold", 0)
        sp_damage_on_miss = spell.get("damage_on_miss", None)
        sp_extra_damage_dice = spell.get("extra_damage_dice", None)
        sp_extra_damage_type = spell.get("extra_damage_type", None)
        sp_hp_pool = spell.get("hp_pool", False)
        sp_condition = spell.get("condition", None)
        sp_condition_duration = spell.get("condition_duration", None)
        sp_requires_concentration = spell.get("requires_concentration", False)
        sp_duration = spell.get("duration", "Instantaneous")
        sp_buffs = spell.get("buffs", None)
        sp_temp_hp = spell.get("temporary_hp", False)
    else:
        if attack_type is None or damage_dice is None:
            return {
                "success": False,
                "error": f"Spell '{spell_name}' not found in spells database.",
                "hint": "Provide attack_type and damage_dice to cast a custom spell, or use one of the known spells.",
                "custom_params_required": ["attack_type", "damage_dice"],
                "custom_params_optional": [
                    "save_type", "save_half", "damage_modifier", "damage_type",
                    "cantrip_scaling", "higher_levels", "healing", "aoe",
                ],
            }
        sp_attack_type = attack_type.lower().strip()
        sp_damage_dice = damage_dice
        sp_damage_modifier = damage_modifier
        sp_damage_type = damage_type or ""
        sp_save_type = save_type
        sp_save_half = save_half
        sp_healing = healing
        sp_aoe = aoe
        sp_cantrip_scaling = cantrip_scaling
        sp_higher_levels = higher_levels
        sp_level = slot_level if slot_level is not None else 0
        sp_no_damage = False
        sp_instant_kill = False
        sp_instant_kill_threshold = 0
        sp_damage_on_miss = None
        sp_extra_damage_dice = None
        sp_extra_damage_type = None
        sp_hp_pool = False
        sp_condition = None
        sp_condition_duration = None
        sp_requires_concentration = False
        sp_duration = "Instantaneous"
        sp_buffs = None
        sp_temp_hp = False

    # ── DUPLICATE ACTIVE EFFECT CHECK ──
    if sp_buffs and DB_CONNECTION is not None:
        buff_data_raw = _db_val(cursor, "_active_buff_data", {})
        if isinstance(buff_data_raw, str):
            buff_data_raw = json.loads(buff_data_raw)
        lookup_entries = {k.lower(): k for k in buff_data_raw}
        if spell_key in lookup_entries:
            return _action_failure(
                f"{spell_name} is already active.",
                f"{actor} {spell_name} — already active, not recast",
                spell_name=spell_name,
                hint=(
                    f"Remove it first via update_player_list(key='active_effects', "
                    f"item='{lookup_entries[spell_key]}', action='remove') before recasting."
                ),
            )

    # ── SPELLBOOK CHECK ──
    # A caster whose spell list lives in a spellbook (wizard-style) cannot cast
    # leveled spells while the book is missing from the inventory. Cantrips are
    # memorised and keep working; scrolls carry their own magic.
    if (DB_CONNECTION is not None and cursor is not None
            and not is_npc_attack and not is_npc_vs_npc and not is_scroll
            and sp_level > 0 and _needs_spellbook(cursor) and not _has_spellbook_item(cursor)):
        return _action_failure(
            f"Cannot cast {spell_name}: the spellbook is missing.",
            f"{actor} {spell_name} — not cast: the spellbook is missing",
            spell_name=spell_name,
            hint="The character's spellbook is not in their inventory. "
                 "Recover it before casting leveled spells.",
        )

    # ── SPELL SLOT MANAGEMENT ──
    is_cantrip = (sp_level == 0)
    slot_consumed = None
    slot_level_used = None
    slot_narrative = None
    scroll_check_info = None

    if not is_npc_attack and not is_npc_vs_npc and not is_cantrip and not ritual and not is_scroll and DB_CONNECTION is not None:
        if slot_level is not None:
            effective_slot = slot_level
        elif spell:
            effective_slot = sp_level
        else:
            return {
                "success": False,
                "error": f"Cannot determine spell slot level for custom spell '{spell_name}'. Provide slot_level parameter.",
                "spell_name": spell_name,
                "hint": f"For custom spells, specify which slot level to use (e.g. slot_level=3 for a 3rd-level slot).",
            }

        if slot_level is not None and slot_level < sp_level:
            return {
                "success": False,
                "error": f"Cannot cast {spell_name} (level {sp_level}) in a {slot_level}{_ordinal(slot_level).replace(str(slot_level), '')}-level slot. Slot level must be >= spell level ({sp_level}).",
                "spell_name": spell_name,
                "spell_level": sp_level,
                "slot_level_provided": slot_level,
                "hint": f"Upcasting requires a slot of level {sp_level} or higher. Use slot_level={sp_level} or above.",
            }

        slot_key = f"spellcasting.slots.{effective_slot}"
        validation = _validate_spell_slot(cursor, slot_key, -1)

        if validation:
            cursor.execute("SELECT value FROM player WHERE key = ?", ("spellcasting",))
            sc_row = cursor.fetchone()
            available_slots = {}
            if sc_row:
                sc_data = json.loads(sc_row[0])
                for k, v in sc_data.get("slots", {}).items():
                    if int(v) > 0:
                        available_slots[f"lv{k}"] = v
            return _action_failure(
                f"No level {effective_slot} spell slots remaining to cast {spell_name}.",
                f"{actor} {spell_name} — not cast: no {_ordinal(effective_slot)}-level slot remaining",
                spell_name=spell_name,
                slot_level_needed=effective_slot,
                available_slots=available_slots if available_slots else "No spell slots available.",
                hint="Take a long rest to recover spell slots, or cast using a higher-level slot by providing slot_level.",
            )

        cursor.execute("SELECT value FROM player WHERE key = ?", ("spellcasting",))
        sc_check = cursor.fetchone()
        if sc_check:
            sc_data_check = json.loads(sc_check[0])
            str_effective_slot = str(effective_slot)
            if str_effective_slot not in sc_data_check.get("slots", {}):
                cursor.execute("SELECT key FROM player")
                return _action_failure(
                    f"Player has no level {effective_slot} spell slots. Maximum available slot level may be insufficient for this spell.",
                    f"{actor} {spell_name} — not cast: no {_ordinal(effective_slot)}-level slot available",
                    spell_name=spell_name,
                    slot_level_needed=effective_slot,
                    available_slots={f"lv{k}": v for k, v in sc_data_check.get("slots", {}).items() if int(v) > 0} if sc_data_check.get("slots") else "No spell slots.",
                    hint=f"This character does not have level {effective_slot} spell slots available.",
                )

    # ── PARAMETER VALIDATION (before consuming the slot) ──
    if sp_attack_type == "attack_roll" and target_ac is None:
        return {"success": False, "error": f"Spell '{spell_name}' requires an attack roll. You must provide target_ac."}
    if sp_attack_type == "saving_throw" and spell_save_dc <= 0:
        return {"success": False, "error": f"Spell '{spell_name}' requires a saving throw. You must provide spell_save_dc."}

    # ── CONSUME SPELL SLOT ──
    slot_result_data = None
    if not is_npc_attack and not is_npc_vs_npc and not is_cantrip and not ritual and not is_scroll and DB_CONNECTION is not None:
        slot_result_data = modify_player_numeric(key=slot_key, delta=-1)
        slot_level_used = effective_slot
        slot_consumed = True
        if slot_result_data.get("success"):
            new_remaining = slot_result_data.get("new_value", "?")
            slot_narrative = f"Slot Used: {_ordinal(effective_slot)}-level ({new_remaining} remaining)"
        else:
            slot_narrative = f"Slot Used: {_ordinal(effective_slot)}-level"

    elif not is_npc_attack and not is_npc_vs_npc and is_cantrip and not is_scroll:
        slot_consumed = "cantrip"
        slot_narrative = "Cantrip — no slot used"
    elif not is_npc_attack and not is_npc_vs_npc and ritual:
        slot_consumed = "ritual"
        slot_narrative = "Ritual Cast — no slot consumed"
    elif not is_npc_attack and not is_npc_vs_npc and is_scroll:
        slot_consumed = "scroll"
        slot_narrative = "Scroll Cast — no slot consumed"
        effective_slot = slot_level if slot_level is not None else sp_level
        if not is_cantrip and effective_slot > 0 and cursor:
            sc_row = cursor.execute(
                "SELECT value FROM player WHERE key = ?", ("spellcasting",)
            ).fetchone()
            if sc_row:
                sc_data = json.loads(sc_row[0])
                if str(effective_slot) not in sc_data.get("slots", {}):
                    ability_name = sc_data.get("ability", "intelligence").lower()
                    stat_key = {"intelligence": "int", "wisdom": "wis", "charisma": "cha"}.get(ability_name, "int")
                    ability_mod = _ability_mod(_effective_stats(cursor).get(stat_key, 10))
                    dc = 10 + effective_slot
                    scroll_d20 = random.randint(1, 20)
                    scroll_total = scroll_d20 + ability_mod
                    if scroll_total < dc:
                        return _action_failure(
                            f"Scroll ability check failed for {spell_name}. "
                            f"Check: {scroll_total} vs DC {dc} ({scroll_d20} + {ability_mod}). "
                            "The scroll's magic fizzles and is wasted.",
                            f"{actor} {spell_name} (scroll) — check {scroll_total} vs DC {dc}: the scroll fizzles",
                            spell_name=spell_name,
                            scroll_level=effective_slot,
                            scroll_check={"d20": scroll_d20, "ability_modifier": ability_mod,
                                          "total": scroll_total, "dc": dc, "passed": False},
                            hint="The scroll is consumed but the spell does not take effect. Remove it from inventory."
                        )
                    else:
                        slot_narrative += f" (Ability Check: {scroll_total} vs DC {dc} — passed)"
                        scroll_check_info = {"d20": scroll_d20, "ability_modifier": ability_mod,
                                             "total": scroll_total, "dc": dc, "passed": True}
    elif is_npc_attack or is_npc_vs_npc:
        slot_consumed = "npc"

    if is_npc_attack:
        character_level = 1
    elif is_npc_vs_npc:
        character_level = caster_level if caster_level is not None else 1
    elif cursor:
        character_level = int(_db_val(cursor, "level", 1))
    else:
        character_level = 1

    computed_slot = slot_level if slot_level is not None else (sp_level if not sp_cantrip_scaling else 0)
    final_dice, final_mod, final_extra_dice = _compute_spell_damage(
        spell if spell else {
            "damage_dice": sp_damage_dice,
            "damage_modifier": sp_damage_modifier,
            "level": sp_level,
            "cantrip_scaling": sp_cantrip_scaling,
            "cantrip_scale_dice": None,
            "higher_levels": sp_higher_levels,
            "extra_damage_dice": sp_extra_damage_dice,
            "extra_damage_type": sp_extra_damage_type,
            "extra_higher_levels": None,
        },
        character_level,
        computed_slot if not sp_cantrip_scaling else None,
    )



    narrative_parts = []
    if slot_narrative:
        narrative_parts.append(slot_narrative)
    result = {
        "success": True,
        "actor": actor,
        "spell_name": spell_name,
        "target_name": target_name,
        "attack_type": sp_attack_type,
        "slot_consumed": slot_consumed,
    }
    if slot_level_used is not None:
        result["slot_level_used"] = slot_level_used
    if scroll_check_info:
        result["scroll_check"] = scroll_check_info
    if slot_result_data is not None:
        result["slot_result"] = slot_result_data

    is_crit = False

    # ── PROJECTILE SPELLS (Magic Missile / Scorching Ray / Eldritch Blast) ──
    if spell and (spell.get("projectiles") or spell.get("cantrip_projectile_scaling")) and not sp_healing:
        if spell.get("cantrip_projectile_scaling"):
            projectile_count = 1 + sum(1 for lvl in (5, 11, 17) if character_level >= lvl)
        else:
            projectile_count = (int(spell.get("projectiles", 0))
                                + int(spell.get("projectiles_per_level", 0)) * max(0, (computed_slot or 0) - sp_level))
        proj = _resolve_projectile_spell(
            actor=actor, spell_name=spell_name, spell=spell, count=projectile_count,
            attack_type=sp_attack_type, damage_dice=sp_damage_dice, damage_modifier=sp_damage_modifier,
            damage_type=sp_damage_type, targets=targets, target_name=target_name,
            target_current_hp=target_current_hp, target_ac=target_ac, challenge_rating=challenge_rating,
            is_npc_attack=is_npc_attack, is_npc_vs_npc=is_npc_vs_npc, cursor=cursor,
            spell_attack_modifier=spell_attack_modifier, advantage=advantage, force_crit=force_crit,
            result=result, narrative_parts=narrative_parts, disadvantage=enc_disadvantage,
        )
        if proj.get("success") is False:
            return proj
        if enc_disadvantage:
            result["disadvantage_sources"] = list(encumbrance_sources)
            if advantage:
                result["advantage_cancelled"] = True
        return _finalize_spell_result(proj, narrative_parts, sp_duration, sp_buffs,
                                      sp_requires_concentration,
                                      is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    # ── ATTACK ROLL ──
    if sp_attack_type == "attack_roll":
        d20, advantage_rolls, roll_mode, advantage_cancelled = _roll_d20(
            advantage=advantage, disadvantage=enc_disadvantage)
        die_label = f"{min(advantage_rolls)} / {max(advantage_rolls)} → " if advantage_rolls else ""
        total_attack = d20 + spell_attack_modifier
        is_natural_1 = d20 == 1
        is_natural_20 = d20 == 20
        is_forced_crit = False

        if is_natural_1:
            outcome = "Critical Failure"
            is_crit = False
        elif is_natural_20:
            outcome = "Critical Success"
            is_crit = True
        elif total_attack >= target_ac:
            if force_crit:
                outcome = "Critical Success"
                is_crit = True
                is_forced_crit = True
            else:
                outcome = "Success"
                is_crit = False
        else:
            outcome = "Failure"
            is_crit = False

        adv_label = f" ({roll_mode.capitalize()})" if roll_mode else ""
        narrative_parts.append(
            f"{actor} {spell_name} Attack{adv_label}{_target_suffix(cursor, target_name, bool(is_npc_attack))}: {die_label}{total_attack} vs AC {target_ac} ({outcome}) ({d20} + {spell_attack_modifier})"
        )

        result["attack_roll"] = d20
        result["attack_modifier"] = spell_attack_modifier
        result["total_attack"] = total_attack
        result["target_ac"] = target_ac
        result["outcome"] = outcome
        result["is_crit"] = is_crit
        result["is_natural_20"] = is_natural_20
        if advantage_rolls:
            result[f"{roll_mode}_rolls"] = advantage_rolls
        result["disadvantage"] = bool(enc_disadvantage)
        _encumbrance_note(result, encumbrance_sources, advantage_cancelled)
        if is_forced_crit:
            result["forced_crit"] = True

        if outcome in ("Failure", "Critical Failure"):
            if sp_damage_on_miss:
                miss_die_size, miss_rolls, miss_sum = _parse_and_roll_dice(sp_damage_on_miss)
                if miss_die_size is not None:
                    miss_damage = miss_sum + final_mod
                    narrative_parts.append(
                        f"{actor} {spell_name} Miss Damage{_target_suffix(cursor, target_name, bool(is_npc_attack))}: {miss_damage} ({' + '.join(str(r) for r in miss_rolls)})"
                    )
                    result["miss_damage"] = miss_damage
                    result["miss_damage_rolls"] = miss_rolls
            result["damage_total"] = result.get("miss_damage", 0)
            result["target_killed"] = None
            return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    # ── NO DAMAGE SPELLS ──
    if sp_no_damage:
        result["damage_total"] = 0
        result["target_killed"] = None
        # Fall through to saving throw / condition logic — do NOT return

    # ── DAMAGE / HEALING CALCULATION ──
    elif final_dice in ("0d0", "0", "") and not sp_healing:
        total_damage = sp_damage_modifier + final_mod
        result["damage_total"] = total_damage
        result["target_killed"] = None
        return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    # ── TEMPORARY-HP-ONLY SPELLS (False Life, ...) ──
    # These grant a temporary pool that is allowed to EXCEED the maximum HP. They restore no
    # real hit points; the hp_temporary buff below applies the pool.
    if sp_healing and sp_temp_hp:
        narrative_parts.append(
            f"{actor} {spell_name} — temporary hit points only, no hit points restored")
        result["healing_total"] = 0
        result["healing_applied"] = 0
        result["temporary_hp_only"] = True
        result["damage_type"] = sp_damage_type
        return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs,
                                      sp_requires_concentration,
                                      is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    if sp_healing and not is_npc_attack and not is_npc_vs_npc and sp_attack_type == "automatic":
        heal_die_size, heal_rolls, heal_raw = _parse_and_roll_dice(final_dice)
        if heal_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation: '{final_dice}'. Use format 'XdY' (e.g., '2d6')."}
        ability_bonus = 0
        if spell and spell.get("add_spellcasting_mod"):
            ability_bonus = _caster_spellcasting_mod(cursor)
        heal_mod = final_mod + ability_bonus
        total_healing = heal_raw + heal_mod
        full_restore = bool(sp_flat_healing and total_healing == 0)

        if full_restore:
            narrative_parts.append(f"{actor} {spell_name} Healing{_target_suffix(cursor, target_name or actor, bool(is_npc_attack))}: full HP restore")
            result["healing_total"] = "full"
        else:
            if heal_rolls:
                heal_rolls_str = " + ".join(str(r) for r in heal_rolls)
                if heal_mod != 0:
                    heal_narrative = f"{actor} {spell_name} Healing{_target_suffix(cursor, target_name or actor, bool(is_npc_attack))}: {total_healing} ({heal_rolls_str} + {heal_mod})"
                else:
                    heal_narrative = f"{actor} {spell_name} Healing{_target_suffix(cursor, target_name or actor, bool(is_npc_attack))}: {total_healing} ({heal_rolls_str})"
            else:
                heal_narrative = f"{actor} {spell_name} Healing{_target_suffix(cursor, target_name or actor, bool(is_npc_attack))}: {total_healing}"
            narrative_parts.append(heal_narrative)
            result["healing_total"] = total_healing
            result["healing_rolls"] = heal_rolls
        result["damage_type"] = sp_damage_type

        def _heal_one(tname, is_player, override_current, override_max):
            entry = _heal_target(
                tname, total_healing, cursor, is_player=is_player,
                override_current=override_current, override_max=override_max,
                full=full_restore)
            applied = entry["healing_applied"]
            if applied > 0:
                narrative_parts.append(
                    f"{tname} fully restored ({applied} HP)" if full_restore
                    else f"{tname} healed for {applied} HP")
            else:
                narrative_parts.append(f"{tname} already at full HP")
            return entry

        applied_total = 0
        if targets and len(targets) > 0:
            healed_list = []
            for t in targets:
                tname = t.get("name", "Unknown")
                is_player = bool(t.get("is_player", False)) or _is_player_actor(cursor, tname)
                entry = _heal_one(tname, is_player, t.get("current_hp"), t.get("max_hp"))
                applied_total += entry["healing_applied"]
                healed_list.append(entry)
            result["targets_healed"] = healed_list
        elif target_name:
            is_player = _is_player_actor(cursor, target_name)
            entry = _heal_one(target_name, is_player, target_current_hp, target_max_hp)
            applied_total = entry["healing_applied"]
            result["target_healed"] = entry
            if entry.get("warning"):
                result["healing_warning"] = entry["warning"]
        else:
            self_name = (_db_val(cursor, "name", actor) if cursor else actor) or actor
            entry = _heal_one(self_name, True, None, target_max_hp)
            applied_total = entry["healing_applied"]
            result["target_healed"] = entry

        result["healing_applied"] = applied_total
        if full_restore:
            result["healing_rolls"] = []
        return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    # ── HP POOL (Sleep, Color Spray) ──
    if sp_hp_pool and not sp_healing:
        pool_die_size, pool_rolls, pool_raw = _parse_and_roll_dice(final_dice)
        if pool_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation for HP pool: '{final_dice}'."}
        hp_pool_total = pool_raw + final_mod

        pool_rolls_str = " + ".join(str(r) for r in pool_rolls)
        pool_narrative = f"{actor} {spell_name} HP Pool{_target_suffix(cursor, target_name, bool(is_npc_attack))}: {hp_pool_total}"
        if final_mod != 0:
            pool_narrative += f" ({pool_rolls_str} + {final_mod})"
        else:
            pool_narrative += f" ({pool_rolls_str})"
        narrative_parts.append(pool_narrative)

        result["hp_pool"] = True
        result["hp_pool_total"] = hp_pool_total
        result["hp_pool_rolls"] = pool_rolls
        result["damage_total"] = 0
        result["target_killed"] = None
        if sp_condition:
            result["condition"] = sp_condition
            if sp_condition_duration:
                result["condition_duration"] = sp_condition_duration
            if sp_requires_concentration:
                result["requires_concentration"] = True

        if targets and len(targets) > 0:
            for t in targets:
                if "current_hp" not in t:
                    name = t.get("name", "")
                    registry_hp = _registry_hp(name)
                    if registry_hp is not None:
                        t["current_hp"] = registry_hp
            sorted_targets = sorted(targets, key=lambda t: t.get("current_hp", 0))
            affected = []
            unaffected = []
            remaining_pool = hp_pool_total
            for t in sorted_targets:
                name = t.get("name", "Unknown")
                chp = t.get("current_hp", 0)
                if chp <= remaining_pool:
                    affected.append({"name": name, "hp": chp})
                    remaining_pool -= chp
                else:
                    unaffected.append({"name": name, "hp": chp})
            result["targets_affected"] = affected
            result["targets_unaffected"] = unaffected
            result["hp_pool_remaining"] = remaining_pool
            if sp_condition and affected:
                result["condition_targets"] = [t["name"] for t in affected]

            for t in affected:
                narrative_parts.append(
                    f"{t['name']}: Affected — {sp_condition} ({sp_condition_duration})")
            for t in unaffected:
                narrative_parts.append(
                    f"{t['name']}: Unaffected — HP exceeds remaining pool ({remaining_pool})")
        elif sp_condition:
            narrative_parts.append(f"Condition: {sp_condition} ({sp_condition_duration})")

        return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    # ── ROLL DAMAGE ──
    if not sp_no_damage:
        primary_die_size, primary_rolls, primary_sum = _parse_and_roll_dice(final_dice)
        if primary_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation: '{final_dice}'. Use format 'XdY' (e.g., '2d6')."}

        label = "Healing" if sp_healing else "Damage"
        dmg_suffix = _target_suffix(cursor, (target_name or actor) if sp_healing else target_name,
                                    bool(is_npc_attack))

        if is_crit:
            crit_rolls = [random.randint(1, primary_die_size) for _ in range(len(primary_rolls))]
            primary_damage = primary_sum + sum(crit_rolls) + final_mod
            crit_rolls_str = " + ".join(str(r) for r in crit_rolls)
            if primary_rolls:
                base_str = " + ".join(str(r) for r in primary_rolls)
                if final_mod != 0:
                    narrative_parts.append(
                        f"{actor} {spell_name} {label}{dmg_suffix}: {primary_damage} ({final_dice}: {base_str} + {crit_rolls_str}, {final_mod:+d}) [CRIT]"
                    )
                else:
                    narrative_parts.append(
                        f"{actor} {spell_name} {label}{dmg_suffix}: {primary_damage} ({final_dice}: {base_str} + {crit_rolls_str}) [CRIT]"
                    )
            else:
                narrative_parts.append(
                    f"{actor} {spell_name} {label}{dmg_suffix}: {primary_damage} (flat + {crit_rolls_str} + {final_mod}) [CRIT]"
                )
            result["crit_damage_rolls"] = crit_rolls
        else:
            primary_damage = primary_sum + final_mod
            if primary_rolls:
                base_str = " + ".join(str(r) for r in primary_rolls)
                if final_mod != 0:
                    narrative_parts.append(f"{actor} {spell_name} {label}{dmg_suffix}: {primary_damage} ({final_dice}: {base_str}, {final_mod:+d})")
                else:
                    narrative_parts.append(f"{actor} {spell_name} {label}{dmg_suffix}: {primary_damage} ({final_dice}: {base_str})")
            else:
                narrative_parts.append(f"{actor} {spell_name} {label}{dmg_suffix}: {primary_damage}")

        result["primary_damage"] = primary_damage
        result["primary_damage_rolls"] = primary_rolls
        result["damage_modifier"] = final_mod
        result["primary_die_size"] = primary_die_size

        extra_damage = 0
        extra_rolls = []
        extra_crit_rolls = []
        if final_extra_dice:
            extra_die_size, extra_base_rolls, extra_base_sum = _parse_and_roll_dice(final_extra_dice)
            if extra_die_size is None:
                return {"success": False, "error": f"Invalid extra_damage_dice notation: '{final_extra_dice}'."}
            extra_rolls = extra_base_rolls
            extra_damage = sum(extra_base_rolls)
            extra_base_str = " + ".join(str(r) for r in extra_base_rolls)
            ext_type_label = sp_extra_damage_type.title() if sp_extra_damage_type else "Extra"
            narrative_parts.append(
                f"{actor} {spell_name} {ext_type_label} Damage{dmg_suffix}: {extra_damage} ({extra_base_str})"
            )
            result["extra_damage"] = extra_damage
            result["extra_damage_rolls"] = extra_rolls
            result["extra_damage_type"] = sp_extra_damage_type

        total_damage = primary_damage + extra_damage
    else:
        primary_damage = 0
        extra_damage = 0
        total_damage = 0

    # ── MULTI-TARGET SAVING THROW ──
    target_results = None
    if targets and len(targets) > 0 and sp_attack_type == "saving_throw":
        total_xp = 0
        target_results = []
        killed_count = 0
        save_name = sp_save_type.upper() if sp_save_type else "SAVE"
        condition_targets = []

        for t in targets:
            tname = t.get("name", "Unknown")
            tchp = t.get("current_hp")
            if tchp is None:
                tchp = _registry_hp(tname) or 0
            is_player = t.get("is_player", False)
            save_detail = None
            if is_player and cursor is not None:
                # The player's save is engine-derived; any target save_modifier is situational.
                save_detail = _derived_save(cursor, sp_save_type, t.get("save_modifier") or 0,
                                            against="spells", damage=sp_damage_type,
                                            condition=sp_condition)
                tsave = save_detail["modifier"]
            else:
                tsave = t.get("save_modifier")
                if tsave is None:
                    tsave = _registry_save(tname, sp_save_type)
                if tsave is None:
                    tsave = 0
            tcr = t.get("challenge_rating")
            if tcr is None:
                tcr = _registry_cr(tname)

            save_disadvantage, save_sources = _encumbrance(cursor, is_player, sp_save_type)
            cond_save_dis, auto_fail, cond_save_sources = _condition_save_effects(
                _conditions_for(cursor, tname, is_player=is_player), sp_save_type)
            save_disadvantage = save_disadvantage or cond_save_dis
            save_sources = [*save_sources, *cond_save_sources]
            save_advantage = bool(save_detail.get("advantage")) if save_detail else False
            if save_detail is not None:
                exh = _exhaustion_effects(_exhaustion_level(cursor))
                if exh["save_disadvantage"]:
                    save_disadvantage = True
                    save_sources = [*save_sources, f"exhaustion {exh['level']}"]
            save_d20, save_rolls, save_mode, _save_cancelled = _roll_d20(
                advantage=save_advantage, disadvantage=save_disadvantage)
            save_total = save_d20 + tsave
            save_success = save_total >= spell_save_dc and not auto_fail
            save_outcome = "Success" if save_success else "Failure"

            t_damage = total_damage
            if save_success:
                if sp_save_half:
                    t_damage = max(1, total_damage // 2)
                    narrative_parts.append(
                        f"{tname} {save_name} Save: {save_total} vs DC {spell_save_dc} ({save_outcome}) ({save_d20} + {tsave})"
                    )
                    if sp_no_damage:
                        narrative_parts.append(f"{tname} saved — no effect.")
                    elif sp_healing:
                        narrative_parts.append(f"{tname} saved — half healing: {t_damage}")
                    else:
                        narrative_parts.append(f"{tname} saved — half damage: {t_damage}")
                else:
                    t_damage = 0
                    narrative_parts.append(
                        f"{tname} {save_name} Save: {save_total} vs DC {spell_save_dc} ({save_outcome}) ({save_d20} + {tsave})"
                    )
                    if sp_no_damage:
                        narrative_parts.append(f"{tname} saved — no effect.")
                    elif sp_healing:
                        narrative_parts.append(f"{tname} saved — no healing.")
                    else:
                        narrative_parts.append(f"{tname} saved — no damage.")
            else:
                narrative_parts.append(
                    f"{tname} {save_name} Save: {save_total} vs DC {spell_save_dc} ({save_outcome}) ({save_d20} + {tsave})"
                )
                if sp_no_damage and sp_condition:
                    narrative_parts.append(f"{tname}: Affected — {sp_condition} ({sp_condition_duration})")
                    condition_targets.append(tname)

            # SRD resistances/immunities/vulnerabilities, after any save halving.
            if not sp_healing and not is_player and t_damage > 0:
                t_damage, dmg_note = _apply_damage_modifiers(
                    _COMBAT_REGISTRY.get(tname), t_damage, sp_damage_type)
                if dmg_note:
                    narrative_parts.append(f"{tname} damage adjusted: {dmg_note}")
            heal_entry = None
            if sp_healing:
                heal_entry = _heal_target(
                    tname, t_damage, cursor, is_player=is_player,
                    override_current=t.get("current_hp"), override_max=t.get("max_hp"))
                remaining = heal_entry["remaining_hp"]
                killed = False
                if heal_entry["healing_applied"] > 0:
                    narrative_parts.append(f"{tname} healed for {heal_entry['healing_applied']} HP")
                elif not sp_no_damage:
                    narrative_parts.append(f"{tname} already at full HP")
            else:
                remaining = tchp - t_damage
                killed = (remaining <= 0 if tchp > 0 else False)
                if not is_player and tname:
                    _registry_update_hp(tname, max(remaining, 0))
                if killed:
                    killed_count += 1
                    if not is_player:
                        _registry_kill(tname)
                    max_hp = _registry_max_hp(tname) if tname else 0
                    display_max = f"/{max_hp}" if max_hp else ""
                    narrative_parts.append(f"{tname} HP: 0{display_max} (KILLED)")

            if heal_entry is not None:
                tr = {"name": tname, "save_roll": save_d20, "save_modifier": tsave,
                      "save_total": save_total, "save_success": save_success,
                      "damage": t_damage,
                      "healing_total": heal_entry["healing_total"],
                      "healing_applied": heal_entry["healing_applied"],
                      "remaining_hp": remaining, "max_hp": heal_entry["max_hp"],
                      "killed": False}
                if heal_entry.get("hp_change"):
                    tr["hp_change"] = heal_entry["hp_change"]
            else:
                tr = {"name": tname, "save_roll": save_d20, "save_modifier": tsave,
                       "save_total": save_total, "save_success": save_success,
                       "damage": t_damage, "remaining_hp": max(0, remaining), "killed": killed}
                if is_player and t_damage > 0 and cursor:
                    t_damage, dmg_note = _apply_player_damage_modifiers(cursor, t_damage, sp_damage_type)
                    if dmg_note:
                        tr["damage_modified"] = dmg_note
                        tr["damage"] = t_damage
                    hp_result = _apply_hp_change(cursor, -t_damage)
                    tr["hp_change"] = hp_result
            if auto_fail:
                tr["save_auto_failed"] = True
            if save_rolls:
                tr[f"save_{save_mode}_rolls"] = save_rolls
            if save_sources:
                tr["disadvantage_sources"] = list(save_sources)

            if killed and tcr is not None:
                xp = CR_XP_TABLE.get(tcr, 0)
                if xp > 0:
                    total_xp += xp
                    tr["xp_awarded"] = xp

            if save_detail is not None:
                tr["save"] = save_detail
            target_results.append(tr)

        result["targets"] = target_results
        if not sp_healing:
            result["killed_count"] = killed_count
        else:
            result["healing_total"] = total_damage
            result["healing_applied"] = sum(t.get("healing_applied", 0) for t in target_results)
        if sp_condition and condition_targets:
            result["condition"] = sp_condition
            if sp_condition_duration:
                result["condition_duration"] = sp_condition_duration
            if sp_requires_concentration:
                result["requires_concentration"] = True
            result["condition_targets"] = condition_targets

        if total_xp > 0 and not is_npc_attack and not is_npc_vs_npc and cursor:
            xp_result = modify_player_numeric(key="xp", delta=total_xp)
            result["xp_awarded"] = total_xp
            result["xp_result"] = xp_result
            narrative_parts.append(f"Total XP Awarded: {total_xp}")
            if xp_result.get("level_up"):
                narrative_parts.append(xp_result["level_up_summary"])

        result["damage_total"] = total_damage
        result["damage_type"] = sp_damage_type
        return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    # ── SINGLE-TARGET SAVING THROW ──
    if sp_attack_type == "saving_throw":
        saver_name = _target_label(cursor, target_name, bool(is_npc_attack)) or target_name or actor
        save_detail = None
        if is_npc_attack and cursor is not None:
            # The player is the saver: the engine derives the whole save (SRD 5.1).
            save_detail = _derived_save(cursor, sp_save_type, player_situational_modifier or 0,
                                        against="spells", damage=sp_damage_type,
                                        condition=sp_condition)
            save_mod = save_detail["modifier"]
        elif target_save_modifier is not None:
            save_mod = target_save_modifier
        else:
            save_mod = _registry_save(target_name or actor, sp_save_type)
            if save_mod is None:
                save_mod = 0
        # The player is the one saving when an NPC casts on them.
        save_disadvantage, save_sources = _encumbrance(cursor, is_npc_attack, sp_save_type)
        cond_save_dis, auto_fail, cond_save_sources = _condition_save_effects(
            _conditions_for(cursor, saver_name, is_player=is_npc_attack), sp_save_type)
        save_disadvantage = save_disadvantage or cond_save_dis
        save_sources = [*save_sources, *cond_save_sources]
        save_advantage = bool(save_detail.get("advantage")) if save_detail else False
        if save_detail is not None:
            exh = _exhaustion_effects(_exhaustion_level(cursor))
            if exh["save_disadvantage"]:
                save_disadvantage = True
                save_sources = [*save_sources, f"exhaustion {exh['level']}"]
        save_d20, save_rolls, save_mode, _save_cancelled = _roll_d20(
            advantage=save_advantage, disadvantage=save_disadvantage)
        save_total = save_d20 + save_mod
        save_success = save_total >= spell_save_dc and not auto_fail
        if auto_fail:
            save_outcome = "Failure"

        result["save_roll"] = save_d20
        result["save_modifier"] = save_mod
        result["save_total"] = save_total
        result["save_dc"] = spell_save_dc
        result["save_success"] = save_success
        if save_detail is not None:
            result["save"] = save_detail
        result["save_disadvantage"] = bool(save_disadvantage)
        if save_disadvantage:
            result["save_disadvantage_sources"] = save_sources
        save_outcome = "Success" if save_success else "Failure"

        narrative_parts.append(
            f"{saver_name} {sp_save_type.upper()} Save: {save_total} vs DC {spell_save_dc}"
            f" ({save_outcome}) ({save_d20} + {save_mod})"
        )

        if save_success:
            if sp_save_half:
                total_damage = max(1, total_damage // 2)
                if sp_no_damage:
                    narrative_parts.append(f"{saver_name} saved — no effect.")
                elif sp_healing:
                    narrative_parts.append(f"{saver_name} saved — half healing: {total_damage}")
                else:
                    narrative_parts.append(f"{saver_name} saved — half damage: {total_damage}")
            else:
                total_damage = 0
                if sp_no_damage:
                    narrative_parts.append(f"{saver_name} saved — no effect.")
                elif sp_healing:
                    narrative_parts.append(f"{saver_name} saved — no healing.")
                else:
                    narrative_parts.append(f"{saver_name} saved — no damage.")
        elif sp_no_damage and sp_condition:
            narrative_parts.append(f"{saver_name}: Affected — {sp_condition} ({sp_condition_duration})")
            result["condition"] = sp_condition
            if sp_condition_duration:
                result["condition_duration"] = sp_condition_duration
            if sp_requires_concentration:
                result["requires_concentration"] = True
            result["condition_targets"] = [saver_name]
        if auto_fail:
            result["save_auto_failed"] = True

        # SRD resistances/immunities/vulnerabilities of a registered NPC target (not the player).
        if not is_npc_attack and not sp_healing and total_damage > 0:
            total_damage, dmg_note = _apply_damage_modifiers(
                _COMBAT_REGISTRY.get(target_name), total_damage, sp_damage_type)
            if dmg_note:
                result["damage_modified"] = dmg_note
                narrative_parts.append(f"Damage adjusted: {dmg_note} ({target_name})")

    result["damage_total"] = total_damage
    result["damage_type"] = sp_damage_type

    # ── APPLY HP CHANGES (single target) ──
    if is_npc_attack and total_damage > 0 and not sp_healing:
        total_damage, dmg_note = _apply_player_damage_modifiers(cursor, total_damage, sp_damage_type)
        if dmg_note:
            result["damage_modified"] = dmg_note
            result["damage_total"] = total_damage
            narrative_parts.append(f"Damage adjusted: {dmg_note}")
        hp_result = _apply_hp_change(cursor, -total_damage)
        result["hp_change"] = hp_result
    elif is_npc_attack and sp_healing:
        player_name = (target_name or (_db_val(cursor, "name", "Player") if cursor else "Player"))
        entry = _heal_target(player_name, total_damage, cursor, is_player=True,
                             override_max=target_max_hp)
        result["healing_total"] = entry["healing_total"]
        result["healing_applied"] = entry["healing_applied"]
        result["target_healed"] = entry
        if entry.get("hp_change"):
            result["hp_change"] = entry["hp_change"]
        if entry.get("warning"):
            result["healing_warning"] = entry["warning"]
        narrative_parts.append(
            f"Healed {player_name} for {entry['healing_applied']} HP "
            f"({entry['remaining_hp']}/{entry['max_hp']})")
    elif is_npc_vs_npc and total_damage > 0 and not sp_healing:
        from_registry = False
        if target_current_hp is None and target_name:
            target_current_hp = _registry_hp(target_name)
            if target_current_hp is not None:
                from_registry = True
        if challenge_rating is None and target_name:
            challenge_rating = _registry_cr(target_name)
        if target_current_hp is not None:
            target_remaining = target_current_hp - total_damage
            result["target_remaining_hp"] = target_remaining

            if target_name and from_registry:
                _registry_update_hp(target_name, max(target_remaining, 0))

            if target_remaining <= 0:
                result["target_killed"] = True
                if target_name and from_registry:
                    _registry_kill(target_name)
                max_hp = _registry_max_hp(target_name)
                display_max = f"/{max_hp}" if max_hp else ""
                narrative_parts.append(f"{target_name} HP: 0{display_max} (KILLED)")
            else:
                result["target_killed"] = False
        else:
            result["target_killed"] = None
        result["npc_vs_npc"] = True
    elif is_npc_vs_npc and sp_healing:
        result["npc_vs_npc"] = True
        result["target_killed"] = None
        entry = _heal_target(target_name, total_damage, cursor, is_player=False,
                             override_current=target_current_hp, override_max=target_max_hp)
        result["healing_total"] = entry["healing_total"]
        result["healing_applied"] = entry["healing_applied"]
        result["target_healed"] = entry
        if entry.get("warning"):
            result["healing_warning"] = entry["warning"]
        if entry["healing_applied"] > 0:
            narrative_parts.append(
                f"{actor} heals {target_name} for {entry['healing_applied']} HP "
                f"({entry['remaining_hp']}/{entry['max_hp']})")
        else:
            narrative_parts.append(
                f"{actor}'s heal has no effect on {target_name or 'target'} (already at full HP).")
    elif not is_npc_attack and not is_npc_vs_npc and not sp_healing:
        from_registry = False
        db_target = False
        if target_current_hp is None and target_name:
            target_current_hp = _registry_hp(target_name)
            if target_current_hp is not None:
                from_registry = True
        if target_current_hp is None and target_name and cursor:
            player_name = (_db_val(cursor, "name", "") or "").lower()
            if (target_name or "").lower() == player_name:
                target_current_hp = int(_db_val(cursor, "current_hit_points", 0))
                db_target = True
        if challenge_rating is None and target_name:
            challenge_rating = _registry_cr(target_name)
        if target_current_hp is not None:
            target_remaining = target_current_hp - total_damage
            result["target_remaining_hp"] = target_remaining

            if target_name and from_registry:
                _registry_update_hp(target_name, max(target_remaining, 0))
            elif db_target and cursor:
                total_damage, dmg_note = _apply_player_damage_modifiers(cursor, total_damage, sp_damage_type)
                if dmg_note:
                    result["damage_modified"] = dmg_note
                hp_result = _apply_hp_change(cursor, -total_damage)
                result["hp_change"] = hp_result

            if target_remaining <= 0:
                result["target_killed"] = True
                if target_name and from_registry:
                    _registry_kill(target_name)
                max_hp = _registry_max_hp(target_name)
                display_max = f"/{max_hp}" if max_hp else ""
                narrative_parts.append(f"{target_name} HP: 0{display_max} (KILLED)")
            else:
                result["target_killed"] = False

            if result.get("target_killed") and challenge_rating is not None:
                xp_awarded = CR_XP_TABLE.get(challenge_rating, 0)
                if xp_awarded > 0 and cursor:
                    xp_result = modify_player_numeric(key="xp", delta=xp_awarded)
                    result["xp_awarded"] = xp_awarded
                    result["challenge_rating"] = challenge_rating
                    result["xp_result"] = xp_result
                    narrative_parts.append(f"XP Awarded: {xp_awarded}")
                    if xp_result.get("level_up"):
                        narrative_parts.append(xp_result["level_up_summary"])
        else:
            result["target_killed"] = None
    elif not is_npc_attack and not is_npc_vs_npc and sp_healing:
        tname = target_name or (_db_val(cursor, "name", actor) if cursor else actor)
        is_player = _is_player_actor(cursor, tname)
        entry = _heal_target(tname, total_damage, cursor, is_player=is_player,
                             override_current=target_current_hp, override_max=target_max_hp)
        result["healing_total"] = entry["healing_total"]
        result["healing_applied"] = entry["healing_applied"]
        result["target_healed"] = entry
        if entry.get("hp_change"):
            result["hp_change"] = entry["hp_change"]
        if entry.get("warning"):
            result["healing_warning"] = entry["warning"]
        if entry["healing_applied"] > 0:
            narrative_parts.append(
                f"{tname} healed for {entry['healing_applied']} HP "
                f"({entry['remaining_hp']}/{entry['max_hp']})")
        else:
            narrative_parts.append(f"{tname} already at full HP")
    else:
        result["target_killed"] = None
        if is_npc_vs_npc:
            result["npc_vs_npc"] = True

    return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)


if __name__ == "__main__":
    DEBUG_MODE = "--debug" in sys.argv
    player_file = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    if player_file:
        init_status = init_player_db(player_file)
        print(f"Server DB Init: {init_status}", file=sys.stderr)

    mcp.run(transport="stdio")
