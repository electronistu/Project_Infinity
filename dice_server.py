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
import equipment  # local SRD 5.1 equipped-items model (armour, hands, derived AC)

try:
    import yaml
except ImportError:
    yaml = None

mcp = FastMCP("InfinityRolls", log_level="WARNING")

DB_CONNECTION = None

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


def _carry_block(cursor) -> dict:
    """Derived carrying state (never persisted: it is stripped before saving)."""
    return carrying.carry_state(_carry_get(cursor))


# ── equipped items / derived armour class (SRD 5.1) ───────────────────────────

def _equipment_block(cursor) -> dict:
    """Derived equipped-items state (never persisted: stripped before saving)."""
    return equipment.equipment_state(_carry_get(cursor))


def _recompute_armor_class(cursor) -> int | None:
    """Set `armor_class` from the equipped set. No-op without an `equipped` value.

    The Forge writes the first consistent pair; from then on every equip/unequip
    recomputes it here, so the sheet and the engine can never disagree with what
    the character is actually wearing. Temporary effects still layer on top via
    `modify_player_numeric(key='armor_class', ...)`.
    """
    if not isinstance(_db_val(cursor, "equipped", None), dict):
        return None
    ac = _equipment_block(cursor)["base_ac"]
    _db_set(cursor, "armor_class", ac)
    return ac


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


WORN_KINDS = {"cloak", "boots", "gloves", "bracers", "headwear", "ring"}


def _is_worn_slot(item, declared) -> bool:
    """True when equipping should put the item in the `worn` container rather than a hand.

    5e has no slots, but clothing, cloaks, boots, gloves, bracers, headwear and rings are worn
    rather than held — they need somewhere to live so attunement and pairing rules have something
    to bind to.
    """
    if equipment.is_weapon(item) or equipment.is_shield(item) or equipment.is_armor(item):
        return False
    kind = str((declared or {}).get("kind") or "").strip().lower()
    return kind in WORN_KINDS or equipment.is_clothing(item)


def _in_active_combat() -> bool:
    """True while the combat registry holds a living non-player combatant.

    Donning or doffing armour takes minutes, so it cannot happen mid-fight; shields take one
    action and are always allowed. A registry whose enemies are all dead no longer counts.
    """
    for entry in (_COMBAT_REGISTRY or {}).values():
        if isinstance(entry, dict) and not entry.get("is_player") and not entry.get("killed"):
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
    low = [p.lower() for p in props]
    melee = bool(wpn.get("melee", True))
    stats = _db_val(cursor, "stats", {}) or {}
    str_mod = _ability_mod(stats.get("str", 10))
    dex_mod = _ability_mod(stats.get("dex", 10))
    if any("finesse" in p for p in low):
        ability, ability_mod = (("dexterity", dex_mod) if dex_mod >= str_mod else ("strength", str_mod))
    elif melee:
        ability, ability_mod = "strength", str_mod
    else:
        ability, ability_mod = "dexterity", dex_mod

    proficiencies = {str(p).strip().lower() for p in (_db_val(cursor, "weapon_proficiencies", []) or [])}
    category = str(wpn.get("category") or "").lower()
    proficient = (not proficiencies
                  or f"{category} weapons" in proficiencies
                  or weapon.strip().lower() in proficiencies)
    prof_bonus = int(_db_val(cursor, "proficiency_bonus", 2) or 2)

    # Magic bonuses: attunement / paired-item gating.
    equipped = _db_val(cursor, "equipped", {}) or {}
    equipped_names = ([equipped.get("armor")] if equipped.get("armor") else []) \
        + [h for h in (equipped.get("hands") or []) if h]
    suppressed = equipment.bonus_suppressed_reason(weapon, inventory,
                                                   _db_val(cursor, "attuned", []) or [],
                                                   equipped_names)
    attack_bonus = int(entry.get("attack_bonus") or 0) if suppressed is None else 0
    damage_bonus = int(entry.get("damage_bonus") or 0) if suppressed is None else 0

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

    damage_modifier = ability_mod + damage_bonus
    if off_hand:
        # Two-weapon fighting: no ability modifier on the bonus attack's damage unless negative.
        damage_modifier = min(ability_mod, 0) + damage_bonus

    return {
        "attack_modifier": ability_mod + (prof_bonus if proficient else 0) + attack_bonus,
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
    stats = _db_val(cursor, "stats", {}) or {}
    if isinstance(stats, str):
        try:
            stats = json.loads(stats)
        except (json.JSONDecodeError, TypeError):
            stats = {}
    try:
        return (int(stats.get(stat_key, 10)) - 10) // 2
    except (TypeError, ValueError):
        return 0


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
    }
    if thp_absorbed > 0:
        result["temporary_hit_points"] = {"old": thp, "new": 0, "absorbed": thp_absorbed}
    if clamped:
        result["clamped"] = True
        if new_val == 0 and current_hp > 0:
            result["status"] = "Unconscious"
            result["death_saves"] = True
            result["message"] = f"HP has reached 0. {result['hp_status']}. Begin death saves."
        elif new_val == total_hp and delta > 0:
            result["message"] = f"HP restored to maximum. {result['hp_status']}."
        else:
            result["message"] = f"Value was clamped. {result['hp_status']}."
    elif new_val == 0 and current_hp > 0:
        result["status"] = "Unconscious"
        result["death_saves"] = True
        result["message"] = f"HP has reached 0. {result['hp_status']}. Begin death saves."
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

    cursor.execute("SELECT value FROM player WHERE key = ?", ("stats",))
    stats_row = cursor.fetchone()
    if not stats_row:
        return None
    stats = json.loads(stats_row[0])
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
    """
    Increments or decrements a numeric player attribute. Supports dotted notation for nested paths.

    PARAMETERS:
    - key: dotted path to the numeric field (e.g. 'gold', 'spellcasting.slots.1', 'consumables.Bolts')
    - delta: integer increment (negative to decrement)

    PROJECT-SPECIFIC BEHAVIORS:
    1. current_hit_points: Clamped to [0, total_hit_points]. At 0, returns death_saves flag and Unconscious status.
    2. spellcasting.slots.N: Validates slot availability before decrementing. Returns error with available slots if empty.
    3. consumables.ITEM: Auto-creates at 0 if missing. At 0 or below, auto-removed with DEPLETION message.
       Values are clamped to 0 — items cannot have negative quantity.
    4. xp: Crossing a level threshold auto-applies ALL numeric level-up changes (level, proficiency, hit dice,
       HP rolls, spell slots, DC, attack modifier). You MUST still manually apply class features,
       cantrips/spells known, ASIs (levels 4/8/12/16/19), and subclass features via update_player_list.
    5. capacity_multiplier: scales carrying capacity for effects such as enhance ability (Bull's
       Strength): set it to 2 while the effect is active and back to 1 when it ends; a long rest
       resets it to 1.

    EXAMPLES:
    modify_player_numeric(key='gold', delta=-10)
    modify_player_numeric(key='spellcasting.slots.1', delta=-1)
    modify_player_numeric(key='consumables.Bolts', delta=-1)
    modify_player_numeric(key='consumables.Arrows', delta=20)
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
        return result
    except Exception as e:
        return {"success": False, "error": f"Error modifying numeric value: {str(e)}", "key": key}


_DEFAULT_REPUTATION_FACTION = "misc"


def _resolve_reputation_bucket(data, path_in_obj):
    """Resolve a `reputation.<kingdom>[.<faction>]` path to a writable list.

    Reputation is stored as `{kingdom: {faction: [entries]}}`. A missing faction
    under an *existing* kingdom is auto-created; targeting a kingdom alone uses
    the `_DEFAULT_REPUTATION_FACTION` bucket. Unknown kingdoms are never created,
    so a typo in the kingdom name still fails.

    Returns `(list_or_None, resolved_path_in_obj)`.
    """
    if not isinstance(data, dict):
        return None, path_in_obj
    parts = path_in_obj.split('.')
    kingdom = parts[0] if parts else ''
    if not kingdom or kingdom not in data or not isinstance(data[kingdom], dict):
        return None, path_in_obj
    if len(parts) == 1:
        parts = [kingdom, _DEFAULT_REPUTATION_FACTION]
    if len(parts) != 2 or not parts[1]:
        return None, path_in_obj
    faction = parts[1]
    bucket = data[kingdom].get(faction)
    if bucket is None:
        bucket = []
        data[kingdom][faction] = bucket
    elif not isinstance(bucket, list):
        return None, path_in_obj
    return bucket, f"{kingdom}.{faction}"


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
                       pair: bool | None = None) -> dict:
    """
    Adds or removes an item from a player list.

    PARAMETERS:
    - key: dotted path to the list (e.g. 'inventory', 'spellcasting.spells_known', 'reputation.eldoria.guard')
    - item: for add — 'Name: Description' (description optional); for remove — name ONLY (never include the description)
    - action: 'add' or 'remove'
    - weight: (inventory adds only) the item's weight in POUNDS, e.g. weight=2.5. Declare it for
      anything the SRD weight catalog does not know — magic items, loot, homebrew gear (see
      behaviour 5). SRD items are already weighed, so leave it out for those.
    - base: (inventory adds only) the SRD archetype an invented item is built on, e.g.
      base='Dagger' for 'Voidfang, a magic dagger'. It supplies properties, hands, weight and AC
      for anything the catalogs do not know.
    - damage_dice / damage_type / properties: a weapon's combat stats (e.g. damage_dice='1d4',
      damage_type='piercing', properties=['Finesse', 'Light', 'Thrown']).
    - ac / dex_cap / strength_req: an armour's stats (e.g. ac=16, dex_cap=0, strength_req=13).
    - ac_bonus / attack_bonus / damage_bonus: magic bonuses, declared explicitly (never parsed
      from a '+1' in the name). They apply only while the item is ATTUNED (set attunement=True) and,
      for a paired item, only while both halves are worn.
    - attunement: True when the item's magic requires attunement (SRD: at most 3 attuned items, one
      of each kind). attunement_by optionally names a restriction (class, creature type).
    - kind: the slot family for the one-of-each-kind rule — 'armor', 'cloak', 'boots', 'gloves',
      'bracers', 'headwear', 'ring', 'other'.
    - pair: True when this entry is ONE HALF of a paired item (boots, bracers, gauntlets, gloves).
      Add both halves as separate inventory entries sharing the same `base`; a single half grants no
      bonus (SRD: paired items confer no benefit unless both are worn).

    DECLARE FULL COMBAT STATS FOR INVENTED ITEMS: the engine derives attack rolls, damage and
    armour class from these fields, so a weapon without damage and no known `base` cannot be used
    to attack. Declare `base`, the damage/ac fields and any bonuses in the same add.

    PROJECT-SPECIFIC BEHAVIORS:
    1. Prepared casters: capacity enforced on spells_prepared (max = spellcasting_ability_mod + level).
       At capacity, the add is rejected with the current spell list.
    2. Removing from active_effects auto-reverts any stat deltas applied by that effect.
    3. CONSUMABLES: NEVER use this tool for consumable quantities — use modify_player_numeric(key='consumables.ITEM', delta=N) instead.
    4. Reputation: use key='reputation.KINGDOM.FACTION' with lowercase kingdom/faction names and no apostrophes.
       Each entry is a 'Title: Description' pair. A missing FACTION under an existing kingdom is
       created automatically; a bare 'reputation.KINGDOM' writes to the 'misc' bucket. Unknown
       kingdoms are rejected — never invent a kingdom name.
    5. INVENTORY WEIGHT (SRD 5.1): every inventory change returns a 'carrying' block
       (carried, capacity, push_drag_lift, thresholds, status, speed, unweighed). Variant
       encumbrance is enforced: above 5x STR you are ENCUMBERED (speed -10 ft); above 10x STR
       you are HEAVILY ENCUMBERED (speed -20 ft, and the engine rolls attack rolls and
       STR/DEX/CON checks and saves with disadvantage for you). Coins count: 50 coins = 1 lb.
       An add of an unknown item without 'weight' is accepted, counts as 0 lb and comes back
       as 'unweighed_item' with a warning — re-add it with weight=<pounds> to fix the total.
    6. Removing a worn or wielded item from the inventory auto-unequips it and the result reports
       'unequipped'. Replacing a weapon or tool means remove + add the replacement with its stats +
       equip_item — the replacement is NOT auto-equipped.

    EXAMPLES:
    update_player_list(key='inventory', item='Dagger: A rusty blade (1d4 piercing, Finesse, Light, Thrown (range 20/60))', action='add')
    update_player_list(key='inventory', item='Dagger', action='remove')          ← name only, NOT 'Dagger: A rusty blade...'
    update_player_list(key='inventory', item='Void Crystal: a humming shard of black glass', action='add', weight=2.5)
    update_player_list(key='spellcasting.spells_known', item='Shield', action='remove')
    update_player_list(key='spellcasting.spells_prepared', item='Fireball', action='add')
    update_player_list(key='reputation.eldoria.guard', item='Hero of the City: After defending the city from a dragon attack, {player_name} is a well known hero among people of Eldoria', action='add')
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
            current_list = get_nested_value(data, path_in_obj)

            if current_list is None or not isinstance(current_list, list):
                resolved = None
                if action == "add" and root_key == "reputation":
                    resolved, path_in_obj = _resolve_reputation_bucket(data, path_in_obj)
                if resolved is None:
                    return {"success": False, "error": f"Key '{key}' not found or is not a list.", "available_nested_keys": list(data.keys()), "key": key}
                current_list = resolved
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

        if action == "add":
            name = item
            desc = ""
            if ":" in item:
                name, desc = [p.strip() for p in item.split(":", 1)]
            added_name = name

            new_entry = {"name": name, "description": desc} if (desc or ":" in item) else name
            declared = {}
            if key == "inventory":
                if weight is not None:
                    declared["weight"] = float(weight)
                for field, value in (("base", base), ("damage_dice", damage_dice),
                                     ("damage_type", damage_type), ("properties", properties),
                                     ("ac", ac), ("dex_cap", dex_cap),
                                     ("strength_req", strength_req), ("ac_bonus", ac_bonus),
                                     ("attack_bonus", attack_bonus), ("damage_bonus", damage_bonus),
                                     ("attunement", attunement), ("attunement_by", attunement_by),
                                     ("kind", kind), ("pair", pair)):
                    if value is not None:
                        declared[field] = value
            if declared:
                new_entry = {"name": name, "description": desc, **declared}

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
                buff_data_raw = _db_val(cursor, "_active_buff_data", {})
                if isinstance(buff_data_raw, str):
                    buff_data_raw = json.loads(buff_data_raw)
                if item in buff_data_raw:
                    entries = buff_data_raw[item]
                    reverted = {}
                    for entry in entries:
                        modify_player_numeric(key=entry["field"], delta=-entry["delta"])
                        reverted[entry["field"]] = {"delta": -entry["delta"]}
                    del buff_data_raw[item]
                    _db_set(cursor, "_active_buff_data", buff_data_raw)
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
                    }
                    return result_early
        else:
            return {"success": False, "error": "Invalid action. Use 'add' or 'remove'.", "key": key, "action": action}

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
            if added_base and not (equipment.is_armor(added_base)
                                   or equipment.is_weapon(added_base)):
                result["unknown_base"] = added_base
                result["warning"] = (
                    f"Unknown base archetype '{added_base}' — it is stored as given, but the engine "
                    f"cannot derive hands, weight or combat stats from it. Use an SRD weapon or "
                    f"armour name, or declare the stats explicitly."
                )
            elif (action == "add" and weight is None and added_name
                    and carrying.weight_for(added_name, base=added_base) is None):
                result["unweighed_item"] = added_name
                result["warning"] = (
                    f"'{added_name}' is not in the SRD weight catalog and no weight was given, so it "
                    f"counts as 0 lb. Re-add it with weight=<pounds> so carrying capacity stays accurate."
                )

        return result
    except Exception as e:
        return {"success": False, "error": f"Error updating list: {str(e)}", "key": key}


@mcp.tool()
def equip_item(item: str, action: str = "equip", slot: str | None = None,
               replace: bool = False, instant: bool = False) -> dict:
    """
    Equips or unequips something the character is carrying (SRD 5.1: one suit of armour, two hands).

    PARAMETERS:
    - item: the item's name exactly as it appears in the inventory
    - action: 'equip' (default) or 'unequip'
    - slot: optional target — 'main_hand', 'off_hand', 'armor' or 'worn'. Omit to let the engine
      choose (armour goes to the armour slot; anything else takes a free hand). Cloaks, boots,
      gloves, bracers, headwear and rings go to the 'worn' container; clothing (Common Clothes,
      Vestments, Robes) is worn automatically and listed there.
    - replace: when the slot or the second hand is already taken, set True to stow whatever was
      there instead of being refused. The stowed item stays in the inventory.
    - instant: allow an armour change during combat. By default donning/doffing armour is REFUSED
      while a fight is running (SRD: light 1 min, medium 5 min, heavy 10 min to don) — use instant=True
      only for magic or a GM call. Shields take one action and are always allowed.

    PROJECT-SPECIFIC BEHAVIORS:
    1. 5e has NO equipment slots — only two hands plus one suit of armour. A shield is an item held
       in a hand, and a two-handed weapon needs BOTH hands to attack with, so equipping one
       requires the other hand to be free (or replace=True).
    2. Equipping recomputes armour_class from the equipped set and returns it before → after, plus
       the SRD time_cost for armour/shields. Worn armour replaces the unarmoured formula; heavy
       armour adds no Dexterity at all (and no penalty for a negative one). Only one suit of armour
       and one shield benefit a creature (a second shield is held but confers nothing).
    3. Unequipping never drops the item: it stays in the inventory.
    4. The item must already be in the inventory (update_player_list); this tool only moves it.
    5. The result carries the derived 'equipment' block (hands, hands_free, base_ac, ac_breakdown,
       warnings).
    6. The in-combat refusal checks the registry: a fight whose hostiles are all dead no longer
       counts as combat, so armour may be changed.

    EXAMPLES:
    equip_item(item='Chain Mail')
    equip_item(item='Shield')
    equip_item(item='Greatsword', replace=True)
    equip_item(item='Longsword', slot='off_hand')
    equip_item(item='Shield', action='unequip')
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
                "narrative_format": f"{item} {label} — AC {before} → {after}"
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
                        f"{item} is already worn — AC {before}"}
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
                        f"{item} is already held — AC {before}"}
            if target == "worn" or (target is None and _is_worn_slot(item, declared)):
                if item in worn:
                    return {"success": True, "action": "equip", "item": item,
                            "already_equipped": True, "equipped": equipped,
                            "armor_class_before": before, "armor_class_after": before,
                            "equipment": _equipment_block(cursor),
                            "narrative_format": f"{item} is already worn — AC {before}"}
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
            "narrative_format": f"{item} equipped ({where}) — AC {before} → {after}"
                                + (f" ({time_cost})" if time_cost else ""),
        }
    except Exception as e:
        return {"success": False, "error": f"Error equipping item: {str(e)}", "item": item}


@mcp.tool()
def attune_item(item: str, action: str = "attune", instant: bool = False) -> dict:
    """
    Attunes to (or breaks attunement with) a magic item (SRD 5.1: at most 3, one of each kind).

    PARAMETERS:
    - item: the item's name exactly as it appears in the inventory
    - action: 'attune' (default) or 'unattune'
    - instant: allow it during combat. By default attuning takes a short rest, so it is REFUSED
      while a fight is running.

    PROJECT-SPECIFIC BEHAVIORS:
    1. A creature can be attuned to no more than THREE magic items, to no more than one copy of an
       item, and to only one item of each `kind` (armor, cloak, boots, gloves, bracers, headwear,
       ring).
    2. Only items declared with attunement=True can be attuned; their ac_bonus / attack_bonus /
       damage_bonus apply only while attuned (until then the derived bonuses stay off and the
       'equipment' block warns 'not_attuned').
    3. The item must be in the inventory first (update_player_list).

    EXAMPLES:
    attune_item(item='Voidmail')
    attune_item(item='Ring of Protection')
    attune_item(item='Voidmail', action='unattune')
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
        attuned = _db_val(cursor, "attuned", []) or []
        attuned = [str(a) for a in attuned] if isinstance(attuned, list) else []

        def _declared_kind(name):
            entry = next((e for e in inventory if isinstance(e, dict) and e.get("name") == name), {})
            return str(entry.get("kind") or "").strip().lower()

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
        kind = str(declared.get("kind") or "").strip().lower()
        if kind and kind != "other":
            clash = next((a for a in attuned if _declared_kind(a) == kind), None)
            if clash is not None:
                return _fail("attunement_kind_conflict",
                             f"Already attuned to another {kind} ({clash}).",
                             conflicting_item=clash, kind=kind)
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
def dump_player_db() -> dict:
    """
    Returns a full dump of the current in-memory player database for state refresh.

    The dump includes a derived `_carrying` block (SRD 5.1 carrying capacity, encumbrance
    status and effective speed) and a derived `_equipment` block (worn armour, hands,
    hands free, and the armour-class breakdown). Both are computed, never saved: the
    save file does not contain them.
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

        return result
    except Exception as e:
        return {"error": f"Error dumping database: {str(e)}"}


@mcp.tool()
def request_scene_image(description: str, kingdom: str = "", area: str = "",
                        location: str = "", sublocation: str = "",
                        time_of_day: str = "", weather: str = "",
                        characters: dict[str, str] | None = None,
                        establishing: str = "", main_npc: dict | None = None,
                        npcs: list[dict] | None = None,
                        seed_change: str = "", mood: str = "") -> dict:
    """Request a storyline illustration of the turn's moment. Emit exactly one per
    narrative turn, attached to the narrative prose (the system prompt's imagery
    protocol says when).

    TWO IMAGES PER PLACE. On entering a NEW location/sublocation, ask for its
    establishing view too by filling `establishing`; the engine then makes an empty
    'seed' image (never shown to the player). Afterwards only the action image is
    produced, and every one of them is drawn FRESH from that hidden establishing view
    — never from the previous action — so a figure can never be duplicated and the
    cast comes only from your `characters` dict. Only ask for a seed when the place is
    NOT already in the KNOWN IMAGE PLACES list; if a new place's seed is missing the
    tool returns a WARNING and you must call again with `establishing`.

    PARAMETERS:
    - description: what HAPPENS — the protagonist's action, and any notable transient event
      (e.g. the place is on fire). NAME every NPC ('Corvin watches the protagonist read'), never
      identify them by look ('a thin man in a damp grey coat watches'). Never describe a person's
      look, the place, its furniture or its light, the time or the weather — the seed carries the
      place, `time_of_day`/`weather` carry the light, and a declared NPC's look comes from the
      registry. You MAY say where the protagonist is standing or sitting. DO restate anything
      from earlier in this scene that is still true and still visible (spilled ale, a broken
      table, blood, a body, an open door) — nothing carries over from the previous image on its
      own.
    - kingdom: the realm the place lies in. Declare it ONLY when creating the seed (together
      with `establishing`); otherwise omit it and reuse the known place. Reuse the exact known
      name.
    - area: the city, town, settlement or general region within the kingdom — whatever fits
      (e.g. 'Eldoria City', 'Millbrook', 'the Eldoria–Silverwood border'). Declare it ONLY when
      creating the seed (together with `establishing`). Reuse the exact known name.
    - location: the place (building, street or area). Reuse the exact known name.
    - sublocation: the exact room or spot within it ("" for an open place). Reuse the exact
      known name; a different room is a different place.
    - time_of_day: the time of day or night, e.g. 'dawn', 'midday', 'dusk', 'deep night'. Fed
      to the image generator so the lighting is right.
    - weather: the weather or conditions, e.g. 'heavy rain', 'dense fog', 'clear skies',
      'a howling blizzard'. Fed to the image generator.
    - characters: a dict of EVERY NPC/creature on stage -> what they are doing or how they
      act toward the protagonist. The KEY is the NPC's **name** (exactly as declared — see
      `npcs` / `register_npcs`; never repeat their look here); the VALUE carries the action.
      Use EXACT counts, never 'a few' (e.g. 'three dockhands'). The engine injects the stored
      description for every declared name, so continuity does not depend on your wording.
      Exclude the protagonist (their portrait is attached). Example: {'Maera': 'drawing ale,
      watching the door', 'the harbourmaster': 'handing over a sealed writ'}.
    - establishing: a short description of the place, given ONLY when its seed does not
      exist yet. It must be empty and unpopulated — no people, creatures or animals — and
      weather-free and timeless: no rain, fog or snow, and no time of day ('at dusk'), because
      the seed is permanent and weather-neutral; those belong in `weather` / `time_of_day`.
    - main_npc: the place's main NPC, given at seed creation as {'name': ...,
      'description': ...}. The description is the stable look (gender, build, distinguishing
      features) plus any helpers/aides/partners (e.g. {'name': 'Gorson', 'description': 'a
      burly, grey-bearded smith with a burn-scarred left hand; two apprentices'}). Pass {} if
      the place has no main NPC. Declared ONLY when creating the seed; afterwards refer to
      them by name in `characters`.
    - npcs: recurring storyline NPCs declared in this same call, as a list of {'name': ...,
      'description': ...}. Use it when someone recurring first appears; the same names can
      also be declared ahead of time with register_npcs. After that, use the NAME ONLY
      everywhere — never repeat the description.
    - seed_change: a PERMANENT change to the place (e.g. 'it burned down'). Regenerates the
      hidden seed immediately, so this image and every later one already show the change.
    - mood: a short mood word for the light/atmosphere.

    Refer to the player/main character as 'the protagonist' (the image prompt maps the
    attached portrait onto that word): say where the protagonist is and what they are
    doing — they face the action, not the camera, and their back to the camera is fine —
    but NEVER describe their physical appearance (face, hair, build, race,
    clothing) — the portrait is attached automatically. Describe clothing, armour,
    weapons and accessories ONLY from what they actually have equipped (check
    `_equipment` in your latest dump_player_db if unsure): never invent a hood,
    hooded cloak, cowl, hat, helmet, armour or other item they do not have, and
    never write 'hooded'/'cloaked'/'armoured' unless it is equipped.

    This does not change game state and the engine does not wait for the picture;
    it is rendered and shown with your narrative.
    """
    return {
        "status": "requested",
        "kingdom": kingdom or "",
        "area": area or "",
        "location": location or "",
        "sublocation": sublocation or "",
        "characters": len(characters) if isinstance(characters, dict) else 0,
        "main_npc": (main_npc or {}).get("name", "") if isinstance(main_npc, dict) else "",
        "npcs": len(npcs) if isinstance(npcs, list) else 0,
        "seed_requested": bool(establishing or seed_change),
        "note": ("The illustration will appear with your narrative. Do NOT mention "
                 "this tool or its result in the Mechanics block."),
    }


@mcp.tool()
def register_npcs(npcs: list[dict]) -> dict:
    """Declares recurring storyline NPCs so the illustrator draws them identically every time.

    Declare each recurring character ONCE, the first time they appear, with a stable look. The
    engine stores the description and injects it into every later illustration: after that you
    refer to the NPC by NAME ONLY and never repeat their description.

    PARAMETERS:
    - npcs: a list of {'name': ..., 'description': ...} dicts. `name` is the exact name you will
      keep using (a person's name, or a stable handle like 'the harbourmaster'). `description`
      is the stable look: gender, build, distinguishing features, clothing/role-defining gear
      (e.g. {'name': 'Maera', 'description': 'a broad, one-eared woman with iron-grey braids,
      leaning on the bar'}).

    Re-declaring an existing name updates its description. Use request_scene_image's `npcs`
    field instead when you are also requesting the illustration in the same call.
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
def rest(rest_type: str, prepared_spells: list[str] | None = None) -> dict:
    """
    Applies a short or long rest. All numeric changes auto-applied to the database.

    PARAMETERS:
    - rest_type: "short" or "long"
    - prepared_spells: (long rest only, optional) full replacement list of spell names to prepare.
      Validated against max capacity. Wizards validated against spellbook. Known casters ignored/error.

    PROJECT-SPECIFIC BEHAVIORS:
    1. Short rest: auto-spends hit dice one-by-one until HP full or no dice remain.
       Warlocks: full Pact Magic restore. Wizards: Arcane Recovery auto-applied
       (ceil(level/2) combined slot levels, greedily from lowest expended, cannot recover 6th+ slots).
    2. Long rest: full HP, regain max(level//2, 1) hit dice (capped at level), all slots restored,
       all active effects cleared with stat deltas reverted.
    3. Long rest rejected if HP is 0.
    4. Returns hints for class features that need manual recharge.

    EXAMPLES:
    rest(rest_type='short')
    rest(rest_type='long')
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

        stats_raw = _db_val(cursor, "stats", {})
        if isinstance(stats_raw, str):
            stats_raw = json.loads(stats_raw)
        con_mod = (int(stats_raw.get("con", 10)) - 10) // 2

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

            while missing_hp > 0 and dice_spent < hd_count:
                roll = random.randint(1, hd_size)
                healing = max(roll + con_mod, 0)
                total_healing += healing
                missing_hp -= healing
                dice_spent += 1

            new_hd = hd_count - dice_spent

            if dice_spent > 0:
                hp_result = _apply_hp_change(cursor, total_healing)
                _db_set(cursor, "hit_dice_count", str(new_hd))
                DB_CONNECTION.commit()
                changes["hp"] = {"old": current_hp, "new": hp_result["new_value"],
                                 "healed": total_healing, "dice_spent": dice_spent,
                                 "status": hp_result["hp_status"]}
                changes["hit_dice"] = {"old": hd_count, "new": new_hd, "spent": dice_spent}
            else:
                changes["hp"] = {"old": current_hp, "new": current_hp,
                                 "healed": 0, "dice_spent": 0,
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
                return {"success": False,
                        "error": "Cannot benefit from a long rest with 0 HP. The character must be stabilized first."}

            _db_set(cursor, "current_hit_points", str(total_hp))
            changes["hp"] = {"old": current_hp, "new": total_hp,
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
                    modify_player_numeric(key=entry["field"], delta=-entry["delta"])
                effects_cleared.append(spell_name)
            _db_set(cursor, "active_effects", [])
            _db_set(cursor, "_active_buff_data", {})
            DB_CONNECTION.commit()
            if effects_cleared:
                changes["effects_cleared"] = effects_cleared

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
def roll_dice(dice_notation: str, modifier: int = 0, actor: str = "{player_name}") -> dict:
    """
    Rolls dice for damage, healing, loot quantity, or any random magnitude.

    PARAMETERS:
    - dice_notation: dice only (e.g. '3d4'), do NOT include modifiers in this string
    - modifier: flat bonus/penalty to add to the roll total
    - actor: who is rolling — character name for player, NPC/creature name for NPCs

    RULES:
    - Use this for "how much?" scenarios only. For success/failure checks, use perform_check.

    EXAMPLES:
    roll_dice(actor='Senna', dice_notation='3d4', modifier=3)
    roll_dice(actor='Goblin Brute', dice_notation='1d6', modifier=2)
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
def perform_check(modifier: int, dc: int, check_name: str = "Check", actor: str = "{player_name}",
                  ability: str | None = None, grapple: bool = False) -> dict:
    """
    Performs a skill check or saving throw (d20 + modifier vs DC).

    PARAMETERS:
    - modifier: bonus to add to the d20 roll
    - dc: difficulty class to beat
    - check_name: label for the check (e.g. 'Athletics', 'Perception')
    - actor: who is performing the check — character name for player, NPC/creature name for NPCs
    - ability: (optional) the ability the check uses — 'str', 'dex', 'con', 'int', 'wis' or 'cha'.
      Pass it for every ability check and save so the engine can apply encumbrance and armour.
    - grapple: set True when this check is an attempt to GRAPPLE a creature (SRD: it needs at least
      one free hand). The check is refused, and the action spent, when both hands are full. Do not
      set it for escaping a grapple — that needs no free hand.

    RULES:
    - For weapon/unarmed attacks, use resolve_attack instead.
    - For spell attacks, use resolve_magic.

    PROJECT-SPECIFIC BEHAVIORS:
    1. HEAVILY ENCUMBERED (SRD 5.1 variant): if the player carries more than 10x STR, this check is
       rolled with disadvantage when `ability` is 'str', 'dex' or 'con'. The response then carries
       'disadvantage_sources' and both dice in 'disadvantage_rolls'.
    2. NOT PROFICIENT WITH WORN ARMOUR (SRD 5.1): disadvantage on STR/DEX checks; the response names
       the source in 'disadvantage_sources'.
    3. GRAPPLE (grapple=True): refused with turn_lost=true when no hand is free — no roll is made.
    4. The GM's own granted advantage is not a parameter here — roll the check twice and narrate,
       or tell the player their encumbrance is costing them.

    EXAMPLES:
    perform_check(actor='Thorin', modifier=5, dc=15, check_name='Athletics', ability='str')
    perform_check(actor='Guard Captain', modifier=2, dc=13, check_name='Perception', ability='wis')
    perform_check(actor='Senna', modifier=1, dc=13, check_name='Deception', ability='cha')
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
    roll, rolls, roll_mode, _cancelled = _roll_d20(disadvantage=disadvantage)
    total = roll + modifier

    if roll == 20:
        result = "Critical Success"
    elif roll == 1:
        result = "Critical Failure"
    elif total >= dc:
        result = "Success"
    else:
        result = "Failure"

    narrative = f"{actor} {check_name}: {total} vs DC {dc} ({result}) ({roll} + {modifier})"
    if roll_mode:
        narrative += f" [{roll_mode}]"

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
    "exhaustion": {},  # tracked; the level table is not applied
}


def _registry_save(target_name: str, ability) -> int | None:
    """Save modifier for a registry target: per-ability `saves`, else the legacy `save_modifier`."""
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
    legacy = entry.get("save_modifier")
    if legacy is not None:
        try:
            return int(legacy)
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
    props = [str(p).lower() for p in (attack.get("properties") or [])]
    if any("two-handed" in p for p in props):
        pass  # a two-handed melee weapon is still melee — range/reach decides
    if attack.get("range"):
        return True
    return any(k in " ".join(props) for k in ("ammunition", "ranged"))


def _normalize_combatant(c, add_to_existing=False) -> dict:
    """Normalize a declared stat block into a registry entry."""
    max_hp = int(c.get("max_hp", c["hp"]))
    saves = c.get("saves") if isinstance(c.get("saves"), dict) else {}
    normalized = {
        "current_hp": int(c["hp"]),
        "max_hp": max_hp,
        "ac": int(c["ac"]),
        "save_modifier": c.get("save_modifier"),
        "saves": {k: int(v) for k, v in saves.items()
                  if k in ABILITY_KEYS and isinstance(v, (int, float))},
        "challenge_rating": c.get("challenge_rating"),
        "initiative_modifier": 0 if add_to_existing else int(c.get("initiative_modifier", 0) or 0),
        "initiative_advantage": bool(c.get("initiative_advantage", False)),
        "role": str(c.get("role") or "hostile").strip().lower(),
        "speed": c.get("speed"),
        "damage_resistances": [str(t).strip().lower() for t in (c.get("damage_resistances") or [])],
        "damage_immunities": [str(t).strip().lower() for t in (c.get("damage_immunities") or [])],
        "damage_vulnerabilities": [str(t).strip().lower()
                                  for t in (c.get("damage_vulnerabilities") or [])],
        "condition_immunities": [str(t).strip().lower() for t in (c.get("condition_immunities") or [])],
        "attacks": [a for a in (c.get("attacks") or []) if isinstance(a, dict)],
        "multiattack": c.get("multiattack", 1),
        "spellcasting": c.get("spellcasting") if isinstance(c.get("spellcasting"), dict) else None,
        "traits": [str(t) for t in (c.get("traits") or [])],
        "conditions": [str(t).strip().lower() for t in (c.get("conditions") or [])],
        "initiative_roll": 0,
        "initiative_total": 0,
        "is_player": False,
        "killed": False,
    }
    return normalized


def _player_registry_entry(cursor) -> dict:
    """The player's registry entry, with saves/speed/spellcasting derived from their sheet."""
    name = _db_val(cursor, "name", "Player")
    stats_raw = _db_val(cursor, "stats", {}) or {}
    if isinstance(stats_raw, str):
        stats_raw = json.loads(stats_raw)
    mods = {k: (int(stats_raw.get(k, 10)) - 10) // 2 for k in ABILITY_KEYS}
    proficient = {str(s).strip().lower()[:3] for s in (_db_val(cursor, "saves", []) or [])}
    prof_bonus = int(_db_val(cursor, "proficiency_bonus", 2) or 2)
    saves = {k: mods[k] + (prof_bonus if k in proficient else 0) for k in ABILITY_KEYS}
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
        "save_modifier": None,
        "saves": saves,
        "challenge_rating": None,
        "initiative_modifier": mods["dex"],
        "initiative_advantage": False,
        "role": "ally",
        "speed": _db_val(cursor, "speed", 30),
        "damage_resistances": [],
        "damage_immunities": [],
        "damage_vulnerabilities": [],
        "condition_immunities": [],
        "attacks": [],
        "multiattack": 1,
        "spellcasting": spellcasting,
        "traits": [],
        "conditions": sorted(_player_conditions(cursor)),
        "initiative_roll": 0,
        "initiative_total": 0,
        "is_player": True,
        "killed": False,
    }


@mcp.tool()
def register_combatants(combatants: list[dict], add_to_existing: bool = False) -> dict:
    """
    Registers all combatants for a battle and rolls initiative for everyone. Player is auto-registered.

    DECLARE EVERY COMBATANT FULLY. You will not be asked for these values again: the engine derives
    attacks, saves, armour class and damage from this block, so hand it the real stats. Estimate
    only when the creature genuinely has no stat block.

    PARAMETERS:
    - combatants: list of NPC dicts. Required: `name`, `hp`, `ac`. Everything else optional:
      - name (str): must match target_name / actor in resolve_attack / resolve_magic calls
      - hp (int): starting (current) hit points
      - max_hp (int): maximum, defaults to hp
      - ac (int): armour class
      - initiative_modifier (int, required unless add_to_existing=True): DEX modifier
      - initiative_advantage (bool): roll initiative with advantage
      - challenge_rating (float): CR for XP awards
      - role (str): 'hostile' | 'ally' | 'neutral' (default 'hostile')
      - speed (int)
      - saves (dict): one modifier per ability, e.g. {"str": -1, "dex": 2, "con": 0,
        "int": 0, "wis": -1, "cha": -1} — used for every saving throw the creature makes
      - save_modifier (int): legacy single save bonus (used only when `saves` is absent)
      - damage_resistances / damage_immunities / damage_vulnerabilities (list of str): damage types;
        'all' is allowed. The engine halves/zeroes/doubles incoming damage automatically
      - condition_immunities (list of str): conditions the creature cannot gain
      - attacks (list of dicts): each {name, base?, attack_bonus, damage_dice, damage_modifier?,
        damage_type?, properties?, reach?, range?} — resolve_attack(actor=…, attack=…, …) uses these
      - multiattack (int or str): how many attacks per Attack action (reported, not enforced)
      - spellcasting (dict): {ability, save_dc, attack_modifier} for NPC casters
      - conditions (list of str): starting conditions (blinded, prone, restrained, poisoned, ...)
      - traits (list of str): free-text special abilities, for reference
    - add_to_existing (bool, default False): adds to the existing registry without wiping it. No
      initiative rolled for the arrivals; existing HP/saves/conditions are preserved.

    PROJECT-SPECIFIC BEHAVIORS:
    1. resolve_attack / resolve_magic auto-lookup a target's HP, AC, CR and save modifiers from the
       registry — you do not need to pass target_current_hp, target_ac or a save every call.
       HP is carried forward between hits automatically.
    2. resolve_attack(actor='Goblin', attack='Scimitar', target_name='Borin') derives the NPC's
       attack bonus and damage from its declared `attacks`. Omit attack= only for an improvised or
       undeclared action, in which case pass attack_modifier and damage_dice directly.
    3. Calling this again without add_to_existing overwrites the registry entirely — that is how a
       fight ends and a new one begins. There is no separate end-combat call.
    4. Conditions declared here and changed via update_combatant drive advantage/disadvantage, auto
       failures and critical hits against helpless targets (blinded, prone, restrained, paralyzed,
       petrified, stunned, unconscious, poisoned, frightened, invisible).

    EXAMPLES:
    register_combatants(combatants=[
        {"name": "Goblin", "hp": 7, "ac": 15, "initiative_modifier": 2, "challenge_rating": 0.25,
         "role": "hostile", "speed": 30,
         "saves": {"str": -1, "dex": 2, "con": 0, "int": 0, "wis": -1, "cha": -1},
         "attacks": [
             {"name": "Scimitar", "attack_bonus": 4, "damage_dice": "1d6", "damage_modifier": 2,
              "damage_type": "slashing", "reach": 5, "properties": ["Finesse", "Light"]},
             {"name": "Shortbow", "base": "Shortbow", "attack_bonus": 4, "damage_dice": "1d6",
              "damage_modifier": 2, "damage_type": "piercing", "range": "80/320"}]},
        {"name": "Ogre", "hp": 59, "ac": 11, "initiative_modifier": -1, "challenge_rating": 2,
         "damage_resistances": ["fire"], "multiattack": 1,
         "attacks": [{"name": "Greatclub", "attack_bonus": 6, "damage_dice": "2d8",
                      "damage_modifier": 4, "damage_type": "bludgeoning", "reach": 5}]},
    ])

    register_combatants(combatants=[
        {"name": "Guard Reinforce 1", "hp": 11, "ac": 16, "role": "hostile"},
        {"name": "Guard Reinforce 2", "hp": 11, "ac": 16, "role": "hostile"},
    ], add_to_existing=True)
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

        player_d20 = random.randint(1, 20)
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

    registry_summary = []
    for rname, entry in _COMBAT_REGISTRY.items():
        summary = {
            "name": rname,
            "hp": f"{entry['current_hp']}/{entry['max_hp']}",
            "ac": entry["ac"],
            "initiative": entry["initiative_total"],
            "is_player": entry.get("is_player", False),
            "role": entry.get("role", "hostile"),
        }
        if entry.get("conditions"):
            summary["conditions"] = entry["conditions"]
        registry_summary.append(summary)

    narrative_parts = [f"Combatants registered ({len(_COMBAT_REGISTRY)} total)."]

    if add_to_existing:
        added_names = [c["name"] for c in combatants]
        narrative_parts.append(f"Added to existing registry: {', '.join(added_names)}")
        return {
            "success": True,
            "registry_summary": registry_summary,
            "narrative_format": "\n".join(narrative_parts),
        }

    initiative_results.sort(key=lambda r: (-r["total"], r["name"]))
    order = [r["name"] for r in initiative_results]

    narrative_parts.append("Initiative Order:")
    for i, r in enumerate(initiative_results, 1):
        tag = " (Player)" if r["is_player"] else ""
        narrative_parts.append(
            f"  {i}. {r['name']}{tag}: {r['total']} ({r['roll']} + {r['modifier']})"
        )

    return {
        "success": True,
        "initiative": initiative_results,
        "initiative_order": order,
        "registry_summary": registry_summary,
        "narrative_format": "\n".join(narrative_parts),
    }


@mcp.tool()
def update_combatant(name: str, conditions_add: list[str] | None = None,
                     conditions_remove: list[str] | None = None,
                     hp_delta: int | None = None, max_hp: int | None = None,
                     ac: int | None = None) -> dict:
    """
    Changes a registered combatant mid-fight: conditions and (NPC) HP / defence.

    PARAMETERS:
    - name: the combatant exactly as registered
    - conditions_add / conditions_remove: SRD conditions, e.g. ['prone', 'restrained']. Adding a
      condition the creature is immune to is refused (reported in 'blocked_conditions').
    - hp_delta: (NPCs only) signed change; clamped to [0, max_hp], sets 'killed' at 0
    - max_hp / ac: (NPCs only) correct the declared values

    PROJECT-SPECIFIC BEHAVIORS:
    1. Conditions drive the engine automatically: blinded/prone/restrained/poisoned/frightened/
       invisible modify attack rolls, paralyzed/petrified/stunned/unconscious auto-fail STR/DEX
       saves and are hit critically in melee, petrified resists all damage.
    2. The player's conditions are stored on their sheet; use modify_player_numeric for player HP.
    3. There is no end-combat call — declare a new registry with register_combatants when one is
       needed.

    EXAMPLES:
    update_combatant(name='Goblin', conditions_add=['prone'])
    update_combatant(name='Ogre', hp_delta=-13)
    update_combatant(name='{player_name}', conditions_add=['restrained'])
    """
    global DB_CONNECTION
    entry = _COMBAT_REGISTRY.get(name)
    if entry is None:
        return {"success": False, "error": "not_registered", "name": name,
                "reason": f"'{name}' is not in the combat registry.",
                "combatants": list(_COMBAT_REGISTRY)}
    is_player = bool(entry.get("is_player"))
    notes = []
    blocked = []

    if is_player:
        if DB_CONNECTION is None:
            return {"success": False, "error": "Database not initialized."}
        cursor = DB_CONNECTION.cursor()
        current = _player_conditions(cursor)
        immunities = set()
    else:
        current = {str(c).strip().lower() for c in (entry.get("conditions") or [])}
        immunities = {str(c).strip().lower() for c in (entry.get("condition_immunities") or [])}

    for condition in conditions_add or []:
        key = str(condition).strip().lower()
        if not key:
            continue
        if key in immunities:
            blocked.append({"condition": key, "reason": "immune"})
            continue
        current.add(key)
    for condition in conditions_remove or []:
        current.discard(str(condition).strip().lower())
    current = sorted(current)

    if is_player:
        cursor = DB_CONNECTION.cursor()
        _db_set(cursor, "conditions", current)
        entry["conditions"] = current
    else:
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

    narrative = f"{name}: {entry['current_hp']}/{entry['max_hp']} HP, AC {entry['ac']}"
    if current:
        narrative += f" — {', '.join(current)}"
    if blocked:
        narrative += f" (immune to {', '.join(b['condition'] for b in blocked)})"
    return {
        "success": True, "name": name, "is_player": is_player,
        "hp": f"{entry['current_hp']}/{entry['max_hp']}", "ac": entry["ac"],
        "conditions": current, "blocked_conditions": blocked,
        "note": " ".join(notes) or None,
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
    """
    Resolves a full weapon/unarmed attack: attack roll, damage, HP application, kill detection, XP award.

    PARAMETERS:
    - actor: who is attacking — character name for player, NPC name for NPCs
    - weapon: (player attacks) the item the player attacks with, named exactly as it appears in the
      inventory. The engine derives attack_modifier, damage dice and damage modifier from it, its
      SRD archetype (or declared `base`) and the character's stats/proficiency. If it is not in
      hand the attack is REFUSED (no roll) — the action is spent, see behaviour 11.
    - attack: (NPC attacks) the name of a declared attack in the NPC's registry entry, e.g.
      'Scimitar'. The engine derives attack bonus, damage dice, damage modifier and damage type
      from it (declare a `base` to fill any gap). Refused when the actor is not registered or the
      attack is not declared.
    - damage_type: the damage type (e.g. 'fire'). Used for resistances/immunities/vulnerabilities;
      filled automatically from weapon= or attack= when omitted.
    - off_hand: set True for the bonus-action attack of TWO-WEAPON FIGHTING. Both hands must hold a
      different light melee weapon; the damage takes no ability modifier unless it is negative.
      Refused otherwise (the bonus action is spent).
    - target_ac: omit it for a target in the combat registry — the engine uses the AC you declared
      at register_combatants (falls back to 10).
    - attack_modifier: bonus to the d20 attack roll (omit when passing weapon; an explicit value
      always wins)
    - target_ac: target's armor class
    - damage_dice: primary damage dice (e.g. '1d8'), doubled on crit (omit when passing weapon)
    - damage_modifier: flat bonus added to damage (omit when passing weapon; an explicit 0 is kept)
    - target_name: optional name for HP lookup via combat registry
    - target_current_hp: optional current HP (auto-looked up from registry if omitted)
    - challenge_rating: optional CR for XP awards (auto-looked up from registry if omitted)
    - extra_damage_dice: bonus damage dice NOT doubled on crit (e.g. elemental riders, sneak attack)
    - extra_damage_modifier: flat bonus for extra damage (default 0)
    - is_npc_attack: NPC attacking player — damage auto-applied to player HP, no slot consumed
    - is_npc_vs_npc: NPC attacking NPC — no player HP modified, no XP auto-awarded
    - advantage: roll 2d20 take highest
    - force_crit: any successful hit becomes a crit (for unconscious/paralyzed targets within 5 feet)

    PROJECT-SPECIFIC BEHAVIORS:
    1. Combat registry: if register_combatants was called, target_current_hp and challenge_rating
       auto-lookup from the registry. Registry HP is updated after each hit — sequential hits on
       the same target use the correct reduced HP.
    2. Extra damage dice are NOT doubled on crit. Put everything in damage_dice if you want all dice doubled.
    3. Temporary HP on the player is drained before real HP when is_npc_attack=True.
    4. XP auto-awarded on kill (unless is_npc_vs_npc=True). Uses the CR/XP table internally.
    5. EQUIPPED WEAPONS (SRD 5.1): pass weapon='<item>' for a player attack and the engine derives
       the roll from the equipped item (ability + proficiency + magic bonuses, damage from its
       dice/base). If the item is not in hand the attack is REFUSED without a roll: the result has
       success=false, error='item_not_equipped' (or 'item_not_carried' / 'weapon_stats_unknown'),
       turn_lost=true and a gm_instruction.
    6. A Versatile weapon uses its two-handed damage die while the other hand is free. A one-handed
       Ammunition weapon (hand crossbow, sling, blowgun) cannot be fired while the other hand holds
       something — it needs a free hand to load (error 'cannot_reload').
    7. Conditions on either side drive the roll automatically (blinded, prone, restrained, poisoned,
       frightened, invisible, paralyzed, petrified, stunned, unconscious): advantage/disadvantage,
       and a critical hit against a helpless target in melee. Damage against a registered NPC is
       adjusted by its declared resistances/immunities/vulnerabilities and reported as
       'damage_modified'.
    8. A player attacking in armour/shields they are not proficient with rolls at disadvantage
       (reported in 'disadvantage_sources').

    EXAMPLES:
    resolve_attack(actor='{player_name}', weapon='Longsword', target_ac=13,
                   target_name='Goblin', target_current_hp=12, challenge_rating=0.5)

    resolve_attack(actor='Goblin', attack='Scimitar', target_name='{player_name}', is_npc_attack=True)

    resolve_attack(actor='{player_name}', attack_modifier=4, target_ac=13,
                   damage_dice='1d8', damage_modifier=2, target_name='Goblin',
                   target_current_hp=12, challenge_rating=0.5)

    resolve_attack(actor='Goblin', attack_modifier=4, target_ac=13,
                   damage_dice='1d6', damage_modifier=2, target_name='{player_name}',
                   is_npc_attack=True)

    resolve_attack(actor='{player_name}', attack_modifier=5, target_ac=15,
                   damage_dice='1d4', damage_modifier=3,
                   extra_damage_dice='1d6', extra_damage_modifier=0,
                   target_name='Orc Brute', target_current_hp=25,
                   challenge_rating=0.5)

    resolve_attack(actor='Town Guard', attack_modifier=4, target_ac=13,
                   damage_dice='1d8', damage_modifier=2, target_name='Goblin',
                   target_current_hp=12, is_npc_vs_npc=True)
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
        d20, advantage_rolls, roll_mode, advantage_cancelled = _roll_d20(
            advantage=advantage or cond_adv, disadvantage=enc_disadvantage or cond_dis)
        die_label = f"{min(advantage_rolls)} / {max(advantage_rolls)} → " if advantage_rolls else ""

        total_attack = d20 + attack_modifier

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
            f"{actor} Attack{attack_label}{adv_label}: {die_label}{total_attack} vs AC {target_ac} ({outcome}) ({d20} + {attack_modifier})"
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
            result["narrative_format"] = narrative_parts[0]
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

        total_damage = primary_damage + extra_damage

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
                    if target_name:
                        max_hp = _registry_max_hp(target_name)
                        display_max = f"/{max_hp}" if max_hp else ""
                        narrative_parts.append(f"{target_name} HP: {target_remaining}{display_max}")
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
                    if target_name:
                        max_hp = _registry_max_hp(target_name)
                        display_max = f"/{max_hp}" if max_hp else ""
                        narrative_parts.append(f"{target_name} HP: {target_remaining}{display_max}")

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


def _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc, is_npc_vs_npc=False):
    buffs_applied = {}
    active_names = None

    if sp_buffs and DB_CONNECTION is not None and not is_npc:
        cursor = DB_CONNECTION.cursor()
        stats_dict = _db_val(cursor, "stats", {})
        if isinstance(stats_dict, str):
            stats_dict = json.loads(stats_dict)

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
    result["narrative_format"] = "\n".join(narrative_parts)
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
                hp_result = _apply_hp_change(cursor, -dealt)
                tr["hp_change"] = hp_result
                hp_lines.append(f"{name} HP: {hp_result['hp_status']}")
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
        else:
            hp_lines.append(f"{name} HP: {max(remaining, 0)}{display_max}")
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
        narrative_parts.append(f"{actor} {spell_name}: {total_damage} — {count} {label} — " + " | ".join(bits))
    elif len(entries) == 1:
        vals = per_target[entries[0]["name"]]["darts"]
        narrative_parts.append(f"{actor} {spell_name}: {total_damage} — {count} darts: " + ", ".join(str(v) for v in vals))
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
    challenge_rating: float | None = None,
    target_save_modifier: int | None = None,
    player_save_modifier: int | None = None,
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
    """
    Resolves a full spell: spell slot management, attack/save, damage/healing, HP application, kill detection, XP award.

    PARAMETERS:
    - spell_name: spell name (looked up in config/spells.yml; custom spells need attack_type + damage_dice)
    - actor: who is casting — character name for player, NPC name for NPCs
    - spell_attack_modifier: bonus to d20 for attack_roll spells
    - spell_save_dc: save DC for saving_throw spells
    - target_ac: target AC (required for attack_roll spells)
    - target_name: optional name for HP lookup via combat registry
    - target_current_hp: optional current HP (auto-looked up from registry if omitted)
    - challenge_rating: optional CR for XP awards (auto-looked up from registry if omitted)
    - target_save_modifier: save bonus for single-target saving_throw spells (omit it for a target
      in the combat registry — the engine uses its declared saves)
    - player_save_modifier: player's save bonus when is_npc_attack=True with saving throws (omit it
      to let the engine derive the player's save from their sheet)
    - spell_attack_modifier / spell_save_dc: omit for a registered NPC caster — the engine uses the
      `spellcasting` block you declared at register_combatants
    - slot_level: upcast slot level (defaults to spell's native level)
    - is_npc_attack: NPC casting on player — damage auto-applied to player HP, no slot consumed
    - is_scroll: cast from scroll — no slot consumed, ability check for scrolls above caster level (DMG p.200)
    - attack_type: "attack_roll", "saving_throw", or "automatic" (from DB or override)
    - save_type: ability for saving throw (e.g. 'dex', 'wis')
    - save_half: half damage on successful save (default True)
    - damage_dice: custom damage dice (override, needed for spells not in DB)
    - damage_modifier: flat damage bonus (default 0)
    - damage_type: damage type label (e.g. 'fire', 'force')
    - cantrip_scaling: enable auto-scaling at levels 5/11/17
    - higher_levels: upcast scaling string (e.g. '+1d6')
    - healing: spell heals instead of dealing damage
    - aoe: spell affects an area
    - ritual: cast as ritual — no slot consumed
    - is_npc_vs_npc: NPC casting on NPC — no player HP modified, no XP auto-awarded
    - caster_level: caster level for NPC-vs-NPC cantrip scaling
    - advantage: roll 2d20 take highest (attack_roll only)
    - force_crit: successful hit becomes crit (attack_roll only, unconscious/paralyzed targets within 5 feet)
    - targets: list of dicts for AoE multi-target resolution. Fields vary by spell type:
        HP pool (Sleep/Color Spray): {"name": str, "current_hp": int}
        Saving throw (Fireball/etc.): {"name": str, "current_hp": int, "save_modifier": int, "challenge_rating": float}
          Add {"is_player": True} to auto-apply damage to player HP in the DB.
        Projectiles (Magic Missile / Scorching Ray / Eldritch Blast): {"name": str, "darts": int}
          'darts' must be given on every target and total the spell's projectile count
          (3 for Magic Missile at 1st level, +1 per upcast level). Omit 'darts' (or omit
          'targets') to send every projectile at a single target.

    PROJECT-SPECIFIC BEHAVIORS:
    1. Slot validation happens BEFORE dice are rolled. Empty slots return an error with available slots.
    2. Known spells from DB auto-consume a slot. Cantrips, rituals, scrolls, and NPC attacks do not.
    3. Duplicate active buff spells (e.g. casting Shield while Shield is already active) are rejected
       BEFORE slot consumption.
    4. Multi-target AoE: damage is rolled ONCE, individual saves per target, one slot consumed.
       HP is tracked through the combat registry. XP summed from all kills with CRs.
    5. HP pool spells (Sleep): targets sorted by HP ascending, pool drained in order.
    6. Extra damage dice are NOT doubled on crit.
    7. Temporary HP on the player is drained before real HP when is_npc_attack=True.
    8. Scrolls above caster's available slot level trigger an ability check (d20 + spellcasting mod vs DC 10 + spell level).
       On failure, scroll is wasted and spell does not take effect.
    9. A spellbook caster (wizard-style) whose Spellbook item is missing from the inventory cannot cast leveled
       spells (cantrips still work) and cannot prepare spells until the book is recovered.
    10. Multi-projectile spells resolve each projectile separately: automatic spells (Magic Missile)
       never roll to hit and strike simultaneously; attack-roll spells (Scorching Ray, Eldritch Blast)
       roll a separate attack per projectile. 'darts' splits them across targets.
    11. FREE HAND (SRD 5.1 components): a player spell with somatic (S) or material (M) components
       cannot be cast with both hands occupied — the engine REFUSES it before any dice or slot use
       (success=false, error='both_hands_occupied', turn_lost=true, gm_instruction; no slot spent).
       A spell with only V needs no hand. Components come from config/components.yml, else the
       components='...' you pass (use it for homebrew/custom spells); an unknown spell is treated as
       V,S,M.
    12. ARMOUR PROFICIENCY (SRD 5.1): a player wearing armour they are not proficient with cannot
       cast ANY spell — refused before any roll or slot use (success=false,
       error='armor_not_proficient', turn_lost=true; no slot spent). The armour's type must be in
       armor_proficiencies ('Light armor', 'Medium armor', 'Heavy armor', 'Shields'); a shield counts.

    EXAMPLES:
    resolve_magic(spell_name='Fireball', actor='{player_name}',
                  spell_save_dc=15,
                  targets=[
                      {"name": "Goblin", "current_hp": 7, "save_modifier": 2, "challenge_rating": 0.25},
                      {"name": "Goblin", "current_hp": 7, "save_modifier": 2, "challenge_rating": 0.25},
                      {"name": "Hobgoblin", "current_hp": 11, "save_modifier": 1, "challenge_rating": 0.5},
                  ])

    resolve_magic(spell_name='Fireball', actor='Evil Wizard',
                  is_npc_attack=True, spell_save_dc=15,
                  targets=[
                      {"name": "{player_name}", "current_hp": 7, "save_modifier": 3, "is_player": True},
                      {"name": "Captain Holt", "current_hp": 30, "save_modifier": 4},
                      {"name": "Town Guard", "current_hp": 25, "save_modifier": 2},
                  ])

    resolve_magic(spell_name='Sleep', actor='{player_name}',
                  targets=[
                      {"name": "Guard 1", "current_hp": 11},
                      {"name": "Guard 2", "current_hp": 11},
                      {"name": "Guard 3", "current_hp": 11},
                  ])

    resolve_magic(spell_name='Fireball', actor='{player_name}',
                  spell_save_dc=15,
                  target_name='Goblin Shaman', target_current_hp=24,
                  challenge_rating=1)

    resolve_magic(spell_name='Fireball', actor='{player_name}',
                  spell_save_dc=15,
                  target_name='Ogre', target_current_hp=60,
                  challenge_rating=2, slot_level=5)

    resolve_magic(spell_name='Fire Bolt', actor='{player_name}',
                  spell_attack_modifier=6, target_ac=14,
                  target_name='Orc', target_current_hp=18,
                  challenge_rating=0.5)

    resolve_magic(spell_name='Detect Magic', actor='{player_name}',
                  attack_type='saving_throw', save_type='wis',
                  spell_save_dc=13, ritual=True)

    resolve_magic(spell_name='Void Blast', actor='{player_name}',
                  spell_attack_modifier=7, target_ac=16,
                  attack_type='attack_roll',
                  damage_dice='3d10', damage_type='force',
                  target_name='Shadow Wraith', target_current_hp=40,
                  challenge_rating=4, slot_level=3)

    resolve_magic(spell_name='Magic Missile', actor='Evil Wizard',
                  is_npc_attack=True,
                  attack_type='automatic',
                  damage_dice='3d4', damage_modifier=3,
                  damage_type='force',
                  target_name='{player_name}')

    resolve_magic(spell_name='Fireball', actor='Dark Wizard',
                  spell_save_dc=15, target_save_modifier=2,
                  target_name='Town Guard', target_current_hp=30,
                  is_npc_vs_npc=True, caster_level=7)

    resolve_magic(spell_name='Fire Bolt', actor='Dark Wizard',
                  spell_attack_modifier=6, target_ac=14,
                  target_name='Town Guard', target_current_hp=20,
                  is_npc_vs_npc=True, caster_level=11)

    resolve_magic(spell_name='Inflict Wounds', actor='{player_name}',
                  spell_attack_modifier=4, target_ac=10,
                  target_name='Sleeping Guard', target_current_hp=6,
                  challenge_rating=0, advantage=True, force_crit=True)
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

    # ── DUPLICATE ACTIVE EFFECT CHECK ──
    if sp_buffs and DB_CONNECTION is not None:
        buff_data_raw = _db_val(cursor, "_active_buff_data", {})
        if isinstance(buff_data_raw, str):
            buff_data_raw = json.loads(buff_data_raw)
        lookup_entries = {k.lower(): k for k in buff_data_raw}
        if spell_key in lookup_entries:
            return {
                "success": False,
                "error": f"{spell_name} is already active.",
                "spell_name": spell_name,
                "hint": (
                    f"Remove it first via update_player_list(key='active_effects', "
                    f"item='{lookup_entries[spell_key]}', action='remove') before recasting."
                ),
            }

    # ── SPELLBOOK CHECK ──
    # A caster whose spell list lives in a spellbook (wizard-style) cannot cast
    # leveled spells while the book is missing from the inventory. Cantrips are
    # memorised and keep working; scrolls carry their own magic.
    if (DB_CONNECTION is not None and cursor is not None
            and not is_npc_attack and not is_npc_vs_npc and not is_scroll
            and sp_level > 0 and _needs_spellbook(cursor) and not _has_spellbook_item(cursor)):
        return {
            "success": False,
            "error": f"Cannot cast {spell_name}: the spellbook is missing.",
            "spell_name": spell_name,
            "hint": "The character's spellbook is not in their inventory. "
                    "Recover it before casting leveled spells.",
        }

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
            return {
                "success": False,
                "error": f"No level {effective_slot} spell slots remaining to cast {spell_name}.",
                "spell_name": spell_name,
                "slot_level_needed": effective_slot,
                "available_slots": available_slots if available_slots else "No spell slots available.",
                "hint": "Take a long rest to recover spell slots, or cast using a higher-level slot by providing slot_level.",
            }

        cursor.execute("SELECT value FROM player WHERE key = ?", ("spellcasting",))
        sc_check = cursor.fetchone()
        if sc_check:
            sc_data_check = json.loads(sc_check[0])
            str_effective_slot = str(effective_slot)
            if str_effective_slot not in sc_data_check.get("slots", {}):
                cursor.execute("SELECT key FROM player")
                return {
                    "success": False,
                    "error": f"Player has no level {effective_slot} spell slots. Maximum available slot level may be insufficient for this spell.",
                    "spell_name": spell_name,
                    "slot_level_needed": effective_slot,
                    "available_slots": {f"lv{k}": v for k, v in sc_data_check.get("slots", {}).items() if int(v) > 0} if sc_data_check.get("slots") else "No spell slots.",
                    "hint": f"This character does not have level {effective_slot} spell slots available.",
                }

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
                    stats_dict = _db_val(cursor, "stats", {})
                    if isinstance(stats_dict, str):
                        stats_dict = json.loads(stats_dict)
                    ability_mod = (int(stats_dict.get(stat_key, 10)) - 10) // 2
                    dc = 10 + effective_slot
                    scroll_d20 = random.randint(1, 20)
                    scroll_total = scroll_d20 + ability_mod
                    if scroll_total < dc:
                        return {
                            "success": False,
                            "error": f"Scroll ability check failed for {spell_name}. "
                                     f"Check: {scroll_total} vs DC {dc} ({scroll_d20} + {ability_mod}). "
                                     "The scroll's magic fizzles and is wasted.",
                            "spell_name": spell_name,
                            "scroll_level": effective_slot,
                            "scroll_check": {"d20": scroll_d20, "ability_modifier": ability_mod,
                                             "total": scroll_total, "dc": dc, "passed": False},
                            "hint": "The scroll is consumed but the spell does not take effect. Remove it from inventory."
                        }
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
            f"{actor} {spell_name} Attack{adv_label}: {die_label}{total_attack} vs AC {target_ac} ({outcome}) ({d20} + {spell_attack_modifier})"
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
                        f"{actor} {spell_name} Miss Damage: {miss_damage} ({' + '.join(str(r) for r in miss_rolls)})"
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

    if sp_healing and not is_npc_attack and not is_npc_vs_npc and sp_attack_type == "automatic":
        heal_die_size, heal_rolls, heal_raw = _parse_and_roll_dice(final_dice)
        if heal_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation: '{final_dice}'. Use format 'XdY' (e.g., '2d6')."}
        ability_bonus = 0
        if spell and spell.get("add_spellcasting_mod"):
            ability_bonus = _caster_spellcasting_mod(cursor)
        heal_mod = final_mod + ability_bonus
        total_healing = heal_raw + heal_mod

        if sp_flat_healing and total_healing == 0:
            heal_narrative = f"{actor} {spell_name} Healing: full HP restore"
            narrative_parts.append(heal_narrative)
            result["healing_total"] = "full"

            if targets and len(targets) > 0:
                healed_list = []
                for t in targets:
                    tname = t.get("name", "Unknown")
                    is_player = t.get("is_player", False)
                    tchp = t.get("current_hp")
                    if tchp is None:
                        tchp = _registry_hp(tname) or 0
                    max_hp = _registry_max_hp(tname) or tchp
                    delta = max_hp - tchp
                    if is_player and cursor:
                        hp_result = _apply_hp_change(cursor, delta) if delta > 0 else None
                        healed_list.append({"name": tname, "healing": delta, "hp_change": hp_result})
                        narrative_parts.append(f"{tname} HP: {hp_result['hp_status']}" if hp_result else f"{tname} HP: already full ({max_hp}/{max_hp})")
                    elif not is_player and tname:
                        _registry_update_hp(tname, max_hp)
                        healed_list.append({"name": tname, "healing": delta, "remaining_hp": max_hp, "max_hp": max_hp})
                        narrative_parts.append(f"{tname} HP: {max_hp}/{max_hp} (fully restored)")
                result["targets_healed"] = healed_list
            elif target_name:
                registry_hp = _registry_hp(target_name)
                if registry_hp is not None:
                    max_hp = _registry_max_hp(target_name) or registry_hp
                    new_hp = max_hp
                    _registry_update_hp(target_name, new_hp)
                    result["target_healed"] = {"name": target_name, "healing": new_hp - registry_hp, "remaining_hp": new_hp, "max_hp": max_hp}
                    narrative_parts.append(f"{target_name} HP: {new_hp}/{max_hp} (fully restored)")
                else:
                    full_hp = int(_db_val(cursor, "total_hit_points", 0))
                    hp_result = _apply_hp_change(cursor, full_hp - int(_db_val(cursor, "current_hit_points", 0))) if cursor else None
                    if hp_result:
                        result["hp_change"] = hp_result
            else:
                full_hp = int(_db_val(cursor, "total_hit_points", 0))
                current_hp = int(_db_val(cursor, "current_hit_points", 0))
                delta = full_hp - current_hp
                hp_result = _apply_hp_change(cursor, delta) if cursor and delta > 0 else None
                if hp_result:
                    result["hp_change"] = hp_result
                    narrative_parts.append(f"HP: {hp_result['hp_status']}")

            result["healing_rolls"] = []
            result["damage_type"] = sp_damage_type
            return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

        if heal_rolls:
            heal_rolls_str = " + ".join(str(r) for r in heal_rolls)
            if heal_mod != 0:
                heal_narrative = f"{actor} {spell_name} Healing: {total_healing} ({heal_rolls_str} + {heal_mod})"
            else:
                heal_narrative = f"{actor} {spell_name} Healing: {total_healing} ({heal_rolls_str})"
        else:
            heal_narrative = f"{actor} {spell_name} Healing: {total_healing}"
        narrative_parts.append(heal_narrative)
        result["healing_total"] = total_healing
        result["healing_rolls"] = heal_rolls
        result["damage_type"] = sp_damage_type

        if targets and len(targets) > 0:
            healed_list = []
            for t in targets:
                tname = t.get("name", "Unknown")
                is_player = t.get("is_player", False)
                tchp = t.get("current_hp")
                if tchp is None:
                    tchp = _registry_hp(tname) or 0
                max_hp = _registry_max_hp(tname) or tchp
                new_hp = min(tchp + total_healing, max_hp)
                if is_player and cursor:
                    delta = new_hp - tchp
                    hp_result = _apply_hp_change(cursor, delta)
                    healed_list.append({"name": tname, "healing": delta, "hp_change": hp_result})
                    narrative_parts.append(f"{tname} HP: {hp_result['hp_status']}")
                elif not is_player and tname:
                    _registry_update_hp(tname, new_hp)
                    healed_list.append({"name": tname, "healing": new_hp - tchp, "remaining_hp": new_hp, "max_hp": max_hp})
                    narrative_parts.append(f"{tname} HP: {new_hp}/{max_hp}")
            result["targets_healed"] = healed_list
        elif target_name:
            registry_hp = _registry_hp(target_name)
            if registry_hp is not None:
                max_hp = _registry_max_hp(target_name) or registry_hp
                new_hp = min(registry_hp + total_healing, max_hp)
                _registry_update_hp(target_name, new_hp)
                result["target_healed"] = {"name": target_name, "healing": new_hp - registry_hp, "remaining_hp": new_hp, "max_hp": max_hp}
                narrative_parts.append(f"{target_name} HP: {new_hp}/{max_hp}")
            else:
                hp_result = _apply_hp_change(cursor, total_healing) if cursor else None
                if hp_result:
                    result["hp_change"] = hp_result
        else:
            hp_result = _apply_hp_change(cursor, total_healing) if cursor else None
            if hp_result:
                result["hp_change"] = hp_result
        return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    # ── HP POOL (Sleep, Color Spray) ──
    if sp_hp_pool and not sp_healing:
        pool_die_size, pool_rolls, pool_raw = _parse_and_roll_dice(final_dice)
        if pool_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation for HP pool: '{final_dice}'."}
        hp_pool_total = pool_raw + final_mod

        pool_rolls_str = " + ".join(str(r) for r in pool_rolls)
        pool_narrative = f"{actor} {spell_name} HP Pool: {hp_pool_total}"
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
                    affected.append({"name": name})
                    remaining_pool -= chp
                else:
                    unaffected.append({"name": name})
            result["targets_affected"] = affected
            result["targets_unaffected"] = unaffected
            result["hp_pool_remaining"] = remaining_pool

            for t in affected:
                narrative_parts.append(f"{t['name']}: Affected — {sp_condition} ({sp_condition_duration})")
            for t in unaffected:
                narrative_parts.append(f"{t['name']}: Unaffected — HP exceeds remaining pool ({remaining_pool})")
        elif sp_condition:
            narrative_parts.append(f"Condition: {sp_condition} ({sp_condition_duration})")

        return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    # ── ROLL DAMAGE ──
    if not sp_no_damage:
        primary_die_size, primary_rolls, primary_sum = _parse_and_roll_dice(final_dice)
        if primary_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation: '{final_dice}'. Use format 'XdY' (e.g., '2d6')."}

        label = "Healing" if sp_healing else "Damage"

        if is_crit:
            crit_rolls = [random.randint(1, primary_die_size) for _ in range(len(primary_rolls))]
            primary_damage = primary_sum + sum(crit_rolls) + final_mod
            crit_rolls_str = " + ".join(str(r) for r in crit_rolls)
            if primary_rolls:
                base_str = " + ".join(str(r) for r in primary_rolls)
                if final_mod != 0:
                    narrative_parts.append(
                        f"{actor} {spell_name} {label}: {primary_damage} ({final_dice}: {base_str} + {crit_rolls_str}, {final_mod:+d}) [CRIT]"
                    )
                else:
                    narrative_parts.append(
                        f"{actor} {spell_name} {label}: {primary_damage} ({final_dice}: {base_str} + {crit_rolls_str}) [CRIT]"
                    )
            else:
                narrative_parts.append(
                    f"{actor} {spell_name} {label}: {primary_damage} (flat + {crit_rolls_str} + {final_mod}) [CRIT]"
                )
            result["crit_damage_rolls"] = crit_rolls
        else:
            primary_damage = primary_sum + final_mod
            if primary_rolls:
                base_str = " + ".join(str(r) for r in primary_rolls)
                if final_mod != 0:
                    narrative_parts.append(f"{actor} {spell_name} {label}: {primary_damage} ({final_dice}: {base_str}, {final_mod:+d})")
                else:
                    narrative_parts.append(f"{actor} {spell_name} {label}: {primary_damage} ({final_dice}: {base_str})")
            else:
                narrative_parts.append(f"{actor} {spell_name} {label}: {primary_damage}")

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
                f"{actor} {spell_name} {ext_type_label} Damage: {extra_damage} ({extra_base_str})"
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

        for t in targets:
            tname = t.get("name", "Unknown")
            tchp = t.get("current_hp")
            if tchp is None:
                tchp = _registry_hp(tname) or 0
            tsave = t.get("save_modifier")
            if tsave is None:
                tsave = _registry_save(tname, sp_save_type)
            if tsave is None:
                tsave = 0
            tcr = t.get("challenge_rating")
            if tcr is None:
                tcr = _registry_cr(tname)
            is_player = t.get("is_player", False)

            save_disadvantage, save_sources = _encumbrance(cursor, is_player, sp_save_type)
            cond_save_dis, auto_fail, cond_save_sources = _condition_save_effects(
                _conditions_for(cursor, tname, is_player=is_player), sp_save_type)
            save_disadvantage = save_disadvantage or cond_save_dis
            save_sources = [*save_sources, *cond_save_sources]
            save_d20, save_rolls, save_mode, _save_cancelled = _roll_d20(disadvantage=save_disadvantage)
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

            # SRD resistances/immunities/vulnerabilities, after any save halving.
            if not sp_healing and not is_player and t_damage > 0:
                t_damage, dmg_note = _apply_damage_modifiers(
                    _COMBAT_REGISTRY.get(tname), t_damage, sp_damage_type)
                if dmg_note:
                    narrative_parts.append(f"{tname} damage adjusted: {dmg_note}")
            remaining = min(tchp + t_damage, _registry_max_hp(tname) or tchp) if sp_healing else tchp - t_damage
            killed = False if sp_healing else (remaining <= 0 if tchp > 0 else False)

            if not is_player and tname:
                _registry_update_hp(tname, remaining if sp_healing else max(remaining, 0))
            if killed:
                killed_count += 1
                if not is_player:
                    _registry_kill(tname)
                max_hp = _registry_max_hp(tname) if tname else 0
                display_max = f"/{max_hp}" if max_hp else ""
                narrative_parts.append(f"{tname} HP: 0{display_max} (KILLED)")
            elif tchp > 0:
                max_hp = _registry_max_hp(tname) if tname else 0
                display_max = f"/{max_hp}" if max_hp else ""
                narrative_parts.append(f"{tname} HP: {remaining}{display_max}")

            tr = {"name": tname, "save_roll": save_d20, "save_modifier": tsave,
                   "save_total": save_total, "save_success": save_success,
                   "damage": t_damage, "remaining_hp": max(0, remaining), "killed": killed}
            if auto_fail:
                tr["save_auto_failed"] = True
            if save_rolls:
                tr[f"save_{save_mode}_rolls"] = save_rolls
            if save_sources:
                tr["disadvantage_sources"] = list(save_sources)

            if is_player and t_damage > 0 and cursor:
                hp_result = _apply_hp_change(cursor, -t_damage)
                tr["hp_change"] = hp_result
                if tname:
                    narrative_parts.append(f"{tname} HP: {hp_result['hp_status']}")

            if killed and tcr is not None:
                xp = CR_XP_TABLE.get(tcr, 0)
                if xp > 0:
                    total_xp += xp
                    tr["xp_awarded"] = xp

            target_results.append(tr)

        result["targets"] = target_results
        if not sp_healing:
            result["killed_count"] = killed_count

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
        if is_npc_attack and player_save_modifier is not None:
            save_mod = player_save_modifier
        elif target_save_modifier is not None:
            save_mod = target_save_modifier
        else:
            save_mod = _registry_save(target_name or actor, sp_save_type)
            if save_mod is None:
                save_mod = 0
        saver_name = target_name or actor
        # The player is the one saving when an NPC casts on them.
        save_disadvantage, save_sources = _encumbrance(cursor, is_npc_attack, sp_save_type)
        cond_save_dis, auto_fail, cond_save_sources = _condition_save_effects(
            _conditions_for(cursor, saver_name, is_player=is_npc_attack), sp_save_type)
        save_disadvantage = save_disadvantage or cond_save_dis
        save_sources = [*save_sources, *cond_save_sources]
        save_d20, save_rolls, save_mode, _save_cancelled = _roll_d20(disadvantage=save_disadvantage)
        save_total = save_d20 + save_mod
        save_success = save_total >= spell_save_dc and not auto_fail
        if auto_fail:
            save_outcome = "Failure"

        result["save_roll"] = save_d20
        result["save_modifier"] = save_mod
        result["save_total"] = save_total
        result["save_dc"] = spell_save_dc
        result["save_success"] = save_success
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
        hp_result = _apply_hp_change(cursor, -total_damage)
        result["hp_change"] = hp_result
    elif is_npc_attack and sp_healing:
        hp_result = _apply_hp_change(cursor, total_damage)
        result["hp_change"] = hp_result
        narrative_parts.append(f"Healed {target_name or 'Player'}: {hp_result['hp_status']}")
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
                if target_name:
                    max_hp = _registry_max_hp(target_name)
                    display_max = f"/{max_hp}" if max_hp else ""
                    narrative_parts.append(f"{target_name} HP: {target_remaining}{display_max}")
        else:
            result["target_killed"] = None
        result["npc_vs_npc"] = True
    elif is_npc_vs_npc and sp_healing:
        result["npc_vs_npc"] = True
        result["target_killed"] = None
        registry_hp = _registry_hp(target_name) if target_name else None
        if registry_hp is not None:
            max_hp = _registry_max_hp(target_name) or registry_hp
            new_hp = min(registry_hp + total_damage, max_hp)
            _registry_update_hp(target_name, new_hp)
            result["target_healed"] = {"name": target_name, "healing": new_hp - registry_hp, "remaining_hp": new_hp, "max_hp": max_hp}
            narrative_parts.append(f"{actor} heals {target_name} for {new_hp - registry_hp} HP ({new_hp}/{max_hp})")
        else:
            narrative_parts.append(f"{actor} heals {target_name or 'target'} for {total_damage} HP.")
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
                if target_name:
                    if db_target and result.get("hp_change"):
                        narrative_parts.append(f"{target_name} HP: {result['hp_change']['hp_status']}")
                    else:
                        max_hp = _registry_max_hp(target_name)
                        display_max = f"/{max_hp}" if max_hp else ""
                        narrative_parts.append(f"{target_name} HP: {target_remaining}{display_max}")

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
        registry_hp = _registry_hp(target_name) if target_name else None
        if registry_hp is not None:
            max_hp = _registry_max_hp(target_name) or registry_hp
            new_hp = min(registry_hp + total_damage, max_hp)
            _registry_update_hp(target_name, new_hp)
            result["target_healed"] = {"name": target_name, "healing": new_hp - registry_hp, "remaining_hp": new_hp, "max_hp": max_hp}
            narrative_parts.append(f"{target_name} HP: {new_hp}/{max_hp}")
        else:
            hp_result = _apply_hp_change(cursor, total_damage) if cursor else None
            if hp_result:
                result["hp_change"] = hp_result
                narrative_parts.append(f"Healed {target_name}: {hp_result['hp_status']}")
    else:
        result["target_killed"] = None
        if is_npc_vs_npc:
            result["npc_vs_npc"] = True

    return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        player_file = sys.argv[1]
        init_status = init_player_db(player_file)
        print(f"Server DB Init: {init_status}", file=sys.stderr)

    mcp.run(transport="stdio")
