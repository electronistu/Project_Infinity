"""Immutable engine tables for dice_server: rules data, never mutated at runtime.

Every table the engine treats as a constant lives here and nowhere else; anything the
engine mutates is in `rules.state`.
"""


# A companion travels with the Traveller, so the store is one party -- at most this many, in
# every age. Current HP is never stored: a fight states it (default: full).
COMPANION_LIMIT = 3


XP_THRESHOLDS = [
    (2, 300), (3, 900), (4, 2700), (5, 6500),
    (6, 14000), (7, 23000), (8, 33000), (9, 48000),
    (10, 64000), (11, 85000), (12, 100000), (13, 120000),
    (14, 145000), (15, 175000), (16, 210000), (17, 255000),
    (18, 305000), (19, 360000), (20, 400000),
]


KNOWN_CASTER_CLASSES = {"Bard", "Sorcerer", "Warlock", "Ranger"}


PREPARED_CASTER_CLASSES = {"Cleric", "Druid", "Paladin"}


SRD_CLASSES = {"Barbarian", "Bard", "Cleric", "Druid", "Fighter", "Monk", "Paladin",
               "Ranger", "Rogue", "Sorcerer", "Warlock", "Wizard"}


CR_XP_TABLE = {
    0: 10, 0.125: 25, 0.25: 50, 0.5: 100,
    1: 200, 2: 450, 3: 700, 4: 1100,
    5: 1800, 6: 2300, 7: 2900, 8: 3900,
    9: 5000, 10: 5900, 11: 7200, 12: 8400,
    13: 10000, 14: 11500, 15: 13000, 16: 15000,
    17: 18000, 18: 20000, 19: 22000, 20: 25000,
    21: 33000, 22: 41000, 23: 50000, 24: 62000,
    25: 75000, 26: 90000, 27: 105000, 28: 120000,
    29: 135000, 30: 155000,
}


WORN_KINDS = {"cloak", "boots", "gloves", "gauntlets", "bracers", "headwear", "ring", "amulet",
              "belt"}


# SRD 5.1 "Multiple Items of the Same Kind": a character can't normally wear more than one pair
# of footwear, one pair of gloves or gauntlets, one pair of bracers, one item of headwear, or one
# cloak. Rings, amulets, necklaces and belts are NOT on that list (only the attunement rule "one
# copy of an item" applies to them).
WORN_EXCLUSIVE = {
    "boots": "footwear",
    "gloves": "hands",
    "gauntlets": "hands",
    "bracers": "arms",
    "cloak": "shoulders",
    "headwear": "head",
}


EXHAUSTION_MAX = 6


PREPARED_CASTER_ABILITIES = {
    "Wizard": "int",
    "Cleric": "wis",
    "Druid": "wis",
    "Paladin": "cha",
}


_DEFAULT_REPUTATION_FACTION = "others"


_LEGACY_REPUTATION_FACTIONS = ("misc",)


ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")


CONDITION_EFFECTS = {
    "blinded": {"self_attack_disadvantage": True, "attackers_advantage": True},
    "frightened": {"self_attack_disadvantage": True, "self_check_disadvantage": True},
    "invisible": {"self_attack_advantage": True, "attackers_disadvantage": True},
    "paralyzed": {"attackers_advantage": True, "auto_fail_str_dex_saves": True, "melee_crit": True},
    "petrified": {"attackers_advantage": True, "auto_fail_str_dex_saves": True,
                  "melee_crit": True, "resist_all": True},
    "stunned": {"attackers_advantage": True, "auto_fail_str_dex_saves": True},
    "unconscious": {"attackers_advantage": True, "auto_fail_str_dex_saves": True,
                    "melee_crit": True},
    "prone": {"self_attack_disadvantage": True, "attackers_advantage_melee": True,
              "attackers_disadvantage_ranged": True},
    "restrained": {"self_attack_disadvantage": True, "self_dex_save_disadvantage": True,
                   "attackers_advantage": True},
    "poisoned": {"self_attack_disadvantage": True, "self_check_disadvantage": True},
    "grappled": {},
    "incapacitated": {},
    "exhaustion": {},  # tracked as the numeric `exhaustion` level; see _exhaustion_effects
}


_INCAPACITATING_CONDITIONS = {"incapacitated", "paralyzed", "petrified", "stunned",
                            "unconscious"}


_LASTING_CONDITIONS = {"petrified", "poisoned"}


# The durable fields of a companion sheet. Everything else a combatant entry carries
# (current HP, conditions, exhaustion, initiative) is fight state and is never stored.
_COMPANION_KEEP = (
    "role", "ac", "max_hp", "speed", "saves", "attacks", "multiattack", "spellcasting",
    "damage_resistances", "damage_immunities", "damage_vulnerabilities",
    "condition_immunities", "initiative_modifier", "initiative_advantage",
)


# A companion is a full character, so its ability scores are required: they are what lets the
# engine roll its own checks and saves instead of trusting a number the GM computed.
COMPANION_ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")


COMPANION_DEFAULT_PROF = 2   # the floor for any creature; give the real bonus when it is stronger
