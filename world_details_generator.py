# world_details_generator.py v2
# Layer 5 of the Generation Cascade. Creates pre-calculated consequences and descriptive prose.

from models import WorldState

def generate_chronicle_triggers(world_state: WorldState):
    """
    Generates a dictionary of Trigger -> Outcome events for major world changes.
    The GM will use this to narrate consequences without having to invent them.
    """
    triggers = {}
    
    # Generate triggers for the death of faction leaders
    for faction in world_state.factions.values():
        if not faction.leader:
            continue
            
        leader_name = faction.leader
        
        # Find the rival faction
        rival_faction_name = ""
        if faction.name == "Merchants' Guild":
            rival_faction_name = "Thieves' Guild"
        elif faction.name == "Thieves' Guild":
            rival_faction_name = "Merchants' Guild"
            
        outcome_text = (
            f"With the death of {leader_name}, the {faction.name} is thrown into chaos. "
            f"Their operations become disorganized and fearful. "
        )
        if rival_faction_name:
            outcome_text += (
                f"Sensing weakness, the {rival_faction_name} grows bolder, their influence spreading like a shadow in the ensuing power vacuum."
            )
            
        trigger_key = f"NPC_DEATH:{leader_name}"
        triggers[trigger_key] = outcome_text
        
    world_state.chronicle_triggers = triggers

def generate_environmental_prose(world_state: WorldState):
    """
    Generates a library of descriptive text for various environmental contexts.
    The GM can pull from this library to provide rich, consistent descriptions.
    """
    prose = {}
    biomes = ["Forest", "Mountains", "Plains", "Swamp", "Hills"]
    times = ["Day", "Night"]
    weathers = ["Clear", "Rain", "Fog"]

    for biome in biomes:
        for time in times:
            for weather in weathers:
                key = f"{biome.upper()}_{time.upper()}_{weather.upper()}"
                text = ""
                # Default text
                text = f"You find yourself in a {biome.lower()} during the {time.lower()} with {weather.lower()} skies."

                # Specific prose overrides
                if key == "FOREST_DAY_CLEAR":
                    text = "Sunlight dapples through the thick canopy, painting the forest floor in shifting patterns of light and shadow. The air is alive with the gentle hum of insects and the distant call of birds."
                elif key == "FOREST_NIGHT_RAIN":
                    text = "A steady rain drums against the leaves overhead, a constant, soothing rhythm in the otherwise silent woods. The world is reduced to the glistening bark of nearby trees and the earthy smell of wet soil."
                elif key == "MOUNTAINS_DAY_CLEAR":
                    text = "The air is thin and crisp. Jagged peaks of grey stone claw at the deep blue sky around you. Below, the world spreads out like a map, vast and green."
                elif key == "MOUNTAINS_NIGHT_FOG":
                    text = "A thick, cold fog clings to the mountainside, swallowing all sound and sight. The world is a silent, grey void, and you can see no more than a few feet in any direction. The silence is absolute."
                elif key == "PLAINS_DAY_CLEAR":
                    text = "An endless sea of grass sways in the gentle breeze under a vast, open sky. The horizon is a flat, unbroken line in all directions, giving a profound sense of space and solitude."
                elif key == "SWAMP_NIGHT_RAIN":
                    text = "Warm, heavy rain falls, hissing as it strikes the stagnant, black water of the swamp. Strange, gurgling sounds echo through the twisted trees, and the air is thick with the smell of decay and wet vegetation."
                
                prose[key] = text

    world_state.environmental_prose = prose


def generate_world_details_layer(world_state: WorldState) -> WorldState:
    """

    v2: The main function to orchestrate the generation of all world details.
    """
    print("[STATUS] Generating World Details Layer...")
    
    generate_chronicle_triggers(world_state)
    generate_environmental_prose(world_state)
    
    print(f"[STATUS] World Details Layer complete. Generated {len(world_state.chronicle_triggers)} chronicle triggers and {len(world_state.environmental_prose)} environmental descriptions.")
    return world_state
