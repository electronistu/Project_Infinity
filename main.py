# main.py v3.3
# The Forge for Project Infinity - The user-facing orchestrator for the v3.3 world generation cascade.

import uuid
from datetime import datetime
import copy

# Import v3.3 Pydantic models
from models import PlayerCharacter, WorldState

# Import all v3.3 generator modules
from geopolitical_generator import generate_geopolitical_layer
from sociological_generator import generate_sociological_layer
from economic_generator import generate_economic_layer
from quest_generator import generate_quest_layer
from world_details_generator import generate_world_details_layer
from abilities_generator import assign_abilities_to_guild_masters
from roads_generator import generate_roads_layer
from time_and_event_generator import generate_time_and_event_layer

# Import the final formatter module
from formatter import generate_world_weave_string

# --- v3.3 Constants ---
STAT_POINTS = 27
BASE_STAT_COSTS = {
    "Warrior": {"STR": 1, "DEX": 2, "CON": 1, "INT": 3, "WIS": 2, "CHA": 2},
    "Rogue":   {"STR": 2, "DEX": 1, "CON": 2, "INT": 2, "WIS": 2, "CHA": 1},
    "Mage":    {"STR": 3, "DEX": 2, "CON": 2, "INT": 1, "WIS": 1, "CHA": 2},
}

# D&D 5e Races with stat discounts and perks
RACE_DATA = {
    "Human": {"perk": "Versatile: Start with an extra 2 stat points.", "discount_stat": None, "stat_bonus": 2},
    "Elf": {"perk": "Graceful: You have a discount on increasing Dexterity.", "discount_stat": "DEX", "stat_bonus": 0},
    "Dwarf": {"perk": "Sturdy: You have a discount on increasing Constitution.", "discount_stat": "CON", "stat_bonus": 0},
    "Halfling": {"perk": "Nimble: You have a discount on increasing Dexterity.", "discount_stat": "DEX", "stat_bonus": 0},
    "Gnome": {"perk": "Artificer's Mind: You have a discount on increasing Intelligence.", "discount_stat": "INT", "stat_bonus": 0},
    "Half-Elf": {"perk": "Charming: You have a discount on increasing Charisma.", "discount_stat": "CHA", "stat_bonus": 1},
    "Half-Orc": {"perk": "Savage: You have a discount on increasing Strength.", "discount_stat": "STR", "stat_bonus": 0},
    "Tiefling": {"perk": "Infernal Legacy: You have a discount on increasing Charisma.", "discount_stat": "CHA", "stat_bonus": 0},
    "Dragonborn": {"perk": "Draconic Might: You have a discount on increasing Strength.", "discount_stat": "STR", "stat_bonus": 0},
}

def get_user_input(prompt, options=None):
    """A robust function to get and validate user input."""
    while True:
        print(f"\n{prompt}")
        if options:
            # Create a list of keys to handle numeric input gracefully
            option_keys = list(options.keys())
            for i, key in enumerate(option_keys):
                print(f"  [{i+1}] {options[key]}")

        choice = input("> ").strip()

        if not options:
            if choice:
                return choice
            else:
                print("! Input cannot be empty.")
        # Check if the choice is a number corresponding to an option
        elif choice.isdigit() and 1 <= int(choice) <= len(option_keys):
            return options[option_keys[int(choice) - 1]]
        # Check if the choice is the literal key
        elif choice in options:
            return options[choice]
        else:
            print(f"! Invalid selection. Please choose one of the available options.")

def run_character_interview():
    """v3.3: Guides the user through character creation with expanded options and racial bonuses."""
    print("// PROJECT INFINITY V3.3 - WORLD-SEED FORGE //")
    print("Let's create your character. This will define the world that is forged for you.")

    character_data = {}

    # --- Basic Info ---
    character_data['name'] = get_user_input("Enter your character's name:")
    while True:
        age_str = get_user_input("Enter your character's age:")
        if age_str.isdigit() and int(age_str) > 0:
            character_data['age'] = int(age_str)
            break
        else:
            print("! Please enter a valid positive number for age.")

    sex_options = {"1": "Male", "2": "Female"}
    character_data['sex'] = get_user_input("Choose your character's sex:", sex_options)

    # --- v3.3 Race, Class, and Alignment ---
    race_options = {str(i+1): r for i, r in enumerate(RACE_DATA.keys())}
    chosen_race = get_user_input("Choose your character's race:", race_options)
    character_data['race'] = chosen_race
    
    race_info = RACE_DATA[chosen_race]
    character_data['racial_perk'] = race_info['perk']
    print(f"  -> Racial Perk: {race_info['perk']}")

    class_options = {"1": "Warrior", "2": "Rogue", "3": "Mage"}
    character_class = get_user_input("Choose your character's class:", class_options)
    character_data['class'] = character_class

    alignment_options = {
        "1": "Lawful Good", "2": "Neutral Good", "3": "Chaotic Good",
        "4": "Lawful Neutral", "5": "True Neutral", "6": "Chaotic Neutral",
        "7": "Lawful Evil", "8": "Neutral Evil", "9": "Chaotic Evil"
    }
    character_data['alignment'] = get_user_input("Choose your character's alignment:", alignment_options)

    # --- v3.3 Stat Point-Buy System with Racial Modifiers ---
    stats = {"STR": 8, "DEX": 8, "CON": 8, "INT": 8, "WIS": 8, "CHA": 8}
    points_remaining = STAT_POINTS + race_info['stat_bonus']
    
    costs = copy.deepcopy(BASE_STAT_COSTS[character_class])
    if race_info['discount_stat']:
        costs[race_info['discount_stat']] = max(1, costs.get(race_info['discount_stat'], 2) - 1)

    print(f"\n--- STAT ALLOCATION ({character_class} - {chosen_race}) ---")
    print(f"You have {points_remaining} points to distribute among your stats.")
    print("Each stat starts at 8. Increasing a stat costs points based on your class and race.")

    while points_remaining > 0:
        print(f"\nPoints Remaining: {points_remaining}")
        stat_keys = list(stats.keys())
        for i, stat in enumerate(stat_keys):
            value = stats[stat]
            cost = costs[stat]
            print(f"  [{i+1}] {stat}: {value} (Cost to increase: {cost})")

        stat_choice_idx = get_user_input("Choose a stat to increase (or type 'done'):")
        if stat_choice_idx.lower() == 'done':
            break

        try:
            stat_to_increase = stat_keys[int(stat_choice_idx)-1]
            cost = costs[stat_to_increase]

            if points_remaining >= cost:
                stats[stat_to_increase] += 1
                points_remaining -= cost
            else:
                print("! Not enough points to increase that stat.")
        except (ValueError, IndexError):
             print("! Invalid selection.")

    character_data['stats'] = stats
    return character_data

if __name__ == "__main__":
    # --- Phase 1: The Character Interview ---
    player_character_dict = run_character_interview()

    # --- Phase 2: World State Instantiation ---
    player_character_model = PlayerCharacter(**player_character_dict)

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    unique_hash = str(uuid.uuid4()).split('-')[0]
    instance_id = f"{timestamp}-{unique_hash}"

    world_state = WorldState(
        instance_id=instance_id,
        player_character=player_character_model
    )
    print("\n--- World State Initialized ---")
    print("The foundational reality has been instantiated. Beginning generation cascade...")

    # --- Phase 3: The Reactive Generation Cascade (v3.3) ---
    world_state = generate_geopolitical_layer(world_state)
    world_state = generate_sociological_layer(world_state)
    world_state = generate_economic_layer(world_state)
    world_state = assign_abilities_to_guild_masters(world_state)
    world_state = generate_quest_layer(world_state)
    world_state = generate_world_details_layer(world_state)
    world_state = generate_roads_layer(world_state)
    world_state = generate_time_and_event_layer(world_state)

    print("\n--- Generation Cascade Complete ---")

    # --- Phase 4: The Weaving ---
    world_weave_content = generate_world_weave_string(world_state)

    # --- Phase 5: The Forging ---
    file_name = f"{player_character_model.name.lower()}_{player_character_model.character_class.lower()}_weave_v3-3.wwf"
    try:
        with open(file_name, "w") as f:
            f.write(world_weave_content)
        print(f"\n--- FORGE COMPLETE ---")
        print(f"Your personalized v3.3 World-Weave Key has been forged: {file_name}")
        print("You may now use this key with the v3.3 Game Master persona to begin your adventure.")
        print("----------------------")
    except IOError as e:
        print(f"\n--- FORGE FAILED ---")
        print(f"Error: Could not write the World-Weave Key to file. Reason: {e}")
        print("--------------------")
