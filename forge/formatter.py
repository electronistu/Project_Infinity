# forge/formatter.py
# Version 4.0 - Schema-Driven Compact Formatter

from .models import WorldState, Stats, NPC
import json

WWF_SCHEMA = """
schemas:
  npc: [name, lvl, race, class, ac, hp, stats, walker, abilities_for_sale]
  stats: [str, dex, con, int, wis, cha]
"""

def get_npc_array(npc: NPC) -> list:
    """Converts an NPC object to a compact array based on the schema."""
    stats_array = [npc.stats.strength, npc.stats.dexterity, npc.stats.constitution, npc.stats.intelligence, npc.stats.wisdom, npc.stats.charisma]
    abilities = [f"{a.name}:{a.tier}" for a in npc.abilities_for_sale] if npc.abilities_for_sale else None
    return [npc.name, npc.level, npc.race, npc.character_class, npc.armor_class, npc.hit_points, stats_array, True if npc.is_walker else None, abilities]

def format_world_to_wwf(world_state: WorldState, output_path: str):
    """Serializes the WorldState to a schema-driven compact .wwf file."""
    output = []
    output.append("// WWF v4.0 //")
    output.append("// SCHEMA-DRIVEN COMPACT FORMAT //\n")
    output.append(WWF_SCHEMA)
    output.append("\n---\n")

    # --- Player ---
    pc = world_state.player_character
    output.append("player:")
    output.append(f"  name: {pc.name}")
    output.append(f"  lvl: {pc.level}")
    output.append(f"  xp: {pc.xp}")
    output.append(f"  gold: {pc.gold}")
    output.append(f"  class: {pc.character_class}")
    output.append(f"  race: {pc.race}")
    output.append(f"  background: {pc.background}")
    output.append(f"  align: {pc.alignment}")
    output.append(f"  ac: {pc.armor_class}")
    output.append(f"  hp: {pc.hit_points}")
    output.append(f"  speed: {pc.speed}")
    output.append(f"  stats: {{str:{pc.stats.strength},dex:{pc.stats.dexterity},con:{pc.stats.constitution},int:{pc.stats.intelligence},wis:{pc.stats.wisdom},cha:{pc.stats.charisma}}}")
    output.append(f"  prof_bonus: {pc.proficiency_bonus}")
    output.append("  prof:")
    output.append(f"    skills: {json.dumps([s.name for s in pc.skills if s.proficient])}")
    output.append(f"    saves: {json.dumps([s.name for s in pc.saving_throws if s.proficient])}")
    output.append(f"  features: {json.dumps([f.name for f in pc.features_and_traits])}")
    output.append(f"  inventory: {json.dumps([item.name for item in pc.equipment.inventory])}")
    if pc.spellcasting_ability:
        output.append("  spellcasting:")
        output.append(f"    ability: {pc.spellcasting_ability}")
        output.append(f"    dc: {pc.spell_save_dc}")
        output.append(f"    atk: {pc.spell_attack_modifier}")
        output.append(f"    cantrips: {json.dumps(pc.cantrips_known)}")
        output.append(f"    spells: {json.dumps(pc.spells_known)}")
        output.append(f"    slots: {json.dumps(pc.spell_slots)}")

    # --- Map, Time, History ---
    output.append("map:")
    output.append(f"  size: {len(world_state.map_grid[0])}x{len(world_state.map_grid)}")
    output.append("  grid: |\n    " + "\n    ".join("".join(row) for row in world_state.map_grid))
    output.append(f"time: {world_state.current_tick}")
    output.append("history:")
    for entry in world_state.world_history:
        output.append(f"  - {entry}")

    # --- Kingdoms ---
    output.append("kingdoms:")
    for k in world_state.kingdoms:
        output.append(f"  - name: {k.name}")
        output.append(f"    capital: {k.capital}")
        output.append(f"    align: {k.alignment}")
        output.append(f"    relations: {json.dumps(k.relations)}")
        output.append(f"    ruler: {json.dumps(get_npc_array(k.ruler))}")
        output.append("    guilds:")
        for g in k.guilds:
            output.append(f"      - name: {g.name}")
            if g.reports_to:
                output.append(f"        reports_to: {g.reports_to}")
            output.append(f"        leader: {json.dumps(get_npc_array(g.leader))}")
            output.append(f"        right_hand: {json.dumps(get_npc_array(g.right_hand))}")

    with open(output_path, 'w') as f:
        f.write("\n".join(output))

    print(f"Schema-driven World-Weave File successfully generated at: {output_path}")
