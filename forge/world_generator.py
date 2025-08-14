from .map_generator import create_map
from .population_generator import populate_world
from .dungeon_and_creature_placer import place_dungeons_and_creatures
from .guild_generator import create_guilds
from .quest_generator import generate_quests
from .models import WorldState, PlayerCharacter, Config

def generate_world(player_character: PlayerCharacter, config: Config) -> WorldState:
    """Generates the complete world state."""
    map_grid = create_map()
    kingdoms, npcs, creatures = populate_world(config, map_grid)
    place_dungeons_and_creatures(kingdoms, creatures, config.items, map_grid)
    create_guilds(kingdoms, config)

    world_state = WorldState(
        player_character=player_character,
        map_grid=map_grid,
        kingdoms=kingdoms,
        npcs=npcs,
        creatures=creatures, # This needs to be handled carefully, as creatures are now in locations
        all_abilities=config.abilities, # Pass all abilities from config
        quests=[], # Initialize with an empty list
        current_tick="06:00"
    )

    quests = generate_quests(world_state, config)
    world_state.quests = quests

    return world_state