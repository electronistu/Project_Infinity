# forge/geopolitical_engine.py

from .models import WorldState
import random

INTERESTS = {
    "Lawful Good": ["Order", "Justice", "Prosperity"],
    "Lawful Evil": ["Control", "Power", "Supremacy"],
    "True Neutral": ["Balance", "Knowledge", "Preservation"],
    "Chaotic Evil": ["Domination", "Wealth", "Anarchy"]
}

CONFLICTING_INTERESTS = {
    "Order": ["Anarchy"],
    "Justice": ["Supremacy", "Domination"],
    "Prosperity": ["Domination"],
    "Control": ["Anarchy"],
    "Power": ["Balance"],
    "Supremacy": ["Balance"],
    "Balance": ["Power", "Supremacy", "Anarchy"],
    "Domination": ["Justice", "Prosperity"]
}

def assign_interests(kingdom):
    """Assigns interests to a kingdom based on its alignment."""
    base_interests = INTERESTS.get(kingdom.alignment, ["Survival"])
    return random.sample(base_interests, k=min(len(base_interests), 2))

def determine_relations(world_state: WorldState):
    """Dynamically sets kingdom relations based on conflicting interests."""
    # First, assign interests to all kingdoms
    kingdom_interests = {k.name: assign_interests(k) for k in world_state.kingdoms}

    for i, k1 in enumerate(world_state.kingdoms):
        for k2 in world_state.kingdoms[i+1:]:
            interests1 = set(kingdom_interests[k1.name])
            interests2 = set(kingdom_interests[k2.name])

            is_at_war = False
            for interest in interests1:
                conflicts = CONFLICTING_INTERESTS.get(interest, [])
                if any(c in interests2 for c in conflicts):
                    is_at_war = True
                    break
            
            if is_at_war:
                k1.relations[k2.name] = "War"
                k2.relations[k1.name] = "War"
            else:
                k1.relations[k2.name] = "Neutral"
                k2.relations[k1.name] = "Neutral"

    print("Geopolitical relations dynamically determined.")
