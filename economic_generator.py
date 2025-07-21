# economic_generator.py v2
# Layer 3 of the Generation Cascade. Creates a finite economy, distributes gold, and generates inventories.

import json
import random
from models import WorldState, Item

# --- v2 Constants ---
TOTAL_WORLD_GOLD = 100000
INVENTORY_BUDGET_PERCENTAGE = (0.2, 0.5) # Each NPC spends 20-50% of their gold on items

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

def generate_inventory(entity, budget: int, item_list: list) -> list:
    """Generates a list of items for an entity based on a budget."""
    inventory = []
    
    # Filter items appropriate for the entity's role
    if hasattr(entity, 'status'): # It's an NPC
        if entity.status == "Guard":
            possible_items = [i for i in item_list if i['type'] in ['Weapon', 'Armor']]
        elif entity.status == "Thief":
            possible_items = [i for i in item_list if i['type'] in ['Weapon', 'Misc']]
        else: # Merchants and Commoners can have a wider variety
            possible_items = item_list
    else: # It's a Creature
        possible_items = [i for i in item_list if i['type'] in ['Misc', 'Consumable']]

    if not possible_items:
        return []

    # Spend the budget
    while budget > 0:
        affordable_items = [i for i in possible_items if i['base_value'] <= budget and i['base_value'] > 0]
        if not affordable_items:
            break
        
        chosen_item_dict = random.choice(affordable_items)
        inventory.append(Item(**chosen_item_dict))
        budget -= chosen_item_dict['base_value']
        
    return inventory


def generate_economic_layer(world_state: WorldState) -> WorldState:
    """
    v2: Manages the entire economic generation cascade.
    """
    print("[STATUS] Generating Economic Layer...")

    # --- Step 1: Load Master Item List ---
    try:
        with open("items.json", "r") as f:
            item_list = json.load(f)
    except FileNotFoundError:
        print("[ERROR] items.json not found! Economic layer generation failed.")
        return world_state

    # --- Step 2: Calculate Wealth Points for all Entities ---
    total_wealth_points = 0
    entity_wealth_points = {}

    # Pre-calculate family sizes for the family bonus
    family_counts = {}
    for npc in world_state.npcs.values():
        if npc.family_id not in family_counts:
            family_counts[npc.family_id] = 0
        family_counts[npc.family_id] += 1

    # Calculate points for NPCs
    for npc in world_state.npcs.values():
        base_score = WEALTH_HIERARCHY.get(npc.status, 1)
        family_bonus = 1 + ((family_counts.get(npc.family_id, 1) - 1) * 0.1) # 10% bonus per extra family member
        points = base_score * family_bonus
        entity_wealth_points[npc.name] = points
        total_wealth_points += points

    # Calculate points for Creatures
    for creature_id, creature in world_state.creatures.items():
        points = WEALTH_HIERARCHY.get(creature.difficulty, 1)
        entity_wealth_points[creature_id] = points
        total_wealth_points += points

    if total_wealth_points == 0:
        print("[WARNING] Total wealth points is zero. Cannot distribute gold.")
        return world_state

    # --- Step 3: Distribute Gold ---
    print("[STATUS] Distributing world gold...")
    gold_per_point = TOTAL_WORLD_GOLD / total_wealth_points
    
    for npc in world_state.npcs.values():
        npc.gold = int(entity_wealth_points[npc.name] * gold_per_point)
        
    for creature_id, creature in world_state.creatures.items():
        creature.gold = int(entity_wealth_points[creature_id] * gold_per_point)

    world_state.total_world_gold = TOTAL_WORLD_GOLD

    # --- Step 4: Generate Inventories and Loot ---
    print("[STATUS] Generating inventories and loot tables...")
    for npc in world_state.npcs.values():
        if npc.gold > 0:
            budget = int(npc.gold * random.uniform(*INVENTORY_BUDGET_PERCENTAGE))
            npc.inventory = generate_inventory(npc, budget, item_list)

    for creature in world_state.creatures.values():
        if creature.gold > 0:
            budget = int(creature.gold * random.uniform(*INVENTORY_BUDGET_PERCENTAGE))
            creature.loot = generate_inventory(creature, budget, item_list)

    print(f"[STATUS] Economic Layer complete. Distributed {TOTAL_WORLD_GOLD} gold across {len(world_state.npcs) + len(world_state.creatures)} entities.")
    return world_state
