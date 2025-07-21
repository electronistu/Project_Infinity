# geopolitical_generator.py v2.5.1
# Increases location density and adds variable sizes for dungeons. (Helper functions restored)

import random
from models import WorldState, Location

# --- v2.5 Constants ---
GRID_SIZE = 42
CAPITAL_PREFIXES = ["Aethel", "Casterly", "Pyke", "Storm's", "King's"]
CAPITAL_SUFFIXES = ["gard", "Rock", "Landing", "End", "Watch"]
SETTLEMENT_PREFIXES = ["Oak", "Green", "Stone", "River", "Clear", "Iron", "White"]
SETTLEMENT_SUFFIXES = ["wood", "haven", "bridge", "creek", "fall", "dale", "watch"]
DUNGEON_PREFIXES = ["Gloom", "Shadow", "Blood", "Skull", "Whispering", "Forgotten"]
DUNGEON_SUFFIXES = ["Caverns", "Crypt", "Lair", "Ruins", "Tomb", "Spire"]
BIOMES = ["Forest", "Mountains", "Plains", "Swamp", "Hills"]

LOCATION_SIZES = {
    "Small": (1, 1),
    "Medium": (3, 3),
    "Big": (5, 5)
}

# CORRECTED: Restored the missing helper function.
def generate_location_name(prefixes, suffixes):
    """Generates a unique-ish name from two lists."""
    return f"{random.choice(prefixes)}{random.choice(suffixes)}"

# CORRECTED: Restored the missing helper function.
def create_coded_map(world_state: WorldState, grid_size=GRID_SIZE):
    """Places locations with variable footprints onto a 2D grid, preventing overlaps."""
    print("[STATUS] Rendering world map grid...")
    grid = [['.' for _ in range(grid_size)] for _ in range(grid_size)]
    occupied_coords = set()

    sorted_locations = sorted(world_state.world_map.values(), key=lambda loc: loc.size[0] * loc.size[1], reverse=True)

    for location in sorted_locations:
        width, height = location.size
        placed = False
        attempts = 0
        
        while not placed and attempts < 500:
            attempts += 1
            
            if location.type == "Capital":
                x = random.randint(grid_size // 4, (grid_size // 2) - width)
                y = random.randint(grid_size // 4, (grid_size // 2) - height)
            else:
                x = random.randint(0, grid_size - width)
                y = random.randint(0, grid_size - height)

            is_collision = False
            potential_coords = []
            for i in range(width):
                for j in range(height):
                    coord = (x + i, y + j)
                    if coord in occupied_coords:
                        is_collision = True
                        break
                    potential_coords.append(coord)
                if is_collision: break
            
            if not is_collision:
                location.coordinates = (x, y)
                icon = location.type[0]
                for i in range(width):
                    for j in range(height):
                        grid[y + j][x + i] = icon
                occupied_coords.update(potential_coords)
                placed = True
        
        if not placed:
            print(f"[WARNING] Could not place location {location.name} of size {location.size} after 500 attempts.")

    world_state.coded_map_grid = ["".join(row) for row in grid]
    print("[STATUS] World map grid rendered.")


def generate_geopolitical_layer(world_state: WorldState) -> WorldState:
    """Generates the world map, populates it with locations of varying sizes, and connects them."""
    print("[STATUS] Generating Geopolitical Layer...")

    num_settlements = random.randint(4, 6)
    num_dungeons = random.randint(10, 15)
    all_locations = []
    generated_names = set()

    # 1. Generate the Capital City first
    capital_name = generate_location_name(CAPITAL_PREFIXES, CAPITAL_SUFFIXES)
    generated_names.add(capital_name)
    capital = Location(
        name=capital_name, type="Capital", biome=random.choice(BIOMES),
        size=LOCATION_SIZES["Big"], challenge_level=1
    )
    all_locations.append(capital)
    print(f"[INFO] Capital city '{capital_name}' founded.")

    # 2. Generate other Settlements
    for _ in range(num_settlements):
        name = generate_location_name(SETTLEMENT_PREFIXES, SETTLEMENT_SUFFIXES)
        while name in generated_names: name = generate_location_name(SETTLEMENT_PREFIXES, SETTLEMENT_SUFFIXES)
        generated_names.add(name)
        location = Location(
            name=name, type="Settlement", biome=random.choice(BIOMES),
            size=random.choices([LOCATION_SIZES["Medium"], LOCATION_SIZES["Big"]], weights=[0.8, 0.2], k=1)[0],
            challenge_level=random.randint(1, 3)
        )
        all_locations.append(location)

    # 3. Generate Dungeons with variable sizes
    for _ in range(num_dungeons):
        name = generate_location_name(DUNGEON_PREFIXES, DUNGEON_SUFFIXES)
        while name in generated_names: name = generate_location_name(DUNGEON_PREFIXES, DUNGEON_SUFFIXES)
        generated_names.add(name)
        
        dungeon_size_key = random.choices(["Small", "Medium", "Big"], weights=[0.6, 0.3, 0.1], k=1)[0]
        dungeon_size = LOCATION_SIZES[dungeon_size_key]
        
        location = Location(
            name=name, type="Dungeon", biome=random.choice(BIOMES),
            size=dungeon_size,
            challenge_level=random.randint(4, 10)
        )
        all_locations.append(location)

    # 4. Create Connections
    location_names = [loc.name for loc in all_locations]
    for loc in all_locations:
        num_connections = random.randint(1, 3)
        possible_connections = [name for name in location_names if name != loc.name and name not in loc.connections]
        for _ in range(num_connections):
            if not possible_connections: break
            connection_name = random.choice(possible_connections)
            loc.connections.append(connection_name)
            for other_loc in all_locations:
                if other_loc.name == connection_name and loc.name not in other_loc.connections:
                    other_loc.connections.append(loc.name)
            possible_connections.remove(connection_name)

    # 5. Finalize map and set player starting location
    world_state.world_map = {loc.name: loc for loc in all_locations}
    starting_location_name = capital.name
    world_state.player_character.known_locations.append(starting_location_name)

    # 6. Create the coded map grid
    create_coded_map(world_state)

    print(f"[STATUS] Geopolitical Layer complete. Player starting in the capital, {starting_location_name}.")
    return world_state
