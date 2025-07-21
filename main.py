# main.py v2.1
# The Forge for Project Infinity - The user-facing orchestrator for the v2 world generation cascade.

import uuid
from datetime import datetime

# Import v2 Pydantic models
from models import PlayerCharacter, WorldState

# Import all v2 generator modules
from geopolitical_generator import generate_geopolitical_layer
from sociological_generator import generate_sociological_layer
from economic_generator import generate_economic_layer
from quest_generator import generate_quest_layer
from world_details_generator import generate_world_details_layer

# Import the final formatter module
from formatter import generate_world_weave_string

def get_user_input(prompt, options=None):
    """A robust function to get and validate user input."""
    while True:
        print(f"\n{prompt}")
        if options:
            for key, value in options.items():
                print(f"  [{key}] {value}")

        choice = input("> ").strip()

        if not options:
            if choice:
                return choice
            else:
                print("! Input cannot be empty.")
        elif choice in options:
            return options[choice]
        else:
            print(f"! Invalid selection. Please choose one of the available options.")

def run_character_interview():
    """v2.1: Guides the user through the character creation process, including alignment."""
    print("// PROJECT INFINITY V2 - WORLD-SEED FORGE //")
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
    
    # CORRECTED: 'sex' is now a choice, and 'orientation' is removed.
    sex_options = {"1": "Male", "2": "Female"}
    character_data['sex'] = get_user_input("Choose your character's sex:", sex_options)

    # --- Race, Class, and Alignment with Options ---
    race_options = {"1": "Human", "2": "Elf", "3": "Dwarf"}
    character_data['race'] = get_user_input("Choose your character's race:", race_options)

    class_options = {"1": "Warrior", "2": "Rogue", "3": "Mage"}
    character_data['class'] = get_user_input("Choose your character's class:", class_options)
    
    alignment_options = {"1": "Lawful", "2": "Neutral", "3": "Chaotic"}
    character_data['alignment'] = get_user_input("Choose your character's alignment:", alignment_options)

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

    # --- Phase 3: The Reactive Generation Cascade ---
    # Layer 1: Geopolitical
    world_state = generate_geopolitical_layer(world_state)
    # Layer 2: Sociological
    world_state = generate_sociological_layer(world_state)
    # Layer 3: Economic
    world_state = generate_economic_layer(world_state)
    # Layer 4: Quest & Reputation
    world_state = generate_quest_layer(world_state)
    # Layer 5: World Details
    world_state = generate_world_details_layer(world_state)

    print("\n--- Generation Cascade Complete ---")

    # --- Phase 4: The Weaving ---
    world_weave_content = generate_world_weave_string(world_state)

    # --- Phase 5: The Forging ---
    file_name = f"{player_character_model.name.lower()}_{player_character_model.character_class.lower()}_weave_v2.wwf"
    try:
        with open(file_name, "w") as f:
            f.write(world_weave_content)
        print(f"\n--- FORGE COMPLETE ---")
        print(f"Your personalized v2 World-Weave Key has been forged: {file_name}")
        print("You may now use this key with the v2 Game Master persona to begin your adventure.")
        print("----------------------")
    except IOError as e:
        print(f"\n--- FORGE FAILED ---")
        print(f"Error: Could not write the World-Weave Key to file. Reason: {e}")
        print("--------------------")
