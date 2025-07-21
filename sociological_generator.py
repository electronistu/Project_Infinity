# sociological_generator.py v2.4
# Adds special sub-location generation for the Capital city.

import random
from models import WorldState, Faction, NPC, Creature, SubLocation, Location

# --- v2 Name/Data Components ---
MALE_NAMES = ["Roric", "Gunnar", "Falk", "Bjorn", "Sten", "Arne", "Erik"]
FEMALE_NAMES = ["Astrid", "Freya", "Ingrid", "Sigrid", "Hilda", "Gerda", "Sif"]
FAMILY_NAMES = ["Ironhand", "Stonefist", "Longbeard", "Swiftfoot", "Greymane"]
RACES = ["Human", "Elf", "Dwarf"]
FACTION_TEMPLATES = {
    "Town Guard": {"disposition": "Lawful", "description": "The official protectors of the settlement."},
    "Merchants' Guild": {"disposition": "Neutral", "description": "A powerful consortium of traders and artisans."},
    "Thieves' Guild": {"disposition": "Chaotic", "description": "A shadowy network of spies, burglars, and smugglers."}
}
CREATURE_TYPES = {
    "Goblin": {"challenge_level": 1, "difficulty": "Easy"},
    "Wolf": {"challenge_level": 2, "difficulty": "Easy"},
    "Orc": {"challenge_level": 4, "difficulty": "Medium"},
    "Troll": {"challenge_level": 7, "difficulty": "Hard"},
    "Ogre": {"challenge_level": 6, "difficulty": "Hard"},
    "Dungeon Boss": {"challenge_level": 10, "difficulty": "Boss"}
}

def generate_creatures_for_dungeon(dungeon: Location, world_state: WorldState):
    """Generates and places creatures within a specific dungeon's footprint."""
    num_creatures = dungeon.size[0] * dungeon.size[1] * random.randint(1, 3)
    dungeon_x, dungeon_y = dungeon.coordinates
    
    # CORRECTED: Convert to list THEN slice.
    possible_creatures = list(CREATURE_TYPES.keys())[:-1]
    if not possible_creatures: return # Failsafe if CREATURE_TYPES is too small

    for i in range(num_creatures):
        creature_type = random.choice(possible_creatures)
        stats = CREATURE_TYPES[creature_type]
        
        coord_x = dungeon_x + random.randint(0, dungeon.size[0] - 1)
        coord_y = dungeon_y + random.randint(0, dungeon.size[1] - 1)
        
        creature_id = f"{creature_type}_{i}_{dungeon.name}"
        creature = Creature(
            name=creature_type, type=creature_type, challenge_level=stats["challenge_level"],
            location=dungeon.name, coordinates=(coord_x, coord_y), difficulty=stats["difficulty"]
        )
        world_state.creatures[creature_id] = creature

    if dungeon.challenge_level >= 9:
        boss_stats = CREATURE_TYPES["Dungeon Boss"]
        coord_x = dungeon_x + random.randint(0, dungeon.size[0] - 1)
        coord_y = dungeon_y + random.randint(0, dungeon.size[1] - 1)
        boss = Creature(
            name="Dungeon Lord", type="Dungeon Boss", challenge_level=boss_stats["challenge_level"],
            location=dungeon.name, coordinates=(coord_x, coord_y), difficulty=boss_stats["difficulty"]
        )
        world_state.creatures[f"boss_{dungeon.name}"] = boss


def generate_sociological_layer(world_state: WorldState) -> WorldState:
    """
    v2.3: Generates factions, NPCs, creatures, and sub-locations, then assigns roles and difficulty tiers.
    """
    print("[STATUS] Generating Sociological Layer...")

    # --- Step 1: Create Factions ---
    for name, template in FACTION_TEMPLATES.items():
        world_state.factions[name] = Faction(name=name, **template)

    # --- Step 2: Generate NPC Population for Settlements ---
    for loc in world_state.world_map.values():
        if loc.type == "Settlement":
            num_families = loc.size[0] * loc.size[1] * random.randint(1, 2)
            for _ in range(num_families):
                family_name = random.choice(FAMILY_NAMES)
                family_race = random.choice(RACES)
                # Parents
                for i in range(random.randint(1, 2)):
                    sex = "Male" if i == 0 else "Female"
                    name = f"{random.choice(MALE_NAMES if sex == 'Male' else FEMALE_NAMES)} {family_name}"
                    if name in world_state.npcs: continue
                    npc = NPC(name=name, age=random.randint(30, 55), sex=sex, race=family_race, status="Commoner", 
                              family_id=family_name, role_in_family="Parent", location=loc.name, difficulty="Easy")
                    world_state.npcs[name] = npc
                    loc.inhabitants.append(name)
                # Children
                for _ in range(random.choices([0, 1, 2], weights=[0.4, 0.4, 0.2], k=1)[0]):
                    sex = random.choice(["Male", "Female"])
                    name = f"{random.choice(MALE_NAMES if sex == 'Male' else FEMALE_NAMES)} {family_name}"
                    if name in world_state.npcs: continue
                    npc = NPC(name=name, age=random.randint(5, 16), sex=sex, race=family_race, status="Child", 
                              family_id=family_name, role_in_family="Child", location=loc.name, difficulty="Easy")
                    world_state.npcs[name] = npc
                    loc.inhabitants.append(name)

    # --- Step 3: Balanced Faction Membership Allocation ---
    eligible_adults = [npc for npc in world_state.npcs.values() if npc.role_in_family != "Child"]
    random.shuffle(eligible_adults)
    num_factions = len(FACTION_TEMPLATES)
    base_members = len(eligible_adults) // (num_factions + 1)
    
    role_map = {"Town Guard": "Guard", "Merchants' Guild": "Merchant", "Thieves' Guild": "Thief"}
    faction_names = list(role_map.keys())
    
    npc_idx = 0
    for fac_name in faction_names:
        for _ in range(base_members):
            if npc_idx < len(eligible_adults):
                npc = eligible_adults[npc_idx]
                npc.status = role_map[fac_name]
                npc.faction_membership = fac_name
                world_state.factions[fac_name].members.append(npc.name)
                npc_idx += 1

    # --- Step 4: Generate Sub-Locations and Assign Operators ---
    print("[STATUS] Establishing businesses and assigning operators...")
    for settlement in world_state.world_map.values():
        if settlement.type not in ["Settlement", "Capital"]: continue
        
        # v2.4 CHANGE: Capital gets guaranteed faction HQs
        if settlement.type == "Capital":
            guaranteed_roles = ["Merchant", "Guard", "Thief"]
        else:
            guaranteed_roles = random.sample(["Merchant", "Guard", "Commoner"], k=2)

        operators = {role: None for role in guaranteed_roles}
        # Add a commoner for a tavern if not already selected
        if "Commoner" not in operators:
            operators["Commoner"] = None

        # Find available operators in this settlement
        available_npcs = [npc for name in settlement.inhabitants if (npc := world_state.npcs.get(name))]
        random.shuffle(available_npcs)

        for npc in available_npcs:
            if npc.status in operators and operators[npc.status] is None:
                operators[npc.status] = npc.name
        
        # Create sub-locations based on assigned operators
        if operators.get("Merchant"):
            settlement.sub_locations.append(SubLocation(name=f"{settlement.name} Merchant's Guild", type="Shop", parent_location=settlement.name, operator_npc=operators["Merchant"]))
        if operators.get("Guard"):
            settlement.sub_locations.append(SubLocation(name=f"{settlement.name} Barracks", type="Guard Post", parent_location=settlement.name, operator_npc=operators["Guard"]))
        if operators.get("Thief"):
            settlement.sub_locations.append(SubLocation(name="The Gilded Shadow", type="Thieves' Den", parent_location=settlement.name, operator_npc=operators["Thief"]))
        if operators.get("Commoner"):
            settlement.sub_locations.append(SubLocation(name="The Weary Traveler Tavern", type="Tavern", parent_location=settlement.name, operator_npc=operators["Commoner"]))

    # --- Step 5: Assign Difficulty Tiers & Designate Leadership ---
    for faction in world_state.factions.values():
        if faction.members:
            leader = random.choice(faction.members)
            faction.leader = leader
    
    for npc in world_state.npcs.values():
        if npc.name in [f.leader for f in world_state.factions.values() if f.leader]:
            npc.difficulty = "Hard"
        elif npc.status in ["Guard", "Merchant", "Thief"]:
            npc.difficulty = "Medium"

    # --- Step 6: Generate Creatures ---
    print("[STATUS] Populating dungeons with creatures...")
    for loc in world_state.world_map.values():
        if loc.type == "Dungeon":
            generate_creatures_for_dungeon(loc, world_state)

    print(f"[STATUS] Sociological Layer complete. Generated {len(world_state.npcs)} NPCs and {len(world_state.creatures)} creatures.")
    return world_state
