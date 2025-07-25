# formatter.py v3.0
# The final stage of the Generation Cascade.
# This module translates the rich WorldState v3 object into the token-efficient
# World-Weave Format (.wwf) v3 string.

from models import WorldState

def generate_world_weave_string(world_state: WorldState) -> str:
    """
    v3: Takes the complete WorldState object and weaves it into the final
    .wwf v3 string format for the Game Master AI.
    """
    print("[STATUS] Weaving the final v3 World-Weave Key...")
    weave_parts = []

    # --- Section: World State Meta ---
    weave_parts.append("[WORLD_STATE]")
    weave_parts.append(f"INSTANCE_ID:{world_state.instance_id}")
    weave_parts.append(f"TOTAL_GOLD:{world_state.total_world_gold}")
    weave_parts.append(f"CURRENT_TICK:{world_state.world_time.current_tick}")
    weave_parts.append("")

    # --- Section: Player Character (v3) ---
    pc = world_state.player_character
    weave_parts.append("[PLAYER_CHARACTER_V3]")
    stats_str = ','.join([f"{k}-{v}" for k, v in pc.stats.items()])
    abilities_str = ','.join(pc.abilities)
    pc_line = (
        f"PC_NAME:{pc.name}|AGE:{pc.age}|SEX:{pc.sex}|"
        f"RACE:{pc.race}|CLS:{pc.character_class}|ALIGN:{pc.alignment}|GOLD:{pc.gold}|"
        f"KN_LOC:{','.join(pc.known_locations)}|STATS:{stats_str}|ABILITIES:{abilities_str}"
    )
    weave_parts.append(pc_line)
    weave_parts.append("")

    # --- Section: Equipment (v3) ---
    weave_parts.append("[EQUIPMENT]")
    # Player equipment
    player_equipped = [f"{slot}:{item.name}" for slot, item in pc.equipment.items() if item]
    if player_equipped:
        weave_parts.append(f"{pc.name}|{','.join(player_equipped)}")
    # NPC equipment
    for npc in world_state.npcs.values():
        npc_equipped = [f"{slot}:{item.name}" for slot, item in npc.equipment.items() if item]
        if npc_equipped:
            weave_parts.append(f"{npc.name}|{','.join(npc_equipped)}")
    weave_parts.append("")

    # --- Section: Coded & Road Map (v3) ---
    if world_state.roads_grid:
        weave_parts.append("[CODED_MAP_V3]")
        weave_parts.extend(world_state.roads_grid)
    elif world_state.coded_map_grid:
        weave_parts.append("[CODED_MAP_V3]")
        weave_parts.extend(world_state.coded_map_grid)
    weave_parts.append("")

    # --- Section: Locations & Sub-Locations ---
    weave_parts.append("[LOCATIONS]")
    for loc in world_state.world_map.values():
        sub_loc_names = ','.join([sl.name for sl in loc.sub_locations])
        dungeon_diff_str = f"|D_DIFF:{loc.dungeon_difficulty}" if loc.dungeon_difficulty else ""
        loc_line = (
            f"{loc.name}|T:{loc.type}|B:{loc.biome}|SIZE:{loc.size[0]},{loc.size[1]}|"
            f"CL:{loc.challenge_level}{dungeon_diff_str}|CON:{','.join(loc.connections)}|"
            f"INHAB:{','.join(loc.inhabitants)}|SUB_LOCS:{sub_loc_names}|"
            f"COORDS:{loc.coordinates[0]},{loc.coordinates[1]}"
        )
        weave_parts.append(loc_line)
    weave_parts.append("")

    weave_parts.append("[SUB_LOCATIONS]")
    for loc in world_state.world_map.values():
        for sub_loc in loc.sub_locations:
            sl_line = f"{sub_loc.name}|T:{sub_loc.type}|PARENT:{sub_loc.parent_location}|OP:{sub_loc.operator_npc}"
            weave_parts.append(sl_line)
    weave_parts.append("")

    # --- Section: Factions & Relations ---
    if world_state.factions:
        weave_parts.append("[FACTIONS]")
        for fac in world_state.factions.values():
            fac_line = f"{fac.name}|DISP:{fac.disposition}|LEAD:{fac.leader}"
            weave_parts.append(fac_line)
        weave_parts.append("")

    if world_state.faction_relations:
        weave_parts.append("[FACTION_RELATIONS]")
        for fac, relations in world_state.faction_relations.items():
            rel_str = ','.join([f"{key}:{val}" for key, val in relations.items()])
            weave_parts.append(f"{fac}|{rel_str}")
        weave_parts.append("")

    # --- Section: NPCs & Creatures (v3) ---
    weave_parts.append("[NPCS]")
    for npc in world_state.npcs.values():
        npc_line = (
            f"{npc.name}|AGE:{npc.age}|SEX:{npc.sex}|RACE:{npc.race}|STAT:{npc.status}|"
            f"FAM:{npc.family_id}|ROLE:{npc.role_in_family}|LOC:{npc.location}|"
            f"FAC:{npc.faction_membership}|DIFF_LVL:{npc.difficulty_level}|GOLD:{npc.gold}"
        )
        weave_parts.append(npc_line)
    weave_parts.append("")

    if world_state.creatures:
        weave_parts.append("[CREATURES]")
        for cid, creature in world_state.creatures.items():
            loot_str = ','.join([item.name for item in creature.loot])
            creature_line = (
                f"{cid}|NAME:{creature.name}|T:{creature.type}|"
                f"LOC:{creature.location}|COORDS:{creature.coordinates[0]},{creature.coordinates[1]}|"
                f"DIFF_LVL:{creature.difficulty_level}|GOLD:{creature.gold}|LOOT:{loot_str}"
            )
            weave_parts.append(creature_line)
        weave_parts.append("")

    # --- Section: Quests ---
    if world_state.quests:
        weave_parts.append("[QUESTS]")
        for quest in world_state.quests.values():
            prereq_str = quest.prerequisite_quest if quest.prerequisite_quest else "None"
            quest_line = (
                f"{quest.id}|TITLE:{quest.title}|T:{quest.type}|GIVER:{quest.giver_npc}|"
                f"TARGET:{quest.target}|R_GOLD:{quest.reward_gold}|PREREQ:{prereq_str}|"
                f"REQ_REP:{quest.required_reputation}|DESC:{quest.description}"
            )
            weave_parts.append(quest_line)
        weave_parts.append("")

    # --- Section: Ability Shop (v3) ---
    if world_state.ability_shop:
        weave_parts.append("[ABILITY_SHOP]")
        for ability in world_state.ability_shop:
            ability_line = f"{ability.name}|T{ability.tier}|{ability.cost}g|{ability.class_requirement}|{ability.description}"
            weave_parts.append(ability_line)
        weave_parts.append("")

    # --- Section: World Details ---
    if world_state.chronicle_triggers:
        weave_parts.append("[CHRONICLE_TRIGGERS]")
        for key, val in world_state.chronicle_triggers.items():
            weave_parts.append(f"{key}|{val}")
        weave_parts.append("")

    if world_state.environmental_prose:
        weave_parts.append("[ENVIRONMENTAL_PROSE]")
        for key, val in world_state.environmental_prose.items():
            weave_parts.append(f"{key}|{val}")
        weave_parts.append("")

    print("[STATUS] Weaving complete.")
    return "\n".join(weave_parts)
