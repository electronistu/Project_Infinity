import random
import sys
import json
import sqlite3
import os
import re
from mcp.server.fastmcp import FastMCP
from level_up import apply_level_up

try:
    import yaml
except ImportError:
    yaml = None

mcp = FastMCP("InfinityRolls", log_level="WARNING")

DB_CONNECTION = None

XP_THRESHOLDS = [
    (2, 300), (3, 900), (4, 2700), (5, 6500),
    (6, 14000), (7, 23000), (8, 33000), (9, 48000),
    (10, 64000), (11, 85000), (12, 100000), (13, 120000),
    (14, 145000), (15, 175000), (16, 210000), (17, 255000),
    (18, 305000), (19, 360000), (20, 400000),
]

KNOWN_CASTER_CLASSES = {"Bard", "Sorcerer", "Warlock", "Ranger"}
PREPARED_CASTER_CLASSES = {"Cleric", "Druid", "Paladin", "Artificer"}

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
    with open(config_path, "r") as f:
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
        with open(player_file_path, 'r') as f:
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
        parts = dice_notation.lower().split('d')
        if len(parts) != 2:
            return None, [], 0
        num_dice = int(parts[0]) if parts[0] else 1
        die_size = int(parts[1])
        if num_dice <= 0 or die_size <= 0:
            return None, [], 0
        rolls = [random.randint(1, die_size) for _ in range(num_dice)]
        return die_size, rolls, sum(rolls)
    except (ValueError, TypeError):
        return None, [], 0


def _apply_hp_change(cursor, delta):
    current_hp = int(_db_val(cursor, "current_hit_points", 0))
    total_hp = int(_db_val(cursor, "total_hit_points", 1))
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

    return result


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
    "Artificer": "int",
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
    Increments or decrements a numeric player attribute. Supports dotted notation for nested attributes.

    KEY RULES (enforced by this tool):
    - current_hit_points: Automatically clamped to [0, total_hit_points]. Returns an HP status tag.
      When HP reaches 0, the response includes "status: Unconscious" and a death_saves flag.
    - spellcasting.slots.N: Validates the slot exists and has uses remaining before decrementing.
      Returns an error with available slots if the slot is empty.
    - consumables.ITEM: Auto-creates the entry at 0 if missing. At 0 quantity, the item is removed
      and a DEPLETION message is returned. NEGATIVE values are NOT allowed — the tool clamps to 0.
    - xp: When XP crosses a D&D 5E level threshold, ALL numeric level-up changes are applied
      automatically: level, proficiency_bonus, hit_dice_count, total_hit_points (rolled hit dice + CON mod),
      and spellcasting (slots, dc, attack_modifier). The response includes a full change summary.
      You MUST still manually apply: new class features, new cantrips/spells known, ability score
      improvements (levels 4/8/12/16/19), and subclass progression via update_player_list.
    - IMPORTANT: You MUST award XP for any creatures the player kills and on quest completion.
      Use key='xp' with the appropriate positive delta.

    DOTTED PATH EXAMPLES:
    - Top-level: modify_player_numeric(key='gold', delta=-10)
    - Nested slot: modify_player_numeric(key='spellcasting.slots.1', delta=-1)
    - Consumable: modify_player_numeric(key='consumables.Bolts', delta=-1)
    - Add consumable: modify_player_numeric(key='consumables.Arrows', delta=20)

    KEY REFERENCE (common keys):
    current_hit_points, total_hit_points, armor_class, gold, xp, level,
    proficiency_bonus, hit_dice_count, hit_dice_size, speed,
    spellcasting.slots.1, spellcasting.slots.2, ... spellcasting.slots.9,
    consumables.Bolts, consumables.Arrows, consumables.Health Potion, etc.
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized.", "key": key}
    try:
        cursor = DB_CONNECTION.cursor()

        if key == "current_hit_points":
            return _apply_hp_change(cursor, delta)

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

                    total_hp = int(changes.get('total_hit_points', new_val))

                    result["level_up"] = True
                    result["old_level"] = current_level
                    result["new_level"] = new_level
                    result["level_up_changes"] = summary
                    result["level_up_summary"] = f"LEVEL UP! Level {current_level} → {new_level}. " + "; ".join(summary)
                    result["hp_status"] = _format_hp_status(
                        int(changes.get('total_hit_points', total_hp)),
                        int(changes.get('total_hit_points', total_hp))
                    )
                    result["message"] = (
                        f"LEVEL UP! Level {current_level} → {new_level}. "
                        f"Changes: {', '.join(summary)}. "
                        f"You MUST still apply manually: class features, cantrips/spells, "
                        f"ability score improvements (at levels 4/8/12/16/19), and subclass features."
                    )
        return result
    except Exception as e:
        return {"success": False, "error": f"Error modifying numeric value: {str(e)}", "key": key}


@mcp.tool()
def update_player_list(key: str, item: str, action: str) -> dict:
    """
    Adds or removes an item from a player list.

    SPELL CASTING RULES (enforced by this tool):
    - Known casters (Bard, Sorcerer, Warlock, Ranger): use 'spellcasting.spells_known'.
    - Prepared casters (Cleric, Druid, Paladin, Artificer): use 'spellcasting.spells_prepared'.
      Capacity enforced: max = spellcasting_ability_modifier + character_level.
      At capacity, the tool rejects the add and returns the current spell list.
    - Wizards: add to 'spellcasting.spellbook' for the reference pool, then prepare via 'spellcasting.spells_prepared'.
    - If a player tries to cast a spell not on their castable list, DO NOT resolve it mechanically.
      Narrate that the spell is not known/prepared and list available castable spells.

    CONSUMABLES: NEVER use this tool for consumable quantity changes.
    Use modify_player_numeric with key='consumables.ITEM' instead.

    VALID KEYS:
    inventory, spellcasting.spells_known, spellcasting.spells_prepared,
    spellcasting.spellbook, spellcasting.cantrips, skills, features,
    languages, saves, armor_proficiencies, weapon_proficiencies, tool_proficiencies,
    active_effects

    FORMAT: 'Item Name: Description' (description optional)

    EXAMPLES:
    - update_player_list(key='inventory', item='Dagger: A rusty blade (1d4 piercing, Finesse, Light, Thrown (range 20/60))', action='add')
    - update_player_list(key='inventory', item='Health Potion', action='add')
    - update_player_list(key='spellcasting.spells_known', item='Shield', action='remove')
    - update_player_list(key='spellcasting.spells_prepared', item='Fireball', action='add')

    action: 'add' or 'remove'
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

        if action == "add":
            name = item
            desc = ""
            if ":" in item:
                name, desc = [p.strip() for p in item.split(":", 1)]

            new_entry = {"name": name, "description": desc} if (desc or ":" in item) else name

            exists = any((isinstance(e, dict) and e.get("name") == name) or e == name for e in current_list)
            if exists:
                result = {"success": False, "error": "already_exists", "key": key, "item": name, "action": action}
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
                return {"success": False, "error": "not_found", "key": key, "item": item, "action": action}

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

        return result
    except Exception as e:
        return {"success": False, "error": f"Error updating list: {str(e)}", "key": key}


@mcp.tool()
def dump_player_db() -> dict:
    """
    Returns a full dump of the current in-memory player database.
    Use this tool to refresh your understanding of the player's stats, inventory, spell slots, and condition.
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

        return result
    except Exception as e:
        return {"error": f"Error dumping database: {str(e)}"}


@mcp.tool()
def roll_dice(dice_notation: str, modifier: int = 0, actor: str = "{player_name}") -> dict:
    """
    Rolls dice based on standard notation.

    DECISION GUIDE: Use this tool ONLY for "How much X?" scenarios — damage, healing, loot quantity,
    initiative, or any situation where you need a random magnitude. Do NOT use this for success/failure
    checks; use 'perform_check' instead.

    RULES:
    - dice_notation must ONLY contain the dice (e.g., '3d4'). Do NOT include modifiers like '+3' in the string.
    - All bonuses or penalties MUST go in the modifier parameter.
    - The actor parameter MUST identify who is rolling: use the player's character name for the player,
      and the NPC/creature's name for NPCs (e.g., 'Goblin Brute', 'Guard Captain').
    - NEVER use the player's name for NPC actions, and NEVER use an NPC's name for the player's actions.

    Correct: roll_dice(actor='Senna', dice_notation='3d4', modifier=3)
    Correct: roll_dice(actor='Goblin Brute', dice_notation='1d6', modifier=2)
    Incorrect: roll_dice(dice_notation='3d4+3', modifier=0)

    The response includes a 'narrative_format' field with a pre-formatted string suitable for direct
    inclusion in your narrative output. Use this format when disclosing roll results to the player.
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
def perform_check(modifier: int, dc: int, check_name: str = "Check", actor: str = "{player_name}") -> dict:
    """
    Performs a D&D 5E difficulty check (d20 roll + modifier vs DC).

    DECISION GUIDE: For weapon/unarmed attacks, use 'resolve_attack' instead — it handles attack rolls,
    damage, crits, HP application, kill detection, and XP award in a single call.
    Use perform_check for skill checks, saving throws, and any other binary success/failure roll
    that is NOT an attack.

    RULES:
    - The actor parameter MUST identify who is performing the action: use the player's character name
      for the player, and the NPC/creature's name for NPCs (e.g., 'Senna', 'Guard Captain').
    - NEVER use the player's name for NPC actions, and NEVER use an NPC's name for the player's actions.

    The response includes a 'narrative_format' field with a pre-formatted string suitable for direct
    inclusion in your narrative output. Use this format when disclosing check results to the player.
    Every check result MUST appear in your narrative using this exact format.

    Examples:
    - perform_check(actor='Thorin', modifier=5, dc=15, check_name='Athletics')
    - perform_check(actor='Guard Captain', modifier=2, dc=13, check_name='Perception')
    - perform_check(actor='Senna', modifier=1, dc=13, check_name='Deception')
    """
    if actor == "{player_name}" and DB_CONNECTION is not None:
        actor = _db_val(DB_CONNECTION.cursor(), "name", "Player")

    roll = random.randint(1, 20)
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

    return response


@mcp.tool()
def resolve_attack(
    actor: str,
    attack_modifier: int,
    target_ac: int,
    damage_dice: str,
    damage_modifier: int = 0,
    target_name: str = "",
    target_current_hp: int | None = None,
    challenge_rating: float | None = None,
    extra_damage_dice: str = "",
    extra_damage_modifier: int = 0,
    is_npc_attack: bool = False,
    is_npc_vs_npc: bool = False,
    advantage: bool = False,
    force_crit: bool = False,
) -> dict:
    """
    Resolves a complete D&D 5E attack in one call: attack roll, damage roll,
    HP application (NPC attacks on the player), kill detection, and XP award.

    Use this for ALL weapon and unarmed attacks — player vs NPC, NPC vs player,
    and NPC vs NPC. Do NOT use this for spell attacks; use resolve_magic
    for spells instead.

    CRITICAL HITS: On a natural 20, the primary damage dice are doubled automatically.
    Extra damage dice are NOT doubled (representing elemental/sneak-attack bonus damage
    that doesn't crit). If you want all dice doubled, put everything in damage_dice.

    NPC ATTACKING PLAYER: Set is_npc_attack=True. The tool applies damage to the
    player's HP automatically and returns the updated HP status. target_ac should be
    the player's current AC.

    NPC VS NPC: Set is_npc_vs_npc=True when one NPC attacks another NPC (e.g. a town
    guard attacking a goblin). No player HP is modified, no XP is auto-awarded to the
    player, and the GM must decide if any XP should be awarded. Kill detection still
    works — if target_current_hp is provided and damage reduces the target to 0 or below,
    the response includes target_killed=True. Do NOT set both is_npc_attack and
    is_npc_vs_npc to True.

    ADVANTAGE / FORCED CRIT (D&D 5e RAW):
    - advantage=True: Rolls two d20s and uses the higher result (advantage).
    - force_crit=True: Any successful hit (not a natural 1) is treated as a Critical
      Success. Use this for unconscious targets within 5 feet, paralyzed targets, etc.
      Natural 1 is still a Critical Failure. Natural 20 is still a natural crit.
    - Both can be combined (e.g. advantage + force_crit for an unconscious target).

    KILL DETECTION: If target_current_hp is provided and damage reduces the target
    to 0 or below, the response includes target_killed=True.

    XP AWARD: If challenge_rating is provided and the target is killed, XP is
    automatically awarded to the player using the D&D 5E CR/XP table. This only
    happens when is_npc_vs_npc=False — NPC-vs-NPC kills do NOT auto-award XP to the
    player. The GM may choose to award XP manually via modify_player_numeric(key='xp')
    if appropriate.

    CR TABLE (for reference):
      CR 0=10, 1/8=25, 1/4=50, 1/2=100, 1=200, 2=450, 3=700, 4=1100,
      5=1800, 6=2300, 7=2900, 8=3900, 9=5000, 10=5900, 11=7200, 12=8400,
      13=10000, 14=11500, 15=13000, 16=15000, 17=18000, 18=20000, 19=22000,
      20=25000

    EXAMPLES:
    # Player attacks goblin with longsword
    resolve_attack(actor='{player_name}', attack_modifier=4, target_ac=13,
                   damage_dice='1d8', damage_modifier=2, target_name='Goblin',
                   target_current_hp=12, challenge_rating=0.5)

    # Goblin attacks player
    resolve_attack(actor='Goblin', attack_modifier=4, target_ac=13,
                   damage_dice='1d6', damage_modifier=2, target_name='{player_name}',
                   is_npc_attack=True)

    # Player attacks with a Flaming Dagger (1d4+3 piercing + 1d6 fire)
    resolve_attack(actor='{player_name}', attack_modifier=5, target_ac=15,
                   damage_dice='1d4', damage_modifier=3,
                   extra_damage_dice='1d6', extra_damage_modifier=0,
                   target_name='Orc Brute', target_current_hp=25,
                   challenge_rating=0.5)

    # Town guard attacks a goblin (NPC vs NPC, no XP for player)
    resolve_attack(actor='Town Guard', attack_modifier=4, target_ac=13,
                   damage_dice='1d8', damage_modifier=2, target_name='Goblin',
                   target_current_hp=12, is_npc_vs_npc=True)
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}

    try:
        cursor = DB_CONNECTION.cursor()

        if actor == "{player_name}" and DB_CONNECTION is not None:
            actor = _db_val(cursor, "name", "Player")

        if advantage:
            d20_1 = random.randint(1, 20)
            d20_2 = random.randint(1, 20)
            d20 = max(d20_1, d20_2)
            die_label = f"{min(d20_1, d20_2)} / {max(d20_1, d20_2)} → "
        else:
            d20 = random.randint(1, 20)
            die_label = ""

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

        adv_label = " (Advantage)" if advantage else ""
        narrative_parts = []
        narrative_parts.append(
            f"{actor} Attack{adv_label}: {die_label}{total_attack} vs AC {target_ac} ({outcome}) ({d20} + {attack_modifier})"
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
        if advantage:
            result["advantage_rolls"] = [d20_1, d20_2]
        if is_forced_crit:
            result["forced_crit"] = True

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

        result["damage_total"] = total_damage
        result["primary_damage"] = primary_damage
        result["primary_damage_rolls"] = primary_rolls
        result["damage_modifier"] = damage_modifier
        result["primary_die_size"] = primary_die_size

        if is_npc_attack and total_damage > 0:
            hp_result = _apply_hp_change(cursor, -total_damage)
            result["hp_change"] = hp_result
            target_remaining = hp_result["new_value"]
        elif is_npc_vs_npc:
            result["npc_vs_npc"] = True
            if target_current_hp is not None and total_damage > 0:
                target_remaining = target_current_hp - total_damage
                result["target_remaining_hp"] = target_remaining

                if target_remaining <= 0:
                    result["target_killed"] = True
                    if target_name:
                        narrative_parts.append(f"{target_name} HP: 0 (KILLED)")
                else:
                    result["target_killed"] = False
                    if target_name:
                        narrative_parts.append(f"{target_name} HP: {target_remaining}/{target_current_hp}")
            else:
                result["target_killed"] = None
        elif not is_npc_attack and not is_npc_vs_npc and target_current_hp is not None:
            target_remaining = target_current_hp - total_damage
            result["target_remaining_hp"] = target_remaining

            if target_remaining <= 0:
                result["target_killed"] = True
                if target_name:
                    narrative_parts.append(f"{target_name} HP: 0 (KILLED)")
            else:
                result["target_killed"] = False
                if target_name:
                    narrative_parts.append(f"{target_name} HP: {target_remaining}/{target_current_hp}")

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


@mcp.tool()
def resolve_magic(
    spell_name: str,
    actor: str = "{player_name}",
    spell_attack_modifier: int = 0,
    spell_save_dc: int = 0,
    target_ac: int | None = None,
    target_name: str = "",
    target_current_hp: int | None = None,
    challenge_rating: float | None = None,
    target_save_modifier: int = 0,
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
) -> dict:
    """
    Resolves a complete D&D 5E spell attack in one call. Supports attack roll spells,
    saving throw spells, and automatic-hit spells. Lookups spell properties from the
    spells config, or uses custom override params if the spell is not found.

    SPELL LOOKUP: The function first checks the spell database (config/spells.yml).
    If found, all spell properties (attack_type, damage_dice, save_type, etc.) come from
    the database. If NOT found, you MUST provide at minimum attack_type and damage_dice
    as custom override parameters.

    SPELL SLOT MANAGEMENT (AUTOMATIC):
    - Cantrips (level 0): No spell slot consumed. Always castable.
    - Leveled spells (level 1+): A spell slot of the appropriate level is consumed
      automatically from the player's spellcasting profile. If the player has no slots
      remaining at that level, the function returns an error with available slots —
      NO dice are rolled, NO damage is dealt, NO spell effect is applied.
    - The effective slot level is determined by:
      1. The slot_level parameter, if provided (for upcasting, e.g. Fireball in a 5th-level slot).
      2. The spell's native level from the database, if slot_level is not provided.
      3. For custom spells not in the database: slot_level MUST be provided for leveled spells,
         or the function returns an error.
    - If slot_level is higher than the spell's native level, the spell is upcast and damage
      scales automatically (per the spell's higher_levels field in spells.yml).
    - ritual=True: Casts the spell as a ritual. No spell slot is consumed. Only valid for spells
      with the Ritual tag in D&D 5E. The GM is responsible for ensuring the spell can be cast
      as a ritual.
    - is_scroll=True: Casts the spell from a scroll. Never consumes a spell slot — the scroll
      provides the magic. For scrolls of a level the player CANNOT cast (no slot of that level),
      resolves a scroll ability check (d20 + spellcasting modifier vs DC 10 + spell level)
      per D&D 5e (DMG p.200). On failure, the scroll is wasted. On success, the spell resolves.
      Cantrips always pass. The GM must remove the scroll from inventory after use.
    - NPC attacks (is_npc_attack=True or is_npc_vs_npc=True): No spell slot is deducted.

    ATTACK TYPES:
    - "attack_roll": d20 + spell_attack_modifier vs target_ac. Natural 20 = crit (double base dice).
    - "saving_throw": Target rolls d20 + save_modifier vs spell_save_dc.
      On fail: full damage. On success: half damage (if save_half=true) or no damage.
    - "automatic": Always hits. Just roll damage. No attack roll or save needed.

    ADVANTAGE / FORCED CRIT (D&D 5e RAW):
    - advantage=True: Rolls two d20s for attack_roll spells and uses the higher result.
    - force_crit=True: Any successful hit (not a natural 1) on an attack_roll spell is
      treated as a Critical Success. Use for unconscious targets within 5 feet, etc.
      Natural 1 is still a Critical Failure. Natural 20 is still a natural crit.
    - These only apply to "attack_roll" spells. They are silently ignored for
      "saving_throw" and "automatic" spells.

    CRITICAL HITS: Only apply to "attack_roll" type spells. On a natural 20, the base
    damage dice are doubled. Extra damage dice (from spell properties) are NOT doubled.
    A forced_crit functions identically to a natural 20 for damage purposes.

    CANTRIP SCALING: Cantrips (level 0) scale automatically based on character level:
      Levels 1-4: base dice
      Levels 5-10: base dice x2
      Levels 11-16: base dice x3
      Levels 17+: base dice x4
    For NPC-vs-NPC combat, provide caster_level to control cantrip scaling. Without it,
    cantrips use base damage (no scaling).

    HEALING SPELLS: Set healing=true. Damage dice heal the target instead of dealing damage.
    For player healing (is_npc_attack=False, target is ally), positive values restore HP.
    Healing spells still consume a spell slot.

    NPC ATTACKING PLAYER: Set is_npc_attack=True. Damage is applied to the player's HP
    automatically for damaging spells. For saving throws, use player_save_modifier.
    No spell slot is consumed for NPC attacks.

    NPC VS NPC: Set is_npc_vs_npc=True when one NPC casts a spell on another NPC
    (e.g. an enemy mage casting Fireball at a guard). No player HP is modified, no spell
    slot is consumed, no XP is auto-awarded. For cantrip scaling, provide caster_level
    to control the NPC caster's effective level. Do NOT set both is_npc_attack and
    is_npc_vs_npc to True.

    KILL DETECTION: If target_current_hp is provided and damage reduces the target to 0
    or below, the response includes target_killed=True.

    XP AWARD: If challenge_rating is provided and the target is killed, XP is automatically
    awarded to the player. This only happens for player attacks (is_npc_attack=False,
    is_npc_vs_npc=False). NPC-vs-NPC kills do NOT auto-award XP to the player.

    EXAMPLES:
    # Player casts Fireball (level 3 spell) - slot consumed automatically
    resolve_magic(spell_name='Fireball', actor='{player_name}',
                         spell_save_dc=15,
                         target_name='Goblin Shaman', target_current_hp=24,
                         challenge_rating=1)

    # Player upcasts Fireball using a 5th-level slot (8d6 damage) - 5th-level slot consumed
    resolve_magic(spell_name='Fireball', actor='{player_name}',
                         spell_save_dc=15,
                         target_name='Ogre', target_current_hp=60,
                         challenge_rating=2, slot_level=5)

    # Player casts Fire Bolt (cantrip, no slot consumed)
    resolve_magic(spell_name='Fire Bolt', actor='{player_name}',
                   spell_attack_modifier=6, target_ac=14,
                   target_name='Orc', target_current_hp=18,
                   challenge_rating=0.5)

    # Player casts Detect Magic as a ritual (no slot consumed)
    resolve_magic(spell_name='Detect Magic', actor='{player_name}',
                   attack_type='saving_throw', save_type='wis',
                   spell_save_dc=13, ritual=True)

    # Player casts a custom homebrew spell not in the database
    resolve_magic(spell_name='Void Blast', actor='{player_name}',
                   spell_attack_modifier=7, target_ac=16,
                   attack_type='attack_roll',
                   damage_dice='3d10', damage_type='force',
                   target_name='Shadow Wraith', target_current_hp=40,
                   challenge_rating=4, slot_level=3)

    # NPC casts Magic Missile at the player (no slot consumed)
    resolve_magic(spell_name='Magic Missile', actor='Evil Wizard',
                         is_npc_attack=True,
                         attack_type='automatic',
                         damage_dice='3d4', damage_modifier=3,
                         damage_type='force',
                         target_name='{player_name}')

    # NPC mage casts Fireball at a guard (NPC vs NPC, no slot consumed, no XP)
    resolve_magic(spell_name='Fireball', actor='Dark Wizard',
                         spell_save_dc=15, target_save_modifier=2,
                         target_name='Town Guard', target_current_hp=30,
                         is_npc_vs_npc=True, caster_level=7)

    # NPC casts a cantrip at another NPC (cantrip scaling with caster_level)
    resolve_magic(spell_name='Fire Bolt', actor='Dark Wizard',
                         spell_attack_modifier=6, target_ac=14,
                         target_name='Town Guard', target_current_hp=20,
                         is_npc_vs_npc=True, caster_level=11)

    # Player casts Inflict Wounds on unconscious target (automatic crit within 5 feet)
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
    damage_multiplier = 1.0

    # ── ATTACK ROLL ──
    if sp_attack_type == "attack_roll":
        if advantage:
            d20_1 = random.randint(1, 20)
            d20_2 = random.randint(1, 20)
            d20 = max(d20_1, d20_2)
            die_label = f"{min(d20_1, d20_2)} / {max(d20_1, d20_2)} → "
        else:
            d20 = random.randint(1, 20)
            die_label = ""
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

        adv_label = " (Advantage)" if advantage else ""
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
        if advantage:
            result["advantage_rolls"] = [d20_1, d20_2]
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
        return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    # ── DAMAGE / HEALING CALCULATION ──
    if final_dice in ("0d0", "0", ""):
        total_damage = sp_damage_modifier + final_mod
        result["damage_total"] = total_damage
        result["target_killed"] = None
        return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    if sp_healing and not is_npc_attack and sp_attack_type == "automatic":
        heal_die_size, heal_rolls, heal_raw = _parse_and_roll_dice(final_dice)
        if heal_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation: '{final_dice}'. Use format 'XdY' (e.g., '2d6')."}
        total_healing = heal_raw + final_mod

        heal_rolls_str = " + ".join(str(r) for r in heal_rolls)
        heal_narrative = f"{actor} {spell_name} Healing: {total_healing}"
        if final_mod != 0:
            heal_narrative += f" ({heal_rolls_str} + {final_mod})"
        else:
            heal_narrative += f" ({heal_rolls_str})"
        narrative_parts.append(heal_narrative)

        hp_result = _apply_hp_change(cursor, total_healing) if cursor else None
        result["healing_total"] = total_healing
        result["healing_rolls"] = heal_rolls
        result["damage_type"] = sp_damage_type
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
            narrative_parts.append(f"Condition: {sp_condition} ({sp_condition_duration})")
        return _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc_attack or is_npc_vs_npc, is_npc_vs_npc)

    # ── ROLL DAMAGE ──
    primary_die_size, primary_rolls, primary_sum = _parse_and_roll_dice(final_dice)
    if primary_die_size is None:
        return {"success": False, "error": f"Invalid damage_dice notation: '{final_dice}'. Use format 'XdY' (e.g., '2d6')."}

    if is_crit:
        crit_rolls = [random.randint(1, primary_die_size) for _ in range(len(primary_rolls))]
        primary_damage = sum(primary_rolls) + sum(crit_rolls) + final_mod
        crit_rolls_str = " + ".join(str(r) for r in crit_rolls)
        base_str = " + ".join(str(r) for r in primary_rolls)
        if final_mod != 0:
            narrative_parts.append(
                f"{actor} {spell_name} Damage: {primary_damage} ({base_str} + {crit_rolls_str} + {final_mod}) [CRIT]"
            )
        else:
            narrative_parts.append(
                f"{actor} {spell_name} Damage: {primary_damage} ({base_str} + {crit_rolls_str}) [CRIT]"
            )
        result["crit_damage_rolls"] = crit_rolls
    else:
        primary_damage = sum(primary_rolls) + final_mod
        base_str = " + ".join(str(r) for r in primary_rolls)
        if final_mod != 0:
            narrative_parts.append(f"{actor} {spell_name} Damage: {primary_damage} ({base_str} + {final_mod})")
        else:
            narrative_parts.append(f"{actor} {spell_name} Damage: {primary_damage} ({base_str})")

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

    # ── APPLY SAVE HALF / NO DAMAGE ──
    if sp_attack_type == "saving_throw" and damage_multiplier < 1.0:
        if damage_multiplier == 0.0:
            total_damage = 0
            narrative_parts.append(f"{saver_name} saved — no damage.")
        else:
            total_damage = max(1, total_damage // 2)
            narrative_parts.append(f"{saver_name} saved — half damage: {total_damage}")

    result["damage_total"] = total_damage
    result["damage_type"] = sp_damage_type

    # ── APPLY HP CHANGES ──
    if is_npc_attack and total_damage > 0 and not sp_healing:
        hp_result = _apply_hp_change(cursor, -total_damage)
        result["hp_change"] = hp_result
        target_remaining = hp_result["new_value"]
    elif is_npc_attack and sp_healing:
        hp_result = _apply_hp_change(cursor, total_damage)
        result["hp_change"] = hp_result
        target_remaining = hp_result["new_value"]
        narrative_parts.append(f"Healed {target_name or 'Player'}: {hp_result['hp_status']}")
    elif is_npc_vs_npc and total_damage > 0 and not sp_healing:
        if target_current_hp is not None:
            target_remaining = target_current_hp - total_damage
            result["target_remaining_hp"] = target_remaining

            if target_remaining <= 0:
                result["target_killed"] = True
                if target_name:
                    narrative_parts.append(f"{target_name} HP: 0 (KILLED)")
            else:
                result["target_killed"] = False
                if target_name:
                    narrative_parts.append(f"{target_name} HP: {target_remaining}/{target_current_hp}")
        else:
            result["target_killed"] = None
        result["npc_vs_npc"] = True
    elif is_npc_vs_npc and sp_healing:
        result["npc_vs_npc"] = True
        result["target_killed"] = None
        narrative_parts.append(f"{actor} heals {target_name or 'target'} for {total_damage} HP.")
    elif not is_npc_attack and not is_npc_vs_npc and target_current_hp is not None and not sp_healing:
        target_remaining = target_current_hp - total_damage
        result["target_remaining_hp"] = target_remaining

        if target_remaining <= 0:
            result["target_killed"] = True
            if target_name:
                narrative_parts.append(f"{target_name} HP: 0 (KILLED)")
        else:
            result["target_killed"] = False
            if target_name:
                narrative_parts.append(f"{target_name} HP: {target_remaining}/{target_current_hp}")

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
    elif not is_npc_attack and not is_npc_vs_npc and sp_healing:
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
