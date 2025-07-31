
from pydantic import BaseModel, Field
from typing import List, Dict, Tuple, Optional

class Stats(BaseModel):
    strength: int
    dexterity: int
    constitution: int
    intelligence: int
    wisdom: int
    charisma: int

class Item(BaseModel):
    name: str
    slot: str # e.g., 'main_hand', 'chest'
    stat_modifiers: Optional[Dict[str, int]] = None

class Equipment(BaseModel):
    head: Optional[Item] = None
    chest: Optional[Item] = None
    arms: Optional[Item] = None
    legs: Optional[Item] = None
    ring: Optional[Item] = None
    neck: Optional[Item] = None
    main_hand: Optional[Item] = None
    off_hand: Optional[Item] = None

class BaseEntity(BaseModel):
    name: str
    level: int
    stats: Stats
    alignment: str

class PlayerCharacter(BaseEntity):
    age: int
    sex: str
    race: str
    character_class: str
    perks: List[str]
    equipment: Equipment = Field(default_factory=Equipment)
    xp: int = 0

class NPC(BaseEntity):
    role: str
    faction: str
    dialogue_options: List[str]
    abilities_for_sale: List['Ability'] = []

class Ability(BaseModel):
    name: str
    description: str
    tier: int
    guild_source: str

class Guild(BaseModel):
    name: str
    leader: NPC
    right_hand: NPC

class Creature(BaseModel):
    name: str
    creature_type: str
    difficulty: int
    stats: Stats
    xp_value: int

class Location(BaseModel):
    name: str
    coordinates: Tuple[int, int]
    biome: str
    description: str
    npcs: List[NPC] = []
    creatures: List[Creature] = []
    guilds: List[Guild] = []
    loot: List[Item] = []

class Kingdom(BaseModel):
    name: str
    capital: str
    alignment: str
    ruler: NPC
    locations: List[Location] = []
    guilds: List[Guild] = []
    relations: Dict[str, str] = {}

class Quest(BaseModel):
    title: str
    description: str
    giver: str
    reward: str
    xp_reward: int

class QuestTemplate(BaseModel):
    type: str
    title: str
    description: str
    reward_template: str
    xp_reward: int

class WorldState(BaseModel):
    player_character: PlayerCharacter
    map_grid: List[List[str]]
    kingdoms: List[Kingdom]
    npcs: List[NPC]
    creatures: List[Creature]
    quests: List[Quest]
    current_tick: str # e.g., '06:00'
