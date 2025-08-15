# forge/formatter.py
# Version 2.0 - Full D&D 5e Compliance

from .models import WorldState, Stats, Item, Action, SpecialAbility, Skill

# --- Helper Functions for complex formatting ---

def format_stats(stats: Stats) -> str:
    return '|'.join([f"{k.upper()}:{v}" for k, v in stats.dict().items()])

def format_list_of_objects(obj_list: list) -> str:
    return '|'.join([obj.name for obj in obj_list])

def format_skills(skills: list[Skill]) -> str:
    return '|'.join([f"{s.name} ({s.ability[:3].upper()}){'[P]' if s.proficient else ''}" for s in skills])

def format_actions(actions: list[Action], action_type: str) -> str:
    action_str = f"  {action_type}:\n"
    for action in actions:
        action_str += f"    - name:{action.name}|desc:{action.description}\n"
    return action_str

def format_special_abilities(abilities: list[SpecialAbility]) -> str:
    ability_str = "  special_abilities:\n"
    for ability in abilities:
        usage = f"|usage:{ability.usage}" if ability.usage else ""
        ability_str += f"    - name:{ability.name}|desc:{ability.description}{usage}\n"
    return ability_str

# --- Main Formatter ---

def format_world_to_wwf(world_state: WorldState, output_path: str):
    """Serializes the new 5e-compliant WorldState object to a .wwf file."""
    with open(output_path, 'w') as f:
        f.write("// WORLD-WEAVE-FILE v2.0 //\n\n")

        # --- PLAYER ---
        f.write("[PLAYER]\n")
        pc = world_state.player_character
        f.write(f"name:{pc.name}\n")
        f.write(f"level:{pc.level}|xp:{pc.xp}|gold:{pc.gold}\n")
        f.write(f"class:{pc.character_class}|background:{pc.background}\n")
        f.write(f"race:{pc.race}|alignment:{pc.alignment}\n")
        f.write(f"ac:{pc.armor_class}|hp:{pc.hit_points}|speed:{pc.speed}\n")
        f.write(f"stats:{format_stats(pc.stats)}\n")
        f.write(f"prof_bonus:{pc.proficiency_bonus}\n")
        f.write(f"armor_proficiencies:{'|'.join(pc.armor_proficiencies)}\n")
        f.write(f"weapon_proficiencies:{'|'.join(pc.weapon_proficiencies)}\n")
        f.write(f"tool_proficiencies:{'|'.join(pc.tool_proficiencies)}\n")
        f.write(f"skills:{format_skills(pc.skills)}\n")
        f.write(f"saving_throws:{format_skills(pc.saving_throws)}\n")
        f.write(f"languages:{'|'.join(pc.languages)}\n")
        if pc.spellcasting_ability:
            f.write(f"spellcasting_ability:{pc.spellcasting_ability}\n")
            f.write(f"spell_save_dc:{pc.spell_save_dc}\n")
            f.write(f"spell_attack_modifier:{pc.spell_attack_modifier}\n")
            f.write(f"cantrips_known:{'|'.join(pc.cantrips_known)}\n")
            f.write(f"spells_known:{'|'.join(pc.spells_known)}\n")
            f.write(f"spell_slots:{'|'.join([f'{k}:{v}' for k, v in pc.spell_slots.items()])}\n")
        if pc.features_and_traits:
            f.write(format_special_abilities(pc.features_and_traits))
        f.write("\n")

        # --- EQUIPMENT ---
        f.write("[EQUIPMENT]\n")
        if pc.equipment.main_hand:
            f.write(f"main_hand:{pc.equipment.main_hand.name}\n")
        if pc.equipment.off_hand:
            f.write(f"off_hand:{pc.equipment.off_hand.name}\n")
        
        # Write all items in inventory
        if pc.equipment.inventory:
            f.write("inventory:\n")
            for item in pc.equipment.inventory:
                f.write(f"  - name:{item.name}|type:{item.item_type}|desc:{item.description}\n")
        f.write("\n")

        # --- MAP & TIME ---
        f.write("[MAP]\n")
        f.write(f"size:{len(world_state.map_grid[0])}x{len(world_state.map_grid)}\n")
        f.write("grid:\n")
        for row in world_state.map_grid:
            f.write("".join(row) + "\n")
        f.write("\n")
        f.write(f"[TIME]\ncurrent_tick:{world_state.current_tick}\n\n")

        # --- WORLD HISTORY (L.I.C.) ---
        if world_state.world_history:
            f.write("[HISTORY]\n")
            for entry in world_state.world_history:
                f.write(f"- {entry}\n")
            f.write("\n")

        # --- KINGDOMS --- (Summary view)
        f.write("[KINGDOMS]\n")
        for kingdom in world_state.kingdoms:
            f.write("::\n")
            f.write(f"name:{kingdom.name}|capital:{kingdom.capital}|alignment:{kingdom.alignment}\n")
            relations_str = '|'.join([f"{k}:{v}" for k, v in kingdom.relations.items()])
            f.write(f"relations:{relations_str}\n")

            # --- RULER ---
            f.write("  RULER:\n")
            f.write(f"    name:{kingdom.ruler.name}|role:{kingdom.ruler.role}|faction:{kingdom.ruler.faction}{'|is_walker:True' if kingdom.ruler.is_walker else ''}\n")
            f.write(f"    level:{kingdom.ruler.level}|cr:{kingdom.ruler.challenge_rating}|xp:{kingdom.ruler.xp_value}\n")
            f.write(f"    ac:{kingdom.ruler.armor_class}|hp:{kingdom.ruler.hit_points}|speed:{kingdom.ruler.speed}\n")
            f.write(f"    stats:{format_stats(kingdom.ruler.stats)}\n")
            if kingdom.ruler.actions:
                f.write(format_actions(kingdom.ruler.actions, '    actions'))
            if kingdom.ruler.special_abilities:
                f.write(format_special_abilities(kingdom.ruler.special_abilities))
            
            # --- GUILDS within Kingdom ---
            if kingdom.guilds:
                f.write("  [GUILDS]\n")
                for guild in kingdom.guilds:
                    f.write(f"    ::name:{guild.name}\n")
                    if guild.reports_to:
                        f.write(f"      reports_to:{guild.reports_to}\n")
                    
                    # Format Leader
                    f.write("    LEADER:\n")
                    f.write(f"      name:{guild.leader.name}|role:{guild.leader.role}{f'|faction:{guild.leader.faction}' if guild.leader.faction != 'Civilian' else ''}{'|is_walker:True' if guild.leader.is_walker else ''}\n")
                    f.write(f"      level:{guild.leader.level}|cr:{guild.leader.challenge_rating}|xp:{guild.leader.xp_value}\n")
                    f.write(f"      ac:{guild.leader.armor_class}|hp:{guild.leader.hit_points}|speed:{guild.leader.speed}\n")
                    f.write(f"      stats:{format_stats(guild.leader.stats)}\n")
                    if guild.leader.abilities_for_sale:
                        f.write(f"      abilities_for_sale:{'|'.join([f'{a.name}:{a.tier}:{a.guild_source}' for a in guild.leader.abilities_for_sale])}\n")
                    if guild.leader.actions:
                        f.write(format_actions(guild.leader.actions, '      actions'))
                    if guild.leader.special_abilities:
                        f.write(format_special_abilities(guild.leader.special_abilities))

                    # Format Right Hand
                    f.write("    RIGHT_HAND:\n")
                    f.write(f"      name:{guild.right_hand.name}|role:{guild.right_hand.role}{f'|faction:{guild.right_hand.faction}' if guild.right_hand.faction != 'Civilian' else ''}{'|is_walker:True' if guild.right_hand.is_walker else ''}\n")
                    f.write(f"      level:{guild.right_hand.level}|cr:{guild.right_hand.challenge_rating}|xp:{guild.right_hand.xp_value}\n")
                    f.write(f"      ac:{guild.right_hand.armor_class}|hp:{guild.right_hand.hit_points}|speed:{guild.right_hand.speed}\n")
                    f.write(f"      stats:{format_stats(guild.right_hand.stats)}\n")
                    if guild.right_hand.abilities_for_sale:
                        f.write(f"      abilities_for_sale:{'|'.join([f'{a.name}:{a.tier}:{a.guild_source}' for a in guild.right_hand.abilities_for_sale])}\n")
                    if guild.right_hand.actions:
                        f.write(format_actions(guild.right_hand.actions, '      actions'))
                    if guild.right_hand.special_abilities:
                        f.write(format_special_abilities(guild.right_hand.special_abilities))

                    # Format Members
                    # Format Members (Removed for brevity)
                    # if guild.members:
                    #     f.write("    MEMBERS:\n")
                    #     for member in guild.members:
                    #         f.write(f"      - name:{member.name}|role:{member.role}{f'|faction:{member.faction}' if member.faction != 'Civilian' else ''}{'|is_walker:True' if member.is_walker else ''}\n")
                    #         f.write(f"        level:{member.level}|cr:{member.challenge_rating}|xp:{member.xp_value}\n")
                    #         f.write(f"        ac:{member.armor_class}|hp:{member.hit_points}|speed:{member.speed}\n")
                    #         f.write(f"        stats:{format_stats(member.stats)}\n")
                    #         if member.actions:
                    #             f.write(format_actions(member.actions, '        actions'))
                    #         if member.special_abilities:
                    #             f.write(format_special_abilities(member.special_abilities))
        f.write("\n")
        
        

        f.write("// END-OF-FILE //\n")

    print(f"World-Weave File successfully generated at: {output_path}")