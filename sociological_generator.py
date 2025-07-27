# sociological_generator.py v3.3
# Generates factions, a royal court, detailed NPCs, creatures, sub-locations, and establishes complex faction relations.

import random
from models import WorldState, Faction, NPC, Creature, SubLocation, Location

# --- v3.3 Name/Data Components ---
MALE_NAMES = ["Roric", "Gunnar", "Falk", "Bjorn", "Sten", "Arne", "Erik", "Alaric", "Brand", "Corbin"]
FEMALE_NAMES = ["Astrid", "Freya", "Ingrid", "Sigrid", "Hilda", "Gerda", "Sif", "Brenna", "Dagmar", "Eira"]
FAMILY_NAMES = ["Ironhand", "Stonefist", "Longbeard", "Swiftfoot", "Greymane", "Blackwood", "Silverstream"]

# Expanded D&D 5e Races
RACES = [
    "Human", "Elf", "Dwarf", "Halfling", "Gnome", "Half-Elf", "Half-Orc",
    "Tiefling", "Dragonborn", "Aasimar", "Firbolg", "Goliath", "Kenku",
    "Lizardfolk", "Tabaxi", "Triton", "Yuan-ti Pureblood"
]

# Expanded Factions with Alignments
FACTION_TEMPLATES = {
    "Royal Guard": {"disposition": "Lawful Good", "description": "The elite, sworn protectors of the monarch and the capital."},
    "Merchants' Guild": {"disposition": "Lawful Neutral", "description": "A powerful consortium of traders and artisans, focused on profit and order."},
    "Mages' Guild": {"disposition": "Lawful Neutral", "description": "An ancient order dedicated to the study and application of the arcane arts."},
    "Thieves' Guild": {"disposition": "Chaotic Neutral", "description": "A shadowy network of spies, burglars, and smugglers operating in the city's underbelly."},
    "The Crimson Hand": {"disposition": "Neutral Evil", "description": "A secretive and ruthless cabal of assassins who trade in death."}
}

# 10-Tier Creature Difficulty
CREATURE_TYPES = {
    1: ["Giant Rat", "Kobold"],
    2: ["Goblin", "Giant Wolf"],
    3: ["Orc", "Hobgoblin"],
    4: ["Bugbear", "Gnoll"],
    5: ["Ogre", "Ghoul"],
    6: ["Minotaur", "Wraith"],
    7: ["Troll", "Mummy"],
    8: ["Vampire Spawn", "Chimera"],
    9: ["Beholder Zombie", "Young Dragon"],
    10: ["Lich", "Adult Dragon"]
}

# Detailed Sub-Location Types
SUB_LOCATION_TYPES = {
    "Shop": ["Blacksmith", "Alchemist", "General Store", "Tailor", "Jeweler"],
    "Service": ["Tavern", "Inn", "Stable"],
    "Guild": ["Mages' Guild Tower", "Assassins' Guild Hideout", "Merchants' Guild Hall"],
    "Civic": ["Barracks", "Jail", "Palace", "Port"]
}

def generate_creatures_for_dungeon(dungeon: Location, world_state: WorldState):
    """Generates creatures for a dungeon based on its challenge level."""
    num_creatures = dungeon.size[0] * dungeon.size[1] * random.randint(1, 3)
    dungeon_x, dungeon_y = dungeon.coordinates

    for i in range(num_creatures):
        creature_difficulty = max(1, min(10, dungeon.challenge_level + random.randint(-1, 1)))
        creature_name = random.choice(CREATURE_TYPES[creature_difficulty])

        coord_x = dungeon_x + random.randint(0, dungeon.size[0] - 1)
        coord_y = dungeon_y + random.randint(0, dungeon.size[1] - 1)

        creature_id = f"{creature_name}_{i}_{dungeon.name}"
        creature = Creature(
            name=creature_name, type=creature_name, race="Beast",
            location=dungeon.name, coordinates=(coord_x, coord_y), difficulty_level=creature_difficulty
        )
        world_state.creatures[creature_id] = creature

    if dungeon.challenge_level >= 8:
        boss_difficulty = dungeon.challenge_level
        boss_name = f"Dungeon Lord ({random.choice(CREATURE_TYPES[boss_difficulty])})"
        coord_x = dungeon_x + (dungeon.size[0] // 2)
        coord_y = dungeon_y + (dungeon.size[1] // 2)
        boss = Creature(
            name=boss_name, type="Dungeon Boss", race="Beast",
            location=dungeon.name, coordinates=(coord_x, coord_y), difficulty_level=boss_difficulty
        )
        world_state.creatures[f"boss_{dungeon.name}"] = boss

def generate_royal_court(capital: Location, world_state: WorldState):
    """Generates the royal family and their court."""
    print(f"[STATUS] Generating Royal Court for {capital.name}...")
    royal_family_name = "Valerion"
    monarch_sex = random.choice(["Male", "Female"])
    monarch_name = f"{random.choice(MALE_NAMES if monarch_sex == 'Male' else FEMALE_NAMES)} {royal_family_name}"
    monarch_title = "King" if monarch_sex == "Male" else "Queen"

    monarch = NPC(
        name=monarch_name, age=random.randint(40, 60), sex=monarch_sex, race="Human",
        status=monarch_title, family_id=royal_family_name, role_in_family="Head",
        location=capital.name, difficulty_level=10, faction_membership="Royal Court"
    )
    world_state.npcs[monarch.name] = monarch
    capital.inhabitants.append(monarch.name)

    world_state.factions["Royal Guard"].leader = monarch.name
    world_state.factions["Merchants' Guild"].leader = monarch.name

    court_roles = {"Royal Advisor": 9, "Court Mage": 8, "Spymaster": 9, "General": 8}
    for role, diff_lvl in court_roles.items():
        sex = random.choice(["Male", "Female"])
        name = f"{random.choice(MALE_NAMES if sex == 'Male' else FEMALE_NAMES)} {random.choice(FAMILY_NAMES)}"
        npc = NPC(
            name=name, age=random.randint(35, 65), sex=sex, race=random.choice(RACES),
            status=role, family_id="Court", role_in_family="Courtier",
            location=capital.name, difficulty_level=diff_lvl, faction_membership="Royal Court"
        )
        world_state.npcs[name] = npc
        capital.inhabitants.append(name)

def generate_detailed_sub_locations(settlement: Location, world_state: WorldState):
    """Generates detailed sub-locations with NPC operators for a settlement."""
    num_shops = (settlement.size[0] * settlement.size[1]) // 2
    num_services = (settlement.size[0] * settlement.size[1]) // 3

    for _ in range(num_shops):
        shop_type = random.choice(SUB_LOCATION_TYPES["Shop"])
        sub_loc_name = f"{settlement.name} {shop_type}"
        if any(sl.name == sub_loc_name for sl in settlement.sub_locations): continue

        sex = random.choice(["Male", "Female"])
        operator_name = f"{random.choice(MALE_NAMES if sex == 'Male' else FEMALE_NAMES)} {random.choice(FAMILY_NAMES)}"
        operator = NPC(
            name=operator_name, age=random.randint(25, 60), sex=sex, race=random.choice(RACES),
            status=shop_type, family_id="Merchant", role_in_family="Owner",
            location=settlement.name, difficulty_level=random.randint(4, 6), faction_membership="Merchants' Guild"
        )
        world_state.npcs[operator.name] = operator
        settlement.inhabitants.append(operator.name)
        settlement.sub_locations.append(SubLocation(name=sub_loc_name, type=shop_type, parent_location=settlement.name, operator_npc=operator.name))

def generate_sociological_layer(world_state: WorldState) -> WorldState:
    """v3.3: Generates factions, NPCs, creatures, sub-locations, and establishes complex faction relations."""
    print("[STATUS] Generating Sociological Layer...")

    # --- Step 1: Create Factions & Establish Relations ---
    for name, template in FACTION_TEMPLATES.items():
        world_state.factions[name] = Faction(name=name, **template)
    world_state.factions["Royal Court"] = Faction(name="Royal Court", disposition="Lawful Neutral", description="The inner circle of the monarch.")

    world_state.faction_relations = {
        "Mages' Guild": {"The Crimson Hand": "HOSTILE", "Merchants' Guild": "ALLIED", "Thieves' Guild": "NEUTRAL", "Royal Guard": "NEUTRAL"},
        "The Crimson Hand": {"Mages' Guild": "HOSTILE", "Thieves' Guild": "ALLIED", "Merchants' Guild": "NEUTRAL", "Royal Guard": "NEUTRAL"},
        "Merchants' Guild": {"Thieves' Guild": "HOSTILE", "Mages' Guild": "ALLIED", "The Crimson Hand": "NEUTRAL", "Royal Guard": "NEUTRAL"},
        "Thieves' Guild": {"Merchants' Guild": "HOSTILE", "The Crimson Hand": "ALLIED", "Mages' Guild": "NEUTRAL", "Royal Guard": "NEUTRAL"},
        "Royal Guard": {f: "NEUTRAL" for f in FACTION_TEMPLATES.keys() if f != "Royal Guard"}
    }
    print("[STATUS] Faction relations established.")

    # --- Step 2: Generate Population, Royalty, and Sub-Locations ---
    capital = next(loc for loc in world_state.world_map.values() if loc.type == "Capital")
    generate_royal_court(capital, world_state)

    for loc in world_state.world_map.values():
        if loc.type in ["Settlement", "Capital"]:
            num_families = loc.size[0] * loc.size[1]
            for _ in range(num_families):
                family_name = random.choice(FAMILY_NAMES)
                family_race = random.choice(RACES)
                for i in range(random.randint(1, 2)):
                    sex = "Male" if i == 0 else "Female"
                    name = f"{random.choice(MALE_NAMES if sex == 'Male' else FEMALE_NAMES)} {family_name}"
                    if name in world_state.npcs: continue
                    npc = NPC(name=name, age=random.randint(30, 55), sex=sex, race=family_race, status="Commoner",
                              family_id=family_name, role_in_family="Parent", location=loc.name, difficulty_level=random.randint(1, 3))
                    world_state.npcs[name] = npc
                    loc.inhabitants.append(name)

            generate_detailed_sub_locations(loc, world_state)

    # --- Step 3: Balanced Faction Membership & Leadership ---
    eligible_npcs = [npc for npc in world_state.npcs.values() if npc.status not in ["King", "Queen", "Royal Advisor", "Court Mage", "Spymaster", "General"]]
    random.shuffle(eligible_npcs)

    faction_roles = {
        "Royal Guard": ("Guard", 7), "Mages' Guild": ("Mage", 8),
        "Thieves' Guild": ("Thief", 6), "The Crimson Hand": ("Assassin", 9)
    }
    for fac_name, (role, diff) in faction_roles.items():
        num_members = len(eligible_npcs) // 5
        for _ in range(num_members):
            if not eligible_npcs: break
            npc = eligible_npcs.pop()
            npc.status = role
            npc.faction_membership = fac_name
            npc.difficulty_level = diff
            world_state.factions[fac_name].members.append(npc.name)

    for fac_name in ["Mages' Guild", "Thieves' Guild", "The Crimson Hand"]:
        faction = world_state.factions[fac_name]
        if faction.members:
            leader = world_state.npcs[random.choice(faction.members)]
            leader.difficulty_level += 1
            faction.leader = leader.name

    # --- Step 4: Generate Creatures in Dungeons ---
    print("[STATUS] Populating dungeons with creatures...")
    for loc in world_state.world_map.values():
        if loc.type == "Dungeon":
            generate_creatures_for_dungeon(loc, world_state)

    print(f"[STATUS] Sociological Layer complete. Generated {len(world_state.npcs)} NPCs and {len(world_state.creatures)} creatures.")
    return world_state
