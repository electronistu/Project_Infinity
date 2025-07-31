from .models import WorldState

def format_world_to_wwf(world_state: WorldState, output_path: str):
    """Serializes the WorldState object to a .wwf file with full details."""
    with open(output_path, 'w') as f:
        f.write("// WORLD-WEAVE-FILE v1.0 //\n\n")

        # --- PLAYER ---
        f.write("[PLAYER]\n")
        pc = world_state.player_character
        f.write(f"name:{pc.name}\n")
        f.write(f"level:{pc.level}\n")
        f.write(f"class:{pc.character_class}\n")
        f.write(f"race:{pc.race}\n")
        f.write(f"alignment:{pc.alignment}\n")
        f.write(f"age:{pc.age}\n")
        f.write(f"sex:{pc.sex}\n")
        stats_str = '|'.join([f"{k.upper()}:{v}" for k, v in pc.stats.dict().items()])
        f.write(f"stats:{stats_str}\n")
        f.write(f"perks:{'|'.join(pc.perks)}\n")

        # --- EQUIPMENT ---
        f.write("[EQUIPMENT]\n")
        for slot, item in pc.equipment.dict().items():
            if item:
                f.write(f"{slot}:{item['name']}\n")
        f.write("\n")

        # --- MAP ---
        f.write("[MAP]\n")
        f.write(f"size:{len(world_state.map_grid[0])}x{len(world_state.map_grid)}\n")
        f.write("grid:\n")
        for row in world_state.map_grid:
            f.write("".join(row) + "\n")
        f.write("\n")

        # --- KINGDOMS ---
        f.write("[KINGDOMS]\n")
        for kingdom in world_state.kingdoms:
            f.write("::\
")
            f.write(f"name:{kingdom.name}\n")
            f.write(f"capital:{kingdom.capital}\n")
            f.write(f"alignment:{kingdom.alignment}\n")
            relations_str = '|'.join([f"{k}:{v}" for k, v in kingdom.relations.items()])
            f.write(f"relations:{relations_str}\n")
            f.write("ruler:\n")
            f.write(f"    name:{kingdom.ruler.name}\n")
            f.write(f"    level:{kingdom.ruler.level}\n")
            f.write("locations:\n")
            for loc in kingdom.locations:
                f.write(f"    - name:{loc.name}|biome:{loc.biome}|coords:{loc.coordinates[0]},{loc.coordinates[1]}\n")
                f.write(f"      description:{loc.description}\n")
                if loc.npcs:
                    f.write(f"      npcs:{'|'.join([npc.name for npc in loc.npcs])}\n")
                if loc.creatures:
                    f.write(f"      creatures:{'|'.join([creature.name for creature in loc.creatures])}\n")
                if loc.loot:
                    f.write(f"      loot:{'|'.join([item.name for item in loc.loot])}\n")
        f.write("\n")

        # --- NPCS ---
        f.write("[NPCS]\n")
        for npc in world_state.npcs:
            f.write("::\
")
            f.write(f"name:{npc.name}\n")
            f.write(f"level:{npc.level}\n")
            f.write(f"alignment:{npc.alignment}\n")
            f.write(f"role:{npc.role}\n")
            f.write(f"faction:{npc.faction}\n")
            stats_str = '|'.join([f"{k.upper()}:{v}" for k, v in npc.stats.dict().items()])
            f.write(f"stats:{stats_str}\n")
        f.write("\n")

        # --- CREATURES ---
        f.write("[CREATURES]\n")
        for creature in world_state.creatures:
            f.write("::\
")
            f.write(f"name:{creature.name}\n")
            f.write(f"type:{creature.creature_type}\n")
            f.write(f"difficulty:{creature.difficulty}\n")
            f.write(f"xp_value:{creature.xp_value}\n")
            stats_str = '|'.join([f"{k.upper()}:{v}" for k, v in creature.stats.dict().items()])
            f.write(f"stats:{stats_str}\n")
        f.write("\n")

        # --- QUESTS ---
        f.write("[QUESTS]\n")
        for quest in world_state.quests:
            f.write("::\
")
            f.write(f"title:{quest.title}\n")
            f.write(f"description:{quest.description}\n")
            f.write(f"giver:{quest.giver}\n")
            f.write(f"reward:{quest.reward}\n")
        f.write("\n")

        # --- GUILDS ---
        f.write("[GUILDS]\n")
        for kingdom in world_state.kingdoms:
            for guild in kingdom.guilds:
                f.write("::\
")
                f.write(f"name:{guild.name}\n")
                f.write(f"leader:{guild.leader.name}\n")
                f.write(f"    abilities:{'|'.join([a.name for a in guild.leader.abilities_for_sale])}\n")
                f.write(f"right_hand:{guild.right_hand.name}\n")
                f.write(f"    abilities:{'|'.join([a.name for a in guild.right_hand.abilities_for_sale])}\n")
        f.write("\n")

        # --- TIME ---
        f.write("[TIME]\n")
        f.write(f"current_tick:{world_state.current_tick}\n\n")

        f.write("// END-OF-FILE //\n")

    print(f"World-Weave File successfully generated at: {output_path}")