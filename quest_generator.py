# quest_generator.py v3.0
# Layer 4 of the Generation Cascade. Creates a structured quest system with chains and faction dynamics.

import random
import uuid
from models import WorldState, Quest, Item, NPC

# --- v3 Quest Templates ---
QUEST_TEMPLATES = {
    "Easy": {
        "Fetch": "A Simple Errand: Fetch a {target} for {giver}.",
        "Kill": "Pest Control: Kill {target}s near {location}.",
    },
    "Medium": {
        "Fetch": "Faction Supplies: Secure a shipment of {target}s for the {faction}.",
        "Kill": "A Rival's End: Eliminate {target}, a thorn in our side.",
        "Clear": "Reclaim the Ruins: Clear the {target} of its foul inhabitants.",
        "Escort": "A Safe Passage: Escort {target} to {destination}."
    },
    "Hard": {
        "Kill": "Decapitation Strike: Assassinate {target}, the leader of the {faction}.",
        "Clear": "Purge the Depths: Purge the {target} of its ultimate evil.",
        "Investigate": "A Shadowy Plot: Investigate rumors of a conspiracy in {location}."
    }
}

def find_quest_givers(world_state: WorldState):
    """Categorizes NPCs by their suitability as quest givers."""
    givers = {"Easy": [], "Medium": [], "Hard": []}
    for npc in world_state.npcs.values():
        if npc.difficulty_level <= 2 and npc.status == "Commoner":
            givers["Easy"].append(npc)
        elif npc.difficulty_level <= 4:
            givers["Medium"].append(npc)
        else:
            givers["Hard"].append(npc)
    return givers

def generate_quest_layer(world_state: WorldState) -> WorldState:
    """
    v3: Generates the entire quest and reputation framework.
    """
    print("[STATUS] Generating Quest & Reputation Layer...")

    # --- Step 1: Establish Faction Relations ---
    world_state.faction_relations = {
        "Merchants' Guild": {"Thieves' Guild": "HOSTILE", "Town Guard": "NEUTRAL"},
        "Thieves' Guild": {"Merchants' Guild": "HOSTILE", "Town Guard": "NEUTRAL"},
        "Town Guard": {"Merchants' Guild": "NEUTRAL", "Thieves' Guild": "NEUTRAL"}
    }

    # --- Step 2: Find Quest Givers and Potential Targets ---
    quest_givers = find_quest_givers(world_state)
    creatures_by_difficulty = {1: [], 2: [], 3: [], 4: [], 5: []}
    for c in world_state.creatures.values():
        creatures_by_difficulty[c.difficulty_level].append(c)

    dungeons = [loc for loc in world_state.world_map.values() if loc.type == "Dungeon"]
    settlements = [loc for loc in world_state.world_map.values() if loc.type == "Settlement"]

    # --- Step 3: Generate Easy Quests (Tier 1) ---
    generated_quests = []
    if quest_givers["Easy"]:
        for i in range(random.randint(3, 5)):
            giver = random.choice(quest_givers["Easy"])
            quest_type = random.choice(["Fetch", "Kill"])

            if quest_type == "Fetch":
                target = "Healing Poultice"
                title = QUEST_TEMPLATES["Easy"]["Fetch"].format(target=target, giver=giver.name)
                desc = f"{giver.name} has asked you to bring them a {target}. They seem to be in need."
            else: # Kill
                if not creatures_by_difficulty.get("Easy"): continue
                target_creature = random.choice(creatures_by_difficulty["Easy"])
                target = f"3 {target_creature.name}"
                title = QUEST_TEMPLATES["Easy"]["Kill"].format(target=target_creature.name, location=giver.location)
                desc = f"Goblins and wolves are harassing the outskirts of {giver.location}. {giver.name} wants someone to thin their numbers."

            quest = Quest(id=str(uuid.uuid4()), title=title, type=quest_type, giver_npc=giver.name, target=target,
                          reward_gold=random.randint(10, 25), required_reputation=0, description=desc)
            world_state.quests[quest.id] = quest
            generated_quests.append(quest)

    # --- Step 4: Generate Medium Quests (Tier 2) ---
    if quest_givers["Medium"] and generated_quests:
        for giver in quest_givers["Medium"]:
            prereq = random.choice(generated_quests)
            quest_type = random.choice(["Fetch", "Kill", "Clear", "Escort"])
            faction = giver.faction_membership

            if quest_type == "Clear":
                if not dungeons: continue
                target_dungeon = random.choice(dungeons)
                target = target_dungeon.name
                title = QUEST_TEMPLATES["Medium"]["Clear"].format(target=target)
                desc = f"The {faction} wants to secure the area around {target}. They've tasked you with clearing it out."
            elif quest_type == "Escort":
                if not settlements or len(settlements) < 2: continue
                destination = random.choice([s for s in settlements if s.name != giver.location])
                target_npc = random.choice([n for n in world_state.npcs.values() if n.location == giver.location and n.status == 'Commoner'])
                if not target_npc: continue
                target = target_npc.name
                title = QUEST_TEMPLATES["Medium"]["Escort"].format(target=target, destination=destination.name)
                desc = f"{giver.name} needs you to escort {target} safely to {destination.name}."
            else: # Fetch or Kill
                target = "some valuable goods" # Placeholder
                title = f"A Task for the {faction}"
                desc = f"{giver.name} has a special task for you on behalf of the {faction}."

            quest = Quest(id=str(uuid.uuid4()), title=title, type=quest_type, giver_npc=giver.name, target=target,
                          reward_gold=random.randint(50, 150), prerequisite_quest=prereq.id, required_reputation=10, description=desc)
            world_state.quests[quest.id] = quest
            generated_quests.append(quest)

    # --- Step 5: Generate Hard Quests (Tier 3) ---
    medium_quests = [q for q in generated_quests if q.required_reputation == 10]
    if quest_givers["Hard"] and medium_quests:
        for giver in quest_givers["Hard"]:
            faction = giver.faction_membership
            possible_prereqs = [q for q in medium_quests if world_state.npcs[q.giver_npc].faction_membership == faction]
            if not possible_prereqs: continue
            prereq = random.choice(possible_prereqs)

            quest_type = random.choice(["Kill", "Clear", "Investigate"])

            if quest_type == "Investigate":
                target = giver.location
                title = QUEST_TEMPLATES["Hard"]["Investigate"].format(location=target)
                desc = f"{giver.name} has heard whispers of a conspiracy within {target} and needs you to uncover the truth."
            else: # Kill or Clear
                rival_faction = "Thieves' Guild" if faction == "Merchants' Guild" else "Merchants' Guild"
                rival_leader = world_state.factions[rival_faction].leader
                if not rival_leader: continue

                target = rival_leader
                title = QUEST_TEMPLATES["Hard"]["Kill"].format(target=target, faction=rival_faction)
                desc = f"The time has come to strike a decisive blow. {giver.name} has ordered the assassination of {target}, the leader of the rival {rival_faction}."

            quest = Quest(id=str(uuid.uuid4()), title=title, type=quest_type, giver_npc=giver.name, target=target,
                          reward_gold=random.randint(500, 1000), prerequisite_quest=prereq.id, required_reputation=25, description=desc)
            world_state.quests[quest.id] = quest
            generated_quests.append(quest)

    print(f"[STATUS] Quest & Reputation Layer complete. Generated {len(world_state.quests)} quests.")
    return world_state
