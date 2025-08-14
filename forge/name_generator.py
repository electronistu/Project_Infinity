# forge/name_generator.py

import random
from typing import Optional

NAME_COMPONENTS = {
    "Eldoria": {
        "prefix": ["Silver", "Green", "Clear", "Fair", "Sun", "River"],
        "suffix": ["wood", "dale", "brook", "haven", "ford", "meadow"]
    },
    "Zarthus": {
        "prefix": ["Shadow", "Black", "Ash", "Dread", "Gloom", "Iron"],
        "suffix": ["mire", "fen", "marsh", "hold", "crag", "burn"]
    },
    "Silverwood": {
        "prefix": ["Whisper", "Verdant", "Moss", "Silent", "Deep", "Star"],
        "suffix": ["glade", "wood", "grove", "hollow", "fall", "light"]
    },
    "Blacksail Archipelago": {
        "prefix": ["Shark", "Salt", "Drift", "Kraken", "Broken", "Grog"],
        "suffix": ["tooth", "tide", "wood", "bay", "cove", "fury"]
    }
}

def generate_name(kingdom_name: Optional[str] = None, existing_names: Optional[list] = None, context: Optional[str] = None) -> str:
    """Generates a unique, thematic name for a settlement or an NPC."""
    if context:
        return context # For NPC names, we'll use the context directly for now

    if kingdom_name and existing_names is not None:
        if kingdom_name not in NAME_COMPONENTS:
            return f"{kingdom_name} Settlement"

        components = NAME_COMPONENTS[kingdom_name]
        
        while True:
            prefix = random.choice(components["prefix"])
            suffix = random.choice(components["suffix"])
            new_name = f"{prefix}{suffix}"
            
            if new_name not in existing_names:
                return new_name
    
    return "Unnamed Entity" # Fallback if neither context nor kingdom_name/existing_names are provided
