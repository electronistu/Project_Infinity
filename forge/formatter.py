# forge/formatter.py
# Player file (`.player`) writer. The world scaffold is static (config/world.yml)
# and injected by the engine, so no WWF file is generated any more.


import json


def _build_reputation(kingdoms) -> dict:
    """Builds an empty reputation dictionary with all kingdom/faction entries as empty lists."""
    KNOWN_FACTIONS = {"Guard", "Mage", "Assassin", "Merchant", "Thief", "Alchemist's League",
                       "Ranger's Conclave", "Order of Scribes"}
    rep = {}
    for kingdom in kingdoms:
        kname = kingdom.name.lower()
        rep[kname] = {}
        rep[kname]["ruler"] = []
        for guild in kingdom.guilds:
            gname = guild.name
            for faction in KNOWN_FACTIONS:
                if gname.endswith(faction):
                    key = faction.lower().replace("'", "")
                    rep[kname][key] = []
                    break
    rep["others"] = {}
    return rep


def _player_item(item):
    """One inventory entry for the `.player` file: a bare name when plain, else a dict
    carrying the description and (for homebrew/magic gear) its declared `base`."""
    base = getattr(item, "base", None)
    if not item.description and not base:
        return item.name
    entry = {"name": item.name, "description": item.description}
    if base:
        entry["base"] = base
    return entry


def get_player_json(pc, kingdoms=None) -> str:
    player_data = {
        "name": pc.name,
        "level": pc.level,
        "xp": pc.xp,
        "gold": pc.gold,
        "character_class": pc.character_class,
        "race": pc.race,
        "background": pc.background,
        "alignment": pc.alignment,
        "gender": pc.gender,
        "armor_class": pc.armor_class,
        "current_hit_points": pc.current_hit_points,
        "total_hit_points": pc.total_hit_points,
        "hit_dice_count": pc.hit_dice_count,
        "hit_dice_size": pc.hit_dice_size,
        "speed": pc.speed,
        "stats": {
            "str": pc.stats.strength,
            "dex": pc.stats.dexterity,
            "con": pc.stats.constitution,
            "int": pc.stats.intelligence,
            "wis": pc.stats.wisdom,
            "cha": pc.stats.charisma
        },
        "proficiency_bonus": pc.proficiency_bonus,
        "skills": [s.name for s in pc.skills if s.proficient],
        "expertise": list(getattr(pc, "expertise", []) or []),
        "saves": [s.name for s in pc.saving_throws if s.proficient],
        "armor_proficiencies": pc.armor_proficiencies,
        "weapon_proficiencies": pc.weapon_proficiencies,
        "tool_proficiencies": pc.tool_proficiencies,
        "languages": pc.languages,
        "features": [f.name for f in pc.features_and_traits],
        "inventory": [
            _player_item(item)
            for item in pc.equipment.inventory if item.item_type not in ("ammunition", "consumable")
        ],
        "equipped": getattr(pc, "equipped", {}) or {},
        "consumables": pc.consumables if pc.consumables else {},
        "reputation": _build_reputation(kingdoms) if kingdoms else {},
    }
    if pc.spellcasting_ability:
        spell_data = {
            "ability": pc.spellcasting_ability,
            "dc": pc.spell_save_dc,
            "attack_modifier": pc.spell_attack_modifier,
            "cantrips": pc.cantrips_known,
            "slots": pc.spell_slots
        }
        if pc.character_class == "Wizard":
            spell_data["spellbook"] = pc.spellbook
            spell_data["spells_prepared"] = pc.spells_prepared
        elif pc.character_class in ("Cleric", "Druid", "Paladin"):
            spell_data["spells_prepared"] = pc.spells_prepared
        else:
            spell_data["spells_known"] = pc.spells_known
        player_data["spellcasting"] = spell_data
    return json.dumps(player_data, indent=2)
