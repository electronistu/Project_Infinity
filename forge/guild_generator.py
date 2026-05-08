from .models import Guild, NPC
from .population_generator import _generate_npc_details
import random

# Define lists of guild types for different contexts
COMMON_GUILDS = ["Guard", "Mage", "Assassin", "Merchant", "Thief", "Alchemist's League"]
PIRATE_GUILDS = ["Assassin", "Thief"]
SILVERWOOD_GUILDS = ["Ranger's Conclave"]
ELDORIA_GUILDS = ["Order of Scribes"]

def create_guilds(kingdoms, config):
    """Creates guilds for each kingdom."""
    for kingdom in kingdoms:
        guild_types_to_create = list(COMMON_GUILDS)
        if kingdom.name == "Blacksail Archipelago":
            guild_types_to_create = PIRATE_GUILDS
        elif kingdom.name == "Silverwood":
            guild_types_to_create.extend(SILVERWOOD_GUILDS)
        elif kingdom.name == "Eldoria":
            guild_types_to_create.extend(ELDORIA_GUILDS)

        for guild_type in guild_types_to_create:
            leader = create_guild_member(f"{guild_type} Leader", config)
            right_hand = create_guild_member(f"{guild_type} Right Hand", config)
            
            reports_to = "ruler" if guild_type == "Guard" else None
            guild = Guild(name=f"{kingdom.name} {guild_type}", leader=leader, right_hand=right_hand, reports_to=reports_to)
            kingdom.guilds.append(guild)

def create_guild_member(role, config):
    """Creates a new NPC to serve as a guild member."""
    npc_level = random.randint(5, 10)

    # Determine the faction from the role
    faction = role
    if "Guild" in role:
        faction = role.replace("Leader", "").replace("Right Hand", "").replace("Member", "").strip()

    guild_member = _generate_npc_details(
        level=npc_level,
        role=role,
        faction=faction,
        config=config
    )
    return guild_member