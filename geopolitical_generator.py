# geopolitical_generator.py v3.1
# Increases map size, adds sea access for the capital, variable location sizes, and islands.

import random
from models import WorldState, Location, SubLocation

# --- v3.1 Constants ---
GRID_SIZE = 84
CAPITAL_PREFIXES = ["Aethel", "Casterly", "Pyke", "Storm's", "King's"]
CAPITAL_SUFFIXES = ["gard", "Rock", "Landing", "End", "Watch"]
SETTLEMENT_PREFIXES = ["Oak", "Green", "Stone", "River", "Clear", "Iron", "White"]
SETTLEMENT_SUFFIXES = ["wood", "haven", "bridge", "creek", "fall", "dale", "watch"]
DUNGEON_PREFIXES = ["Gloom", "Shadow", "Blood", "Skull", "Whispering", "Forgotten"]
DUNGEON_SUFFIXES = ["Caverns", "Crypt", "Lair", "Ruins", "Tomb", "Spire"]
ISLAND_PREFIXES = ["Dragon's", "Serpent's", "Ghost", "Forgotten", "Sunken"]
ISLAND_SUFFIXES = ["Isle", "Cay", "Atoll", "Reef", "Rock"]
BIOMES = ["Forest", "Mountains", "Plains", "Swamp", "Hills", "Ocean"]

LOCATION_SIZES = {
    "Very Small": (1, 1),
    "Small": (2, 2),
    "Medium": (3, 3),
    "Big": (5, 5),
    "Capital": (6, 6)
}

def generate_location_name(prefixes, suffixes):
    """Generates a unique-ish name from two lists."""
    return f"{random.choice(prefixes)}{random.choice(suffixes)}"

def create_coded_map(world_state: WorldState, grid_size=GRID_SIZE):
    """Places locations with variable footprints onto a 2D grid, preventing overlaps."""
    print("[STATUS] Rendering world map grid...")
    grid = [['~' for _ in range(grid_size)] for _ in range(grid_size)] # Ocean by default
    occupied_coords = set()

    # Designate a landmass area
    land_start_x, land_end_x = grid_size // 4, grid_size - 1
    land_start_y, land_end_y = grid_size // 4, grid_size - 1
    for y in range(land_start_y, land_end_y):
        for x in range(land_start_x, land_end_x):
            grid[y][x] = '.'

    sorted_locations = sorted(world_state.world_map.values(), key=lambda loc: loc.size[0] * loc.size[1], reverse=True)

    for location in sorted_locations:
        width, height = location.size
        placed = False
        attempts = 0
        
        while not placed and attempts < 1000:
            attempts += 1
            
            if location.type == "Capital":
                # Place capital on the coast
                edge = random.choice(["top", "bottom", "left", "right"])
                if edge == "top":
                    x = random.randint(land_start_x, land_end_x - width)
                    y = land_start_y
                elif edge == "bottom":
                    x = random.randint(land_start_x, land_end_x - width)
                    y = land_end_y - height
                elif edge == "left":
                    x = land_start_x
                    y = random.randint(land_start_y, land_end_y - height)
                else: # right
                    x = land_end_x - width
                    y = random.randint(land_start_y, land_end_y - height)
            elif location.type == "Island":
                 x = random.randint(0, land_start_x - width - 5)
                 y = random.randint(0, grid_size - height)
            else:
                x = random.randint(land_start_x, land_end_x - width)
                y = random.randint(land_start_y, land_end_y - height)

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
            print(f"[WARNING] Could not place location {location.name} of size {location.size} after 1000 attempts.")

    world_state.coded_map_grid = ["".join(row) for row in grid]
    print("[STATUS] World map grid rendered.")


def generate_geopolitical_layer(world_state: WorldState) -> WorldState:
    """Generates the world map, populates it with locations of varying sizes, and connects them."""
    print("[STATUS] Generating Geopolitical Layer...")

    num_settlements = random.randint(15, 25)
    num_dungeons = random.randint(20, 30)
    num_islands = random.randint(3, 5)
    all_locations = []
    generated_names = set()

    # 1. Generate the Capital City first
    capital_name = generate_location_name(CAPITAL_PREFIXES, CAPITAL_SUFFIXES)
    generated_names.add(capital_name)
    capital = Location(
        name=capital_name, type="Capital", biome="Coastal",
        size=LOCATION_SIZES["Capital"], challenge_level=10
    )
    capital.sub_locations.append(SubLocation(name=f"{capital_name} Port", type="Port", parent_location=capital_name))
    all_locations.append(capital)
    print(f"[INFO] Capital city '{capital_name}' founded.")

    # 2. Generate other Settlements
    for _ in range(num_settlements):
        name = generate_location_name(SETTLEMENT_PREFIXES, SETTLEMENT_SUFFIXES)
        while name in generated_names: name = generate_location_name(SETTLEMENT_PREFIXES, SETTLEMENT_SUFFIXES)
        generated_names.add(name)
        size_key = random.choices(["Very Small", "Small", "Medium", "Big"], weights=[0.4, 0.3, 0.2, 0.1], k=1)[0]
        location = Location(
            name=name, type="Settlement", biome=random.choice(BIOMES[:-1]), # Exclude Ocean
            size=LOCATION_SIZES[size_key],
            challenge_level=random.randint(1, 5)
        )
        all_locations.append(location)

    # 3. Generate Dungeons with variable sizes
    for _ in range(num_dungeons):
        name = generate_location_name(DUNGEON_PREFIXES, DUNGEON_SUFFIXES)
        while name in generated_names: name = generate_location_name(DUNGEON_PREFIXES, DUNGEON_SUFFIXES)
        generated_names.add(name)
        
        dungeon_size_key = random.choices(["Very Small", "Small", "Medium", "Big"], weights=[0.5, 0.3, 0.15, 0.05], k=1)[0]
        
        location = Location(
            name=name, type="Dungeon", biome=random.choice(BIOMES[:-1]),
            size=LOCATION_SIZES[dungeon_size_key],
            challenge_level=random.randint(1, 10)
        )
        all_locations.append(location)

    # 4. Generate Islands
    for _ in range(num_islands):
        name = generate_location_name(ISLAND_PREFIXES, ISLAND_SUFFIXES)
        while name in generated_names: name = generate_location_name(ISLAND_PREFIXES, ISLAND_SUFFIXES)
        generated_names.add(name)
        size_key = random.choices(["Very Small", "Small"], weights=[0.7, 0.3], k=1)[0]
        location = Location(
            name=name, type="Island", biome="Ocean",
            size=LOCATION_SIZES[size_key],
            challenge_level=random.randint(3, 8)
        )
        all_locations.append(location)


    # 5. Create Connections
    location_names = [loc.name for loc in all_locations]
    for loc in all_locations:
        num_connections = random.randint(1, 3)
        # Islands are more isolated
        if loc.type == "Island":
            num_connections = random.choices([0, 1], weights=[0.6, 0.4], k=1)[0]

        possible_connections = [name for name in location_names if name != loc.name and name not in loc.connections]
        for _ in range(num_connections):
            if not possible_connections: break
            connection_name = random.choice(possible_connections)
            loc.connections.append(connection_name)
            for other_loc in all_locations:
                if other_loc.name == connection_name and loc.name not in other_loc.connections:
                    other_loc.connections.append(loc.name)
            possible_connections.remove(connection_name)

    # 6. Finalize map and set player starting location
    world_state.world_map = {loc.name: loc for loc in all_locations}
    starting_location_name = capital.name
    world_state.player_character.known_locations.append(starting_location_name)

    # 7. Create the coded map grid
    create_coded_map(world_state)

    print(f"[STATUS] Geopolitical Layer complete. Player starting in the capital, {starting_location_name}.")
    return world_state
