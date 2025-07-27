import argparse
from .config_loader import load_config
from .character_creator import create_character, create_debug_character
from .map_generator import create_map
from .population_generator import populate_world
from .dungeon_and_creature_placer import place_dungeons_and_creatures
from .guild_generator import create_guilds
from .quest_generator import generate_quests
from .formatter import format_world_to_wwf
from .models import WorldState

def forge_world(debug=False):
    """The main function to generate the world."""
    config = load_config()
    if debug:
        player_character = create_debug_character(config)
    else:
        player_character = create_character(config)
    
    map_grid = create_map()
    kingdoms, npcs, creatures = populate_world(config, map_grid)
    place_dungeons_and_creatures(kingdoms, creatures, config.items, map_grid)
    create_guilds(kingdoms, config)

    world_state = WorldState(
        player_character=player_character,
        map_grid=map_grid,
        kingdoms=kingdoms,
        npcs=npcs,
        creatures=creatures,
        quests=[], # Initialize with an empty list
        current_tick="06:00"
    )

    quests = generate_quests(world_state, config)
    world_state.quests = quests

    output_path = "/home/rtmi6/GitHub/project_infinity/infinity_forge/output/world_state.wwf"
    format_world_to_wwf(world_state, output_path)

    print("\n--- FORGE COMPLETE ---")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Run in debug mode, bypassing interactive character creation.")
    args = parser.parse_args()
    forge_world(debug=args.debug)
