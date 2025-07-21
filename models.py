# models.py v2.1
# Pydantic models defining the core data structures for Project Infinity v2.

from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Tuple

# ... (Item, SubLocation models are unchanged) ...

class Item(BaseModel):
    name: str
    type: str
    slot: str
    base_value: int

class SubLocation(BaseModel):
    name: str
    type: str
    parent_location: str
    operator_npc: Optional[str] = None

class PlayerCharacter(BaseModel):
    """Represents the player's character for v2.1."""
    name: str
    age: int
    sex: str
    # CORRECTED: The 'orientation' field has been removed.
    race: str
    character_class: str = Field(..., alias='class')
    alignment: str

    gold: int = 0
    equipped_items: Dict[str, Item] = {}
    known_locations: List[str] = []
    known_npcs: List[str] = []
    completed_quests: List[str] = []

    class Config:
        populate_by_name = True

class Location(BaseModel):
    """Represents a single location on the world map for v2.2."""
    name: str
    # v2.2 CHANGE: Added "Capital" as a valid type.
    type: str # 'Settlement', 'Dungeon', 'Capital'
    biome: str
    size: Tuple[int, int]
    challenge_level: int
    connections: List[str] = []
    inhabitants: List[str] = []
    sub_locations: List[SubLocation] = []
    coordinates: Optional[Tuple[int, int]] = None

class Faction(BaseModel):
    """Represents a faction for v2."""
    name: str
    description: str
    disposition: str
    leader: Optional[str] = None
    members: List[str] = []

class NPC(BaseModel):
    """Represents a single Non-Player Character for v2."""
    name: str
    age: int
    sex: str
    race: str
    status: str
    family_id: str
    role_in_family: str
    location: str
    faction_membership: Optional[str] = None
    difficulty: str # 'Easy', 'Medium', 'Hard'
    inventory: List[Item] = []
    gold: int = 0

class Creature(BaseModel):
    """Represents a non-sentient creature or monster for v2."""
    name: str
    type: str
    challenge_level: int
    location: str # Name of the Dungeon or wilderness area
    coordinates: Tuple[int, int]
    difficulty: str # 'Easy', 'Hard', 'Boss'
    loot: List[Item] = []
    gold: int = 0

class Quest(BaseModel):
    """Represents a single generated quest."""
    id: str
    title: str
    type: str # 'Fetch', 'Kill', 'Clear'
    giver_npc: str
    target: str # Can be an item name, NPC name, or Location name
    reward_gold: int
    reward_item: Optional[Item] = None
    prerequisite_quest: Optional[str] = None
    required_reputation: int
    description: str

class WorldState(BaseModel):
    """The main container for the entire generated world for v2."""
    instance_id: str
    player_character: PlayerCharacter
    total_world_gold: int = 0

    world_map: Dict[str, Location] = {}
    factions: Dict[str, Faction] = {}
    npcs: Dict[str, NPC] = {}
    creatures: Dict[str, Creature] = {}
    quests: Dict[str, Quest] = {}

    # Pre-calculated data for the GM
    coded_map_grid: Optional[List[str]] = None
    faction_relations: Dict[str, Dict[str, str]] = {} # e.g., {'Merchants': {'Thieves': 'HOSTILE'}}
    chronicle_triggers: Dict[str, str] = {} # e.g., {'NPC_DEATH:Gunnar': '...'}
    environmental_prose: Dict[str, str] = {} # e.g., {'FOREST_NIGHT_RAIN': '...'}