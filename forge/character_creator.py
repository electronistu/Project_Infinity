# forge/character_creator.py
# Version 5.0 - Full D&D 5e Character Creation with Accurate Ruleset

from .models import PlayerCharacter, Stats, Equipment, Skill, SpecialAbility, Item, StartingEquipmentOption, CharacterClass
from .config_loader import Config, Race, SubRace
import math
import random
import sys
import os
import re
from typing import Optional, List, Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from level_up import FULL_CASTER_SPELL_SLOTS, HALF_CASTER_SPELL_SLOTS, ARTIFICER_SPELL_SLOTS, WARLOCK_SPELL_SLOTS

ALL_SKILLS = {
    "Acrobatics": "dexterity", "Animal Handling": "wisdom", "Arcana": "intelligence",
    "Athletics": "strength", "Deception": "charisma", "History": "intelligence",
    "Insight": "wisdom", "Intimidation": "charisma", "Investigation": "intelligence",
    "Medicine": "wisdom", "Nature": "intelligence", "Perception": "wisdom",
    "Performance": "charisma", "Persuasion": "charisma", "Religion": "intelligence",
    "Sleight of Hand": "dexterity", "Stealth": "dexterity", "Survival": "wisdom"
}

ALL_SAVES = {
    "Strength": "strength", "Dexterity": "dexterity", "Constitution": "constitution",
    "Intelligence": "intelligence", "Wisdom": "wisdom", "Charisma": "charisma"
}

ARMOR_DATA = {
    "Padded": {"ac": 11, "dex_cap": None, "type": "light"},
    "Leather Tunic": {"ac": 11, "dex_cap": None, "type": "light"},
    "Leather Armor": {"ac": 11, "dex_cap": None, "type": "light"},
    "Studded Leather Tunic": {"ac": 12, "dex_cap": None, "type": "light"},
    "Studded Leather Armor": {"ac": 12, "dex_cap": None, "type": "light"},
    "Hide": {"ac": 12, "dex_cap": 2, "type": "medium"},
    "Scale Mail": {"ac": 14, "dex_cap": 2, "type": "medium"},
    "Breastplate": {"ac": 14, "dex_cap": 2, "type": "medium"},
    "Half Plate": {"ac": 15, "dex_cap": 2, "type": "medium"},
    "Iron Chainmail": {"ac": 16, "dex_cap": 0, "type": "heavy"},
    "Chain Mail": {"ac": 16, "dex_cap": 0, "type": "heavy"},
    "Chain Shirt": {"ac": 13, "dex_cap": 2, "type": "medium"},
    "Splint": {"ac": 17, "dex_cap": 0, "type": "heavy"},
    "Plate": {"ac": 18, "dex_cap": 0, "type": "heavy"},
}

SHIELD_NAMES = {"Shield", "Wooden Shield"}

WEAPON_NAMES = {
    "Club", "Dagger", "Greatclub", "Handaxe", "Javelin", "Light Hammer", "Mace",
    "Quarterstaff", "Sickle", "Spear", "Darts", "Light Crossbow", "Shortbow", "Sling",
    "Battleaxe", "Flail", "Glaive", "Greataxe", "Greatsword", "Halberd", "Hand Crossbow",
    "Heavy Crossbow", "Hammer", "Lance", "Longbow", "Longsword", "Maul", "Morningstar",
    "Pike", "Rapier", "Scimitar", "Shortsword", "Trident", "War Pick", "Warhammer",
    "Whip", "Blowgun", "Hand Crossbow", "Net",
    "Two Handaxes", "Two Shortswords", "Two Daggers", "Four Javelins", "Five Javelins",
    "10 Darts", "Two Simple Melee Weapons", "Any Simple Weapon", "Any Simple Melee Weapon",
    "Any Martial Melee Weapon", "Two Martial Weapons", "Any Two Simple Weapons",
}

KNOWN_SPELL_CLASSES = {"Wizard", "Sorcerer", "Warlock", "Bard"}
PREPARED_SPELL_CLASSES = {"Cleric", "Druid", "Artificer", "Paladin", "Ranger"}

DRACONIC_ANCESTRY = {
    "Black": {"damage_type": "acid", "breath_shape": "5 by 30 ft. line", "save": "Dexterity"},
    "Blue": {"damage_type": "lightning", "breath_shape": "5 by 30 ft. line", "save": "Dexterity"},
    "Brass": {"damage_type": "fire", "breath_shape": "5 by 30 ft. line", "save": "Dexterity"},
    "Bronze": {"damage_type": "lightning", "breath_shape": "5 by 30 ft. line", "save": "Dexterity"},
    "Copper": {"damage_type": "acid", "breath_shape": "5 by 30 ft. line", "save": "Dexterity"},
    "Gold": {"damage_type": "fire", "breath_shape": "15 ft. cone", "save": "Dexterity"},
    "Green": {"damage_type": "poison", "breath_shape": "15 ft. cone", "save": "Constitution"},
    "Red": {"damage_type": "fire", "breath_shape": "15 ft. cone", "save": "Dexterity"},
    "Silver": {"damage_type": "cold", "breath_shape": "15 ft. cone", "save": "Constitution"},
    "White": {"damage_type": "cold", "breath_shape": "15 ft. cone", "save": "Constitution"},
}

STANDARD_LANGUAGES = [
    "Common", "Dwarvish", "Elvish", "Giant", "Gnomish", "Goblin", "Halfling",
    "Orc", "Abyssal", "Celestial", "Draconic", "Deep Speech", "Infernal",
    "Primordial", "Sylvan", "Undercommon"
]

def get_player_input(prompt: str, valid_options: list = None, is_numeric: bool = False, range_min: int = None, range_max: int = None):
    while True:
        user_input = input(prompt).strip()
        
        if is_numeric:
            if user_input.isdigit():
                val = int(user_input)
                if range_min is not None and val < range_min:
                    print(f"Invalid input. Minimum value is {range_min}.")
                    continue
                if range_max is not None and val > range_max:
                    print(f"Invalid input. Maximum value is {range_max}.")
                    continue
                return val
            else:
                print("Invalid input. Please enter a number.")
        elif valid_options:
            for opt in valid_options:
                if user_input.lower() == str(opt).lower():
                    return opt
            print(f"Invalid choice. Please select from: {', '.join(map(str, valid_options))}")
        else:
            if not user_input:
                print("Input cannot be empty.")
                continue
            return user_input

def select_from_list(prompt: str, options: list, display_key='name'):
    if not options:
        return None

    print(f"\n--- {prompt} ---")
    for i, option in enumerate(options):
        if display_key is None:
            print(f"{i + 1}. {option}")
        else:
            print(f"{i + 1}. {getattr(option, display_key) if hasattr(option, display_key) else option}")
    
    noun = prompt.replace("Choose your ", "").lower()
    
    choice_index = get_player_input(
        f"Select a {noun}: ", 
        is_numeric=True, 
        range_min=1, 
        range_max=len(options)
    ) - 1
    return options[choice_index]

def calculate_modifier(stat_value: int) -> int:
    return math.floor((stat_value - 10) / 2)

def classify_item(item_name: str) -> str:
    if item_name in ARMOR_DATA:
        return "armor"
    if item_name in SHIELD_NAMES:
        return "shield"
    if any(w in item_name for w in ["Pack", "Pouch", "Clothes", "Vestments", "Robes"]):
        return "gear"
    if any(w in item_name for w in ["Symbol", "Focus", "Spellbook", "Book"]):
        return "focus"
    if any(w in item_name for w in ["Bolts", "Arrows", "Javelins", "Darts"]):
        return "ammunition"
    if any(w in item_name for w in ["Potion", "Vial", "Flask", "Rations", "Torch", "Oil",
                                     "Incense", "Candle", "Caltrop", "Chalk", "Piton",
                                     "Tinderbox", "Waterskin", "Antitoxin", "Alchemist",
                                     "Acid", "Ball Bearing", "Healing"]):
        return "consumable"
    if re.match(r'^(\d+)\s', item_name):
        if item_name not in WEAPON_NAMES:
            return "consumable"
    word_num_pattern = r'^(One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve|Fifteen|Twenty|Fifty)\s'
    if re.match(word_num_pattern, item_name, re.IGNORECASE):
        if item_name not in WEAPON_NAMES:
            return "consumable"
    return "misc"

def parse_consumable_quantity(item_name: str):
    word_to_num = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                   "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                   "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
                   "fifty": 50}
    match = re.search(r'(\d+)\s+([A-Za-z].*)', item_name)
    if match:
        return match.group(2).strip(), int(match.group(1))
    word_match = re.match(r'(\w+)\s+(.*)', item_name, re.IGNORECASE)
    if word_match:
        num_word = word_match.group(1).lower()
        if num_word in word_to_num:
            return word_match.group(2).strip(), word_to_num[num_word]
    return item_name, 1

def split_compound_items(item_name: str) -> List[Item]:
    parts = [p.strip() for p in item_name.split(",")]
    items = []
    for part in parts:
        item_type = classify_item(part)
        items.append(Item(name=part, item_type=item_type))
    return items

def calculate_ac(stats: Stats, character_class: str, inventory: list, fighting_style: str = None) -> int:
    dex_mod = calculate_modifier(stats.dexterity)
    con_mod = calculate_modifier(stats.constitution)
    wis_mod = calculate_modifier(stats.wisdom)

    candidates = []

    for item in inventory:
        if item.name in ARMOR_DATA:
            armor_info = ARMOR_DATA[item.name]
            if armor_info["dex_cap"] is not None:
                effective_dex = min(dex_mod, armor_info["dex_cap"])
            else:
                effective_dex = dex_mod
            effective_ac = armor_info["ac"] + effective_dex
            candidates.append(effective_ac)

    has_shield = any(item.name in SHIELD_NAMES for item in inventory)

    unarmored_classes = {"Barbarian", "Monk"}
    if character_class == "Barbarian":
        candidates.append(10 + dex_mod + con_mod)
    elif character_class == "Monk":
        candidates.append(10 + dex_mod + wis_mod)
    
    if not candidates:
        candidates.append(10 + dex_mod)

    ac = max(candidates)

    if has_shield:
        if character_class == "Monk":
            pass
        else:
            ac += 2

    if fighting_style == "Defense" and any(item.name in ARMOR_DATA for item in inventory):
        ac += 1

    return ac

def roll_starting_gold(dice_notation: str) -> int:
    match = re.match(r'(\d+)d(\d+)', dice_notation)
    if not match:
        return 0
    num_dice = int(match.group(1))
    die_size = int(match.group(2))
    rolls = [random.randint(1, die_size) for _ in range(num_dice)]
    total = sum(rolls) * 10
    print(f"  Rolled {dice_notation}: [{', '.join(str(r) for r in rolls)}] x 10 = {total} gp")
    return total

def prompt_language_choice(prompt_text: str, already_known: List[str], number: int) -> List[str]:
    available = [lang for lang in STANDARD_LANGUAGES if lang not in already_known]
    if not available:
        print("No additional languages available to choose.")
        return []
    
    chosen = []
    for i in range(number):
        if not available:
            break
        print(f"\n--- {prompt_text} ({i+1} of {number}) ---")
        for j, lang in enumerate(available):
            print(f"{j + 1}. {lang}")
        idx = get_player_input(f"Choose a language: ", is_numeric=True, range_min=1, range_max=len(available)) - 1
        chosen.append(available[idx])
        available.pop(idx)
    return chosen


def create_debug_character(config: Config) -> PlayerCharacter:
    print("--- Character Creation (DEBUG MODE) ---")
    debug_stats = Stats(strength=16, dexterity=14, constitution=15, intelligence=10, wisdom=12, charisma=8)
    debug_equipment = Equipment()
    debug_equipment.inventory.append(Item(name="Chain Mail", item_type="armor"))
    debug_equipment.inventory.append(Item(name="Shield", item_type="shield"))
    con_mod = calculate_modifier(15)
    return PlayerCharacter(
        name="Debug Adventurer",
        race="Dwarf",
        subrace="Hill Dwarf",
        character_class="Fighter",
        background="Soldier",
        alignment="Lawful Good",
        gender="Unknown",
        stats=debug_stats,
        speed=25,
        current_hit_points=10 + con_mod + 1,
        total_hit_points=10 + con_mod + 1,
        hit_dice="1d10",
        hit_dice_count=1,
        hit_dice_size=10,
        armor_class=calculate_ac(debug_stats, "Fighter", debug_equipment.inventory),
        proficiency_bonus=2,
        armor_proficiencies=["Light armor", "Medium armor", "Heavy armor", "Shields"],
        weapon_proficiencies=["Simple weapons", "Martial weapons"],
        tool_proficiencies=[],
        skills=[Skill(name="Athletics", ability="strength", proficient=True)],
        saving_throws=[
            Skill(name="Strength", ability="strength", proficient=True),
            Skill(name="Dexterity", ability="dexterity", proficient=False),
            Skill(name="Constitution", ability="constitution", proficient=True),
            Skill(name="Intelligence", ability="intelligence", proficient=False),
            Skill(name="Wisdom", ability="wisdom", proficient=False),
            Skill(name="Charisma", ability="charisma", proficient=False),
        ],
        languages=["Common", "Dwarvish"],
        features_and_traits=[
            SpecialAbility(name="Dwarven Resilience", description="Adv. on saves vs. poison."),
            SpecialAbility(name="Dwarven Toughness", description="HP maximum increases by 1 per level."),
            SpecialAbility(name="Second Wind", description="Regain 1d10+level HP as bonus action."),
        ],
        equipment=debug_equipment,
        gold=100,
        consumables={},
    )


def create_character(config: Config) -> PlayerCharacter:
    print("--- D&D 5th Edition Character Forge ---")

    name = input("Enter your character's name: ")

    while True:
        gender = input("Enter your character's gender: ").strip()
        if gender and len(gender) <= 15:
            break
        if not gender:
            print("Input cannot be empty.")
        else:
            print(f"Invalid input. Must be no more than 15 characters (current: {len(gender)}).")

    chosen_race = select_from_list("Choose your Race", config.races)

    chosen_subrace = None
    if chosen_race.subraces:
        chosen_subrace = select_from_list("Choose your Subrace", chosen_race.subraces)

    chosen_class = select_from_list("Choose your Class", config.classes)
    chosen_background = select_from_list("Choose your Background", config.backgrounds)

    # --- Point-Buy for Stats ---
    print("\n--- Distribute Your Stat Points (Point-Buy System) ---")
    base_stats = {"strength": 8, "dexterity": 8, "constitution": 8, "intelligence": 8, "wisdom": 8, "charisma": 8}
    points_spent = 0
    point_costs = {9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}

    for stat in base_stats:
        while True:
            points_remaining = 27 - points_spent
            print(f"\nPoints remaining: {points_remaining}")
            value = get_player_input(f"Set {stat.upper()} (8-15): ", is_numeric=True, range_min=8, range_max=15)
            
            current_stat_cost = point_costs.get(base_stats[stat], 0)
            new_stat_cost = point_costs.get(value, 0)
            potential_points_spent = points_spent - current_stat_cost + new_stat_cost

            if potential_points_spent <= 27:
                base_stats[stat] = value
                points_spent = potential_points_spent
                break
            else:
                print(f"Not enough points! This change would cost {new_stat_cost} points, but you only have {points_remaining} left (after accounting for the {current_stat_cost} you'd get back).")
    
    final_stats = base_stats.copy()
    for increase in chosen_race.ability_score_increases:
        final_stats[increase.ability.lower()] += increase.value
    if chosen_subrace:
        for increase in chosen_subrace.ability_score_increases:
            final_stats[increase.ability.lower()] += increase.value
    player_stats = Stats(**final_stats)

    chosen_alignment = select_from_list("Choose your Alignment", config.alignments)

    # --- Dragonborn Draconic Ancestry ---
    draconic_ancestry = None
    if chosen_race.name == "Dragonborn":
        dragon_types = list(DRACONIC_ANCESTRY.keys())
        print("\n--- Choose Your Draconic Ancestry ---")
        for i, dt in enumerate(dragon_types):
            info = DRACONIC_ANCESTRY[dt]
            print(f"{i + 1}. {dt} ({info['damage_type']}, {info['breath_shape']}, {info['save']} save)")
        ancestry_idx = get_player_input("Select ancestry: ", is_numeric=True, range_min=1, range_max=len(dragon_types)) - 1
        draconic_ancestry = dragon_types[ancestry_idx]

    # --- Proficiencies ---
    armor_proficiencies = set(chosen_class.armor_proficiencies)
    weapon_proficiencies = set(chosen_class.weapon_proficiencies)
    tool_proficiencies = set(chosen_class.tool_proficiencies)
    saving_throw_proficiencies = set(chosen_class.saving_throw_proficiencies)
    skill_proficiencies = set(chosen_background.skill_proficiencies)

    for proficiency in chosen_race.proficiencies:
        if proficiency['type'] == "armor":
            armor_proficiencies.add(proficiency['name'])
        elif proficiency['type'] == "weapon":
            weapon_proficiencies.add(proficiency['name'])
        elif proficiency['type'] == "tool":
            tool_proficiencies.add(proficiency['name'])
        elif proficiency['type'] == "skill":
            skill_proficiencies.add(proficiency['name'])

    if chosen_subrace:
        for proficiency in chosen_subrace.proficiencies:
            if proficiency['type'] == "armor":
                armor_proficiencies.add(proficiency['name'])
            elif proficiency['type'] == "weapon":
                weapon_proficiencies.add(proficiency['name'])
            elif proficiency['type'] == "tool":
                tool_proficiencies.add(proficiency['name'])
            elif proficiency['type'] == "skill":
                skill_proficiencies.add(proficiency['name'])

    for bg_tool in chosen_background.tool_proficiencies:
        tool_proficiencies.add(bg_tool)

    if chosen_class.tool_proficiency_choices:
        print(f"\n--- Tool Proficiency Choice ---")
        print(f"As a {chosen_class.name}, choose one from: {', '.join(chosen_class.tool_proficiency_choices.choose_one_from)}")
        chosen_tool = select_from_list("Choose your tool proficiency", chosen_class.tool_proficiency_choices.choose_one_from, display_key=None)
        tool_proficiencies.add(chosen_tool)

    # --- Skills ---
    class_skill_choices_data = chosen_class.skills
    print(f"\n--- Your background gives you proficiency in: {', '.join(skill_proficiencies)} ---")

    skill_choices_list = class_skill_choices_data.choices
    is_any_skill = False
    num_skill_choices = class_skill_choices_data.number

    if len(skill_choices_list) == 1 and skill_choices_list[0] == "*":
        is_any_skill = True
        available_choices = [s for s in ALL_SKILLS.keys() if s not in skill_proficiencies]
        print(f"--- As a {chosen_class.name}, you can choose {num_skill_choices} skills from any ---")
    else:
        available_choices = [s for s in skill_choices_list if s not in skill_proficiencies]
        print(f"--- As a {chosen_class.name}, you can choose {num_skill_choices} more skills ---")

    for i in range(num_skill_choices):
        if not available_choices:
            break
        chosen_skill = select_from_list(f"Select skill {i+1}", available_choices, display_key=None)
        skill_name = chosen_skill.name if hasattr(chosen_skill, 'name') else chosen_skill
        skill_proficiencies.add(skill_name)
        available_choices.remove(chosen_skill)

    final_skills = [Skill(name=s, ability=ALL_SKILLS[s], proficient=True) for s in skill_proficiencies]
    final_skills.extend([Skill(name=s, ability=ALL_SKILLS[s], proficient=False) for s in ALL_SKILLS if s not in skill_proficiencies])

    # --- Saving Throws ---
    final_saves = [
        Skill(name=s, ability=ALL_SAVES[s], proficient=(s in saving_throw_proficiencies))
        for s in ALL_SAVES
    ]

    # --- Fighting Style ---
    fighting_style = None
    if chosen_class.fighting_styles:
        print(f"\n--- Fighting Style ---")
        for i, fs in enumerate(chosen_class.fighting_styles):
            print(f"{i + 1}. {fs.name}: {fs.description}")
        fs_idx = get_player_input(
            f"Choose a fighting style (1-{len(chosen_class.fighting_styles)}): ",
            is_numeric=True, range_min=1, range_max=len(chosen_class.fighting_styles)
        ) - 1
        fighting_style = chosen_class.fighting_styles[fs_idx]
        print(f"  Selected: {fighting_style.name}")

    # --- Equipment ---
    player_equipment = Equipment()
    player_gold = 0
    player_consumables = {}

    print("\n--- Starting Equipment ---")
    equipment_choice_type = get_player_input("Do you want to choose starting equipment or take starting gold? (equipment/gold): ", ["equipment", "gold"])

    if equipment_choice_type.lower() == "equipment":
        for option_group in chosen_class.starting_equipment_options:
            if option_group.choose_one_from:
                chosen_item_name = select_from_list("Choose one item", option_group.choose_one_from, display_key=None)
                if chosen_item_name:
                    for item in split_compound_items(chosen_item_name):
                        if item.item_type in ("ammunition", "consumable"):
                            cons_name, qty = parse_consumable_quantity(item.name)
                            player_consumables[cons_name] = player_consumables.get(cons_name, 0) + qty
                            print(f"  Added {item.name} → consumables.{cons_name}: {qty}")
                        else:
                            player_equipment.inventory.append(item)
                            print(f"  Added {item.name} ({item.item_type})")
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    for item in split_compound_items(item_name):
                        if item.item_type in ("ammunition", "consumable"):
                            cons_name, qty = parse_consumable_quantity(item.name)
                            player_consumables[cons_name] = player_consumables.get(cons_name, 0) + qty
                            print(f"  Added {item.name} → consumables.{cons_name}: {qty}")
                        else:
                            player_equipment.inventory.append(item)
                            print(f"  Added {item.name} ({item.item_type})")
            if option_group.gold_pieces:
                player_gold += option_group.gold_pieces
                print(f"  Added {option_group.gold_pieces} gold pieces.")

        for option_group in chosen_background.starting_equipment_options:
            if option_group.choose_one_from:
                chosen_item_name = select_from_list("Choose one item", option_group.choose_one_from, display_key=None)
                if chosen_item_name:
                    for item in split_compound_items(chosen_item_name):
                        if item.item_type in ("ammunition", "consumable"):
                            cons_name, qty = parse_consumable_quantity(item.name)
                            player_consumables[cons_name] = player_consumables.get(cons_name, 0) + qty
                            print(f"  Added {item.name} → consumables.{cons_name}: {qty}")
                        else:
                            player_equipment.inventory.append(item)
                            print(f"  Added {item.name} ({item.item_type})")
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    for item in split_compound_items(item_name):
                        if item.item_type in ("ammunition", "consumable"):
                            cons_name, qty = parse_consumable_quantity(item.name)
                            player_consumables[cons_name] = player_consumables.get(cons_name, 0) + qty
                            print(f"  Added {item.name} → consumables.{cons_name}: {qty}")
                        else:
                            player_equipment.inventory.append(item)
                            print(f"  Added {item.name} ({item.item_type})")
            if option_group.gold_pieces:
                player_gold += option_group.gold_pieces
                print(f"  Added {option_group.gold_pieces} gold pieces.")
    else:
        if chosen_class.starting_gold_dice:
            print(f"\n--- Rolling Starting Gold ({chosen_class.starting_gold_dice} x 10) ---")
            player_gold = roll_starting_gold(chosen_class.starting_gold_dice)
        else:
            player_gold = roll_starting_gold("4d4")
            print(f"  Default starting gold: {player_gold} gp")

        print("\n--- Fixed Items (always granted) ---")
        for option_group in chosen_class.starting_equipment_options:
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    for item in split_compound_items(item_name):
                        if item.item_type in ("ammunition", "consumable"):
                            cons_name, qty = parse_consumable_quantity(item.name)
                            player_consumables[cons_name] = player_consumables.get(cons_name, 0) + qty
                            print(f"  Added {item.name} → consumables.{cons_name}: {qty}")
                        else:
                            player_equipment.inventory.append(item)
                            print(f"  Added {item.name} ({item.item_type})")

        for option_group in chosen_background.starting_equipment_options:
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    for item in split_compound_items(item_name):
                        if item.item_type in ("ammunition", "consumable"):
                            cons_name, qty = parse_consumable_quantity(item.name)
                            player_consumables[cons_name] = player_consumables.get(cons_name, 0) + qty
                            print(f"  Added {item.name} → consumables.{cons_name}: {qty}")
                        else:
                            player_equipment.inventory.append(item)
                            print(f"  Added {item.name} ({item.item_type})")
            if option_group.gold_pieces:
                player_gold += option_group.gold_pieces
                print(f"  Added {option_group.gold_pieces} gold pieces.")

    # --- Features & Traits ---
    features_and_traits = [SpecialAbility(name=t.name, description=t.description) for t in chosen_race.traits]
    if chosen_subrace:
        features_and_traits.extend([SpecialAbility(name=t.name, description=t.description) for t in chosen_subrace.traits])

    if draconic_ancestry:
        ancestry_info = DRACONIC_ANCESTRY[draconic_ancestry]
        for feat in features_and_traits:
            if feat.name == "Draconic Ancestry":
                feat.description = f"{draconic_ancestry} dragon ancestry. {ancestry_info['damage_type']} damage ({ancestry_info['breath_shape']}, {ancestry_info['save']} save)."
            elif feat.name == "Damage Resistance":
                feat.description = f"You have resistance to {ancestry_info['damage_type']} damage."

    class_features = [SpecialAbility(name=f.name, description=f.description) for f in chosen_class.features if f.level == 1]
    features_and_traits.extend(class_features)

    if fighting_style:
        features_and_traits.append(SpecialAbility(name=f"Fighting Style: {fighting_style.name}", description=fighting_style.description))

    if chosen_background.feature:
        features_and_traits.append(SpecialAbility(name=chosen_background.feature.name, description=chosen_background.feature.description))

    # --- Rogue Expertise ---
    expertise_skills = []
    if chosen_class.name == "Rogue":
        proficient_skill_names = [s for s in skill_proficiencies]
        print(f"\n--- Rogue Expertise ---")
        print("Choose 2 skills you are proficient in to gain expertise (double proficiency bonus).")
        available_expertise = list(proficient_skill_names)
        for i in range(2):
            if not available_expertise:
                break
            print(f"\nAvailable skills for expertise: {', '.join(available_expertise)}")
            chosen_exp = get_player_input(f"Select skill {i+1} for expertise: ", valid_options=available_expertise)
            expertise_skills.append(chosen_exp)
            available_expertise.remove(chosen_exp)
        print(f"  Expertise chosen: {', '.join(expertise_skills)}")

    # --- Languages ---
    languages = list(chosen_race.languages)

    if chosen_subrace:
        for lang in chosen_subrace.languages:
            if lang and lang not in languages:
                languages.append(lang)

    if chosen_class.name == "Druid" and "Druidic" not in languages:
        languages.append("Druidic")

    language_choice_count = 0
    for lang in languages:
        if "choice" in lang.lower():
            language_choice_count += 1
            languages.remove(lang)

    for bg_lang in ([chosen_background.languages] if chosen_background.languages else []):
        if bg_lang and "choice" in bg_lang.lower():
            match = re.match(r'(\w+)', bg_lang)
            num_langs = 1
            if match:
                word = match.group(1).lower()
                word_to_num = {"one": 1, "two": 2, "three": 3}
                num_langs = word_to_num.get(word, 1)
            language_choice_count += num_langs
        elif bg_lang and bg_lang not in languages:
            languages.append(bg_lang)

    if language_choice_count > 0:
        new_langs = prompt_language_choice("Choose additional languages", languages, language_choice_count)
        languages.extend(new_langs)

    languages = [l for l in languages if l and "choice" not in l.lower()]

    # --- Proficiency Bonus ---
    proficiency_bonus = 2

    # --- Spellcasting ---
    spellcasting_ability = None
    spell_save_dc = None
    spell_attack_modifier = None
    cantrips_known = []
    spells_known = []
    spell_slots = {}

    if chosen_class.name in KNOWN_SPELL_CLASSES or chosen_class.name in PREPARED_SPELL_CLASSES:
        if chosen_class.name == "Wizard":
            spellcasting_ability = "intelligence"
            cantrips_known = ["Fire Bolt", "Light", "Mage Hand"]
            spells_known = ["Magic Missile", "Shield", "Burning Hands", "Charm Person", "Detect Magic", "Sleep"]
            spell_slots = {str(k): v for k, v in FULL_CASTER_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Cleric":
            spellcasting_ability = "wisdom"
            cantrips_known = ["Guidance", "Sacred Flame", "Thaumaturgy"]
            spells_known = []
            spell_slots = {str(k): v for k, v in FULL_CASTER_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Sorcerer":
            spellcasting_ability = "charisma"
            cantrips_known = ["Chill Touch", "Light", "Message", "Prestidigitation"]
            spells_known = ["Burning Hands", "Magic Missile"]
            spell_slots = {str(k): v for k, v in FULL_CASTER_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Warlock":
            spellcasting_ability = "charisma"
            cantrips_known = ["Eldritch Blast", "Chill Touch"]
            spells_known = ["Hex", "Armor of Agathys"]
            spell_slots = {str(k): v for k, v in WARLOCK_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Bard":
            spellcasting_ability = "charisma"
            cantrips_known = ["Light", "Vicious Mockery"]
            spells_known = ["Charm Person", "Cure Wounds", "Healing Word", "Tasha's Hideous Laughter"]
            spell_slots = {str(k): v for k, v in FULL_CASTER_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Druid":
            spellcasting_ability = "wisdom"
            cantrips_known = ["Druidcraft", "Produce Flame"]
            spells_known = []
            spell_slots = {str(k): v for k, v in FULL_CASTER_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Artificer":
            spellcasting_ability = "intelligence"
            cantrips_known = ["Acid Splash", "Mending"]
            spells_known = []
            spell_slots = {str(k): v for k, v in ARTIFICER_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Paladin":
            spellcasting_ability = "charisma"
            cantrips_known = []
            spells_known = []
            spell_slots = {}
        elif chosen_class.name == "Ranger":
            spellcasting_ability = "wisdom"
            cantrips_known = []
            spells_known = []
            spell_slots = {}

    if spellcasting_ability:
        spell_save_dc = 8 + calculate_modifier(player_stats.dict()[spellcasting_ability]) + proficiency_bonus
        spell_attack_modifier = calculate_modifier(player_stats.dict()[spellcasting_ability]) + proficiency_bonus

    # --- AC ---
    fs_name = fighting_style.name if fighting_style else None
    if fs_name == "Defense":
        armor_class = calculate_ac(player_stats, chosen_class.name, player_equipment.inventory, fighting_style="Defense")
    else:
        armor_class = calculate_ac(player_stats, chosen_class.name, player_equipment.inventory)

    # --- HP ---
    con_mod = calculate_modifier(player_stats.constitution)
    hit_points = chosen_class.hit_die + con_mod

    if chosen_race.name == "Dwarf" and chosen_subrace and chosen_subrace.name == "Hill Dwarf":
        hit_points += 1

    total_hit_points = hit_points

    # --- Speed ---
    speed = chosen_race.speed
    if chosen_subrace and chosen_subrace.name == "Wood Elf":
        speed = 35

    print("\n--- Character Complete! ---")
    return PlayerCharacter(
        name=name,
        race=chosen_race.name,
        subrace=chosen_subrace.name if chosen_subrace else None,
        character_class=chosen_class.name,
        background=chosen_background.name,
        alignment=chosen_alignment,
        gender=gender,
        stats=player_stats,
        speed=speed,
        current_hit_points=hit_points,
        total_hit_points=total_hit_points,
        hit_dice=f"1d{chosen_class.hit_die}",
        hit_dice_count=1,
        hit_dice_size=chosen_class.hit_die,
        armor_class=armor_class,
        proficiency_bonus=proficiency_bonus,
        armor_proficiencies=list(armor_proficiencies),
        weapon_proficiencies=list(weapon_proficiencies),
        tool_proficiencies=list(tool_proficiencies),
        skills=final_skills,
        saving_throws=final_saves,
        features_and_traits=features_and_traits,
        languages=languages,
        equipment=player_equipment,
        gold=player_gold,
        consumables=player_consumables,
        spellcasting_ability=spellcasting_ability,
        spell_save_dc=spell_save_dc,
        spell_attack_modifier=spell_attack_modifier,
        cantrips_known=cantrips_known,
        spells_known=spells_known,
        spell_slots=spell_slots
    )