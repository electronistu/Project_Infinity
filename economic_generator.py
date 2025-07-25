# economic_generator.py v3.0
# Layer 3 of the Generation Cascade. Distributes equipment and gold.

import json
import random
from models import WorldState, Item, NPC, Creature

# --- v3 Constants ---
TOTAL_WORLD_GOLD = 100000

# Defines the base "wealth score" for different roles.
WEALTH_HIERARCHY = {
    # NPCs
    "Merchant": 15,
    "Thief": 10,
    "Guard": 8,
    "Commoner": 3,
    "Child": 0,
    # Creatures
    "Boss": 50,
    "Hard": 10,
    "Medium": 5,
    "Easy": 2
}

# Maps roles to appropriate equipment tiers
EQUIPMENT_TIERS = {
    "Commoner": ["Padded Tunic", "Leather Trousers"],
    "Guard": ["Chainmail", "Iron Greaves", "Steel Shortsword", "Wooden Shield"],
    "Merchant": ["Silver Ring", "Padded Tunic"],
    "Thief": ["Leather Armor", "Iron Dagger"],
    "Default": ["Padded Tunic"]
}

def get_items_by_slot(item_list: list, slot: str) -> list:
    """Filters items from the master list by their equipment slot."""
    return [Item(**item) for item in item_list if item['slot'] == slot]

def equip_character(character, item_list: list):
    """v3: Equips a character with items based on their role and status."""
    if isinstance(character, NPC):
        tier_items = EQUIPMENT_TIERS.get(character.status, EQUIPMENT_TIERS["Default"])
    else: # It's a creature, for now they don't get equipment
        return

    for item_name in tier_items:
        for item_data in item_list:
            if item_data['name'] == item_name:
                slot = item_data['slot']
                if slot in character.equipment:
                    character.equipment[slot] = Item(**item_data)
                break

def generate_economic_layer(world_state: WorldState) -> WorldState:
    """
    v3: Manages the economic generation, focusing on equipment distribution and gold.
    """
    print("[STATUS] Generating Economic Layer...")

    # --- Step 1: Load Master Item List ---
    try:
        with open("items.json", "r") as f:
            item_list = json.load(f)
    except FileNotFoundError:
        print("[ERROR] items.json not found! Economic layer generation failed.")
        return world_state

    # --- Step 2: Equip all NPCs ---
    print("[STATUS] Distributing equipment to population...")
    for npc in world_state.npcs.values():
        equip_character(npc, item_list)

    # --- Step 3: Calculate Wealth Points & Distribute Gold ---
    total_wealth_points = 0
    entity_wealth_points = {}

    for npc in world_state.npcs.values():
        points = WEALTH_HIERARCHY.get(npc.status, 1)
        entity_wealth_points[npc.name] = points
        total_wealth_points += points

    for creature_id, creature in world_state.creatures.items():
        points = WEALTH_HIERARCHY.get(creature.difficulty_level, 1) # Using difficulty_level now
        entity_wealth_points[creature_id] = points
        total_wealth_points += points

    if total_wealth_points == 0:
        print("[WARNING] Total wealth points is zero. Cannot distribute gold.")
        return world_state

    gold_per_point = TOTAL_WORLD_GOLD / total_wealth_points

    for npc in world_state.npcs.values():
        npc.gold = int(entity_wealth_points[npc.name] * gold_per_point)

    for creature_id, creature in world_state.creatures.items():
        creature.gold = int(entity_wealth_points[creature_id] * gold_per_point)

    world_state.total_world_gold = TOTAL_WORLD_GOLD

    # --- Step 4: Generate Loot for Creatures ---
    # This can be expanded later to be more sophisticated
    for creature in world_state.creatures.values():
        if "Goblin" in creature.name:
            creature.loot.append(Item(name="Goblin Ear", type="Misc", slot="none", base_value=1))
        elif "Wolf" in creature.name:
            creature.loot.append(Item(name="Wolf Pelt", type="Misc", slot="none", base_value=5))


    print(f"[STATUS] Economic Layer complete. Distributed equipment and {TOTAL_WORLD_GOLD} gold.")
    return world_state
