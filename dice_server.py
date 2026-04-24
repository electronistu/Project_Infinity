import random
import sys
import json
import sqlite3
from mcp.server.fastmcp import FastMCP
from level_up import apply_level_up

# FastMCP handles logging internally, but this guarantees stderr usage
mcp = FastMCP("InfinityRolls", log_level="WARNING")

# In-memory database connection
DB_CONNECTION = None

XP_THRESHOLDS = [
    (2, 300), (3, 900), (4, 2700), (5, 6500),
    (6, 14000), (7, 23000), (8, 33000), (9, 48000),
    (10, 64000), (11, 85000), (12, 100000), (13, 120000),
    (14, 145000), (15, 175000), (16, 210000), (17, 255000),
    (18, 305000), (19, 360000), (20, 400000),
]

def get_level_for_xp(xp: int) -> int:
    level = 1
    for lvl, threshold in XP_THRESHOLDS:
        if xp >= threshold:
            level = lvl
        else:
            break
    return level

def init_player_db(player_file_path: str) -> str:
    """
    Initializes the in-memory SQLite database using the provided .player JSON file.
    This function is NOT exposed as an MCP tool - it's called directly by play.py
    """
    global DB_CONNECTION
    try:
        with open(player_file_path, 'r') as f:
            data = json.load(f)
        
        DB_CONNECTION = sqlite3.connect(":memory:")
        cursor = DB_CONNECTION.cursor()
        
        # Create a simple table for player stats
        cursor.execute("CREATE TABLE player (key TEXT PRIMARY KEY, value TEXT)")
        
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                cursor.execute("INSERT INTO player (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            elif isinstance(value, str):
                cursor.execute("INSERT INTO player (key, value) VALUES (?, ?)", (key, value))
            else:
                cursor.execute("INSERT INTO player (key, value) VALUES (?, ?)", (key, json.dumps(value)))

        
        DB_CONNECTION.commit()
        return f"Database initialized with player data from {player_file_path}."
    except Exception as e:
        return f"Failed to initialize database: {str(e)}"

def get_nested_value(data, path):
    """Traverse a dictionary or list using a dotted path. Numeric segments are treated as list indices."""
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
    """Set a value in a dictionary or list using a dotted path. Numeric segments are treated as list indices."""
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
                    # If index is out of bounds, we cannot easily "setdefault" 
                    # for a list without knowing the size. Return data as is.
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
                # If the list is too short, we append if the index is exactly len(data)
                if idx == len(data):
                    data.append(value)
                else:
                    # Out of bounds
                    pass
        except ValueError:
            # Treat as dict key if we are unexpectedly in a list
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
    """
    Returns the maximum number of spells_prepared allowed for a prepared caster,
    or None if the character is not a prepared caster.
    Formula: spellcasting ability modifier + character level (minimum 1).
    """
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
    """
    Returns a dict with spells_prepared capacity info for a prepared caster,
    or None if the character is not a prepared caster.
    """
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


@mcp.tool()
def modify_player_numeric(key: str, delta: int) -> dict:
    """
    Increments or decrements a numeric player attribute. Supports dotted notation for nested attributes and list indices.
    Examples:
    - For top-level stats: modify_player_numeric(key='gold', delta=-10)
    - For nested slots (using index): modify_player_numeric(key='spellcasting.slots.1', delta=-1)
    - For consumables (ammunition, potions, rations): modify_player_numeric(key='consumables.Bolts', delta=-1)
    - Add consumable items: modify_player_numeric(key='consumables.Arrows', delta=20)
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
            
            auto_init_root = False
            if not row:
                if root_key == "consumables":
                    data = {}
                    auto_init_root = True
                else:
                    return {"success": False, "error": f"Root key {root_key} not found.", "key": key}
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
                    return {"success": False, "error": f"Key {key} not found in database.", "key": key}
            
            current_val = int(current_val)
            new_val = current_val + delta
            set_nested_value(data, path_in_obj, new_val)
            
            if key.startswith("consumables.") and new_val <= 0:
                consumable_name = path_in_obj
                if consumable_name in data:
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
                    }
            
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
        else:
            cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                return {"success": False, "error": f"Key {key} not found in database.", "key": key}
            
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
                    result["level_up"] = True
                    result["old_level"] = current_level
                    result["new_level"] = new_level
                    result["level_up_changes"] = summary
        return result
    except Exception as e:
        return {"success": False, "error": f"Error modifying numeric value: {str(e)}", "key": key}

@mcp.tool()
def update_player_list(key: str, item: str, action: str) -> dict:
    """
    Adds or removes an item from a player list. Supports dotted notation.
    For 'add' actions, use the format 'Item Name: Description' to include a description.

    SPELLS_PREPARED CAPACITY ENFORCEMENT:
    For prepared casters (Cleric, Druid, Wizard, Paladin, Artificer), the number of
    spells_prepared is capped at: spellcasting ability modifier + character level.
    - 'add': If the list is at capacity, the spell is REJECTED and an error is returned
      with the current list and the maximum allowed count.
    - 'remove': After removal, the response includes remaining capacity info.

    Examples:
    - Update inventory with description: update_player_list(key='inventory', item='Dagger: A rusty iron blade (1d4 piercing, Finesse, Light, Thrown (range 20/60))', action='add')
    - Update inventory simply: update_player_list(key='inventory', item='Health Potion', action='add')
    - Remove an item by name: update_player_list(key='spellcasting.spells_known', item='Shield', action='remove')
    - Remove a prepared spell: update_player_list(key='spellcasting.spells_prepared', item='Bless', action='remove')
    - Add a prepared spell (capacity enforced): update_player_list(key='spellcasting.spells_prepared', item='Fireball', action='add')
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
                return {"success": False, "error": f"Root key {root_key} not found.", "key": key}
            
            data = json.loads(row[0])
            path_in_obj = key[len(root_key)+1:]
            current_list = get_nested_value(data, path_in_obj)
            
            if current_list is None or not isinstance(current_list, list):
                return {"success": False, "error": f"Key {key} not found or is not a list.", "key": key}
        else:
            cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                return {"success": False, "error": f"Key {key} not found in database.", "key": key}
            current_list = json.loads(row[0]) if 'json' in row[0] or '[' in row[0] else [row[0]]

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
    Returns a full dump of the current in-memory player database as a dictionary.
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
                # Attempt to parse JSON strings back into python objects
                result[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                result[key] = value
                
        return result
    except Exception as e:
        return {"error": f"Error dumping database: {str(e)}"}

@mcp.tool()
def roll_dice(dice_notation: str, modifier: int = 0) -> dict:
    """
    Rolls dice based on standard notation. 
    
    DECISION GUIDE: Use this tool ONLY for "How much X?" scenarios (e.g., damage, healing, loot quantity, or random results).
    CRITICAL: Do NOT use this tool for complexity checks, attacks, or any action where you need to determine success or failure. For those, use 'perform_check'.
    
    IMPORTANT: dice_notation must ONLY contain the dice (e.g., '3d4'). 
    Do NOT include modifiers or operators like '+' in the notation string.
    All bonuses or penalties MUST be passed as a separate integer in the modifier parameter.
    
    Correct: roll_dice(dice_notation='3d4', modifier=3)
    Incorrect: roll_dice(dice_notation='3d4+3', modifier=0)
    """
    try:
        # Parse notation like '2d6'
        parts = dice_notation.lower().split('d')
        if len(parts) != 2:
            return {"error": "Invalid dice notation. Use format 'XdY' (e.g., '2d6')."}
        
        num_dice = int(parts[0]) if parts[0] else 1
        die_size = int(parts[1])
        
        if num_dice <= 0 or die_size <= 0:
            return {"error": "Number of dice and die size must be positive integers."}

        rolls = [random.randint(1, die_size) for _ in range(num_dice)]
        total = sum(rolls) + modifier
        
        return {
            "notation": dice_notation,
            "rolls": rolls,
            "modifier": modifier,
            "total": total
        }
    except ValueError:
        return {"error": "Invalid dice notation. Please provide integers (e.g., '2d6')."}

@mcp.tool()
def perform_check(modifier: int, dc: int, check_name: str = "Check") -> dict:
    """
    Performs a D&D 5E complexity check.
    
    DECISION GUIDE: Use this tool for any "Can I do X?" or "Does Y happen?" scenarios. 
    This is the ONLY tool for attacks, skill checks, and saving throws. 
    If the outcome is binary (Success/Failure) and depends on a Difficulty Class (DC), use this tool.
    
    Example: To check if the player can punch a guard (Sleight of Hand), use perform_check(modifier=2, dc=12, check_name='Sleight of Hand').
    """
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
       
    # Returning a dictionary allows the AI to parse the exact variables
    # perfectly without having to read a formatted sentence.
    return {
        "check_name": check_name,
        "base_roll": roll,
        "modifier": modifier,
        "total": total,
        "dc_to_beat": dc,
        "outcome": result
    }

if __name__ == "__main__":
    if len(sys.argv) > 1:
        player_file = sys.argv[1]
        init_status = init_player_db(player_file)
        # Log the status to stderr because stdout is reserved for MCP
        print(f"Server DB Init: {init_status}", file=sys.stderr)
    
    mcp.run(transport="stdio")
