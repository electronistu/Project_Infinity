# forge/character_creator.py
# Version 4.0 - Full D&D 5e Character Creation with Complete Ruleset

from .models import PlayerCharacter, Stats, Equipment, Skill, SpecialAbility, Item, StartingEquipmentOption, CharacterClass
from .config_loader import Config
import math
import sys
import os
from typing import Optional, List, Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from level_up import FULL_CASTER_SPELL_SLOTS, HALF_CASTER_SPELL_SLOTS, ARTIFICER_SPELL_SLOTS, WARLOCK_SPELL_SLOTS

# --- Data for 5e Rules ---
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
    "Studded Leather Tunic": {"ac": 12, "dex_cap": None, "type": "light"},
    "Hide": {"ac": 12, "dex_cap": 2, "type": "medium"},
    "Scale Mail": {"ac": 14, "dex_cap": 2, "type": "medium"},
    "Breastplate": {"ac": 14, "dex_cap": 2, "type": "medium"},
    "Half Plate": {"ac": 15, "dex_cap": 2, "type": "medium"},
    "Iron Chainmail": {"ac": 16, "dex_cap": 0, "type": "heavy"},
    "Chain Mail": {"ac": 16, "dex_cap": 0, "type": "heavy"},
    "Splint": {"ac": 17, "dex_cap": 0, "type": "heavy"},
    "Plate": {"ac": 18, "dex_cap": 0, "type": "heavy"},
}

SHIELD_NAMES = {"Shield", "Wooden Shield"}

# --- Helper Functions ---

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

def calculate_ac(stats: Stats, character_class: str, inventory: list) -> int:
    dex_mod = calculate_modifier(stats.dexterity)
    con_mod = calculate_modifier(stats.constitution)
    wis_mod = calculate_modifier(stats.wisdom)

    best_armor = None
    for item in inventory:
        if item.name in ARMOR_DATA:
            if best_armor is None or ARMOR_DATA[item.name]["ac"] > ARMOR_DATA[best_armor.name]["ac"]:
                best_armor = item

    has_shield = any(item.name in SHIELD_NAMES for item in inventory)

    if best_armor:
        armor_info = ARMOR_DATA[best_armor.name]
        if armor_info["dex_cap"] is not None:
            effective_dex = min(dex_mod, armor_info["dex_cap"])
        else:
            effective_dex = dex_mod
        ac = armor_info["ac"] + effective_dex
    else:
        unarmored_classes = {"Barbarian", "Monk"}
        if character_class == "Barbarian":
            ac = 10 + dex_mod + con_mod
        elif character_class == "Monk":
            ac = 10 + dex_mod + wis_mod
        else:
            ac = 10 + dex_mod

    if has_shield:
        ac += 2

    return ac


# --- Core Creation Functions ---

def create_debug_character(config: Config) -> PlayerCharacter:
    print("--- Character Creation (DEBUG MODE) ---")
    debug_stats = Stats(strength=16, dexterity=14, constitution=15, intelligence=10, wisdom=12, charisma=8)
    debug_equipment = Equipment()
    debug_equipment.inventory.append(Item(name="Chain Mail", item_type="armor"))
    debug_equipment.inventory.append(Item(name="Shield", item_type="armor"))
    con_mod = calculate_modifier(15)
    return PlayerCharacter(
        name="Debug Adventurer",
        race="Dwarf",
        character_class="Fighter",
        background="Soldier",
        alignment="Lawful Good",
        stats=debug_stats,
        speed=25,
        hit_points=10 + con_mod,
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
        features_and_traits=[SpecialAbility(name="Dwarven Resilience", description="Adv. on saves vs. poison.")],
        equipment=debug_equipment,
        gold=100
    )

def create_character(config: Config) -> PlayerCharacter:
    print("--- D&D 5th Edition Character Forge ---")

    # Basic Info
    name = input("Enter your character's name: ")

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
    player_stats = Stats(**final_stats)

    chosen_alignment = select_from_list("Choose your Alignment", config.alignments)

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

    for bg_tool in chosen_background.tool_proficiencies:
        tool_proficiencies.add(bg_tool)

    # --- Skills ---
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

    # --- Saving Throws ---
    final_saves = [
        Skill(name=s, ability=ALL_SAVES[s], proficient=(s in saving_throw_proficiencies))
        for s in ALL_SAVES
    ]

    # --- Equipment ---
    player_equipment = Equipment()
    player_gold = 0

    print("\n--- Starting Equipment ---")
    equipment_choice_type = get_player_input("Do you want to choose starting equipment or take starting gold? (equipment/gold): ", ["equipment", "gold"])

    if equipment_choice_type.lower() == "equipment":
        for option_group in chosen_class.starting_equipment_options:
            if option_group.choose_one_from:
                chosen_item_name = select_from_list("Choose one item", option_group.choose_one_from, display_key=None)
                if chosen_item_name:
                    player_equipment.inventory.append(Item(name=chosen_item_name, item_type="misc"))
                    print(f"Added {chosen_item_name}")
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    player_equipment.inventory.append(Item(name=item_name, item_type="misc"))
                    print(f"Added {item_name}")
            if option_group.gold_pieces:
                player_gold += option_group.gold_pieces
                print(f"Added {option_group.gold_pieces} gold pieces.")

        for option_group in chosen_background.starting_equipment_options:
            if option_group.choose_one_from:
                chosen_item_name = select_from_list("Choose one item", option_group.choose_one_from, display_key=None)
                if chosen_item_name:
                    player_equipment.inventory.append(Item(name=chosen_item_name, item_type="misc"))
                    print(f"Added {chosen_item_name}")
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    player_equipment.inventory.append(Item(name=item_name, item_type="misc"))
                    print(f"Added {item_name}")
            if option_group.gold_pieces:
                player_gold += option_group.gold_pieces
                print(f"Added {option_group.gold_pieces} gold pieces.")
    else:
        player_gold = 100
        print(f"You start with {player_gold} gold pieces.")

    # --- Features & Traits ---
    features_and_traits = [SpecialAbility(name=t.name, description=t.description) for t in chosen_race.traits]
    class_features = [SpecialAbility(name=f.name, description=f.description) for f in chosen_class.features if f.level == 1]
    features_and_traits.extend(class_features)

    # --- Languages ---
    languages = list(chosen_race.languages)
    if chosen_background.languages:
        languages.append(chosen_background.languages)

    # --- Proficiency Bonus ---
    proficiency_bonus = 2

    # --- Spellcasting ---
    spellcasting_ability = None
    spell_save_dc = None
    spell_attack_modifier = None
    cantrips_known = []
    spells_known = []
    spell_slots = {}

    if chosen_class.name in ["Wizard", "Sorcerer", "Bard", "Cleric", "Druid", "Artificer", "Warlock", "Paladin", "Ranger"]:
        if chosen_class.name == "Wizard":
            spellcasting_ability = "intelligence"
            cantrips_known = ["Fire Bolt", "Light", "Mage Hand"]
            spells_known = ["Magic Missile", "Shield", "Burning Hands", "Charm Person", "Detect Magic", "Sleep"]
            spell_slots = {str(k): v for k, v in FULL_CASTER_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Cleric":
            spellcasting_ability = "wisdom"
            cantrips_known = ["Guidance", "Sacred Flame", "Thaumaturgy"]
            spells_known = ["Cure Wounds", "Guiding Bolt", "Healing Word", "Shield of Faith"]
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
            spells_known = ["Entangle", "Goodberry", "Thunderwave"]
            spell_slots = {str(k): v for k, v in FULL_CASTER_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Artificer":
            spellcasting_ability = "intelligence"
            cantrips_known = ["Acid Splash", "Mending"]
            spells_known = ["Cure Wounds", "Detect Magic"]
            spell_slots = {str(k): v for k, v in ARTIFICER_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Paladin":
            spellcasting_ability = "charisma"
            cantrips_known = []
            spells_known = []
            spell_slots = {str(k): v for k, v in HALF_CASTER_SPELL_SLOTS[1].items()}
        elif chosen_class.name == "Ranger":
            spellcasting_ability = "wisdom"
            cantrips_known = []
            spells_known = []
            spell_slots = {str(k): v for k, v in HALF_CASTER_SPELL_SLOTS[1].items()}

    if spellcasting_ability:
        spell_save_dc = 8 + calculate_modifier(player_stats.dict()[spellcasting_ability]) + proficiency_bonus
        spell_attack_modifier = calculate_modifier(player_stats.dict()[spellcasting_ability]) + proficiency_bonus

    # --- AC ---
    armor_class = calculate_ac(player_stats, chosen_class.name, player_equipment.inventory)

    # --- HP ---
    con_mod = calculate_modifier(player_stats.constitution)
    hit_points = chosen_class.hit_die + con_mod

    print("\n--- Character Complete! ---")
    return PlayerCharacter(
        name=name,
        race=chosen_race.name,
        character_class=chosen_class.name,
        background=chosen_background.name,
        alignment=chosen_alignment,
        stats=player_stats,
        speed=chosen_race.speed,
        hit_points=hit_points,
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
        spellcasting_ability=spellcasting_ability,
        spell_save_dc=spell_save_dc,
        spell_attack_modifier=spell_attack_modifier,
        cantrips_known=cantrips_known,
        spells_known=spells_known,
        spell_slots=spell_slots
    )