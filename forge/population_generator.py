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

def get_cr_and_xp(level: int) -> (float, int):
    """Determines Challenge Rating and XP value based on level."""
    if level <= 1: return (0.125, 25)
    if level <= 3: return (0.25, 50)
    if level <= 5: return (0.5, 100)
    if level <= 8: return (1, 200)
    if level <= 11: return (2, 450)
    if level <= 14: return (3, 700)
    return (5, 1800) # For levels 15+

def find_valid_placement(map_grid, area_size=(5, 5)):
    """Finds a random valid top-left coordinate for a location on land."""
    height = len(map_grid)
    width = len(map_grid[0])
    possible_placements = []
    # Give a buffer from the edge of the map
    for r in range(5, height - area_size[1] - 5):
        for c in range(5, width - area_size[0] - 5):
            is_valid = all(
                map_grid[r + i][c + j] == '.' 
                for i in range(area_size[1]) 
                for j in range(area_size[0])
            )
            if is_valid:
                possible_placements.append((c, r))
    
    return random.choice(possible_placements) if possible_placements else None

def _generate_npc_details(level: int, role: str, faction: str, is_walker: bool, config):
    """Generates detailed NPC attributes based on level, role, and config."""
    # Randomly choose race, class, background
    chosen_race = random.choice(config.races)
    chosen_class = random.choice(config.classes)
    chosen_background = random.choice(config.backgrounds)

    # Generate stats (simplified point buy for NPCs)
    stats_values = [15, 14, 13, 12, 10, 8]
    random.shuffle(stats_values)
    base_stats = Stats(
        strength=stats_values[0], dexterity=stats_values[1], constitution=stats_values[2],
        intelligence=stats_values[3], wisdom=stats_values[4], charisma=stats_values[5]
    )

    # Apply Racial Bonuses
    final_stats = base_stats.copy()
    for increase in chosen_race.ability_score_increases:
        final_stats.dict()[increase.ability.lower()] += increase.value
    npc_stats = Stats(**final_stats.dict())

    # Calculate proficiencies
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

    # Add class skill proficiencies (randomly choose for NPC)
    class_skill_choices_data = chosen_class.skills
    
    # Handle generic skill choices like "Any three skills"
    actual_class_skill_choices = []
    for choice in class_skill_choices_data.choices:
        if "Any" in choice and "skills" in choice:
            # If it's a generic choice, pick from ALL_SKILLS
            num_to_choose = WORD_TO_NUMBER.get(choice.split(" ")[1].lower(), 0) # e.g., "Any three skills" -> 3
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

    # Features and Traits
    features_and_traits = [SpecialAbility(name=t.name, description=t.description) for t in chosen_race.traits]
    class_features = [SpecialAbility(name=f.name, description=f.description) for f in chosen_class.features if f.level <= level]
    features_and_traits.extend(class_features)

    # Equipment (simplified for NPCs)
    npc_equipment = Equipment()
    # Assign a basic weapon based on class proficiency
    if chosen_class.weapon_proficiencies:
        # Create a generic weapon item. GameMaster AI will interpret.
        npc_equipment.main_hand = Item(name="Generic Weapon", item_type="weapon")
    
    # Spellcasting (simplified for NPCs)
    spellcasting_ability = None
    spell_save_dc = None
    spell_attack_modifier = None
    cantrips_known = []
    spells_known = []
    spell_slots = {}

    # This is a very basic implementation. A full implementation would use the spell slot tables.
    if chosen_class.name in ["Wizard", "Sorcerer", "Bard", "Cleric", "Druid", "Artificer"]:
        if chosen_class.name == "Wizard": spellcasting_ability = "intelligence"
        elif chosen_class.name == "Cleric": spellcasting_ability = "wisdom"
        elif chosen_class.name == "Sorcerer": spellcasting_ability = "charisma"
        elif chosen_class.name == "Bard": spellcasting_ability = "charisma"
        elif chosen_class.name == "Druid": spellcasting_ability = "wisdom"
        elif chosen_class.name == "Artificer": spellcasting_ability = "intelligence"
        
        # For simplicity, give them a few cantrips and 1st level spells
        cantrips_known = ["Light", "Mage Hand"]
        spells_known = ["Magic Missile", "Cure Wounds"]
        spell_slots = {"1": 2}

    if spellcasting_ability:
        # Proficiency bonus for NPC is simplified to 2 for now, should scale with level
        proficiency_bonus = 2 
        spell_save_dc = 8 + calculate_modifier(npc_stats.dict()[spellcasting_ability]) + proficiency_bonus
        spell_attack_modifier = calculate_modifier(npc_stats.dict()[spellcasting_ability]) + proficiency_bonus

    # Calculate Hit Points
    con_modifier = calculate_modifier(npc_stats.constitution)
    npc_hit_points = chosen_class.hit_die + (level - 1) * (chosen_class.hit_die // 2 + con_modifier)
    if npc_hit_points < 1: npc_hit_points = 1 # Ensure HP is at least 1

    # Calculate Armor Class (simplified)
    # Base AC 10 + Dex modifier for unarmored
    npc_armor_class = 10 + calculate_modifier(npc_stats.dexterity)
    # If proficient in light/medium armor, assume they have some basic armor
    if "Light armor" in chosen_class.armor_proficiencies or "Medium armor" in chosen_class.armor_proficiencies:
        npc_armor_class = 12 + calculate_modifier(npc_stats.dexterity) # Assume basic light/medium armor
    elif "Heavy armor" in chosen_class.armor_proficiencies:
        npc_armor_class = 16 # Assume basic heavy armor

    # Set Speed
    npc_speed = chosen_race.speed

    cr, xp = get_cr_and_xp(level)

    # Generate a more specific name
    npc_name = f"{chosen_race.name} {chosen_class.name}"
    return NPC(
        name=npc_name,
        level=level,
        stats=npc_stats,
        armor_class=npc_armor_class,
        hit_points=npc_hit_points,
        speed=npc_speed,
        alignment=chosen_background.name, # Using background name as alignment for now, will fix later
        challenge_rating=cr,
        xp_value=xp,
        creature_type='humanoid',
        role=role,
        faction=faction,
        is_walker=is_walker,
        dialogue_options=[], # Dialogue will be set by specific NPC creation functions
        race=chosen_race.name,
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
    """Creates a very basic NPC for a settlement, for the AI GameMaster to imagine."""
    is_walker = random.random() < 0.02 # 2% chance
    dialogue = [f"Just a humble {role}, trying to make a living.", "Welcome to our town."]
    if is_walker:
        dialogue = ["Have you ever noticed the seams in the sky?", "They say this world was 'generated'. What do you think that means?", "The bread is a lie."]

    # Create a very basic NPC for the AI GameMaster to imagine
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
    """Creates a courtier NPC for a kingdom's capital."""
    npc_level = random.randint(3, 7)
    courtier = _generate_npc_details(
        level=npc_level,
        role="Courtier",
        faction=kingdom_name,
        is_walker=random.random() < 0.042,
        config=config
    )
    courtier.name = f"{courtier.race} Courtier" # Overwrite with a more specific courtier name
    courtier.dialogue_options=["Long live the King!", "The court is abuzz with rumors."]
    return courtier

# --- Main Population Generator ---

def populate_world(config, map_grid):
    """Populates the world with kingdoms, capitals, and NPCs with hardcoded relations."""
    kingdoms = []

    kingdom_defs = {
        "Eldoria": {"alignment": "Lawful Good"},
        "Zarthus": {"alignment": "Lawful Evil"},
        "Silverwood": {"alignment": "True Neutral"},
        "Blacksail Archipelago": {"alignment": "Chaotic Evil"}
    }

    # Hardcoded relationship matrix reflecting a tense, post-war world
    RELATIONS = {
        "Eldoria": {"Zarthus": "Rivalry", "Silverwood": "Suspicion", "Blacksail Archipelago": "Raiding"},
        "Zarthus": {"Eldoria": "Rivalry", "Silverwood": "Contempt", "Blacksail Archipelago": "Alliance"},
        "Silverwood": {"Eldoria": "Suspicion", "Zarthus": "Contempt", "Blacksail Archipelago": "Raiding"},
        "Blacksail Archipelago": {"Eldoria": "Raiding", "Zarthus": "Alliance", "Silverwood": "Raiding"}
    }

    for name, data in kingdom_defs.items():
        # Create the Ruler
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

        # Create the Capital City
        capital_coords = find_valid_placement(map_grid, (8, 8))
        if not capital_coords: continue
        
        capital_city = Location(
            name=f"{name} City", coordinates=capital_coords, biome="Plains",
            description=f"The bustling capital of {name}.", npcs=[ruler]
        )

        # Add courtiers to the capital
        num_courtiers = random.randint(2, 3)
        for _ in range(num_courtiers):
            courtier = create_courtier_npc(name, config)
            capital_city.npcs.append(courtier)

        # Create the Kingdom
        kingdom = Kingdom(
            name=name, capital=capital_city.name, alignment=data["alignment"], 
            ruler=ruler, locations=[capital_city],
            relations=RELATIONS[name]
        )
        kingdoms.append(kingdom)

    return kingdoms
