from .models import PlayerCharacter, Stats, Equipment
from .config_loader import Config

def get_player_input(prompt, valid_options=None):
    """Generic function to get validated user input."""
    while True:
        user_input = input(prompt).strip()
        if not valid_options or user_input.lower() in [opt.lower() for opt in valid_options]:
            return user_input
        print(f"Invalid choice. Please select from: {', '.join(valid_options)}")

def create_debug_character(config: Config) -> PlayerCharacter:
    """Creates a hardcoded debug character."""
    print("--- Character Creation (DEBUG MODE) ---")
    return PlayerCharacter(
        name="Debug Adventurer", age=30, sex="Male",
        race="Human", character_class="Fighter", alignment="True Neutral",
        level=1, stats=Stats(strength=15, dexterity=14, constitution=13, intelligence=12, wisdom=10, charisma=8), 
        perks=["Bonus Feat", "Skilled"], 
        equipment=Equipment(main_hand=next((item for item in config.items if item.name == "Rusty Sword"), None))
    )

def create_character(config: Config) -> PlayerCharacter:
    """Creates a new character through an interactive CLI process."""
    print("--- Character Creation ---")

    # Name, Age, Sex
    name = input("Enter your character's name: ")
    age = int(input("Enter your character's age: "))
    sex = get_player_input("Enter your character's sex (Male/Female/Other): ", ["Male", "Female", "Other"])

    # Race Selection
    print("\n--- Choose your Race ---")
    for i, race in enumerate(config.races):
        print(f"{i + 1}. {race.name}")
    race_choice = int(get_player_input("Select a race: ", [str(i+1) for i in range(len(config.races))])) - 1
    chosen_race = config.races[race_choice]
    print(f"You have chosen to be a {chosen_race.name}. You gain the following perks: {', '.join(chosen_race.perks)}")

    # Class Selection
    print("\n--- Choose your Class ---")
    for i, char_class in enumerate(config.classes):
        print(f"{i + 1}. {char_class.name}")
    class_choice = int(get_player_input("Select a class: ", [str(i+1) for i in range(len(config.classes))])) - 1
    chosen_class = config.classes[class_choice]
    print(f"You have chosen to be a {chosen_class.name}. You gain the following perks: {', '.join(chosen_class.perks)}")

    # Alignment Selection
    print("\n--- Choose your Alignment ---")
    for i, alignment in enumerate(config.alignments):
        print(f"{i + 1}. {alignment}")
    alignment_choice = int(get_player_input("Select an alignment: ", [str(i+1) for i in range(len(config.alignments))])) - 1
    chosen_alignment = config.alignments[alignment_choice]

    # Point-Buy for Stats
    print("\n--- Distribute Your Stat Points (Point-Buy System) ---")
    stats = {"strength": 8, "dexterity": 8, "constitution": 8, "intelligence": 8, "wisdom": 8, "charisma": 8}
    points_spent = 0

    for stat in stats:
        while True:
            try:
                points_remaining = 27 - points_spent
                print(f"\nPoints remaining: {points_remaining}")
                value = int(input(f"Set {stat.upper()} (8-15): "))
                if 8 <= value <= 15:
                    cost = value - 8
                    if value > 13:
                        cost += (value - 13)
                    if points_spent + cost <= 27:
                        points_spent += cost
                        stats[stat] = value
                        break
                    else:
                        print("Not enough points!")
                else:
                    print("Invalid stat value. Must be between 8 and 15.")
            except ValueError:
                print("Invalid input. Please enter a number.")

    # Equipment (as before)
    equipment = Equipment()
    if chosen_class.name == "Fighter":
        equipment.main_hand = next((item for item in config.items if item.name == "Rusty Sword"), None)
        equipment.chest = next((item for item in config.items if item.name == "Iron Chainmail"), None)
    # ... (add more for other classes)

    print("\n--- Character Complete! ---")
    return PlayerCharacter(
        name=name, age=age, sex=sex,
        race=chosen_race.name, character_class=chosen_class.name, alignment=chosen_alignment,
        level=1, stats=Stats(**stats), perks=chosen_race.perks, equipment=equipment
    )
