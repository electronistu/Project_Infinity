# formatter.py
# The final stage of the Generation Cascade.
# This module translates the rich WorldState object into the token-efficient
# World-Weave Format (.wwf) string.

from models import WorldState

def generate_world_weave_string(world_state: WorldState) -> str:
    """
    Takes the complete WorldState object and weaves it into the final
    .wwf string format for the Game Master AI.
    """
    print("[STATUS] Weaving the final World-Weave Key...")
    weave_parts = []

    # --- Section: Player Character ---
    pc = world_state.player_character
    weave_parts.append("[PLAYER_CHARACTER]")
    pc_line = (
        f"PC_NAME:{pc.name}|AGE:{pc.age}|SEX:{pc.sex}|ORI:{pc.orientation}|"
        f"RACE:{pc.race}|CLS:{pc.character_class}|"
        f"KN_LOC:{','.join(pc.known_locations)}"
    )
    weave_parts.append(pc_line)
    weave_parts.append("")

    # --- Section: Coded Map ---
    if world_state.coded_map_grid:
        weave_parts.append("[CODED_MAP]")
        for row in world_state.coded_map_grid:
            weave_parts.append(row)
        weave_parts.append("")

    # --- Section: Locations ---
    weave_parts.append("[LOCATIONS]")
    for loc in world_state.world_map.values():
        loc_line = (
            f"{loc.name}|T:{loc.type}|B:{loc.biome}|CL:{loc.challenge_level}|"
            f"CON:{','.join(loc.connections)}|INHAB:{','.join(loc.inhabitants)}|"
            f"COORDS:{loc.coordinates[0]},{loc.coordinates[1]}"
        )
        weave_parts.append(loc_line)
    weave_parts.append("")

    # --- Section: Factions (PATCHED) ---
    if world_state.factions:
        weave_parts.append("[FACTIONS]")
        for fac in world_state.factions.values():
            # Add the leader's name to the string if one was designated.
            leader_str = f"|LEAD:{fac.leader}" if fac.leader else ""
            fac_line = f"{fac.name}|DISP:{fac.disposition}{leader_str}"
            weave_parts.append(fac_line)
        weave_parts.append("")

    # --- Section: NPCs ---
    weave_parts.append("[NPCS]")
    for npc in world_state.npcs.values():
        faction_str = npc.faction_membership if npc.faction_membership else "None"
        npc_line = (
            f"{npc.name}|AGE:{npc.age}|SEX:{npc.sex}|RACE:{npc.race}|"
            f"STAT:{npc.status}|FAM:{npc.family_id}|ROLE:{npc.role_in_family}|"
            f"LOC:{npc.location}|FAC:{faction_str}"
        )
        weave_parts.append(npc_line)
    weave_parts.append("")

    print("[STATUS] Weaving complete.")
    return "\n".join(weave_parts)
