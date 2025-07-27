import yaml
from pydantic import BaseModel
from typing import List
from .models import Creature, Item, Ability, QuestTemplate, QuestTemplate

class Race(BaseModel):
    name: str
    perks: List[str]

class CharacterClass(BaseModel):
    name: str
    perks: List[str]

class Config(BaseModel):
    races: List[Race]
    classes: List[CharacterClass]
    alignments: List[str]
    creatures: List[Creature]
    items: List[Item]
    abilities: List[Ability]
    quests: List[QuestTemplate]

def load_config() -> Config:
    with open('/home/rtmi6/GitHub/project_infinity/config/races.yml', 'r') as f:
        races_data = yaml.safe_load(f)
    
    with open('/home/rtmi6/GitHub/project_infinity/config/classes.yml', 'r') as f:
        classes_data = yaml.safe_load(f)
        
    with open('/home/rtmi6/GitHub/project_infinity/config/alignments.yml', 'r') as f:
        alignments_data = yaml.safe_load(f)
        
    with open('/home/rtmi6/GitHub/project_infinity/config/creatures.yml', 'r') as f:
        creatures_data = yaml.safe_load(f)

    with open('/home/rtmi6/GitHub/project_infinity/config/items.yml', 'r') as f:
        items_data = yaml.safe_load(f)

    with open('/home/rtmi6/GitHub/project_infinity/config/abilities.yml', 'r') as f:
        abilities_data = yaml.safe_load(f)

    with open('/home/rtmi6/GitHub/project_infinity/config/quests.yml', 'r') as f:
        quests_data = yaml.safe_load(f)

    return Config(
        races=[Race(**race) for race in races_data],
        classes=[CharacterClass(**char_class) for char_class in classes_data],
        alignments=alignments_data,
        creatures=[Creature(**creature) for creature in creatures_data],
        items=[Item(**item) for item in items_data],
        abilities=[Ability(**ability) for ability in abilities_data],
        quests=[QuestTemplate(**quest) for quest in quests_data]
    )
