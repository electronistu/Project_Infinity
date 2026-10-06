"""SRD 5.1 skill → ability map and name normalisation.

Single source of truth for the engine (and, optionally, the Forge): which ability a skill check
uses. Keys are the canonical skill names; values are the three-letter engine ability keys used
across `dice_server.py` (`str/dex/con/int/wis/cha`).
"""

from __future__ import annotations

SKILL_ABILITIES: dict[str, str] = {
    "Acrobatics": "dex",
    "Animal Handling": "wis",
    "Arcana": "int",
    "Athletics": "str",
    "Deception": "cha",
    "History": "int",
    "Insight": "wis",
    "Intimidation": "cha",
    "Investigation": "int",
    "Medicine": "wis",
    "Nature": "int",
    "Perception": "wis",
    "Performance": "cha",
    "Persuasion": "cha",
    "Religion": "int",
    "Sleight of Hand": "dex",
    "Stealth": "dex",
    "Survival": "wis",
}

# normalized (casefold + collapsed spaces) -> canonical skill name
_BY_NORM = {name.casefold(): name for name in SKILL_ABILITIES}


def normalize(text) -> str:
    """Casefold and collapse internal whitespace (e.g. 'SLEIGHT  OF   HAND')."""
    return " ".join(str(text or "").split()).casefold()


def skill_name(name) -> str | None:
    """The canonical skill name for `name`, or None when it is not a skill."""
    return _BY_NORM.get(normalize(name))


def skill_ability(name) -> str | None:
    """The three-letter ability key a skill uses, or None when `name` is not a skill."""
    skill = skill_name(name)
    return SKILL_ABILITIES.get(skill) if skill else None


def is_skill(name) -> bool:
    return skill_name(name) is not None
