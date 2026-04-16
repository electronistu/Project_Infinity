from .models import Kingdom, Location, NPC, Stats
import random
from .models import CharacterClass, Background, PlayerAbility, StartingEquipmentOption, Item, Skill, SpecialAbility, Equipment, Stats
from .character_creator import calculate_modifier, ALL_SKILLS

WORD_TO_NUMBER = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10
}

PROFICIENCY_BONUS_BY_LEVEL = {
    1: 2, 2: 2, 3: 2, 4: 2,
    5: 3, 6: 3, 7: 3, 8: 3,
    9: 4, 10: 4, 11: 4, 12: 4,
    13: 5, 14: 5, 15: 5, 16: 5,
    17: 6, 18: 6, 19: 6, 20: 6,
}

def get_cr_and_xp(level: int):
    if level <= 1: return (0.125, 25)
    if level <= 3: return (0.25, 50)
    if level <= 5: return (0.5, 100)
    if level <= 8: return (1, 200)
    if level <= 11: return (2, 450)
    if level <= 14: return (3, 700)
    return (5, 1800)

def _generate_npc_details(level: int, role: str, faction: str, is_walker: bool, config):
    chosen_race = random.choice(config.races)
    chosen_class = random.choice(config.classes)
    chosen_background = random.choice(config.backgrounds)

    chosen_subrace = None
    if chosen_race.subraces:
        chosen_subrace = random.choice(chosen_race.subraces)

    stats_values = [15, 14, 13, 12, 10, 8]
    random.shuffle(stats_values)
    base_stats = Stats(
        strength=stats_values[0], dexterity=stats_values[1], constitution=stats_values[2],
        intelligence=stats_values[3], wisdom=stats_values[4], charisma=stats_values[5]
    )

    final_stats = base_stats.copy()
    for increase in chosen_race.ability_score_increases:
        final_stats.dict()[increase.ability.lower()] += increase.value
    if chosen_subrace:
        for increase in chosen_subrace.ability_score_increases:
            final_stats.dict()[increase.ability.lower()] += increase.value
    npc_stats = Stats(**final_stats.dict())

    armor_proficiencies = set(chosen_class.armor_proficiencies)
    weapon_proficiencies = set(chosen_class.weapon_proficiencies)
    tool_proficiencies = set(chosen_background.tool_proficiencies)
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

    if chosen_class.tool_proficiency_choices:
        chosen_tool = random.choice(chosen_class.tool_proficiency_choices.choose_one_from)
        tool_proficiencies.add(chosen_tool)

    class_skill_choices_data = chosen_class.skills
    
    actual_class_skill_choices = []
    for choice in class_skill_choices_data.choices:
        if choice == "*":
            possible_skills_for_class = [s for s in ALL_SKILLS.keys() if s not in skill_proficiencies]
            num_to_choose = class_skill_choices_data.number
            actual_class_skill_choices.extend(random.sample(possible_skills_for_class, min(num_to_choose, len(possible_skills_for_class))))
        elif "Any" in choice and "skills" in choice:
            num_to_choose = WORD_TO_NUMBER.get(choice.split(" ")[1].lower(), 0)
            possible_skills_for_class = [s for s in ALL_SKILLS.keys() if s not in skill_proficiencies]
            actual_class_skill_choices.extend(random.sample(possible_skills_for_class, min(num_to_choose, len(possible_skills_for_class))))
        else:
            actual_class_skill_choices.append(choice)

    available_choices = [s for s in actual_class_skill_choices if s not in skill_proficiencies]
    for _ in range(class_skill_choices_data.number):
        if available_choices:
            chosen_skill = random.choice(available_choices)
            skill_proficiencies.add(chosen_skill.name if hasattr(chosen_skill, 'name') else chosen_skill)
            available_choices.remove(chosen_skill)

    final_skills = [Skill(name=s, ability=ALL_SKILLS[s], proficient=True) for s in skill_proficiencies]
    final_skills.extend([Skill(name=s, ability=ALL_SKILLS[s], proficient=False) for s in ALL_SKILLS if s not in skill_proficiencies])

    features_and_traits = [SpecialAbility(name=t.name, description=t.description) for t in chosen_race.traits]
    if chosen_subrace:
        features_and_traits.extend([SpecialAbility(name=t.name, description=t.description) for t in chosen_subrace.traits])
    class_features = [SpecialAbility(name=f.name, description=f.description) for f in chosen_class.features if f.level <= level]
    features_and_traits.extend(class_features)

    npc_equipment = Equipment()
    if chosen_class.weapon_proficiencies:
        npc_equipment.main_hand = Item(name="Generic Weapon", item_type="weapon")

    spellcasting_ability = None
    spell_save_dc = None
    spell_attack_modifier = None
    cantrips_known = []
    spells_known = []
    spell_slots = {}

    if chosen_class.name in ["Wizard", "Sorcerer", "Bard", "Cleric", "Druid", "Artificer", "Warlock", "Paladin", "Ranger"]:
        if chosen_class.name == "Wizard": spellcasting_ability = "intelligence"
        elif chosen_class.name == "Cleric": spellcasting_ability = "wisdom"
        elif chosen_class.name == "Sorcerer": spellcasting_ability = "charisma"
        elif chosen_class.name == "Bard": spellcasting_ability = "charisma"
        elif chosen_class.name == "Druid": spellcasting_ability = "wisdom"
        elif chosen_class.name == "Artificer": spellcasting_ability = "intelligence"
        elif chosen_class.name == "Warlock": spellcasting_ability = "charisma"
        elif chosen_class.name == "Paladin": spellcasting_ability = "charisma"
        elif chosen_class.name == "Ranger": spellcasting_ability = "wisdom"

    proficiency_bonus = PROFICIENCY_BONUS_BY_LEVEL.get(level, 2)

    if spellcasting_ability:
        spell_save_dc = 8 + calculate_modifier(npc_stats.dict()[spellcasting_ability]) + proficiency_bonus
        spell_attack_modifier = calculate_modifier(npc_stats.dict()[spellcasting_ability]) + proficiency_bonus

    # HP: Level 1 = hit_die + con_mod, each additional level = hit_die//2 + 1 + con_mod (average)
    con_modifier = calculate_modifier(npc_stats.constitution)
    npc_hit_points = chosen_class.hit_die + con_modifier
    for _ in range(level - 1):
        npc_hit_points += (chosen_class.hit_die // 2 + 1) + con_modifier
    if npc_hit_points < 1:
        npc_hit_points = 1

    # Hill Dwarf bonus
    if chosen_race.name == "Dwarf" and chosen_subrace and chosen_subrace.name == "Hill Dwarf":
        npc_hit_points += level

    npc_armor_class = 10 + calculate_modifier(npc_stats.dexterity)
    if "Light armor" in chosen_class.armor_proficiencies or "Medium armor" in chosen_class.armor_proficiencies:
        npc_armor_class = 12 + min(calculate_modifier(npc_stats.dexterity), 2)
    elif "Heavy armor" in chosen_class.armor_proficiencies or "All armor" in chosen_class.armor_proficiencies:
        npc_armor_class = 16

    npc_speed = chosen_race.speed
    if chosen_subrace and chosen_subrace.name == "Wood Elf":
        npc_speed = 35

    race_display = chosen_race.name
    if chosen_subrace:
        race_display = chosen_subrace.name

    cr, xp = get_cr_and_xp(level)

    npc_name = f"{race_display} {chosen_class.name}"
    return NPC(
        name=npc_name,
        level=level,
        stats=npc_stats,
        armor_class=npc_armor_class,
        current_hit_points=npc_hit_points,
        total_hit_points=npc_hit_points,
        speed=npc_speed,
        alignment=chosen_background.name,
        challenge_rating=cr,
        xp_value=xp,
        creature_type='humanoid',
        role=role,
        faction=faction,
        is_walker=is_walker,
        dialogue_options=[],
        race=race_display,
        character_class=chosen_class.name,
        background=chosen_background.name,
        armor_proficiencies=list(armor_proficiencies),
        weapon_proficiencies=list(weapon_proficiencies),
        tool_proficiencies=list(tool_proficiencies),
        features_and_traits=features_and_traits,
        equipment=npc_equipment,
        spellcasting_ability=spellcasting_ability,
        spell_save_dc=spell_save_dc,
        spell_attack_modifier=spell_attack_modifier,
        cantrips_known=cantrips_known,
        spells_known=spells_known,
        spell_slots=spell_slots
    )

def create_settlement_npc(role, config):
    is_walker = random.random() < 0.02
    dialogue = [f"Just a humble {role}, trying to make a living.", "Welcome to our town."]
    if is_walker:
        dialogue = ["Have you ever noticed the seams in the sky?", "They say this world was 'generated'. What do you think that means?", "The bread is a lie."]

    return NPC(
        name=f"Generic {role}",
        level=1,
        stats=Stats(strength=10, dexterity=10, constitution=10, intelligence=10, wisdom=10, charisma=10),
        alignment="Neutral",
        challenge_rating=0.1,
        xp_value=10,
        creature_type='humanoid',
        role=role,
        faction="Civilian",
        is_walker=is_walker,
        dialogue_options=dialogue
    )

def create_courtier_npc(kingdom_name, config):
    npc_level = random.randint(3, 7)
    courtier = _generate_npc_details(
        level=npc_level,
        role="Courtier",
        faction=kingdom_name,
        is_walker=random.random() < 0.042,
        config=config
    )
    courtier.name = f"{courtier.race} Courtier"
    courtier.dialogue_options=["Long live the King!", "The court is abuzz with rumors."]
    return courtier

def populate_world(config):
    kingdoms = []
    
    kingdom_defs = {
        "Eldoria": {"alignment": "Lawful Good"},
        "Zarthus": {"alignment": "Lawful Evil"},
        "Silverwood": {"alignment": "True Neutral"},
        "Blacksail Archipelago": {"alignment": "Chaotic Evil"}
    }
    
    RELATIONS = {
        "Eldoria": {"Zarthus": "Rivalry", "Silverwood": "Suspicion", "Blacksail Archipelago": "Raiding"},
        "Zarthus": {"Eldoria": "Rivalry", "Silverwood": "Contempt", "Blacksail Archipelago": "Alliance"},
        "Silverwood": {"Eldoria": "Suspicion", "Zarthus": "Contempt", "Blacksail Archipelago": "Raiding"},
        "Blacksail Archipelago": {"Eldoria": "Raiding", "Zarthus": "Alliance", "Silverwood": "Raiding"}
    }
    
    for name, data in kingdom_defs.items():
        ruler_level = random.randint(10, 15)
        ruler = _generate_npc_details(
            level=ruler_level,
            role="Ruler",
            faction=name,
            is_walker=random.random() < 0.042,
            config=config
        )
        ruler.alignment = data["alignment"]
        ruler.dialogue_options = [f"I am the ruler of {name}.", "State your business."]
    
        capital_city = Location(
            name=f"{name} City", coordinates=None, biome="Plains",
            description=f"The bustling capital of {name}.", npcs=[ruler]
        )
    
        num_courtiers = random.randint(2, 3)
        for _ in range(num_courtiers):
            courtier = create_courtier_npc(name, config)
            capital_city.npcs.append(courtier)
    
        kingdom = Kingdom(
            name=name, capital=capital_city.name, alignment=data["alignment"], 
            ruler=ruler, locations=[capital_city],
            relations=RELATIONS[name]
        )
        kingdoms.append(kingdom)
    
    return kingdoms