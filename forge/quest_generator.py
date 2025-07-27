from .models import Quest, WorldState
from .config_loader import Config
import random
from typing import List

def generate_quests(world_state: WorldState, config: Config) -> List[Quest]:
    """Generates a list of dynamic, context-aware quests."""
    quests = []
    if not any(k.locations for k in world_state.kingdoms): return []

    for _ in range(5): # Generate 5 quests
        kingdom = random.choice(world_state.kingdoms)
        if not kingdom.locations: continue

        template = random.choice(config.quests)
        quest = None

        if template.type == "Bounty" and any(loc.creatures for loc in kingdom.locations):
            dungeon = random.choice([loc for loc in kingdom.locations if loc.creatures])
            creature = random.choice(dungeon.creatures)
            title = template.title.replace("<creature_name>", creature.name).replace("<dungeon_name>", dungeon.name)
            description = template.description.replace("<creature_name>", creature.name).replace("<dungeon_name>", dungeon.name)
            reward = template.reward_template.replace("<random_gold>", str(random.randint(50, 250)))
            quest = Quest(title=title, description=description, giver="Local Guard Captain", reward=reward)

        elif template.type == "Fetch" and any(loc.npcs for loc in kingdom.locations) and any(loc.loot for loc in kingdom.locations):
            start_location = random.choice([loc for loc in kingdom.locations if loc.npcs])
            dungeon = random.choice([loc for loc in kingdom.locations if loc.loot])
            npc_giver = random.choice(start_location.npcs)
            item_to_fetch = random.choice(dungeon.loot)
            title = template.title.replace("<item_name>", item_to_fetch.name).replace("<npc_name>", npc_giver.name)
            description = template.description.replace("<npc_name>", npc_giver.name).replace("<location_name>", start_location.name).replace("<item_name>", item_to_fetch.name).replace("<dungeon_name>", dungeon.name)
            reward = template.reward_template
            quest = Quest(title=title, description=description, giver=npc_giver.name, reward=reward)

        elif template.type == "Delivery" and len(kingdom.locations) > 1:
            start_location, dest_location = random.sample([loc for loc in kingdom.locations if loc.npcs], 2)
            giver_npc = random.choice(start_location.npcs)
            receiver_npc = random.choice(dest_location.npcs)
            title = template.title.replace("<destination_location>", dest_location.name)
            description = template.description.replace("<giver_npc_name>", giver_npc.name).replace("<start_location>", start_location.name).replace("<receiver_npc_name>", receiver_npc.name).replace("<destination_location>", dest_location.name)
            reward = template.reward_template
            quest = Quest(title=title, description=description, giver=giver_npc.name, reward=reward)

        if quest:
            quests.append(quest)
            
    return quests