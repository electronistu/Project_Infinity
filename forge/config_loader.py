import yaml
from pydantic import BaseModel
from typing import List, Optional
from .models import Creature, Item, PlayerAbility, StartingEquipmentOption, CharacterClass, Background

class AbilityScoreIncrease(BaseModel):
    ability: str
    value: int

class Trait(BaseModel):
    name: str
    description: str

class Race(BaseModel):
    name: str
    ability_score_increases: List[AbilityScoreIncrease]
    speed: int
    traits: List[Trait]
    languages: List[str]
    proficiencies: List[dict] = [] # Changed to List[dict] to match data structure

# CharacterClass and Background models are now imported from .models
# so we don't redefine them here.

class Config(BaseModel):
    races: List[Race]
    classes: List[CharacterClass]
    backgrounds: List[Background]
    alignments: List[str]
    items: List[Item]
    abilities: List[PlayerAbility]
    creatures: List[Creature] # Moved here to be loaded from GameMaster.md

def _extract_yaml_block_from_md(filepath: str, block_name: str) -> str:
    """Extracts a YAML block from a Markdown file given its block name."""
    with open(filepath, 'r') as f:
        content = f.read()
    
    start_tag = f"**{block_name}:**\n```yaml\n"
    end_tag = "\n```"
    
    start_index = content.find(start_tag)
    if start_index == -1:
        raise ValueError(f"YAML block '{block_name}' not found in {filepath}")
    
    start_index += len(start_tag)
    end_index = content.find(end_tag, start_index)
    
    if end_index == -1:
        raise ValueError(f"Closing YAML block tag not found for '{block_name}' in {filepath}")
        
    return content[start_index:end_index]

def load_config() -> Config:
    with open('/home/rtmi6/GitHub/project_infinity/config/races.yml', 'r') as f:
        races_data = yaml.safe_load(f)
    
    with open('/home/rtmi6/GitHub/project_infinity/config/classes.yml', 'r') as f:
        classes_data = yaml.safe_load(f)

    with open('/home/rtmi6/GitHub/project_infinity/config/backgrounds.yml', 'r') as f:
        backgrounds_data = yaml.safe_load(f)
        
    with open('/home/rtmi6/GitHub/project_infinity/config/alignments.yml', 'r') as f:
        alignments_data = yaml.safe_load(f)
        
    # Load creatures from GameMaster.md
    creatures_yaml_str = _extract_yaml_block_from_md('/home/rtmi6/GitHub/project_infinity/GameMaster.md', 'CREATURE_TEMPLATES')
    creatures_data = yaml.safe_load(creatures_yaml_str)

    with open('/home/rtmi6/GitHub/project_infinity/config/items.yml', 'r') as f:
        items_data = yaml.safe_load(f)

    with open('/home/rtmi6/GitHub/project_infinity/config/abilities.yml', 'r') as f:
        abilities_data = yaml.safe_load(f)

    return Config(
        races=[Race(**race) for race in races_data],
        classes=[CharacterClass(**char_class) for char_class in classes_data],
        backgrounds=[Background(**bg) for bg in backgrounds_data],
        alignments=alignments_data,
        creatures=[Creature(**creature) for creature in creatures_data],
        items=[Item(**item) for item in items_data],
        abilities=[PlayerAbility(**ability) for ability in abilities_data]
    )
