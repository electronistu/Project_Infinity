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
        
        # Flatten the JSON and insert into the database
        # We only flatten at the root level to preserve complex objects (dict/list) as JSON strings
        flattened_data = {}
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                flattened_data[key] = json.dumps(value)
            else:
                flattened_data[key] = value

        for key, value in flattened_data.items():
            cursor.execute("INSERT INTO player (key, value) VALUES (?, ?)", (key, str(value)))

        
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


@mcp.tool()
def modify_player_numeric(key: str, delta: int) -> str:
    """
    Increments or decrements a numeric player attribute. Supports dotted notation for nested attributes and list indices.
    Examples:
    - For top-level stats: modify_player_numeric(key='gold', delta=-10)
    - For nested slots (using index): modify_player_numeric(key='spellcasting.slots.1', delta=-1)
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return "Database not initialized."
    try:
        cursor = DB_CONNECTION.cursor()
        
        if '.' in key:
            root_key = key.split('.')[0]
            cursor.execute("SELECT value FROM player WHERE key = ?", (root_key,))
            row = cursor.fetchone()
            if not row:
                return f"Root key {root_key} not found."
            
            data = json.loads(row[0])
            path_in_obj = key[len(root_key)+1:]
            current_val = get_nested_value(data, path_in_obj)
            
            if current_val is None:
                if key.startswith("spellcasting.slots."):
                    current_val = 0
                else:
                    return f"Key {key} not found in database."
            
            new_val = int(current_val) + delta
            set_nested_value(data, path_in_obj, new_val)
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
        else:
            cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                return f"Key {key} not found in database."
            
            current_val = int(row[0])
            new_val = current_val + delta
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, str(new_val)))
        
        DB_CONNECTION.commit()
        result_msg = f"Updated {key} to {new_val}."
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
                    for line in summary:
                        result_msg += f" {line}."
                    result_msg += f" Player has reached level {new_level}!"
        return result_msg
    except Exception as e:
        return f"Error modifying numeric value: {str(e)}"

@mcp.tool()
def update_player_list(key: str, item: str, action: str) -> str:
    """
    Adds or removes an item from a player list. Supports dotted notation.
    For 'add' actions, use the format 'Item Name: Description' to include a description.
    Examples:
    - Update inventory with description: update_player_list(key='inventory', item='Dagger: A rusty iron blade', action='add')
    - Update inventory simply: update_player_list(key='inventory', item='Health Potion', action='add')
    - Remove an item by name: update_player_list(key='spellcasting.spells', item='Shield', action='remove')
    action: 'add' or 'remove'
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return "Database not initialized."
    try:
        cursor = DB_CONNECTION.cursor()
        
        if '.' in key:
            root_key = key.split('.')[0]
            cursor.execute("SELECT value FROM player WHERE key = ?", (root_key,))
            row = cursor.fetchone()
            if not row:
                return f"Root key {root_key} not found."
            
            data = json.loads(row[0])
            path_in_obj = key[len(root_key)+1:]
            current_list = get_nested_value(data, path_in_obj)
            
            if current_list is None or not isinstance(current_list, list):
                return f"Key {key} not found or is not a list."
        else:
            cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                return f"Key {key} not found in database."
            current_list = json.loads(row[0]) if 'json' in row[0] or '[' in row[0] else [row[0]]

        if action == "add":
            name = item
            desc = ""
            if ":" in item:
                name, desc = [p.strip() for p in item.split(":", 1)]
            
            new_entry = {"name": name, "description": desc} if (desc or ":" in item) else name
            
            # Avoid duplicates by name
            exists = any((isinstance(e, dict) and e.get("name") == name) or e == name for e in current_list)
            if not exists:
                current_list.append(new_entry)
        elif action == "remove":
            # Find and remove by name
            found = False
            for i, e in enumerate(current_list):
                if (isinstance(e, dict) and e.get("name") == item) or e == item:
                    current_list.pop(i)
                    found = True
                    break
            if not found:
                return f"Item {item} not found in {key}."
        else:
            return "Invalid action. Use 'add' or 'remove'."

        if '.' in key:
            set_nested_value(data, path_in_obj, current_list)
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
        else:
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, json.dumps(current_list)))
        
        DB_CONNECTION.commit()
        return f"Successfully performed {action} on {item} in {key}."
    except Exception as e:
        return f"Error updating list: {str(e)}"


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
