# models.py v3.3
# Pydantic models defining the core data structures for Project Infinity v3.3.

from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Tuple

class Item(BaseModel):
    name: str
    type: str
    slot: str
    base_value: int

class Ability(BaseModel):
    name: str
    description: str
    tier: int
    cost: int
    class_requirement: str

class Time(BaseModel):
    current_tick: str = "12:00"
    ticks_per_day: List[str] = ["00:00", "06:00", "12:00", "18:00"]

class SubLocation(BaseModel):
    name: str
    type: str
    parent_location: str
    operator_npc: Optional[str] = None

class BaseCharacter(BaseModel):
    """v3.3: Common base for all characters, including stats and equipment."""
    name: str
    race: str
    stats: Dict[str, int] = {
        "STR": 10, "DEX": 10, "CON": 10,
        "INT": 10, "WIS": 10, "CHA": 10
    }
    equipment: Dict[str, Optional[Item]] = {
        "head": None, "chest": None, "arms": None, "legs": None,
        "ring": None, "main_hand": None, "off_hand": None
    }
    difficulty_level: int = 1
    gold: int = 0

class PlayerCharacter(BaseCharacter):
    """v3.3: Represents the player's character."""
    age: int
    sex: str
    character_class: str = Field(..., alias='class')
    alignment: str
    racial_perk: Optional[str] = None
    abilities: List[str] = []
    known_locations: List[str] = []
    known_npcs: List[str] = []
    completed_quests: List[str] = []

    class Config:
        populate_by_name = True

class NPC(BaseCharacter):
    """v3.3: Represents a single Non-Player Character."""
    age: int
    sex: str
    status: str # e.g., 'King', 'Merchant', 'Guard', 'Court Mage'
    family_id: str
    role_in_family: str
    location: str
    faction_membership: Optional[str] = None
    inventory: List[Item] = []
    for_sale_abilities: List[Ability] = [] # Abilities offered by this NPC

class Creature(BaseCharacter):
    """v3.3: Represents a non-sentient creature or monster."""
    type: str
    location: str
    coordinates: Tuple[int, int]
    loot: List[Item] = []

class Location(BaseModel):
    """v3.3: Represents a single location on the world map."""
    name: str
    type: str # 'Settlement', 'Dungeon', 'Capital', 'Island'
    biome: str
    size: Tuple[int, int]
    challenge_level: int # Now scaled 1-10
    connections: List[str] = []
    inhabitants: List[str] = []
    sub_locations: List[SubLocation] = []
    coordinates: Optional[Tuple[int, int]] = None

class Faction(BaseModel):
    """v3.3: Represents a faction or guild."""
    name: str
    description: str
    disposition: str # e.g., 'Lawful Good', 'Neutral Evil'
    leader: Optional[str] = None
    members: List[str] = []

class Quest(BaseModel):
    id: str
    title: str
    type: str # 'Fetch', 'Kill', 'Clear', 'Escort', 'Investigate'
    giver_npc: str
    target: str
    reward_gold: int
    reward_item: Optional[Item] = None
    prerequisite_quest: Optional[str] = None
    required_reputation: int
    description: str
    tier: int

class WorldState(BaseModel):
    """v3.3: The main container for the entire generated world."""
    instance_id: str
    player_character: PlayerCharacter
    world_time: Time = Time()
    total_world_gold: int = 0

    world_map: Dict[str, Location] = {}
    factions: Dict[str, Faction] = {}
    npcs: Dict[str, NPC] = {}
    creatures: Dict[str, Creature] = {}
    quests: Dict[str, Quest] = {}

    # Pre-calculated data for the GM
    coded_map_grid: Optional[List[str]] = None
    roads_grid: Optional[List[str]] = None
    faction_relations: Dict[str, Dict[str, str]] = {}
    chronicle_triggers: Dict[str, str] = {}
    environmental_prose: Dict[str, str] = {}