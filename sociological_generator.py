# sociological_generator.py
# Layer 2 of the Generation Cascade. Populates the world with factions and people.
# (Patched to ensure balanced faction distribution)

import random
from models import WorldState, Faction, NPC

# --- Name/Data Components ---
MALE_NAMES = ["Roric", "Gunnar", "Falk", "Bjorn", "Sten", "Arne", "Erik"]
FEMALE_NAMES = ["Astrid", "Freya", "Ingrid", "Sigrid", "Hilda", "Gerda", "Sif"]
FAMILY_NAMES = ["Ironhand", "Stonefist", "Longbeard", "Swiftfoot", "Greymane"]
RACES = ["Human", "Elf", "Dwarf"]

# --- PATCHED: "Thief" is now a primary faction template ---
FACTION_TEMPLATES = {
    "Town Guard": {"disposition": "Lawful", "description": "The official protectors of the settlement."},
    "Merchants' Guild": {"disposition": "Neutral", "description": "A powerful consortium of traders and artisans."},
    "Thieves' Guild": {"disposition": "Chaotic", "description": "A shadowy network of spies, burglars, and smugglers operating in plain sight."}
}

def designate_faction_leadership(world_state: WorldState):
    """
    Calculates a leadership score for all faction members and designates a leader.
    This logic is handled by the Forge, not the GM.
    """
    print("[STATUS] Designating faction leadership...")
    for faction in world_state.factions.values():
        if not faction.members:
            continue

        leader = None
        max_score = -1

        for member_name in faction.members:
            npc = world_state.npcs.get(member_name)
            if not npc:
                continue

            score = 0
            if npc.age > 20:
                score += (npc.age - 20) // 10
            if npc.status in ["Merchant", "Guard", "Thief"]: # Added Thief
                score += 5
            elif npc.status == "Commoner":
                score += 1
            if npc.role_in_family == "Parent":
                score += 3

            if score > max_score:
                max_score = score
                leader = npc
            elif score == max_score and leader and npc.age > leader.age:
                leader = npc
        
        if leader:
            faction.leader = leader.name
            print(f"[INFO] {leader.name} designated leader of {faction.name}.")


def generate_sociological_layer(world_state: WorldState) -> WorldState:
    """
    Generates factions and populates settlements with diverse family units,
    ensuring a balanced distribution of members among the primary factions.
    """
    print("[STATUS] Generating Sociological Layer...")

    # --- Step 1: Create the three primary factions guaranteed ---
    world_faction_names = ["Town Guard", "Merchants' Guild", "Thieves' Guild"]
    for fac_name in world_faction_names:
        if fac_name not in world_state.factions:
            template = FACTION_TEMPLATES[fac_name]
            faction = Faction(name=fac_name, **template)
            world_state.factions[fac_name] = faction

    # --- Step 2: Generate NPCs, defaulting all adults to "Commoner" initially ---
    for loc_name, location in world_state.world_map.items():
        if location.type == "Settlement":
            num_families = random.randint(2, 4)
            for _ in range(num_families):
                family_name = random.choice(FAMILY_NAMES)
                family_race = random.choice(RACES)
                # Generate Parents
                for i in range(random.randint(1, 2)):
                    sex = "Male" if i == 0 else "Female"
                    name = f"{random.choice(MALE_NAMES if sex == 'Male' else FEMALE_NAMES)} {family_name}"
                    if name in world_state.npcs: continue
                    
                    # All adults start as Commoners; roles are assigned later.
                    npc = NPC(name=name, age=random.randint(30, 55), sex=sex, race=family_race, status="Commoner", family_id=family_name, role_in_family="Parent", location=loc_name)
                    world_state.npcs[name] = npc
                    location.inhabitants.append(name)
                # Generate Children
                num_children = random.choices([0, 1, 2], weights=[0.4, 0.4, 0.2], k=1)[0]
                for _ in range(num_children):
                    sex = random.choice(["Male", "Female"])
                    name = f"{random.choice(MALE_NAMES if sex == 'Male' else FEMALE_NAMES)} {family_name}"
                    if name in world_state.npcs: continue
                    npc = NPC(name=name, age=random.randint(5, 16), sex=sex, race=family_race, status="Child", family_id=family_name, role_in_family="Child", location=loc_name)
                    world_state.npcs[name] = npc
                    location.inhabitants.append(name)

    # --- Step 3: Balanced Faction Membership Allocation ---
    print("[STATUS] Allocating faction roles for balanced distribution...")
    
    # Gather all eligible adults into a pool
    eligible_adults = [npc for npc in world_state.npcs.values() if npc.role_in_family != "Child"]
    random.shuffle(eligible_adults) # Shuffle for random assignment

    # Calculate how many members each faction gets
    num_factions = len(world_faction_names)
    base_members_per_faction = len(eligible_adults) // num_factions
    remainder = len(eligible_adults) % num_factions

    faction_member_counts = {name: base_members_per_faction for name in world_faction_names}
    # Distribute the remainder
    for i in range(remainder):
        faction_member_counts[world_faction_names[i]] += 1

    # Assign roles and faction memberships
    current_npc_index = 0
    role_map = {
        "Town Guard": "Guard",
        "Merchants' Guild": "Merchant",
        "Thieves' Guild": "Thief"
    }

    for faction_name, count in faction_member_counts.items():
        for _ in range(count):
            if current_npc_index < len(eligible_adults):
                npc_to_assign = eligible_adults[current_npc_index]
                
                # Update the NPC's status and faction membership
                npc_to_assign.status = role_map[faction_name]
                npc_to_assign.faction_membership = faction_name
                
                # Add the NPC to the faction's member list
                world_state.factions[faction_name].members.append(npc_to_assign.name)
                
                current_npc_index += 1

    # --- Step 4: Designate Faction Leadership ---
    designate_faction_leadership(world_state)

    print(f"[STATUS] Sociological Layer complete. Generated {len(world_state.factions)} factions and {len(world_state.npcs)} NPCs.")
    for name, count in faction_member_counts.items():
        print(f"[INFO] {name} assigned {count} members.")
        
    return world_state
