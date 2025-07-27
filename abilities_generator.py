# abilities_generator.py v3.2
# Defines the hardcoded library of abilities available from Guild Masters.

from models import Ability, WorldState

# --- v3.2 Guild Ability Library ---
# Abilities are now tied to specific guilds and have 5 tiers with scaling costs.
GUILD_ABILITIES = {
    "Royal Guard": [
        Ability(name="Shield Bash", description="Use your shield to daze an opponent.", tier=1, cost=500, class_requirement="Warrior"),
        Ability(name="Sunder Armor", description="A powerful strike that temporarily reduces an enemy's defense.", tier=2, cost=2500, class_requirement="Warrior"),
        Ability(name="Hold the Line", description="Increase your defense and draw enemy aggression for a short time.", tier=3, cost=10000, class_requirement="Warrior"),
        Ability(name="Champion's Challenge", description="Force a powerful enemy to focus their attacks on you.", tier=4, cost=50000, class_requirement="Warrior"),
        Ability(name="Unstoppable Juggernaut", description="Become nearly immune to damage for a brief period.", tier=5, cost=250000, class_requirement="Warrior"),
    ],
    "Mages' Guild": [
        Ability(name="Arcane Bolt", description="A bolt of pure magic energy.", tier=1, cost=500, class_requirement="Mage"),
        Ability(name="Prismatic Orb", description="Hurl an orb that cycles through elements, causing random effects.", tier=2, cost=2500, class_requirement="Mage"),
        Ability(name="Teleport", description="Instantly move to a nearby location.", tier=3, cost=10000, class_requirement="Mage"),
        Ability(name="Summon Elemental", description="Summon a minor elemental to fight by your side.", tier=4, cost=50000, class_requirement="Mage"),
        Ability(name="Meteor Swarm", description="Call down a devastating shower of meteors from the sky.", tier=5, cost=250000, class_requirement="Mage"),
    ],
    "Thieves' Guild": [
        Ability(name="Distract", description="Create a diversion to make sneaking easier.", tier=1, cost=400, class_requirement="Rogue"),
        Ability(name="Evasion", description="Dodge the next attack that would have hit you.", tier=2, cost=2000, class_requirement="Rogue"),
        Ability(name="Shadow Step", description="Move instantly from one shadow to another.", tier=3, cost=8000, class_requirement="Rogue"),
        Ability(name="Smoke Bomb", description="Create a large cloud of smoke to cover your escape.", tier=4, cost=40000, class_requirement="Rogue"),
        Ability(name="Master of Deception", description="Briefly trick an enemy into fighting for you.", tier=5, cost=200000, class_requirement="Rogue"),
    ],
    "The Crimson Hand": [
        Ability(name="Mark for Death", description="Mark a target, increasing all damage they take from you.", tier=1, cost=750, class_requirement="Rogue"),
        Ability(name="Garrote", description="A silent attack from behind that deals heavy damage.", tier=2, cost=3500, class_requirement="Rogue"),
        Ability(name="Alchemical Poison", description="Coat your weapon in a potent, fast-acting poison.", tier=3, cost=15000, class_requirement="Rogue"),
        Ability(name="Death from the Shadows", description="A devastating attack made from stealth that can kill weaker enemies instantly.", tier=4, cost=75000, class_requirement="Rogue"),
        Ability(name="The Final Whisper", description="A legendary assassination technique that bypasses most defenses.", tier=5, cost=350000, class_requirement="Rogue"),
    ]
}

def get_guild_abilities():
    """Returns the complete library of guild-specific abilities."""
    return GUILD_ABILITIES

def assign_abilities_to_guild_masters(world_state: WorldState) -> WorldState:
    """
    Assigns the ability lists to the 'for_sale_abilities' field of the
    respective guild leaders.
    """
    print("[STATUS] Assigning abilities to Guild Masters...")
    guild_abilities = get_guild_abilities()
    for faction_name, abilities in guild_abilities.items():
        # Find the faction and its leader
        if faction_name in world_state.factions:
            leader_name = world_state.factions[faction_name].leader
            if leader_name and leader_name in world_state.npcs:
                # Assign the list of abilities to the NPC model
                world_state.npcs[leader_name].for_sale_abilities = abilities
                print(f"[INFO] Assigned {len(abilities)} abilities to {leader_name} of the {faction_name}.")
    return world_state