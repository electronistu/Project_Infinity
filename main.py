import argparse
import os
from forge.config_loader import load_config
from forge.character_creator import create_character, create_debug_character
from forge.map_generator import create_map
from forge.population_generator import populate_world
from forge.guild_generator import create_guilds
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
    create_guilds(kingdoms, config)

    world_history = [
        "The War of the Ashen Crown, a bitter conflict ignited by Zarthus's expansionism, ended a decade ago in a fragile truce. The cities of Eldoria still bear the scars, and its people have long memories.",
        "During the war, the Blacksail Archipelago allied with Zarthus, preying on Eldorian shipping lanes. Though the war is over, their piracy continues, a constant thorn in the side of all civilized kingdoms.",
        "Silverwood's staunch neutrality during the war earned it no friends. Eldoria views them with suspicion for not aiding their cause, while Zarthus holds them in contempt for refusing to bow to their power.",
        "An uneasy peace now holds between Eldoria and Zarthus. It is not a peace of friendship, but a bitter rivalry of two great powers rebuilding their strength, each waiting for the other to show a sign of weakness."
    ]

    world_state = WorldState(
        player_character=player_character,
        map_grid=map_grid,
        kingdoms=kingdoms,
        current_tick="06:00",
        world_history=world_history
    )

    output_dir = "output"
    output_filename = f"{player_character.name.lower().replace(' ', '_')}_weave.wwf"
    output_path = os.path.abspath(os.path.join(output_dir, output_filename))
    format_world_to_wwf(world_state, output_path)

    print("\n--- World Forge Complete! ---\n")
    print(f"Your world has been saved to: {output_path}")

if __name__ == "__main__":
    main()