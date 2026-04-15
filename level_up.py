# level_up.py
# D&D 5e Level-Up Rules and Spell Slot Tables - Single Source of Truth

import json
import math
import random

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

WARLOCK_SPELL_SLOTS = {
    1:  {1: 1},
    2:  {1: 2},
    3:  {2: 2},
    4:  {2: 2},
    5:  {3: 2},
    6:  {3: 2},
    7:  {4: 2},
    8:  {4: 2},
    9:  {5: 2},
    10: {5: 2},
    11: {5: 3},
    12: {5: 3},
    13: {5: 3},
    14: {5: 3},
    15: {5: 3},
    16: {5: 3},
    17: {5: 4},
    18: {5: 4},
    19: {5: 4},
    20: {5: 4},
}

CASTER_TYPE_MAP = {
    "Wizard": "full",
    "Cleric": "full",
    "Sorcerer": "full",
    "Bard": "full",
    "Druid": "full",
    "Paladin": "half",
    "Ranger": "half",
    "Artificer": "artificer",
    "Warlock": "warlock",
}

FIRST_SPELLCASTING_ABILITIES = {
    "Paladin": "charisma",
    "Ranger": "wisdom",
}

ABILITY_TO_STAT = {
    "intelligence": "int",
    "wisdom": "wis",
    "charisma": "cha",
}

PROFICIENCY_BONUS_TABLE = {
    1: 2, 2: 2, 3: 2, 4: 2,
    5: 3, 6: 3, 7: 3, 8: 3,
    9: 4, 10: 4, 11: 4, 12: 4,
    13: 5, 14: 5, 15: 5, 16: 5,
    17: 6, 18: 6, 19: 6, 20: 6,
}

CLASS_HIT_DICE = {
    "Artificer": 8,
    "Barbarian": 12,
    "Bard": 8,
    "Cleric": 8,
    "Druid": 8,
    "Fighter": 10,
    "Monk": 8,
    "Paladin": 10,
    "Ranger": 10,
    "Rogue": 8,
    "Sorcerer": 6,
    "Warlock": 8,
    "Wizard": 6,
}

SLOT_TABLES = {
    "full": FULL_CASTER_SPELL_SLOTS,
    "half": HALF_CASTER_SPELL_SLOTS,
    "artificer": ARTIFICER_SPELL_SLOTS,
    "warlock": WARLOCK_SPELL_SLOTS,
}


def compute_spell_slots(character_class, new_level, current_spellcasting=None):
    """
    Compute the updated spellcasting dict on level-up.

    All caster types use full replace for slots.
    For Paladin/Ranger gaining spellcasting for the first time,
    current_spellcasting should be None — a full object is created
    with ability set and dc/attack_modifier left for apply_level_up
    to compute correctly.

    Args:
        character_class: str, e.g. "Wizard"
        new_level: int, the new character level
        current_spellcasting: dict or None

    Returns:
        dict: the complete spellcasting object to write back, or
        None if the class is not a caster
    """
    caster_type = CASTER_TYPE_MAP.get(character_class)
    if caster_type is None:
        return None

    table = SLOT_TABLES[caster_type]
    level_slots = table.get(new_level)
    if level_slots is None:
        return None

    new_slots = {str(k): v for k, v in level_slots.items()}

    if current_spellcasting is None:
        ability = FIRST_SPELLCASTING_ABILITIES.get(character_class)
        if ability:
            return {
                "ability": ability,
                "dc": 0,
                "attack_modifier": 0,
                "cantrips": [],
                "spells": [],
                "slots": new_slots,
            }
        else:
            return None

    result = dict(current_spellcasting)
    old_slots = current_spellcasting.get("slots", {})
    merged_slots = {}
    for slot_level in new_slots:
        if slot_level in old_slots:
            merged_slots[slot_level] = old_slots[slot_level]
        else:
            merged_slots[slot_level] = 0
    result["slots"] = merged_slots
    return result


def _get_stat_mod(stats_dict, stat_key):
    """Compute ability modifier from a stats dict like {'str': 8, 'dex': 17, ...}"""
    val = stats_dict.get(stat_key, 10)
    return math.floor((int(val) - 10) / 2)


def apply_level_up(character_class, old_level, new_level, player_data):
    """
    Apply all automatic D&D 5e level-up changes.

    Args:
        character_class: str, e.g. "Wizard"
        old_level: int, the current character level
        new_level: int, the new character level
        player_data: dict of {key: value_as_string} from the SQLite DB

    Returns:
        (changes, summary) where:
        - changes: dict of {db_key: new_value_as_string} to write to DB
        - summary: list of human-readable change descriptions
    """
    changes = {}
    summary = []

    new_prof = PROFICIENCY_BONUS_TABLE.get(new_level, 2)

    # 1. Level
    changes['level'] = str(new_level)
    summary.append(f"level: {old_level} -> {new_level}")

    # 2. Proficiency bonus
    old_prof = int(player_data.get('proficiency_bonus', 2))
    if new_prof != old_prof:
        changes['proficiency_bonus'] = str(new_prof)
        summary.append(f"proficiency_bonus: {old_prof} -> {new_prof}")

    # 3. Hit dice count
    old_hdc = int(player_data.get('hit_dice_count', old_level))
    if new_level != old_hdc:
        changes['hit_dice_count'] = str(new_level)
        summary.append(f"hit_dice_count: {old_hdc} -> {new_level}")

    # 4. Hit points — roll hit dice for each level gained
    hit_dice_size = int(player_data.get('hit_dice_size', CLASS_HIT_DICE.get(character_class, 8)))
    old_total_hp = int(player_data.get('total_hit_points', 0))
    levels_gained = new_level - old_level

    stats_raw = player_data.get('stats', '{}')
    if isinstance(stats_raw, str):
        stats = json.loads(stats_raw)
    elif isinstance(stats_raw, dict):
        stats = stats_raw
    else:
        stats = {}

    con_mod = _get_stat_mod(stats, 'con')

    rolls = []
    total_gain = 0
    for _ in range(levels_gained):
        roll = random.randint(1, hit_dice_size)
        hp_gain = roll + con_mod
        rolls.append(roll)
        total_gain += hp_gain

    new_total_hp = old_total_hp + total_gain
    changes['total_hit_points'] = str(new_total_hp)
    roll_str = ", ".join(str(r) for r in rolls)
    if levels_gained == 1:
        summary.append(f"total_hit_points: {old_total_hp} -> {new_total_hp} (rolled {rolls[0]} on d{hit_dice_size} + {con_mod} CON)")
    else:
        summary.append(f"total_hit_points: {old_total_hp} -> {new_total_hp} (rolled [{roll_str}] on d{hit_dice_size} each + {con_mod}/level CON)")

    # 5. Spellcasting
    sc_raw = player_data.get('spellcasting')
    if isinstance(sc_raw, str):
        current_sc = json.loads(sc_raw)
    elif isinstance(sc_raw, dict):
        current_sc = sc_raw
    else:
        current_sc = None

    new_sc = compute_spell_slots(character_class, new_level, current_sc)
    if new_sc is not None:
        # Recalculate dc and attack_modifier with new proficiency bonus
        ability = new_sc.get('ability')
        if ability:
            stat_key = ABILITY_TO_STAT.get(ability)
            if stat_key:
                ability_mod = _get_stat_mod(stats, stat_key)
                new_dc = 8 + ability_mod + new_prof
                new_atk = ability_mod + new_prof
                new_sc['dc'] = new_dc
                new_sc['attack_modifier'] = new_atk

                if current_sc is None:
                    summary.append(f"spellcasting initialized: ability={ability}, dc={new_dc}, attack_modifier={new_atk}, slots={json.dumps(new_sc['slots'])}")
                else:
                    old_dc = current_sc.get('dc', 0)
                    old_atk = current_sc.get('attack_modifier', 0)
                    summary.append(f"spellcasting.slots: {json.dumps(new_sc['slots'])}")
                    if new_dc != old_dc:
                        summary.append(f"spellcasting.dc: {old_dc} -> {new_dc}")
                    if new_atk != old_atk:
                        summary.append(f"spellcasting.attack_modifier: {old_atk} -> {new_atk}")
            else:
                summary.append(f"spellcasting.slots: {json.dumps(new_sc['slots'])}")

        changes['spellcasting'] = json.dumps(new_sc)

    return changes, summary