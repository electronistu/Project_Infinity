from .models import Guild, NPC, PlayerAbility
from .population_generator import _generate_npc_details
import random

# Define lists of guild types for different contexts
COMMON_GUILDS = ["Guard", "Mage", "Assassin", "Merchant", "Thief", "Alchemist's League"]
PIRATE_GUILDS = ["Assassin", "Thief"]
SILVERWOOD_GUILDS = ["Ranger's Conclave"]
ELDORIA_GUILDS = ["Order of Scribes"]

def create_guilds(kingdoms, config):
    """Creates guilds for each kingdom based on its type and assigns abilities."""
    for kingdom in kingdoms:
        # Start with common guilds
        guild_types_to_create = list(COMMON_GUILDS)

        # Add or filter guilds based on kingdom name
        if kingdom.name == "Blacksail Archipelago":
            guild_types_to_create = PIRATE_GUILDS
        elif kingdom.name == "Silverwood":
            guild_types_to_create.extend(SILVERWOOD_GUILDS)
        elif kingdom.name == "Eldoria":
            guild_types_to_create.extend(ELDORIA_GUILDS)

        for guild_type in guild_types_to_create:
            # Filter abilities for the current guild type
            leader_abilities = [ability for ability in config.abilities if ability.guild_source == guild_type and ability.tier > 2]
            right_hand_abilities = [ability for ability in config.abilities if ability.guild_source == guild_type and ability.tier <= 2]

            # Create guild members
            leader = create_guild_member(f"{guild_type} Leader", config, leader_abilities)
            right_hand = create_guild_member(f"{guild_type} Right Hand", config, right_hand_abilities)
            
            # Create additional guild members
            guild_members = []
            num_members = random.randint(2, 4)
            for _ in range(num_members):
                guild_members.append(create_guild_member(f"{guild_type} Member", config, []))

            # Create and add the guild to the kingdom
            reports_to_ruler = None
            if guild_type == "Guard":
                reports_to_ruler = kingdom.ruler.name

            guild = Guild(name=f"{kingdom.name} {guild_type}", leader=leader, right_hand=right_hand, members=guild_members, reports_to=reports_to_ruler)
            kingdom.guilds.append(guild)

def create_guild_member(role, config, abilities):
    """Creates a new NPC to serve as a guild member with specific abilities."""
    npc_level = random.randint(5, 10)

    # Determine the faction from the role
    faction = role
    if "Guild" in role:
        faction = role.replace("Leader", "").replace("Right Hand", "").replace("Member", "").strip()

    guild_member = _generate_npc_details(
        level=npc_level,
        role=role,
        faction=faction,
        is_walker=False,
        config=config
    )
    guild_member.name = f"{guild_member.race} {guild_member.character_class}"
    guild_member.abilities_for_sale = abilities
    return guild_member