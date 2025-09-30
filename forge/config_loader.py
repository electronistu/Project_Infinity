import os
import yaml
from pydantic import BaseModel
from typing import List, Optional
from .models import PlayerAbility, CharacterClass, Background

class AbilityScoreIncrease(BaseModel):
    ability: str
    value: int

class Trait(BaseModel):
    name: str
    description: str

current_dir = os.path.dirname(__file__)
project_root = os.path.join(current_dir, '..')
config_dir = os.path.join(project_root, 'config')

class Race(BaseModel):
    name: str
    ability_score_increases: List[AbilityScoreIncrease]
    speed: int
    traits: List[Trait]
    languages: List[str]
    proficiencies: List[dict] = []

class Config(BaseModel):
    races: List[Race]
    classes: List[CharacterClass]
    backgrounds: List[Background]
    alignments: List[str]
    abilities: List[PlayerAbility]

def load_config() -> Config:
    with open(os.path.join(config_dir, 'races.yml'), 'r') as f:
        races_data = yaml.safe_load(f)
    
    with open(os.path.join(config_dir, 'classes.yml'), 'r') as f:
        classes_data = yaml.safe_load(f)

    with open(os.path.join(config_dir, 'backgrounds.yml'), 'r') as f:
        backgrounds_data = yaml.safe_load(f)
        
    with open(os.path.join(config_dir, 'alignments.yml'), 'r') as f:
        alignments_data = yaml.safe_load(f)

    with open(os.path.join(config_dir, 'abilities.yml'), 'r') as f:
        abilities_data = yaml.safe_load(f)

    return Config(
        races=[Race(**race) for race in races_data],
        classes=[CharacterClass(**char_class) for char_class in classes_data],
        backgrounds=[Background(**bg) for bg in backgrounds_data],
        alignments=alignments_data,
        abilities=[PlayerAbility(**ability) for ability in abilities_data]
    )
