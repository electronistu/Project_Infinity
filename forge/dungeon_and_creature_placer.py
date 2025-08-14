from .models import Location, Creature, Item
import random

DUNGEON_TYPES = {
    "Cave": {"biome": "Underground", "description": "A dark and damp cave, filled with strange echoes.", "creatures": ["Goblin", "Giant Spider"]},
    "Ruin": {"biome": "Ruins", "description": "The crumbling remains of an ancient civilization.", "creatures": ["Skeleton", "Zombie"]},
    "Mine": {"biome": "Underground", "description": "An abandoned mine, rumored to be rich in minerals.", "creatures": ["Orc", "Shadow Mastiff"]}
}

BOSS_CREATURES = ["Lich"]

def find_valid_placement(map_grid, area_size=(5, 5)):
    """Finds a random valid top-left coordinate for a location on land."""
    height = len(map_grid)
    width = len(map_grid[0])
    possible_placements = []
    for r in range(height - area_size[1]):
        for c in range(width - area_size[0]):
            is_valid = all(
                map_grid[r + i][c + j] == '.' 
                for i in range(area_size[1]) 
                for j in range(area_size[0])
            )
            if is_valid:
                possible_placements.append((c, r))
    
    return random.choice(possible_placements) if possible_placements else None

def place_dungeons_and_creatures(kingdoms, all_items, map_grid, config):
    """Places dungeons with loot and creatures in the world."""
    creature_map = {c.name: c for c in config.creatures}

    for kingdom in kingdoms:
        num_dungeons = random.randint(2, 4)
        for _ in range(num_dungeons):
            dungeon_type_name = random.choice(list(DUNGEON_TYPES.keys()))
            dungeon_info = DUNGEON_TYPES[dungeon_type_name]
            
            dungeon_coords = find_valid_placement(map_grid, (3, 3))
            if dungeon_coords:
                dungeon_creatures = []
                # Increase the number of creatures per dungeon
                num_creatures_to_add = random.randint(3, 5) # Add 3 to 5 creatures
                for _ in range(num_creatures_to_add):
                    creature_name = random.choice(dungeon_info["creatures"])
                    if creature_name in creature_map:
                        dungeon_creatures.append(creature_map[creature_name])

                if random.random() < 0.2:
                    boss_name = random.choice(BOSS_CREATURES)
                    if boss_name in creature_map:
                        dungeon_creatures.append(creature_map[boss_name])

                # Add loot to the dungeon
                dungeon_loot = random.sample(all_items, k=random.randint(1, 3))

                dungeon = Location(
                    name=f"The {dungeon_type_name} of {kingdom.name}",
                    coordinates=dungeon_coords,
                    biome=dungeon_info["biome"],
                    description=dungeon_info["description"],
                    creatures=dungeon_creatures,
                    loot=dungeon_loot
                )
                kingdom.locations.append(dungeon)
