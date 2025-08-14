import argparse
import os
from forge.config_loader import load_config
from forge.character_creator import create_character, create_debug_character
from forge.map_generator import create_map
from forge.population_generator import populate_world
from forge.dungeon_and_creature_placer import place_dungeons_and_creatures
from forge.guild_generator import create_guilds
from forge.geopolitical_engine import determine_relations
from forge.history_generator import generate_histories
from forge.formatter import format_world_to_wwf
from forge.models import WorldState

def main():
    """The main entry point for the Infinity Forge."""
    parser = argparse.ArgumentParser(description="Generate a new world for Project Infinity.")
    parser.add_argument("--debug", action="store_true", help="Run in debug mode, bypassing interactive character creation.")
    args = parser.parse_args()

    config = load_config()

    if args.debug:
        player_character = create_debug_character(config)
    else:
        player_character = create_character(config)
    
    print("\n--- Forging Your World... ---")

    map_grid = create_map()
    kingdoms = populate_world(config, map_grid)
    place_dungeons_and_creatures(kingdoms, config.items, map_grid, config)
    create_guilds(kingdoms, config)

    world_state = WorldState(
        player_character=player_character,
        map_grid=map_grid,
        kingdoms=kingdoms,
        # creatures=creatures, # Removed as creatures are now nested in locations
        current_tick="06:00"
    )

    # Determine kingdom relations based on the UFP Engine
    determine_relations(world_state)

    # Generate narrative history based on the L.I.C. Engine
    generate_histories(world_state)

    output_dir = "output"
    output_filename = f"{player_character.name.lower().replace(' ', '_')}_weave.wwf"
    output_path = os.path.abspath(os.path.join(output_dir, output_filename))
    format_world_to_wwf(world_state, output_path)

    print("\n--- World Forge Complete! ---\n")
    print(f"Your world has been saved to: {output_path}")

if __name__ == "__main__":
    main()