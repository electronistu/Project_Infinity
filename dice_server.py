import math
import random
import sys
import json
import sqlite3
import os
import re
from mcp.server.fastmcp import FastMCP
from level_up import apply_level_up, CASTER_TYPE_MAP, SLOT_TABLES, FULL_CASTER_SPELL_SLOTS, WARLOCK_SPELL_SLOTS

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
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("temporary_hit_points", "0"))
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


@mcp.tool()
def update_player_list(key: str, item: str, action: str) -> dict:
    """
    Adds or removes an item from a player list.

    PARAMETERS:
    - key: dotted path to the list (e.g. 'inventory', 'spellcasting.spells_known', 'reputation.eldoria.guard')
    - item: for add — 'Name: Description' (description optional); for remove — name ONLY (never include the description)
    - action: 'add' or 'remove'

    PROJECT-SPECIFIC BEHAVIORS:
    1. Prepared casters: capacity enforced on spells_prepared (max = spellcasting_ability_mod + level).
       At capacity, the add is rejected with the current spell list.
    2. Removing from active_effects auto-reverts any stat deltas applied by that effect.
    3. CONSUMABLES: NEVER use this tool for consumable quantities — use modify_player_numeric(key='consumables.ITEM', delta=N) instead.
    4. Reputation: use key='reputation.KINGDOM.FACTION' with lowercase kingdom/faction names and no apostrophes.
       Each entry is a 'Title: Description' pair.

    EXAMPLES:
    update_player_list(key='inventory', item='Dagger: A rusty blade (1d4 piercing, Finesse, Light, Thrown (range 20/60))', action='add')
    update_player_list(key='inventory', item='Dagger', action='remove')          ← name only, NOT 'Dagger: A rusty blade...'
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
    Returns a full dump of the current in-memory player database for state refresh.
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

            if prepared_spells is not None:
                if char_class in PREPARED_CASTER_CLASSES or char_class == "Wizard":
                    max_spells = get_max_prepared_spells(cursor)
                    if max_spells is not None and len(prepared_spells) > max_spells:
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
    - Include the 'narrative_format' field from the response verbatim when disclosing results.

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
def perform_check(modifier: int, dc: int, check_name: str = "Check", actor: str = "{player_name}") -> dict:
    """
    Performs a skill check or saving throw (d20 + modifier vs DC).

    PARAMETERS:
    - modifier: bonus to add to the d20 roll
    - dc: difficulty class to beat
    - check_name: label for the check (e.g. 'Athletics', 'Perception')
    - actor: who is performing the check — character name for player, NPC/creature name for NPCs

    RULES:
    - For weapon/unarmed attacks, use resolve_attack instead.
    - For spell attacks, use resolve_magic.
    - Include the 'narrative_format' field from the response verbatim when disclosing results.

    EXAMPLES:
    perform_check(actor='Thorin', modifier=5, dc=15, check_name='Athletics')
    perform_check(actor='Guard Captain', modifier=2, dc=13, check_name='Perception')
    perform_check(actor='Senna', modifier=1, dc=13, check_name='Deception')
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


@mcp.tool()
def register_combatants(combatants: list[dict], add_to_existing: bool = False) -> dict:
    """
    Registers all combatants for a battle and rolls initiative for everyone. Player is auto-registered.

    PARAMETERS:
    - combatants: list of NPC dicts with fields:
      - name (str, required): must match target_name in resolve_attack/resolve_magic calls
      - hp (int, required): starting hit points
      - ac (int, required): armor class
      - initiative_modifier (int, required unless add_to_existing=True): DEX modifier
      - challenge_rating (float, optional): CR for XP awards
      - save_modifier (int, optional, default 0): generic save bonus
    - add_to_existing (bool, default False): if True, adds to existing registry without wiping it.
      No initiative rolled for new arrivals. Use for mid-combat reinforcements or forgotten combatants.
      Existing HP states are preserved.

    PROJECT-SPECIFIC BEHAVIORS:
    1. Resolve_attack and resolve_magic auto-lookup target HP from the registry — no need to pass
       target_current_hp on every call. HP is carried forward between hits automatically.
    2. Calling this again without add_to_existing overwrites the registry entirely.

    EXAMPLES:
    register_combatants(combatants=[
        {"name": "Scarred Half-Orc", "hp": 15, "ac": 14, "initiative_modifier": 1,
         "challenge_rating": 1, "save_modifier": 1},
        {"name": "Crossbow Bandit 1", "hp": 11, "ac": 12, "initiative_modifier": 2,
         "challenge_rating": 0.125},
        {"name": "Kella", "hp": 30, "ac": 15, "initiative_modifier": 2},
        {"name": "Harlen Dregg", "hp": 30, "ac": 16, "initiative_modifier": 0},
    ])

    register_combatants(combatants=[
        {"name": "Guard Reinforce 1", "hp": 11, "ac": 16},
        {"name": "Guard Reinforce 2", "hp": 11, "ac": 16},
    ], add_to_existing=True)
    """
    global _COMBAT_REGISTRY, DB_CONNECTION

    if not add_to_existing:
        _COMBAT_REGISTRY = {}

    initiative_results = []

    if not add_to_existing and DB_CONNECTION is not None:
        cursor = DB_CONNECTION.cursor()
        player_name = _db_val(cursor, "name", "Player")
        stats_raw = _db_val(cursor, "stats", {})
        if isinstance(stats_raw, str):
            stats_raw = json.loads(stats_raw)
        player_dex = int(stats_raw.get("dex", 10))
        player_init_mod = (player_dex - 10) // 2
        player_hp = int(_db_val(cursor, "current_hit_points", 1))
        player_max_hp = int(_db_val(cursor, "total_hit_points", 1))
        player_ac = int(_db_val(cursor, "armor_class", 10))

        _COMBAT_REGISTRY[player_name] = {
            "current_hp": player_hp,
            "max_hp": player_max_hp,
            "ac": player_ac,
            "save_modifier": 0,
            "challenge_rating": None,
            "initiative_modifier": player_init_mod,
            "initiative_roll": 0,
            "initiative_total": 0,
            "is_player": True,
            "killed": False,
        }

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
        max_hp = c["hp"]
        ac = c["ac"]
        cr = c.get("challenge_rating")
        save_mod = c.get("save_modifier", 0)

        if add_to_existing:
            init_mod = 0
        else:
            init_mod = c["initiative_modifier"]

        _COMBAT_REGISTRY[name] = {
            "current_hp": max_hp,
            "max_hp": max_hp,
            "ac": ac,
            "save_modifier": save_mod,
            "challenge_rating": cr,
            "initiative_modifier": init_mod,
            "initiative_roll": 0,
            "initiative_total": 0,
            "is_player": False,
            "killed": False,
        }

        if not add_to_existing:
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
        registry_summary.append({
            "name": rname,
            "hp": f"{entry['current_hp']}/{entry['max_hp']}",
            "ac": entry["ac"],
            "initiative": entry["initiative_total"],
            "is_player": entry.get("is_player", False),
        })

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
    Resolves a full weapon/unarmed attack: attack roll, damage, HP application, kill detection, XP award.

    PARAMETERS:
    - actor: who is attacking — character name for player, NPC name for NPCs
    - attack_modifier: bonus to the d20 attack roll
    - target_ac: target's armor class
    - damage_dice: primary damage dice (e.g. '1d8'), doubled on crit
    - damage_modifier: flat bonus added to damage (default 0)
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

    EXAMPLES:
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
    targets: list[dict] | None = None,
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
    - target_save_modifier: save bonus for single-target saving_throw spells
    - player_save_modifier: player's save bonus when is_npc_attack=True with saving throws
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
        total_healing = heal_raw + final_mod

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
            if final_mod != 0:
                heal_narrative = f"{actor} {spell_name} Healing: {total_healing} ({heal_rolls_str} + {final_mod})"
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
                        f"{actor} {spell_name} {label}: {primary_damage} ({base_str} + {crit_rolls_str} + {final_mod}) [CRIT]"
                    )
                else:
                    narrative_parts.append(
                        f"{actor} {spell_name} {label}: {primary_damage} ({base_str} + {crit_rolls_str}) [CRIT]"
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
                    narrative_parts.append(f"{actor} {spell_name} {label}: {primary_damage} ({base_str} + {final_mod})")
                else:
                    narrative_parts.append(f"{actor} {spell_name} {label}: {primary_damage} ({base_str})")
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
            tsave = t.get("save_modifier", 0)
            tcr = t.get("challenge_rating")
            if tcr is None:
                tcr = _registry_cr(tname)
            is_player = t.get("is_player", False)

            save_d20 = random.randint(1, 20)
            save_total = save_d20 + tsave
            save_success = save_total >= spell_save_dc
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
        save_mod = player_save_modifier if is_npc_attack and player_save_modifier is not None else target_save_modifier
        saver_name = target_name or actor
        save_d20 = random.randint(1, 20)
        save_total = save_d20 + save_mod
        save_success = save_total >= spell_save_dc

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
