import random
import sys
import json
import sqlite3
from mcp.server.fastmcp import FastMCP

# FastMCP handles logging internally, but this guarantees stderr usage
mcp = FastMCP("InfinityRolls", log_level="WARNING")

# In-memory database connection
DB_CONNECTION = None

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
    """Traverse a dictionary using a dotted path."""
    parts = path.split('.')
    for part in parts:
        if isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data

def set_nested_value(data, path, value):
    """Set a value in a dictionary using a dotted path."""
    parts = path.split('.')
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    data[parts[-1]] = value
    return data

@mcp.tool()
def update_player_stat(key: str, value: str) -> str:
    """
    Updates a specific player attribute. Supports dotted notation for nested attributes.
    Example: To change the spellcasting ability, use 'spellcasting.ability'.
    Correct: update_player_stat(key='spellcasting.ability', value='intelligence')
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
            set_nested_value(data, key[len(root_key)+1:], value)
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
        else:
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, value))
            
        DB_CONNECTION.commit()
        return f"Successfully updated {key} to {value}."
    except Exception as e:
        return f"Error updating stat: {str(e)}"

@mcp.tool()
def modify_player_numeric(key: str, delta: int) -> str:
    """
    Increments or decrements a numeric player attribute. Supports dotted notation.
    Examples:
    - For top-level stats: modify_player_numeric(key='gold', delta=-10)
    - For nested slots: modify_player_numeric(key='spellcasting.slots.1', delta=-1)
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
        return f"Updated {key} to {new_val}."
    except Exception as e:
        return f"Error modifying numeric value: {str(e)}"

@mcp.tool()
def update_player_list(key: str, item: str, action: str) -> str:
    """
    Adds or removes an item from a player list. Supports dotted notation.
    Examples:
    - Update inventory: update_player_list(key='inventory', item='Health Potion', action='add')
    - Remove a spell: update_player_list(key='spellcasting.spells', item='Shield', action='remove')
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
            if item not in current_list:
                current_list.append(item)
        elif action == "remove":
            if item in current_list:
                current_list.remove(item)
            else:
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
def get_player_stat(key: str) -> str:
    """
    Retrieves a single specific attribute from the player database.
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return "Database not initialized."
    try:
        cursor = DB_CONNECTION.cursor()
        cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            return f"{key}: {row[0]}"
        return f"Stat '{key}' not found in database."
    except Exception as e:
        return f"Error retrieving stat: {str(e)}"

@mcp.tool()
def dump_player_db() -> str:
    """
    Returns a full dump of the current in-memory player database.
    """
    global DB_CONNECTION
    if DB_CONNECTION is None:
        return "Database not initialized."
    
    try:
        cursor = DB_CONNECTION.cursor()
        cursor.execute("SELECT * FROM player")
        rows = cursor.fetchall()
        
        if not rows:
            return "Database is empty."
            
        dump = "\n".join([f"{row[0]}: {row[1]}" for row in rows])
        return f"--- Player DB Dump ---\n{dump}\n--------------------"
    except Exception as e:
        return f"Error dumping database: {str(e)}"

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
