from .models import Kingdom, Location, NPC, Stats
import random

# --- Helper Functions ---

def find_valid_placement(map_grid, area_size=(5, 5)):
    """Finds a random valid top-left coordinate for a location on land."""
    height = len(map_grid)
    width = len(map_grid[0])
    possible_placements = []
    # Give a buffer from the edge of the map
    for r in range(5, height - area_size[1] - 5):
        for c in range(5, width - area_size[0] - 5):
            is_valid = all(
                map_grid[r + i][c + j] == '.' 
                for i in range(area_size[1]) 
                for j in range(area_size[0])
            )
            if is_valid:
                possible_placements.append((c, r))
    
    return random.choice(possible_placements) if possible_placements else None

def create_settlement_npc(role, config):
    """Creates a generic NPC for a settlement."""
    race = random.choice(config.races)
    alignment = random.choice(config.alignments)
    return NPC(
        name=f"{race.name} {role}",
        level=random.randint(1, 5),
        stats=Stats(strength=10, dexterity=10, constitution=10, intelligence=10, wisdom=10, charisma=10),
        alignment=alignment,
        role=role,
        faction="Civilian",
        dialogue_options=[f"Just a humble {role}, trying to make a living.", "Welcome to our town."]
    )

# --- Main Population Generator ---

def populate_world(config, map_grid):
    """Populates the world with kingdoms, capitals, settlements, and NPCs."""
    kingdoms = []
    all_npcs = []

    kingdom_defs = {
        "Eldoria": {"alignment": "Lawful Good", "ruler_name": "King Theron"},
        "Zarthus": {"alignment": "Lawful Evil", "ruler_name": "Sorcerer-King Malakor"},
        "Silverwood": {"alignment": "True Neutral", "ruler_name": "Archdruid Elara"},
        "Blacksail Archipelago": {"alignment": "Chaotic Evil", "ruler_name": "Dread Pirate King Kaelen"}
    }

    for name, data in kingdom_defs.items():
        # Create the Ruler
        ruler = NPC(
            name=data["ruler_name"], level=random.randint(10, 15),
            stats=Stats(strength=15, dexterity=15, constitution=15, intelligence=15, wisdom=15, charisma=15),
            alignment=data["alignment"], role="Ruler", faction=name,
            dialogue_options=[f"I am the ruler of {name}.", "State your business."]
        )
        all_npcs.append(ruler)

        # Create the Capital City
        capital_coords = find_valid_placement(map_grid, (8, 8))
        if not capital_coords: continue # Skip kingdom if no place for capital
        
        capital_city = Location(
            name=f"{name} City", coordinates=capital_coords, biome="Plains",
            description=f"The bustling capital of {name}.", npcs=[ruler]
        )

        # Create the Kingdom
        kingdom = Kingdom(
            name=name, capital=capital_city.name, alignment=data["alignment"], 
            ruler=ruler, locations=[capital_city]
        )

        # Generate Smaller Settlements
        num_settlements = random.randint(2, 4)
        for i in range(num_settlements):
            settlement_coords = find_valid_placement(map_grid, (3, 3))
            if settlement_coords:
                settlement_npc = create_settlement_npc(random.choice(["Innkeeper", "Blacksmith", "Farmer"]), config)
                all_npcs.append(settlement_npc)
                settlement = Location(
                    name=f"{name} Town {i+1}", coordinates=settlement_coords, biome="Plains",
                    description=f"A small settlement in the realm of {name}.",
                    npcs=[settlement_npc]
                )
                kingdom.locations.append(settlement)
        
        kingdoms.append(kingdom)

    # Set Kingdom Relations
    if len(kingdoms) == 4:
        kingdoms[0].relations = {"Zarthus": "War", "Silverwood": "Neutral", "Blacksail Archipelago": "War"}
        kingdoms[1].relations = {"Eldoria": "War", "Silverwood": "Neutral", "Blacksail Archipelago": "War"}
        kingdoms[2].relations = {"Eldoria": "Neutral", "Zarthus": "Neutral", "Blacksail Archipelago": "War"}
        kingdoms[3].relations = {"Eldoria": "War", "Zarthus": "War", "Silverwood": "War"}

    # Load all creatures from config
    all_creatures = [creature for creature in config.creatures]

    return kingdoms, all_npcs, all_creatures
