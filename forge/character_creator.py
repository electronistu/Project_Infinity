# forge/character_creator.py
# Version 3.2 - Full D&D 5e Character Creation with Spellcasting

from .models import PlayerCharacter, Stats, Equipment, Skill, SpecialAbility, Item, StartingEquipmentOption, CharacterClass
from .config_loader import Config
import math
from typing import Optional, List, Dict

# --- Data for 5e Rules ---
ALL_SKILLS = {
    "Acrobatics": "dexterity", "Animal Handling": "wisdom", "Arcana": "intelligence",
    "Athletics": "strength", "Deception": "charisma", "History": "intelligence",
    "Insight": "wisdom", "Intimidation": "charisma", "Investigation": "intelligence",
    "Medicine": "wisdom", "Nature": "intelligence", "Perception": "wisdom",
    "Performance": "charisma", "Persuasion": "charisma", "Religion": "intelligence",
    "Sleight of Hand": "dexterity", "Stealth": "dexterity", "Survival": "wisdom"
}

# Spell slots per level for full casters (Wizard, Cleric, Sorcerer, Bard, Druid)
# Key: character level, Value: list of spell slots per spell level (1st, 2nd, etc.)
FULL_CASTER_SPELL_SLOTS = {
    1: {1: 2},
    2: {1: 3},
    3: {1: 4, 2: 2},
    4: {1: 4, 2: 3},
    5: {1: 4, 2: 3, 3: 2},
    6: {1: 4, 2: 3, 3: 3},
    7: {1: 4, 2: 3, 3: 3, 4: 1},
    8: {1: 4, 2: 3, 3: 3, 4: 2},
    9: {1: 4, 2: 3, 3: 3, 4: 3, 5: 1},
    10: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2},
    11: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1},
    12: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1},
    13: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1, 7: 1},
    14: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1, 7: 1},
    15: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1, 7: 1, 8: 1},
    16: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1, 7: 1, 8: 1},
    17: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1, 7: 1, 8: 1, 9: 1},
    18: {1: 4, 2: 3, 3: 3, 4: 3, 5: 3, 6: 1, 7: 1, 8: 1, 9: 1},
    19: {1: 4, 2: 3, 3: 3, 4: 3, 5: 3, 6: 2, 7: 1, 8: 1, 9: 1},
    20: {1: 4, 2: 3, 3: 3, 4: 3, 5: 3, 6: 2, 7: 2, 8: 1, 9: 1},
}

# Spell slots per level for half casters (Paladin, Ranger)
HALF_CASTER_SPELL_SLOTS = {
    1: {},
    2: {1: 2},
    3: {1: 3},
    4: {1: 3},
    5: {1: 4, 2: 2},
    6: {1: 4, 2: 2},
    7: {1: 4, 2: 3},
    8: {1: 4, 2: 3},
    9: {1: 4, 2: 3, 3: 2},
    10: {1: 4, 2: 3, 3: 2},
    11: {1: 4, 2: 3, 3: 3},
    12: {1: 4, 2: 3, 3: 3},
    13: {1: 4, 2: 3, 3: 3, 4: 1},
    14: {1: 4, 2: 3, 3: 3, 4: 1},
    15: {1: 4, 2: 3, 3: 3, 4: 2},
    16: {1: 4, 2: 3, 3: 3, 4: 2},
    17: {1: 4, 2: 3, 3: 3, 4: 3},
    18: {1: 4, 2: 3, 3: 3, 4: 3},
    19: {1: 4, 2: 3, 3: 3, 4: 3, 5: 1},
    20: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2},
}

# Spell slots per level for Artificer (unique progression)
ARTIFICER_SPELL_SLOTS = {
    1: {1: 2},
    2: {1: 2},
    3: {1: 3},
    4: {1: 3},
    5: {1: 4, 2: 2},
    6: {1: 4, 2: 2},
    7: {1: 4, 2: 3},
    8: {1: 4, 2: 3},
    9: {1: 4, 2: 3, 3: 2},
    10: {1: 4, 2: 3, 3: 2},
    11: {1: 4, 2: 3, 3: 3},
    12: {1: 4, 2: 3, 3: 3},
    13: {1: 4, 2: 3, 3: 3, 4: 1},
    14: {1: 4, 2: 3, 3: 3, 4: 1},
    15: {1: 4, 2: 3, 3: 3, 4: 2},
    16: {1: 4, 2: 3, 3: 3, 4: 2},
    17: {1: 4, 2: 3, 3: 3, 4: 3},
    18: {1: 4, 2: 3, 3: 3, 4: 3},
    19: {1: 4, 2: 3, 3: 3, 4: 3, 5: 1},
    20: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2},
}

# --- Helper Functions ---

def get_player_input(prompt: str, valid_options: list = None, is_numeric: bool = False):
    """Generic function to get validated user input."""
    while True:
        user_input = input(prompt).strip()
        
        if is_numeric:
            if user_input.isdigit():
                return int(user_input)
            else:
                print("Invalid input. Please enter a number.")
        elif valid_options:
            if user_input.lower() in [str(opt).lower() for opt in valid_options]:
                return user_input
            else:
                print(f"Invalid choice. Please select from: {', '.join(map(str, valid_options))}")
        else:
            return user_input

def select_from_list(prompt: str, options: list, display_key='name'):
    """Helper to present a list and get a numbered choice."""
    print(f"\n--- {prompt} ---")
    for i, option in enumerate(options):
        if display_key is None:
            print(f"{i + 1}. {option}")
        else:
            print(f"{i + 1}. {getattr(option, display_key) if hasattr(option, display_key) else option}")
    
    choice_index = get_player_input(f"Select a {prompt.lower()[:-1]}: ", [str(i + 1) for i in range(len(options))], is_numeric=True) - 1
    return options[choice_index]

def calculate_modifier(stat_value: int) -> int:
    """Calculates a D&D 5e ability modifier."""
    return math.floor((stat_value - 10) / 2)

# Helper to get item by name


# --- Core Creation Functions ---

def create_debug_character(config: Config) -> PlayerCharacter:
    """Creates a hardcoded, 5e-compliant debug character for testing."""
    print("--- Character Creation (DEBUG MODE) ---")
    debug_stats = Stats(strength=16, dexterity=14, constitution=15, intelligence=10, wisdom=12, charisma=8)
    return PlayerCharacter(
        name="Debug Adventurer",
        age=30,
        sex="Female",
        race="Dwarf",
        character_class="Fighter",
        background="Soldier",
        alignment="Lawful Good",
        stats=debug_stats,
        hit_points=12, # 10 (Fighter) + 2 (Con mod)
        hit_dice="1d10",
        armor_proficiencies=["Light armor", "Medium armor", "Heavy armor", "Shields"],
        weapon_proficiencies=["Simple weapons", "Martial weapons"],
        tool_proficiencies=[],
        skills=[Skill(name="Athletics", ability="strength", proficient=True)],
        languages=["Common", "Dwarvish"],
        features_and_traits=[SpecialAbility(name="Dwarven Resilience", description="Adv. on saves vs. poison.")],
        gold=100
    )

def create_character(config: Config) -> PlayerCharacter:
    """Creates a new D&D 5e compliant character through an interactive CLI process."""
    print("--- D&D 5th Edition Character Forge ---")

    # Basic Info
    name = input("Enter your character's name: ")
    age = get_player_input("Enter your character's age: ", is_numeric=True)
    sex = get_player_input("Enter your character's sex: ")

    # Race & Class Selection
    chosen_race = select_from_list("Choose your Race", config.races)
    chosen_class = select_from_list("Choose your Class", config.classes)
    
    # Background Selection
    chosen_background = select_from_list("Choose your Background", config.backgrounds)

    # Point-Buy for Stats
    print("\n--- Distribute Your Stat Points (Point-Buy System) ---")
    base_stats = {"strength": 8, "dexterity": 8, "constitution": 8, "intelligence": 8, "wisdom": 8, "charisma": 8}
    points_spent = 0
    point_costs = {9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}

    for stat in base_stats:
        while True:
            points_remaining = 27 - points_spent
            print(f"\nPoints remaining: {points_remaining}")
            value = get_player_input(f"Set {stat.upper()} (8-15): ", [str(i) for i in range(8, 16)], is_numeric=True)
            
            # Check if the new value exceeds points or range
            if value < 8 or value > 15:
                print("Stat value must be between 8 and 15.")
                continue

            # Calculate the cost of the current stat value
            current_stat_cost = point_costs.get(base_stats[stat], 0)
            
            # Calculate the cost of the proposed new stat value
            new_stat_cost = point_costs.get(value, 0)

            # Calculate the points that would be spent if this change is made
            potential_points_spent = points_spent - current_stat_cost + new_stat_cost

            if potential_points_spent <= 27:
                base_stats[stat] = value
                points_spent = potential_points_spent
                break
            else:
                print("Not enough points!")
    
    # Apply Racial Bonuses
    final_stats = base_stats.copy()
    for increase in chosen_race.ability_score_increases:
        final_stats[increase.ability.lower()] += increase.value
    player_stats = Stats(**final_stats)

    # Alignment
    chosen_alignment = select_from_list("Choose your Alignment", config.alignments)

    # Proficiencies
    # Initialize all proficiencies
    armor_proficiencies = set(chosen_class.armor_proficiencies)
    weapon_proficiencies = set(chosen_class.weapon_proficiencies)
    tool_proficiencies = set(chosen_class.tool_proficiencies)
    saving_throw_proficiencies = set(chosen_class.saving_throw_proficiencies)
    skill_proficiencies = set(chosen_background.skill_proficiencies)

    # Add racial proficiencies
    for proficiency in chosen_race.proficiencies:
        if proficiency['type'] == "armor":
            armor_proficiencies.add(proficiency['name'])
        elif proficiency['type'] == "weapon":
            weapon_proficiencies.add(proficiency['name'])
        elif proficiency['type'] == "tool":
            tool_proficiencies.add(proficiency['name'])
        elif proficiency['type'] == "skill":
            skill_proficiencies.add(proficiency['name'])

    # Add class skill proficiencies (interactive choice)
    class_skill_choices_data = chosen_class.skills
    print(f"\n--- Your background gives you proficiency in: {', '.join(skill_proficiencies)} ---")
    print(f"--- As a {chosen_class.name}, you can choose {class_skill_choices_data.number} more skills ---")
    
    available_choices = [s for s in class_skill_choices_data.choices if s not in skill_proficiencies]
    for i in range(class_skill_choices_data.number):
        chosen_skill = select_from_list(f"Select skill {i+1}", available_choices)
        skill_proficiencies.add(chosen_skill.name if hasattr(chosen_skill, 'name') else chosen_skill)
        available_choices.remove(chosen_skill)

    final_skills = [Skill(name=s, ability=ALL_SKILLS[s], proficient=True) for s in skill_proficiencies]
    final_skills.extend([Skill(name=s, ability=ALL_SKILLS[s], proficient=False) for s in ALL_SKILLS if s not in skill_proficiencies])

    # Equipment Selection
    player_equipment = Equipment()
    player_gold = 0

    print("\n--- Starting Equipment ---")
    equipment_choice_type = get_player_input("Do you want to choose starting equipment or take starting gold? (equipment/gold): ", ["equipment", "gold"])

    if equipment_choice_type.lower() == "equipment":
        # Process class equipment options
        for option_group in chosen_class.starting_equipment_options:
            if option_group.choose_one_from:
                chosen_item_name = select_from_list("Choose one item", option_group.choose_one_from, display_key=None)
                item = Item(name=chosen_item_name, item_type="misc") # Create a generic Item object
                if item:
                    player_equipment.inventory.append(item)
                print(f"Added {chosen_item_name}")
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    item = Item(name=item_name, item_type="misc") # Create a generic Item object
                    if item:
                        player_equipment.inventory.append(item)
                    print(f"Added {item_name}")
            if option_group.gold_pieces:
                player_gold += option_group.gold_pieces
                print(f"Added {option_group.gold_pieces} gold pieces.")

        # Process background equipment options
        for option_group in chosen_background.starting_equipment_options:
            if option_group.choose_one_from:
                chosen_item_name = select_from_list("Choose one item", option_group.choose_one_from, display_key=None)
                item = Item(name=chosen_item_name, item_type="misc") # Create a generic Item object
                if item:
                    player_equipment.inventory.append(item)
                print(f"Added {chosen_item_name}")
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    item = Item(name=item_name, item_type="misc") # Create a generic Item object
                    if item:
                        player_equipment.inventory.append(item)
                    print(f"Added {item_name}")
            if option_group.gold_pieces:
                player_gold += option_group.gold_pieces
                print(f"Added {option_group.gold_pieces} gold pieces.")
    else: # Player chose gold
        # D&D 5e standard starting gold is 4d4 * 10 for most classes, or specific amounts.
        # For simplicity, let's give a fixed amount for now.
        player_gold = 100 # Example fixed gold
        print(f"You start with {player_gold} gold pieces.")

    # Get features and traits
    features_and_traits = [SpecialAbility(name=t.name, description=t.description) for t in chosen_race.traits]
    class_features = [SpecialAbility(name=f.name, description=f.description) for f in chosen_class.features if f.level == 1]
    features_and_traits.extend(class_features)

    # Spellcasting Logic
    spellcasting_ability = None
    spell_save_dc = None
    spell_attack_modifier = None
    cantrips_known = []
    spells_known = []
    spell_slots = {}

    if chosen_class.name in ["Wizard", "Sorcerer", "Bard", "Cleric", "Druid", "Artificer", "Paladin", "Ranger"]:
        if chosen_class.name == "Wizard":
            spellcasting_ability = "intelligence"
            # Cantrips: 3 for Wizard at level 1
            # Spells: 6 for Wizard at level 1
            # Spell Slots: 2 for Wizard at level 1
            # For now, we'll just note these. Actual selection would be interactive.
            cantrips_known = ["Fire Bolt", "Light", "Mage Hand"]
            spells_known = ["Magic Missile", "Shield", "Burning Hands", "Charm Person", "Detect Magic", "Sleep"]
            spell_slots = {"1": 2}
        elif chosen_class.name == "Cleric":
            spellcasting_ability = "wisdom"
            # Cantrips: 3 for Cleric at level 1
            # Spells: Prepared spells equal to Wisdom modifier + Cleric level
            # Spell Slots: 2 for Cleric at level 1
            cantrips_known = ["Guidance", "Sacred Flame", "Thaumaturgy"]
            spells_known = ["Cure Wounds", "Guiding Bolt", "Healing Word", "Shield of Faith"]
            spell_slots = {"1": 2}
        elif chosen_class.name == "Sorcerer":
            spellcasting_ability = "charisma"
            # Cantrips: 4 for Sorcerer at level 1
            # Spells: 2 for Sorcerer at level 1
            # Spell Slots: 2 for Sorcerer at level 1
            cantrips_known = ["Chill Touch", "Light", "Message", "Prestidigitation"]
            spells_known = ["Burning Hands", "Magic Missile"]
            spell_slots = {"1": 2}
        elif chosen_class.name == "Bard":
            spellcasting_ability = "charisma"
            # Cantrips: 2 for Bard at level 1
            # Spells: 4 for Bard at level 1
            # Spell Slots: 2 for Bard at level 1
            cantrips_known = ["Light", "Vicious Mockery"]
            spells_known = ["Charm Person", "Cure Wounds", "Healing Word", "Tasha's Hideous Laughter"]
            spell_slots = {"1": 2}
        elif chosen_class.name == "Druid":
            spellcasting_ability = "wisdom"
            # Cantrips: 2 for Druid at level 1
            # Spells: Prepared spells equal to Wisdom modifier + Druid level
            # Spell Slots: 2 for Druid at level 1
            cantrips_known = ["Druidcraft", "Produce Flame"]
            spells_known = ["Entangle", "Goodberry", "Thunderwave"]
            spell_slots = {"1": 2}
        elif chosen_class.name == "Artificer":
            spellcasting_ability = "intelligence"
            # Cantrips: 2 for Artificer at level 1
            # Spells: 2 for Artificer at level 1
            # Spell Slots: 2 for Artificer at level 1
            cantrips_known = ["Acid Splash", "Mending"]
            spells_known = ["Cure Wounds", "Detect Magic"]
            spell_slots = {"1": 2}
        elif chosen_class.name == "Paladin":
            # Paladins get spellcasting at level 2
            pass
        elif chosen_class.name == "Ranger":
            # Rangers get spellcasting at level 2
            pass

    if spellcasting_ability:
        proficiency_bonus = 2 # For a level 1 character
        spell_save_dc = 8 + calculate_modifier(player_stats.dict()[spellcasting_ability]) + proficiency_bonus
        spell_attack_modifier = calculate_modifier(player_stats.dict()[spellcasting_ability]) + proficiency_bonus

    print("\n--- Character Complete! ---")
    return PlayerCharacter(
        name=name, age=age, sex=sex, race=chosen_race.name,
        character_class=chosen_class.name, background=chosen_background.name,
        alignment=chosen_alignment, stats=player_stats,
        armor_proficiencies=list(armor_proficiencies),
        weapon_proficiencies=list(weapon_proficiencies),
        tool_proficiencies=list(tool_proficiencies),
        skills=final_skills,
        hit_points=chosen_class.hit_die + calculate_modifier(player_stats.constitution),
        hit_dice=f"1d{chosen_class.hit_die}",
        features_and_traits=features_and_traits,
        languages=chosen_race.languages,
        equipment=player_equipment,
        gold=player_gold,
        spellcasting_ability=spellcasting_ability,
        spell_save_dc=spell_save_dc,
        spell_attack_modifier=spell_attack_modifier,
        cantrips_known=cantrips_known,
        spells_known=spells_known,
        spell_slots=spell_slots
    )