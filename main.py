import argparse
from forge.config_loader import load_config
from forge.character_creator import create_character, create_debug_character
from forge.map_generator import create_map
from forge.population_generator import populate_world
from forge.dungeon_and_creature_placer import place_dungeons_and_creatures
from forge.guild_generator import create_guilds
from forge.quest_generator import generate_quests
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
    kingdoms, npcs, creatures = populate_world(config, map_grid)
    place_dungeons_and_creatures(kingdoms, creatures, config.items, map_grid)
    create_guilds(kingdoms, config)

    world_state = WorldState(
        player_character=player_character,
        map_grid=map_grid,
        kingdoms=kingdoms,
        npcs=npcs,
        creatures=creatures,
        quests=[],
        current_tick="06:00"
    )

    quests = generate_quests(world_state, config)
    world_state.quests = quests

    output_filename = f"{player_character.name.lower().replace(' ', '_')}_weave.wwf"
    output_path = f"output/{output_filename}"
    format_world_to_wwf(world_state, output_path)

    print("\n--- World Forge Complete! ---")
    print(f"Your world has been saved to: {output_path}")

if __name__ == "__main__":
    main()
