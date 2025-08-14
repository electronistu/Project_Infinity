# forge/history_generator.py

from .models import WorldState
import random

WAR_TEMPLATES = [
    "The <WAR_NAME> began after <KINGDOM1_NAME> (driven by <INTEREST1>) laid claim to the fertile <LOCATION> which had long been considered part of <KINGDOM2_NAME> (driven by <INTEREST2>).",
    "A bitter trade dispute over <RESOURCE> escalated into the <WAR_NAME> when a <KINGDOM1_NAME> merchant vessel was sunk by <KINGDOM2_NAME> privateers.",
    "The <WAR_NAME> was sparked by an ideological schism, with the <ALIGNMENT1> <KINGDOM1_NAME> declaring a holy crusade against the <ALIGNMENT2> people of <KINGDOM2_NAME>."
]

WAR_NAMES = ["War of the Ashen Crown", "The Salt-Stained War", "The War of Crimson Rivers", "The Unseen Conflict"]
LOCATIONS = ["Sunken Hills", "Whispering Plains", "Dragon's Tooth Mountains"]
RESOURCES = ["rare spices", "adamantine ore", "ancient artifacts"]

def generate_histories(world_state: WorldState):
    """Generates narrative history for kingdom relations."""
    history = []
    processed_pairs = set()

    for k1 in world_state.kingdoms:
        for k2_name, relation in k1.relations.items():
            # Ensure we only process each pair once
            pair = tuple(sorted((k1.name, k2_name)))
            if pair in processed_pairs:
                continue
            processed_pairs.add(pair)

            if relation == "War":
                k2 = next((k for k in world_state.kingdoms if k.name == k2_name), None)
                if not k2: continue

                template = random.choice(WAR_TEMPLATES)
                
                # For simplicity, we'll just grab the first interest if available
                interest1 = "an unknown motive" # Placeholder
                interest2 = "an unknown motive" # Placeholder

                history_entry = template.replace("<WAR_NAME>", random.choice(WAR_NAMES)) \
                                      .replace("<KINGDOM1_NAME>", k1.name) \
                                      .replace("<INTEREST1>", interest1) \
                                      .replace("<LOCATION>", random.choice(LOCATIONS)) \
                                      .replace("<KINGDOM2_NAME>", k2.name) \
                                      .replace("<INTEREST2>", interest2) \
                                      .replace("<RESOURCE>", random.choice(RESOURCES)) \
                                      .replace("<ALIGNMENT1>", k1.alignment) \
                                      .replace("<ALIGNMENT2>", k2.alignment)
                
                history.append(history_entry)

    world_state.world_history = history
    print("Narrative histories generated.")
