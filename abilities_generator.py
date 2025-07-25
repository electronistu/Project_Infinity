# abilities_generator.py v3.0
# Defines the hardcoded library of abilities available in the game.

from models import Ability

def get_ability_library():
    """Returns a list of all available abilities in the game."""
    return [
        # --- Warrior Abilities ---
        Ability(name="Power Attack", description="A powerful but less accurate attack.", tier=1, cost=500, class_requirement="Warrior"),
        Ability(name="Cleave", description="Attack two adjacent enemies at once.", tier=2, cost=1500, class_requirement="Warrior"),
        Ability(name="War Cry", description="Boosts attack and defense for a short duration.", tier=3, cost=3000, class_requirement="Warrior"),

        # --- Rogue Abilities ---
        Ability(name="Sneak Attack", description="Deal extra damage when attacking from stealth.", tier=1, cost=500, class_requirement="Rogue"),
        Ability(name="Poisoned Blade", description="Apply a damage-over-time poison to your weapon.", tier=2, cost=1500, class_requirement="Rogue"),
        Ability(name="Vanish", description="Instantly become invisible for a short time.", tier=3, cost=3000, class_requirement="Rogue"),

        # --- Mage Abilities ---
        Ability(name="Fireball", description="A fiery explosion that damages enemies in an area.", tier=1, cost=500, class_requirement="Mage"),
        Ability(name="Ice Storm", description="A swirling storm of ice that slows and damages enemies.", tier=2, cost=1500, class_requirement="Mage"),
        Ability(name="Chain Lightning", description="Lightning that arcs from one enemy to another.", tier=3, cost=3000, class_requirement="Mage"),
    ]

def generate_ability_shop_layer(world_state):
    """Populates the world's ability shop."""
    print("[STATUS] Generating Ability Shop Layer...")
    world_state.ability_shop = get_ability_library()
    print(f"[STATUS] Ability Shop Layer complete. {len(world_state.ability_shop)} abilities available.")
    return world_state
