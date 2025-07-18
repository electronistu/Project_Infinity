# geopolitical_generator.py
# Layer 1 of the Generation Cascade. Creates the world map, locations, and a coded map grid.

import random
from models import WorldState, Location

# --- Name Generation Components ---
SETTLEMENT_PREFIXES = ["Oak", "Green", "Stone", "River", "Clear", "Iron", "White"]
SETTLEMENT_SUFFIXES = ["wood", "haven", "bridge", "creek", "fall", "dale", "watch"]
DUNGEON_PREFIXES = ["Gloom", "Shadow", "Blood", "Skull", "Whispering", "Forgotten"]
DUNGEON_SUFFIXES = ["Caverns", "Crypt", "Lair", "Ruins", "Tomb", "Spire"]
BIOMES = ["Forest", "Mountains", "Plains", "Swamp", "Hills"]

def generate_location_name(prefixes, suffixes):
    """Generates a unique-ish name from two lists."""
    return f"{random.choice(prefixes)}{random.choice(suffixes)}"

def create_coded_map(world_state: WorldState, grid_size=10):
    """Places locations on a 2D grid and adds it to the world_state."""
    grid = [['.' for _ in range(grid_size)] for _ in range(grid_size)]
    occupied_coords = set()
    
    for loc_name, location in world_state.world_map.items():
        while True:
            x, y = random.randint(0, grid_size - 1), random.randint(0, grid_size - 1)
            if (x, y) not in occupied_coords:
                # Use first letter of type as map icon (S for Settlement, D for Dungeon)
                icon = location.type[0]
                grid[y][x] = icon
                location.coordinates = (x, y) # Store coordinates on the location object
                occupied_coords.add((x, y))
                break
    
    # Add the grid to a temporary field in world_state for the formatter
    world_state.coded_map_grid = ["".join(row) for row in grid]

def generate_geopolitical_layer(world_state: WorldState) -> WorldState:
    """
    Generates the world map, populates it with locations, and connects them.
    """
    print("[STATUS] Generating Geopolitical Layer...")
    
    num_settlements = random.randint(3, 4)
    num_dungeons = random.randint(4, 6)
    
    all_locations = []
    generated_names = set()

    # 1. Generate Settlements
    for _ in range(num_settlements):
        name = generate_location_name(SETTLEMENT_PREFIXES, SETTLEMENT_SUFFIXES)
        while name in generated_names:
            name = generate_location_name(SETTLEMENT_PREFIXES, SETTLEMENT_SUFFIXES)
        generated_names.add(name)
        
        biome = random.choice(BIOMES)
        location = Location(
            name=name,
            type="Settlement",
            biome=biome,
            description=f"The settlement of {name} is nestled in a {biome}.",
            challenge_level=random.randint(1, 2)
        )
        all_locations.append(location)

    # 2. Generate Dungeons
    for _ in range(num_dungeons):
        name = generate_location_name(DUNGEON_PREFIXES, DUNGEON_SUFFIXES)
        while name in generated_names:
            name = generate_location_name(DUNGEON_PREFIXES, DUNGEON_SUFFIXES)
        generated_names.add(name)

        biome = random.choice(BIOMES)
        location = Location(
            name=name,
            type="Dungeon",
            biome=biome,
            description=f"The {name} are a place of danger, deep within the {biome}.",
            challenge_level=random.randint(3, 10)
        )
        all_locations.append(location)

    # 3. Create Connections (simple graph)
    location_names = [loc.name for loc in all_locations]
    for loc in all_locations:
        num_connections = random.randint(1, 2)
        possible_connections = [name for name in location_names if name != loc.name and name not in loc.connections]
        for _ in range(num_connections):
            if not possible_connections:
                break
            connection_name = random.choice(possible_connections)
            loc.connections.append(connection_name)
            # Ensure the connection is two-way
            for other_loc in all_locations:
                if other_loc.name == connection_name:
                    if loc.name not in other_loc.connections:
                        other_loc.connections.append(loc.name)
            possible_connections.remove(connection_name)

    # 4. Finalize map and set player starting location
    final_map = {loc.name: loc for loc in all_locations}
    world_state.world_map = final_map
    
    # The player starts in the first generated settlement
    starting_location = all_locations[0].name
    world_state.player_character.known_locations.append(starting_location)
    
    # 5. Create the coded map grid
    create_coded_map(world_state)
    
    print(f"[STATUS] Geopolitical Layer complete. Player starting in {starting_location}.")
    return world_state
