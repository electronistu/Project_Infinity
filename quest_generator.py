# quest_generator.py v3.2
# Layer 4 of the Generation Cascade. Creates a 10-tier quest system with guild-specific quest chains.

import random
import uuid
from models import WorldState, Quest, NPC

# --- v3.2 Quest Templates (Expanded to 10 Tiers) ---
QUEST_TEMPLATES = {
    # Tier 1-2: Simple local tasks
    1: {"Fetch": "A Simple Errand: Fetch a {target} for {giver}.", "Kill": "Pest Control: Kill {target_num} {target_name}s near {location}."},
    2: {"Clear": "Clearing the Way: Clear the {target} of its minor threats.", "Escort": "A Safe Passage: Escort {target} to {destination}."},
    # Tier 3-4: More complex, involve travel
    3: {"Investigate": "Whispers in the Dark: Investigate strange happenings at {location}.", "Kill": "Bounty: A bounty has been posted for {target_name} in {location}."},
    4: {"Fetch": "Rare Components: {giver} needs a rare {target} from the dangerous {location}.", "Clear": "Reclaiming the Past: Clear the monsters from the {target} ruins."},
    # Tier 5-6: Faction-level importance
    5: {"Kill": "A Rival's End: Eliminate {target}, a thorn in the side of the {faction}.", "Escort": "High-Value Target: Escort {target}, a person of interest, to {destination}."},
    6: {"Investigate": "Spy Hunt: Uncover the spy operating within {location} for the {faction}.", "Clear": "Securing the Borderlands: Clear the {target} to secure a vital border."},
    # Tier 7-8: Kingdom-level importance
    7: {"Kill": "Decapitation Strike: Assassinate {target}, a leader of the rival {faction}.", "Fetch": "Ancient Relic: Retrieve the {target} from the depths of {location}."},
    8: {"Investigate": "A Shadowy Conspiracy: Unravel a conspiracy that threatens the entire kingdom, starting in {location}.", "Clear": "Purge the Depths: Purge the {target} of its powerful and ancient evil."},
    # Tier 9-10: World-threatening events
    9: {"Kill": "Slaying the Beast: Hunt down and slay the legendary {target} that terrorizes the region.", "Escort": "Protect the Oracle: Escort the Oracle of {location} to the capital to deliver a vital prophecy."},
    10: {"Clear": "The Final Bastion: Clear the {target}, the source of the encroaching darkness, and defeat its Overlord.", "Kill": "End of a Tyrant: Infiltrate the fortress and defeat the tyrannical {target} to save the kingdom."}
}

# --- v3.2 Guild Quest Chains ---
# Defines the 10-step quest chain for each major guild.
GUILD_QUEST_CHAINS = {
    "Royal Guard": {
        1: {"title": "Initiate's Trial: Prove Your Worth", "type": "Kill", "target": "5 Goblins"},
        2: {"title": "Patrol Duty: Secure the Roads", "type": "Clear", "target": "a nearby cave"},
        3: {"title": "The Missing Caravan", "type": "Investigate", "target": "a trade route"},
        4: {"title": "Bandit Leader's Bounty", "type": "Kill", "target": "a Bandit Captain"},
        5: {"title": "A Show of Force", "type": "Clear", "target": "an Orc encampment"},
        6: {"title": "Protect the Emissary", "type": "Escort", "target": "a Noble Emissary"},
        7: {"title": "The Traitor Within", "type": "Investigate", "target": "the Royal Guard barracks"},
        8: {"title": "Siege Breaker", "type": "Clear", "target": "a besieged fortress"},
        9: {"title": "Dragon's Bane", "type": "Kill", "target": "a Young Dragon"},
        10: {"title": "For the Crown", "type": "Kill", "target": "a Rebel Warlord"}
    },
    "Mages' Guild": {
        1: {"title": "Arcane Components", "type": "Fetch", "target": "5 Glow-Shrooms"},
        2: {"title": "A Minor Haunting", "type": "Clear", "target": "a haunted cellar"},
        3: {"title": "The Rogue Apprentice", "type": "Kill", "target": "a Rogue Apprentice"},
        4: {"title": "Retrieve the Lost Grimoire", "type": "Fetch", "target": "a Lost Grimoire"},
        5: {"title": "Elemental Imbalance", "type": "Clear", "target": "a cave of angry elementals"},
        6: {"title": "Consult the Oracle", "type": "Escort", "target": "an Oracle"},
        7: {"title": "The Necromancer's Cult", "type": "Investigate", "target": "a hidden cult"},
        8: {"title": "The Source of Corruption", "type": "Clear", "target": "a corrupted mana rift"},
        9: {"title": "The Lich's Phylactery", "type": "Fetch", "target": "a Lich's Phylactery"},
        10: {"title": "Archmage's Gambit", "type": "Kill", "target": "a powerful Lich"}
    },
    "The Crimson Hand": {
        1: {"title": "The First Cut", "type": "Kill", "target": "a low-level merchant"},
        2: {"title": "A Message Delivered", "type": "Kill", "target": "a city guard captain"},
        3: {"title": "Silence the Witness", "type": "Kill", "target": "a witness in hiding"},
        4: {"title": "The Poisoner's Kiss", "type": "Kill", "target": "a minor noble"},
        5: {"title": "A Rival's Demise", "type": "Kill", "target": "the Thieves' Guild spymaster"},
        6: {"title": "The Infiltrator", "type": "Investigate", "target": "the Mages' Guild library"},
        7: {"title": "The Price of Betrayal", "type": "Kill", "target": "a traitorous Crimson Hand member"},
        8: {"title": "Death to the Spymaster", "type": "Kill", "target": "the Royal Spymaster"},
        9: {"title": "The King's General", "type": "Kill", "target": "the General of the Royal Guard"},
        10: {"title": "Checkmate", "type": "Kill", "target": "the Monarch"}
    }
}


def generate_quest_layer(world_state: WorldState) -> WorldState:
    """
    v3.2: Generates a 10-tier quest system, including major guild quest chains.
    """
    print("[STATUS] Generating Quest & Reputation Layer...")

    # --- Step 1: Categorize Targets ---
    creatures_by_difficulty = {i: [] for i in range(1, 11)}
    for c in world_state.creatures.values():
        if 1 <= c.difficulty_level <= 10:
            creatures_by_difficulty[c.difficulty_level].append(c)

    dungeons_by_difficulty = {i: [] for i in range(1, 11)}
    for d in world_state.world_map.values():
        if d.type == "Dungeon" and 1 <= d.challenge_level <= 10:
            dungeons_by_difficulty[d.challenge_level].append(d)

    # --- Step 2: Generate Generic Quests ---
    num_generic_quests = random.randint(40, 60)
    for _ in range(num_generic_quests):
        tier = random.randint(1, 10)
        quest_type = random.choice(list(QUEST_TEMPLATES[tier].keys()))
        template = QUEST_TEMPLATES[tier][quest_type]

        # Find a suitable giver (any non-child NPC)
        giver = random.choice([npc for npc in world_state.npcs.values() if npc.status != "Child"])
        
        target_str = "an unknown target"
        if quest_type == "Kill":
            if not creatures_by_difficulty[tier]: continue
            target_creature = random.choice(creatures_by_difficulty[tier])
            target_str = f"{random.randint(2,5)} {target_creature.name}"
        elif quest_type == "Clear":
            if not dungeons_by_difficulty[tier]: continue
            target_dungeon = random.choice(dungeons_by_difficulty[tier])
            target_str = target_dungeon.name
        
        quest = Quest(
            id=str(uuid.uuid4()),
            title=template.format(target=target_str, giver=giver.name, location=giver.location, target_num=random.randint(3, 6), target_name=target_str.split(" ")[-1], destination="another town", faction="a local faction"),
            type=quest_type,
            giver_npc=giver.name,
            target=target_str,
            reward_gold=tier * 50 * random.randint(1, 5),
            required_reputation= (tier -1) * 5,
            description=f"{giver.name} needs help with a task of difficulty level {tier}.",
            tier=tier
        )
        world_state.quests[quest.id] = quest

    # --- Step 3: Generate Guild Quest Chains ---
    print("[STATUS] Generating major guild quest chains...")
    for guild_name, chain in GUILD_QUEST_CHAINS.items():
        if guild_name not in world_state.factions: continue
        
        leader_name = world_state.factions[guild_name].leader
        if not leader_name: continue

        previous_quest_id = None
        for tier in sorted(chain.keys()):
            quest_data = chain[tier]
            
            quest = Quest(
                id=str(uuid.uuid4()),
                title=f"{guild_name} ({tier}/10): {quest_data['title']}",
                type=quest_data['type'],
                giver_npc=leader_name,
                target=quest_data['target'],
                reward_gold=tier * 100 * random.randint(tier, tier * 2),
                required_reputation= (tier - 1) * 10,
                description=f"The next step in the {guild_name} questline.",
                prerequisite_quest=previous_quest_id,
                tier=tier
            )
            world_state.quests[quest.id] = quest
            previous_quest_id = quest.id

    print(f"[STATUS] Quest & Reputation Layer complete. Generated {len(world_state.quests)} quests.")
    return world_state