from .models import Guild, NPC, PlayerAbility
from .population_generator import _generate_npc_details
import random

# Define lists of guild types for different contexts
COMMON_GUILDS = ["Guard", "Mage", "Assassin", "Merchant", "Thief", "Alchemist's League"]
PIRATE_GUILDS = ["Assassin", "Thief"]
SILVERWOOD_GUILDS = ["Ranger's Conclave"]
ELDORIA_GUILDS = ["Order of Scribes"]

def create_guilds(kingdoms, config):
    """Creates guilds, distributing abilities for common guilds across kingdoms."""
    guild_structures = {}
    all_guild_types = set()

    # First pass: Create all guild structures without abilities
    for kingdom in kingdoms:
        guild_types_to_create = list(COMMON_GUILDS)
        if kingdom.name == "Blacksail Archipelago":
            guild_types_to_create = PIRATE_GUILDS
        elif kingdom.name == "Silverwood":
            guild_types_to_create.extend(SILVERWOOD_GUILDS)
        elif kingdom.name == "Eldoria":
            guild_types_to_create.extend(ELDORIA_GUILDS)

        for guild_type in guild_types_to_create:
            all_guild_types.add(guild_type)
            leader = create_guild_member(f"{guild_type} Leader", config, [])
            right_hand = create_guild_member(f"{guild_type} Right Hand", config, [])
            members = [create_guild_member(f"{guild_type} Member", config, []) for _ in range(random.randint(2, 4))]
            
            reports_to = "ruler" if guild_type == "Guard" else None
            guild = Guild(name=f"{kingdom.name} {guild_type}", leader=leader, right_hand=right_hand, members=members, reports_to=reports_to)
            kingdom.guilds.append(guild)

            if guild_type not in guild_structures:
                guild_structures[guild_type] = []
            guild_structures[guild_type].append(guild)

    # Second pass: Distribute abilities
    for guild_type in all_guild_types:
        leader_abilities = [ability for ability in config.abilities if ability.guild_source == guild_type and ability.tier > 2]
        right_hand_abilities = [ability for ability in config.abilities if ability.guild_source == guild_type and ability.tier <= 2]
        random.shuffle(leader_abilities)
        random.shuffle(right_hand_abilities)

        guilds_of_type = guild_structures.get(guild_type, [])
        if not guilds_of_type:
            continue

        # If it's a unique guild, it gets all abilities
        if len(guilds_of_type) == 1:
            guilds_of_type[0].leader.abilities_for_sale = leader_abilities
            guilds_of_type[0].right_hand.abilities_for_sale = right_hand_abilities
        else: # Distribute abilities among common guilds
            for i, ability in enumerate(leader_abilities):
                guild_index = i % len(guilds_of_type)
                guilds_of_type[guild_index].leader.abilities_for_sale.append(ability)
            
            for i, ability in enumerate(right_hand_abilities):
                guild_index = i % len(guilds_of_type)
                guilds_of_type[guild_index].right_hand.abilities_for_sale.append(ability)

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
        is_walker=random.random() < 0.042,
        config=config
    )
    guild_member.name = f"{guild_member.race} {guild_member.character_class}"
    guild_member.abilities_for_sale = abilities
    return guild_member