# forge/models.py
# Version 2.1 - Full D&D 5e Compliance with Equipment Options

from pydantic import BaseModel, Field
from typing import List, Dict, Tuple, Optional, Literal

# --- Core Attribute Models ---

class Stats(BaseModel):
    strength: int = 10
    dexterity: int = 10
    constitution: int = 10
    intelligence: int = 10
    wisdom: int = 10
    charisma: int = 10

class Skill(BaseModel):
    name: str
    ability: Literal['strength', 'dexterity', 'constitution', 'intelligence', 'wisdom', 'charisma']
    proficient: bool = False

# --- Item & Equipment Models ---

class Item(BaseModel):
    name: str
    item_type: str # e.g., 'weapon', 'armor', 'potion', 'trinket'
    description: str = ""
    properties: List[str] = [] # e.g., ['Finesse', 'Light', 'Ammunition']
    stat_modifiers: Optional[Dict[str, int]] = None
    damage: Optional[str] = None # e.g., '1d8'
    damage_type: Optional[str] = None
    armor_class: Optional[int] = None

class StartingEquipmentOption(BaseModel):
    # Represents a choice between items or a fixed set of items
    choose_one_from: Optional[List[str]] = None # List of item names to choose one from
    fixed_items: Optional[List[str]] = None # List of item names that are always given
    gold_pieces: Optional[int] = None # Amount of gold given

class Equipment(BaseModel):
    head: Optional[Item] = None
    chest: Optional[Item] = None
    arms: Optional[Item] = None
    legs: Optional[Item] = None
    ring1: Optional[Item] = None
    ring2: Optional[Item] = None
    neck: Optional[Item] = None
    main_hand: Optional[Item] = None
    off_hand: Optional[Item] = None
    inventory: List[Item] = []

# --- Action & Ability Models ---

class Action(BaseModel):
    name: str
    description: str
    attack_bonus: Optional[int] = None
    damage: Optional[str] = None
    damage_type: Optional[str] = None

class SpecialAbility(BaseModel):
    name: str
    description: str
    usage: Optional[str] = None # e.g., "3/Day"

# --- Base Entity Model ---

class BaseEntity(BaseModel):
    name: str
    level: int = 1
    size: str = "Medium"
    speed: int = 30
    stats: Stats = Field(default_factory=Stats)
    armor_class: int = 10
    hit_points: int = 10
    hit_dice: str = "1d8"
    languages: List[str] = ["Common"]
    senses: Dict[str, int] = {"passive_perception": 10}

# --- Creature & NPC Models ---

class Creature(BaseEntity):
    creature_type: str # e.g., 'Aberration', 'Beast', 'Celestial'
    alignment: str
    challenge_rating: float
    xp_value: int
    skills: List[Skill] = []
    saving_throws: List[Skill] = []
    damage_vulnerabilities: List[str] = []
    damage_resistances: List[str] = []
    damage_immunities: List[str] = []
    condition_immunities: List[str] = []
    special_abilities: List[SpecialAbility] = []
    actions: List[Action] = []
    reactions: List[Action] = []
    legendary_actions: List[Action] = []

class NPC(Creature):
    role: str
    faction: str
    is_walker: bool = False
    dialogue_options: List[str] = []
    abilities_for_sale: List['PlayerAbility'] = [] # Forward reference
    race: Optional[str] = None
    character_class: Optional[str] = None
    background: Optional[str] = None
    armor_proficiencies: List[str] = []
    weapon_proficiencies: List[str] = []
    tool_proficiencies: List[str] = []
    features_and_traits: List[SpecialAbility] = []
    equipment: Equipment = Field(default_factory=Equipment)
    # Spellcasting attributes (optional for NPCs)
    spellcasting_ability: Optional[Literal['intelligence', 'wisdom', 'charisma']] = None
    spell_save_dc: Optional[int] = None
    spell_attack_modifier: Optional[int] = None
    cantrips_known: List[str] = []
    spells_known: List[str] = []
    spell_slots: Dict[str, int] = Field(default_factory=dict)

# --- Player Character Models ---

class PlayerAbility(BaseModel):
    name: str
    description: str
    tier: int
    guild_source: str

class ClassFeature(BaseModel):
    name: str
    description: str
    level: int

class SkillChoices(BaseModel):
    choices: List[str]
    number: int

class CharacterClass(BaseModel):
    name: str
    hit_die: int
    saving_throw_proficiencies: List[str]
    armor_proficiencies: List[str]
    weapon_proficiencies: List[str]
    skills: SkillChoices
    features: List[ClassFeature]
    starting_equipment_options: List['StartingEquipmentOption'] = []
    tool_proficiencies: List[str] = [] # Modified: Added default empty list

class Background(BaseModel):
    name: str
    skill_proficiencies: List[str]
    tool_proficiencies: List[str]
    languages: Optional[str] = None
    equipment: List[str]
    starting_equipment_options: List['StartingEquipmentOption'] = []

class PlayerCharacter(BaseEntity):
    level: int = 1
    character_class: str
    background: str
    race: str
    alignment: str
    xp: int = 0
    inspiration: bool = False
    proficiency_bonus: int = 2
    armor_proficiencies: List[str] = []
    weapon_proficiencies: List[str] = []
    tool_proficiencies: List[str] = []
    skills: List[Skill] = []
    saving_throws: List[Skill] = []
    equipment: Equipment = Field(default_factory=Equipment)
    features_and_traits: List[SpecialAbility] = []
    perks: List[str] = [] # Consolidating perks here
    gold: int = 0
    
    # Spellcasting attributes
    spellcasting_ability: Optional[Literal['intelligence', 'wisdom', 'charisma']] = None
    spell_save_dc: Optional[int] = None
    spell_attack_modifier: Optional[int] = None
    cantrips_known: List[str] = []
    spells_known: List[str] = []
    spell_slots: Dict[str, int] = Field(default_factory=dict)

# --- World State Models ---

class Guild(BaseModel):
    name: str
    leader: NPC
    right_hand: NPC
    members: List[NPC] = []
    reports_to: Optional[str] = None

class Location(BaseModel):
    name: str
    coordinates: Tuple[int, int]
    biome: str
    description: str
    npcs: List[NPC] = []
    guilds: List[Guild] = []

class Kingdom(BaseModel):
    name: str
    capital: str
    alignment: str
    ruler: NPC
    locations: List[Location] = []
    guilds: List[Guild] = []
    relations: Dict[str, str] = {} # Will be updated by UFP engine
    interests: List[str] = []

class WorldState(BaseModel):
    player_character: PlayerCharacter
    map_grid: List[List[str]]
    kingdoms: List[Kingdom]
    all_abilities: List[PlayerAbility] = [] # New field for all available abilities
# Note: Creatures are now part of Locations, not a separate top-level list
    current_tick: str
    world_history: List[str] = [] # For L.I.C. Engine

# Update forward reference for NPC
NPC.model_rebuild()