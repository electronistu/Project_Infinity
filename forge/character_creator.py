# forge/character_creator.py
# Version 6.0 - Modern TUI Character Creation with Interactive Spell Selection

from .models import PlayerCharacter, Stats, Equipment, Skill, SpecialAbility, Item, StartingEquipmentOption, CharacterClass
from .config_loader import Config, Race, SubRace, Weapon
import math
import random
import sys
import os
import re
from typing import Optional, List, Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from level_up import FULL_CASTER_SPELL_SLOTS, HALF_CASTER_SPELL_SLOTS, WARLOCK_SPELL_SLOTS
from . import tui
from .class_spells import (
    get_available_cantrips, get_available_level1_spells,
    CANTRIP_COUNTS, KNOWN_SPELL_COUNTS, PREPARED_SPELL_COUNTS_BASE,
)

KNOWN_SPELL_CLASSES = {"Sorcerer", "Warlock", "Bard", "Ranger"}
PREPARED_SPELL_CLASSES = {"Cleric", "Druid", "Paladin"}

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

MUSICAL_INSTRUMENTS = [
    "Bagpipes", "Birdpipes", "Drum", "Dulcimer", "Flute", "Glaur",
    "Hand Drum", "Horn", "Longhorn", "Lute", "Lyre", "Pan Flute",
    "Shawm", "Songhorn", "Tantan", "Thelarr", "Tocken", "Warhorn", "Zulkoon"
]

GAMING_SETS = [
    "Dice Set", "Dragonchess Set", "Playing Card Set", "Three-Dragon Ante Set"
]

ARTISAN_TOOLS = [
    "Alchemist's Supplies", "Brewer's Supplies", "Calligrapher's Supplies",
    "Carpenter's Tools", "Cobbler's Tools", "Cook's Utensils",
    "Glassblower's Tools", "Jeweler's Tools", "Leatherworker's Tools",
    "Mason's Tools", "Painter's Supplies", "Potter's Tools",
    "Smith's Tools", "Tinker's Tools", "Weaver's Tools", "Woodcarver's Tools"
]

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

def build_weapon_data(weapons: List[Weapon]) -> dict:
    weapon_data = {}
    for w in weapons:
        props_str = ", ".join(w.properties) if w.properties else ""
        damage_str = f"{w.damage} {w.damage_type}" if w.damage_type else str(w.damage)
        desc_parts = [damage_str]
        if props_str:
            desc_parts.append(props_str)
        weapon_data[w.name] = {
            "category": w.category,
            "melee": w.melee,
            "damage": str(w.damage),
            "damage_type": w.damage_type,
            "properties": w.properties,
            "description": ", ".join(desc_parts),
        }
    return weapon_data

def build_weapon_categories(weapon_data: dict) -> dict:
    simple = [w for w, d in weapon_data.items() if d["category"] == "simple"]
    martial = [w for w, d in weapon_data.items() if d["category"] == "martial"]
    simple_melee = [w for w, d in weapon_data.items() if d["category"] == "simple" and d["melee"]]
    martial_melee = [w for w, d in weapon_data.items() if d["category"] == "martial" and d["melee"]]
    return {
        "simple": simple,
        "martial": martial,
        "simple_melee": simple_melee,
        "martial_melee": martial_melee,
    }

def make_weapon_item(name: str, weapon_data: dict) -> Item:
    if name in weapon_data:
        w = weapon_data[name]
        return Item(
            name=name,
            item_type="weapon",
            damage=w["damage"],
            damage_type=w["damage_type"],
            properties=w["properties"],
            description=w["description"],
        )
    return Item(name=name, item_type="weapon")

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

def resolve_tool_proficiency(text, interactive=True):
    """Resolve a tool proficiency string to a list of concrete tool names.
    
    Handles patterns like:
      "One type of gaming set"            → 1 gaming set
      "Musical Instrument (three of your choice)" → 3 instruments
      "Artisan's Tools (one type of your choice)" → 1 artisan tool
      "Thieves' tools"                    → ["Thieves' tools"] (pass-through)
    
    Args:
        text: The tool proficiency string to resolve.
        interactive: If True, prompt the user to choose; if False, pick randomly.
    
    Returns:
        A list of concrete tool names.
    """
    text_lower = text.lower()
    count = 1
    count_match = re.search(r'\((\w+)\s+of\s+your\s+choice\)', text_lower)
    if count_match:
        word_to_num = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
        count = word_to_num.get(count_match.group(1), 1)

    if "musical instrument" in text_lower:
        pool = MUSICAL_INSTRUMENTS
    elif "gaming set" in text_lower:
        pool = GAMING_SETS
    elif "artisan" in text_lower:
        pool = ARTISAN_TOOLS
    else:
        return [text]

    count = min(count, len(pool))

    if not interactive:
        return random.sample(pool, count)

    results = []
    available = list(pool)
    for i in range(count):
        if not available:
            break
        chosen = select_from_list(
            f"Choose musical instrument {i+1} of {count}" if "musical instrument" in text_lower
            else f"Choose gaming set {i+1} of {count}" if "gaming set" in text_lower
            else f"Choose artisan's tools {i+1} of {count}",
            available, display_key=None
        )
        results.append(chosen)
        available.remove(chosen)
    return results

def resolve_equipment_choice(item_name, interactive=True, weapon_data=None, weapon_categories=None):
    """Resolve a generic equipment choice to a concrete item name.
    
    Handles patterns like:
      "Artisan's Tools"                       -> pick 1 artisan tool
      "Any Musical Instrument"                 -> pick 1 instrument
      "Any Simple Weapon"                      -> pick 1 simple weapon
      "Any Simple Melee Weapon"                -> pick 1 simple melee weapon
      "Any Martial Melee Weapon"               -> pick 1 martial melee weapon
      "Any Two Simple Weapons"                 -> pick 2 simple weapons
      "Two Simple Melee Weapons"               -> pick 2 simple melee weapons
      "Two Martial Weapons"                    -> pick 2 martial weapons
    
    Returns a list of resolved item names (may have multiple for count>1 patterns).
    """
    item_lower = item_name.lower()
    
    if weapon_categories:
        pool = None
        count = 1
        if "two simple melee weapon" in item_lower:
            pool = weapon_categories["simple_melee"]
            count = 2
        elif "two martial weapon" in item_lower:
            pool = weapon_categories["martial_melee"]
            count = 2
        elif "any two simple weapon" in item_lower:
            pool = weapon_categories["simple"]
            count = 2
        elif "any simple melee weapon" in item_lower:
            pool = weapon_categories["simple_melee"]
        elif "any martial melee weapon" in item_lower:
            pool = weapon_categories["martial_melee"]
        elif "any simple weapon" in item_lower:
            pool = weapon_categories["simple"]
        elif "any martial weapon" in item_lower:
            pool = weapon_categories["martial"]
        
        if pool is not None:
            results = []
            available = list(pool)
            for _ in range(min(count, len(available))):
                if not interactive:
                    chosen = random.choice(available)
                else:
                    chosen = select_from_list(f"Choose a weapon ({len(results)+1}/{count})", available, display_key=None)
                results.append(chosen)
                available.remove(chosen)
            return results
    
    if "artisan" in item_lower and "tools" in item_lower:
        pool = ARTISAN_TOOLS
    elif "musical instrument" in item_lower:
        pool = MUSICAL_INSTRUMENTS
    else:
        return [item_name]

    if not interactive:
        return [random.choice(pool)]

    return [select_from_list("Choose a specific tool/instrument", pool, display_key=None)]


def select_from_list(prompt: str, options: list, display_key='name'):
    if not options:
        return None

    if display_key is None:
        display_fn = None
    else:
        display_fn = lambda opt: getattr(opt, display_key) if hasattr(opt, display_key) else str(opt)

    return tui.select_single(prompt, options, display_fn=display_fn)


def select_multiple(prompt: str, options: list, count: int = 1, default_selected: list = None):
    return tui.select_multiple(prompt, options, min_choices=count, max_choices=count, default_checked=default_selected)

def calculate_modifier(stat_value: int) -> int:
    return math.floor((stat_value - 10) / 2)

def classify_item(item_name: str, weapon_names: set = None) -> str:
    if item_name in ARMOR_DATA:
        return "armor"
    if item_name in SHIELD_NAMES:
        return "shield"
    if weapon_names and item_name in weapon_names:
        return "weapon"
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
        if not (weapon_names and item_name in weapon_names):
            return "consumable"
    word_num_pattern = r'^(One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve|Fifteen|Twenty|Fifty)\s'
    if re.match(word_num_pattern, item_name, re.IGNORECASE):
        if not (weapon_names and item_name in weapon_names):
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

def split_compound_items(item_name: str, weapon_data: dict = None) -> List[Item]:
    word_to_num = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    weapon_names = set(weapon_data.keys()) if weapon_data else set()
    parts = [p.strip() for p in item_name.split(",")]
    items = []
    for part in parts:
        base_name = part
        quantity = 1
        num_match = re.match(r'^(\d+)\s+(.*)', part)
        word_match = re.match(r'^(One|Two|Three|Four|Five)\s+(.*)', part, re.IGNORECASE)
        is_quantity_item = False
        if num_match:
            base_name = num_match.group(2).strip()
            quantity = int(num_match.group(1))
            is_quantity_item = True
        elif word_match:
            num_word = word_match.group(1).lower()
            if num_word in word_to_num:
                base_name = word_match.group(2).strip()
                quantity = word_to_num[num_word]
                is_quantity_item = True
        if weapon_data and base_name in weapon_data and is_quantity_item:
            for _ in range(quantity):
                items.append(make_weapon_item(base_name, weapon_data))
        elif weapon_data and base_name in weapon_data:
            items.append(make_weapon_item(base_name, weapon_data))
        else:
            item_type = classify_item(part, weapon_names)
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

def create_debug_character(config: Config) -> PlayerCharacter:
    print("--- Character Creation (DEBUG MODE) ---")
    debug_stats = Stats(strength=16, dexterity=14, constitution=15, intelligence=10, wisdom=12, charisma=8)
    debug_equipment = Equipment()
    debug_equipment.inventory.append(Item(name="Chain Mail", item_type="armor"))
    debug_equipment.inventory.append(Item(name="Shield", item_type="shield"))
    con_mod = calculate_modifier(15)
    return PlayerCharacter(
        name="Debug Adventurer",
        race="Hill Dwarf",
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
    import yaml, os as _os
    from rich.console import Console
    console = Console()

    console.print("[bold #e94560]═══ D&D 5th Edition Character Forge ═══[/]")

    weapon_data = build_weapon_data(config.weapons)
    weapon_categories = build_weapon_categories(weapon_data)
    weapon_names = set(weapon_data.keys())

    with open(_os.path.join(_os.path.dirname(__file__), '..', 'config', 'spells.yml'), 'r') as f:
        all_spells = yaml.safe_load(f)
    spell_names = {s['name'] for s in all_spells}

    name = tui.input_dialog_val("Enter your character's name:", default="Adventurer") or "Adventurer"

    while True:
        gender = tui.input_dialog_val("Enter your character's gender:", default="Unknown", max_length=15)
        if gender and len(gender) <= 15:
            break
        if not gender:
            console.print("[red]Input cannot be empty.[/]")
        else:
            console.print(f"[red]Must be no more than 15 characters (current: {len(gender)}).[/]")

    chosen_race = select_from_list("Choose your Race", config.races)

    chosen_subrace = None
    if chosen_race.subraces:
        chosen_subrace = select_from_list("Choose your Subrace", chosen_race.subraces)

    chosen_class = select_from_list("Choose your Class", config.classes)
    chosen_background = select_from_list("Choose your Background", config.backgrounds)

    console.print("\n[bold #e94560]--- Distribute Your Stat Points (Point-Buy System) ---[/]")
    base_stats = {"strength": 8, "dexterity": 8, "constitution": 8, "intelligence": 8, "wisdom": 8, "charisma": 8}
    points_spent = 0
    point_costs = {9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}

    for stat in base_stats:
        while True:
            points_remaining = 27 - points_spent
            current_default = str(base_stats[stat])
            result = tui.input_number(
                f"Set {stat.upper()} (8-15)\n(Points remaining: {points_remaining})",
                min_val=8, max_val=15, default=current_default
            )
            if result is None:
                result = base_stats[stat]

            current_stat_cost = point_costs.get(base_stats[stat], 0)
            new_stat_cost = point_costs.get(result, 0)
            potential_points_spent = points_spent - current_stat_cost + new_stat_cost
            if potential_points_spent <= 27:
                base_stats[stat] = result
                points_spent = potential_points_spent
                break
            else:
                console.print(f"[red]Not enough points! Setting {stat.upper()} to {result} costs {new_stat_cost} points, but you only have {points_remaining} left (after getting back {current_stat_cost} from your previous {base_stats[stat]}).[/]")

    final_stats = base_stats.copy()
    for increase in chosen_race.ability_score_increases:
        final_stats[increase.ability.lower()] += increase.value
    if chosen_subrace:
        for increase in chosen_subrace.ability_score_increases:
            final_stats[increase.ability.lower()] += increase.value
    player_stats = Stats(**final_stats)

    chosen_alignment = select_from_list("Choose your Alignment", config.alignments)

    draconic_ancestry = None
    if chosen_race.name == "Dragonborn":
        dragon_types = list(DRACONIC_ANCESTRY.keys())
        ancestry_labels = [
            f"{dt} ({DRACONIC_ANCESTRY[dt]['damage_type']}, {DRACONIC_ANCESTRY[dt]['breath_shape']}, {DRACONIC_ANCESTRY[dt]['save']} save)"
            for dt in dragon_types
        ]
        ancestry_display = dict(zip(dragon_types, ancestry_labels))
        draconic_ancestry = tui.select_single(
            "Choose Your Draconic Ancestry", dragon_types,
            display_fn=lambda x: ancestry_display.get(x, x)
        )

    armor_proficiencies = set(chosen_class.armor_proficiencies)
    weapon_proficiencies = set(chosen_class.weapon_proficiencies)
    tool_proficiencies = set()
    for cls_tool in chosen_class.tool_proficiencies:
        for resolved in resolve_tool_proficiency(cls_tool, interactive=True):
            tool_proficiencies.add(resolved)
    saving_throw_proficiencies = set(chosen_class.saving_throw_proficiencies)
    skill_proficiencies = set(chosen_background.skill_proficiencies)

    for proficiency in chosen_race.proficiencies:
        if proficiency['type'] == "armor":
            armor_proficiencies.add(proficiency['name'])
        elif proficiency['type'] == "weapon":
            weapon_proficiencies.add(proficiency['name'])
        elif proficiency['type'] == "tool":
            for resolved in resolve_tool_proficiency(proficiency['name'], interactive=True):
                tool_proficiencies.add(resolved)
        elif proficiency['type'] == "skill":
            skill_proficiencies.add(proficiency['name'])

    if chosen_subrace:
        for proficiency in chosen_subrace.proficiencies:
            if proficiency['type'] == "armor":
                armor_proficiencies.add(proficiency['name'])
            elif proficiency['type'] == "weapon":
                weapon_proficiencies.add(proficiency['name'])
            elif proficiency['type'] == "tool":
                for resolved in resolve_tool_proficiency(proficiency['name'], interactive=True):
                    tool_proficiencies.add(resolved)
            elif proficiency['type'] == "skill":
                skill_proficiencies.add(proficiency['name'])

    for bg_tool in chosen_background.tool_proficiencies:
        for resolved in resolve_tool_proficiency(bg_tool, interactive=True):
            tool_proficiencies.add(resolved)

    if chosen_class.tool_proficiency_choices:
        console.print(f"\n[bold #e94560]--- Tool Proficiency Choice ---[/]")
        console.print(f"[dim]As a {chosen_class.name}, choose one from: {', '.join(chosen_class.tool_proficiency_choices.choose_one_from)}[/]")
        chosen_tool = select_from_list("Choose your tool proficiency",
                                        chosen_class.tool_proficiency_choices.choose_one_from, display_key=None)
        for resolved in resolve_tool_proficiency(chosen_tool, interactive=True):
            tool_proficiencies.add(resolved)

    console.print(f"\n[#c0caf5]Your background gives you proficiency in: [bold]{', '.join(skill_proficiencies)}[/][/]")

    class_skill_choices_data = chosen_class.skills
    num_skill_choices = class_skill_choices_data.number
    skill_choices_list = class_skill_choices_data.choices

    if len(skill_choices_list) == 1 and skill_choices_list[0] == "*":
        available_choices = [s for s in ALL_SKILLS.keys() if s not in skill_proficiencies]
    else:
        available_choices = [s for s in skill_choices_list if s not in skill_proficiencies]

    chosen_skills = select_multiple(
        f"As a {chosen_class.name}, choose {num_skill_choices} skill proficiencies "
        f"({', '.join(skill_proficiencies)} already from background)",
        available_choices, count=num_skill_choices
    )
    for s in chosen_skills:
        skill_proficiencies.add(s)

    final_skills = [Skill(name=s, ability=ALL_SKILLS[s], proficient=True) for s in skill_proficiencies]
    final_skills.extend([Skill(name=s, ability=ALL_SKILLS[s], proficient=False) for s in ALL_SKILLS if s not in skill_proficiencies])

    final_saves = [
        Skill(name=s, ability=ALL_SAVES[s], proficient=(s in saving_throw_proficiencies))
        for s in ALL_SAVES
    ]

    fighting_style = None
    if chosen_class.fighting_styles:
        fs = tui.select_single(
            "Choose a Fighting Style",
            chosen_class.fighting_styles,
            display_fn=lambda f: f"{f.name}: {f.description}"
        )
        fighting_style = fs
        console.print(f"  [green]Selected: {fighting_style.name}[/]")

    player_equipment = Equipment()
    player_gold = 0
    player_consumables = {}

    console.print("\n[bold #e94560]--- Starting Equipment ---[/]")
    eq_choice = tui.select_single(
        "Starting equipment or gold?", ["equipment", "gold"],
        title="Equipment Choice"
    )
    equipment_choice_type = str(eq_choice) if eq_choice else "equipment"

    def add_items_to_inventory(item_names_or_name, source_desc=""):
        if isinstance(item_names_or_name, str):
            item_names_or_name = [item_names_or_name]
        for resolved_name in item_names_or_name:
            for item in split_compound_items(resolved_name, weapon_data):
                if item.item_type in ("ammunition", "consumable"):
                    cons_name, qty = parse_consumable_quantity(item.name)
                    player_consumables[cons_name] = player_consumables.get(cons_name, 0) + qty
                    console.print(f"  [green]+[/] {item.name} → consumables.{cons_name}: {qty}")
                else:
                    player_equipment.inventory.append(item)
                    desc = f" ({item.description})" if item.description else f" ({item.item_type})"
                    console.print(f"  [green]+[/] {item.name}{desc}")

    if equipment_choice_type.lower() == "equipment":
        for option_group in chosen_class.starting_equipment_options:
            if option_group.choose_one_from:
                chosen_item_name = select_from_list("Choose one item", option_group.choose_one_from, display_key=None)
                if chosen_item_name:
                    resolved = resolve_equipment_choice(chosen_item_name, interactive=True, weapon_data=weapon_data, weapon_categories=weapon_categories)
                    add_items_to_inventory(resolved)
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    add_items_to_inventory(item_name)
            if option_group.gold_pieces:
                player_gold += option_group.gold_pieces
                console.print(f"  [green]+[/] {option_group.gold_pieces} gold pieces.")

        for option_group in chosen_background.starting_equipment_options:
            if option_group.choose_one_from:
                chosen_item_name = select_from_list("Choose one item (background)", option_group.choose_one_from, display_key=None)
                if chosen_item_name:
                    resolved = resolve_equipment_choice(chosen_item_name, interactive=True, weapon_data=weapon_data, weapon_categories=weapon_categories)
                    add_items_to_inventory(resolved)
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    add_items_to_inventory(item_name)
            if option_group.gold_pieces:
                player_gold += option_group.gold_pieces
                console.print(f"  [green]+[/] {option_group.gold_pieces} gold pieces.")
    else:
        if chosen_class.starting_gold_dice:
            console.print(f"\n[bold #e94560]--- Rolling Starting Gold ({chosen_class.starting_gold_dice} x 10) ---[/]")
            player_gold = roll_starting_gold(chosen_class.starting_gold_dice)
        else:
            player_gold = roll_starting_gold("4d4")
            console.print(f"  Default starting gold: [yellow]{player_gold} gp[/]")

        console.print("\n[dim]--- Fixed Items (always granted) ---[/]")
        for option_group in chosen_class.starting_equipment_options:
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    add_items_to_inventory(item_name)
        for option_group in chosen_background.starting_equipment_options:
            if option_group.fixed_items:
                for item_name in option_group.fixed_items:
                    add_items_to_inventory(item_name)
            if option_group.gold_pieces:
                player_gold += option_group.gold_pieces
                console.print(f"  [green]+[/] {option_group.gold_pieces} gold pieces.")

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

    expertise_skills = []
    if chosen_class.name == "Rogue":
        proficient_skill_names = [s for s in skill_proficiencies]
        expertise_skills = select_multiple(
            "Choose 2 skills for Expertise (double proficiency bonus)",
            proficient_skill_names, count=2
        )
        console.print(f"  [green]Expertise chosen: {', '.join(expertise_skills)}[/]")

    # --- Languages ---
    languages = list(chosen_race.languages)
    if chosen_subrace:
        for lang in chosen_subrace.languages:
            if lang and lang not in languages:
                languages.append(lang)
    if chosen_class.name == "Druid" and "Druidic" not in languages:
        languages.append("Druidic")

    choice_langs = [lang for lang in languages if "choice" in lang.lower()]
    language_choice_count = len(choice_langs)
    languages = [lang for lang in languages if "choice" not in lang.lower()]

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
        available_langs = [l for l in STANDARD_LANGUAGES if l not in languages]
        new_langs = select_multiple(
            f"Choose {language_choice_count} additional language(s)",
            available_langs, count=language_choice_count
        )
        languages.extend(new_langs)

    languages = [l for l in languages if l and "choice" not in l.lower()]

    proficiency_bonus = 2

    # --- Interactive Spellcasting ---
    spellcasting_ability = None
    spell_save_dc = None
    spell_attack_modifier = None
    cantrips_known = []
    spells_known = []
    spells_prepared = []
    spellbook = []
    spell_slots = {}

    caster_types = set(list(KNOWN_SPELL_CLASSES) + list(PREPARED_SPELL_CLASSES) + ["Wizard"])
    if chosen_class.name in caster_types:
        cls = chosen_class.name

        has_spellcasting = True
        if cls == "Paladin":
            spellcasting_ability = "charisma"
            has_spellcasting = False
        elif cls == "Ranger":
            spellcasting_ability = "wisdom"
            has_spellcasting = False

        if has_spellcasting:
            if cls == "Wizard":
                spellcasting_ability = "intelligence"
            elif cls == "Cleric":
                spellcasting_ability = "wisdom"
            elif cls == "Sorcerer":
                spellcasting_ability = "charisma"
            elif cls == "Warlock":
                spellcasting_ability = "charisma"
            elif cls == "Bard":
                spellcasting_ability = "charisma"
            elif cls == "Druid":
                spellcasting_ability = "wisdom"

            available_cantrips = get_available_cantrips(cls, spell_names)
            cantrip_count = CANTRIP_COUNTS.get(cls, 0)
            if cantrip_count > 0 and available_cantrips:
                cantrips_known = select_multiple(
                    f"Choose {cantrip_count} cantrip(s) for your {cls}",
                    available_cantrips, count=cantrip_count
                )

            available_l1 = get_available_level1_spells(cls, spell_names)

            if cls in KNOWN_SPELL_CLASSES:
                known_count = KNOWN_SPELL_COUNTS.get(cls, 0)
                if known_count > 0 and available_l1:
                    spells_known = select_multiple(
                        f"Choose {known_count} level 1 spell(s) known for your {cls}",
                        available_l1, count=known_count
                    )
            elif cls in PREPARED_SPELL_CLASSES or cls == "Wizard":
                ability_mod = calculate_modifier(player_stats.dict().get(spellcasting_ability, 10))
                base_count = PREPARED_SPELL_COUNTS_BASE.get(cls, 0)
                prepare_count = ability_mod + base_count
                prepare_count = max(prepare_count, 1)

                if cls == "Wizard":
                    spellbook = select_multiple(
                        f"Choose 6 spells for your spellbook",
                        available_l1, count=6
                    )
                    spells_prepared = select_multiple(
                        f"Prepare {prepare_count} spell(s) from your spellbook (INT mod + level = {prepare_count})",
                        spellbook, count=prepare_count
                    )
                else:
                    spells_prepared = select_multiple(
                        f"Prepare {prepare_count} spell(s) for your {cls} "
                        f"({spellcasting_ability.upper()} mod + level = {prepare_count})",
                        available_l1, count=prepare_count
                    )

            if cls == "Warlock":
                spell_slots = {str(k): v for k, v in WARLOCK_SPELL_SLOTS[1].items()}
            elif cls in ("Bard", "Sorcerer", "Wizard", "Cleric", "Druid"):
                spell_slots = {str(k): v for k, v in FULL_CASTER_SPELL_SLOTS[1].items()}

    if spellcasting_ability:
        spell_save_dc = 8 + calculate_modifier(player_stats.dict()[spellcasting_ability]) + proficiency_bonus
        spell_attack_modifier = calculate_modifier(player_stats.dict()[spellcasting_ability]) + proficiency_bonus

    fs_name = fighting_style.name if fighting_style else None
    if fs_name == "Defense":
        armor_class = calculate_ac(player_stats, chosen_class.name, player_equipment.inventory, fighting_style="Defense")
    else:
        armor_class = calculate_ac(player_stats, chosen_class.name, player_equipment.inventory)

    con_mod = calculate_modifier(player_stats.constitution)
    hit_points = chosen_class.hit_die + con_mod
    if chosen_race.name == "Dwarf" and chosen_subrace and chosen_subrace.name == "Hill Dwarf":
        hit_points += 1
    total_hit_points = hit_points

    speed = chosen_race.speed
    if chosen_subrace and chosen_subrace.name == "Wood Elf":
        speed = 35

    console.print("\n[bold #e94560]═══ Character Complete! ═══[/]")
    return PlayerCharacter(
        name=name,
        race=chosen_subrace.name if chosen_subrace else chosen_race.name,
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
        spells_prepared=spells_prepared,
        spellbook=spellbook,
        spell_slots=spell_slots
    )