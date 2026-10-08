import os
import yaml
from pydantic import BaseModel
from typing import List, Optional
from .models import CharacterClass, Background

class AbilityScoreIncrease(BaseModel):
    ability: str
    value: int

class Trait(BaseModel):
    name: str
    description: str

class SubRace(BaseModel):
    name: str
    ability_score_increases: List[AbilityScoreIncrease] = []
    traits: List[Trait] = []
    proficiencies: List[dict] = []
    languages: List[str] = []

class Weapon(BaseModel):
    name: str
    category: str
    melee: bool
    damage: str
    damage_type: str
    properties: List[str] = []

current_dir = os.path.dirname(__file__)
project_root = os.path.join(current_dir, '..')
config_dir = os.path.join(project_root, 'config')

class RaceAge(BaseModel):
    # SRD 5.1 age guidance: `adulthood` is the race's age of maturity, `max` a typical
    # maximum lifespan. Used to bound the Forge's age prompt and to shape the portrait.
    adulthood: int = 18
    max: int = 90


class AsiChoice(BaseModel):
    """A floating ability-score increase the player chooses (SRD 5.1 half-elf: two +1s)."""
    count: int = 1
    value: int = 1
    exclude: List[str] = []


class Race(BaseModel):
    name: str
    ability_score_increases: List[AbilityScoreIncrease]
    speed: int
    traits: List[Trait]
    languages: List[str]
    proficiencies: List[dict] = []
    subraces: List[SubRace] = []
    age: RaceAge = RaceAge()
    # Player-chosen extras some SRD races grant -- the half-elf's two floating +1s and
    # two skills of its choice. Applied by the Forge right after the fixed ones.
    asi_choices: Optional[AsiChoice] = None
    skill_choices: int = 0

class Config(BaseModel):
    races: List[Race]
    classes: List[CharacterClass]
    backgrounds: List[Background]
    alignments: List[str]
    weapons: List[Weapon] = []

def load_config() -> Config:
    with open(os.path.join(config_dir, 'races.yml'), 'r', encoding="utf-8") as f:
        races_data = yaml.safe_load(f)
    
    with open(os.path.join(config_dir, 'classes.yml'), 'r', encoding="utf-8") as f:
        classes_data = yaml.safe_load(f)

    with open(os.path.join(config_dir, 'backgrounds.yml'), 'r', encoding="utf-8") as f:
        backgrounds_data = yaml.safe_load(f)
        
    with open(os.path.join(config_dir, 'alignments.yml'), 'r', encoding="utf-8") as f:
        alignments_data = yaml.safe_load(f)

    with open(os.path.join(config_dir, 'weapons.yml'), 'r', encoding="utf-8") as f:
        weapons_data = yaml.safe_load(f)

    return Config(
        races=[Race(**race) for race in races_data],
        classes=[CharacterClass(**char_class) for char_class in classes_data],
        backgrounds=[Background(**bg) for bg in backgrounds_data],
        alignments=alignments_data,
        weapons=[Weapon(**weapon) for weapon in weapons_data]
    )
