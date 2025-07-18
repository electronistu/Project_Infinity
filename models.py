# models.py
# Pydantic models defining the core data structures for Project Infinity.
# This is the "shape" of our reality.

from pydantic import BaseModel, Field
from typing import Dict, List, Optional

class PlayerCharacter(BaseModel):
    """
    Represents the player's character, built from user input.
    """
    name: str
    age: int
    sex: str
    orientation: str
    race: str
    character_class: str = Field(..., alias='class')

    inventory: List[str] = []
    reputation: Dict[str, int] = {}
    known_locations: List[str] = []
    known_npcs: List[str] = []

    class Config:
        populate_by_name = True

class Location(BaseModel):
    """
    Represents a single location on the world map.
    """
    name: str
    type: str
    biome: str
    description: str
    challenge_level: int = Field(..., ge=1, le=10)
    connections: List[str] = []
    inhabitants: List[str] = []
    coordinates: Optional[tuple[int, int]] = None # Stores (x, y) on the coded map

class Faction(BaseModel):
    """
    Represents a faction or organization in the world.
    """
    name: str
    description: str
    disposition: str # e.g., "Lawful", "Chaotic", "Neutral"
    members: List[str] = []
    # --- PATCHED ---
    # The leader is now pre-calculated and stored here.
    leader: Optional[str] = None

class NPC(BaseModel):
    """
    Represents a single Non-Player Character.
    """
    name: str
    age: int
    sex: str
    race: str
    status: str # e.g., "Commoner", "Merchant", "Guard"
    family_id: str # e.g., "Smith"
    role_in_family: str # e.g., "Parent", "Child"
    location: str # The name of the settlement they live in
    faction_membership: Optional[str] = None


class WorldState(BaseModel):
    """
    The main container for the entire generated world.
    This object will be serialized into the World-Seed Key.
    """
    instance_id: str
    player_character: PlayerCharacter

    world_map: Dict[str, Location] = {}
    factions: Dict[str, Faction] = {}
    npcs: Dict[str, NPC] = {}

    # This field is for temporary storage and will be formatted into the .wwf file
    coded_map_grid: Optional[List[str]] = None

    quests: Dict = {}
    legendary_set_locations: Dict = {}
