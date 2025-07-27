from .models import Guild, NPC, Stats, Ability
import random

ALL_GUILDS = ["Guard", "Mage", "Assassin", "Merchant", "Thief"]
PIRATE_GUILDS = ["Assassin", "Thief"]

def create_guilds(kingdoms, config):
    """Creates guilds for each kingdom and assigns abilities to their leaders."""
    for kingdom in kingdoms:
        guild_types_to_create = PIRATE_GUILDS if kingdom.name == "Blacksail Archipelago" else ALL_GUILDS
        for guild_type in guild_types_to_create:
            leader_abilities = [ability for ability in config.abilities if ability.guild_source == guild_type and ability.tier > 2]
            right_hand_abilities = [ability for ability in config.abilities if ability.guild_source == guild_type and ability.tier <= 2]

            leader = create_guild_member(f"{guild_type} Guild Leader", config, leader_abilities)
            right_hand = create_guild_member(f"{guild_type} Guild Right Hand", config, right_hand_abilities)
            
            guild = Guild(name=f"{kingdom.name} {guild_type} Guild", leader=leader, right_hand=right_hand)
            kingdom.guilds.append(guild)

def create_guild_member(role, config, abilities):
    """Creates a new NPC to serve as a guild member with specific abilities."""
    race = random.choice(config.races)
    character_class = random.choice(config.classes)
    alignment = random.choice(config.alignments)
    stats = Stats(strength=12, dexterity=12, constitution=12, intelligence=12, wisdom=12, charisma=12)
    return NPC(
        name=f"{race.name} {character_class.name}",
        level=random.randint(5, 10),
        stats=stats,
        alignment=alignment,
        role=role,
        faction=f"{role.split(' ')[0]} Guild",
        dialogue_options=[],
        abilities_for_sale=abilities
    )
