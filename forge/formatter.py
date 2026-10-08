# forge/formatter.py
# Player file (`.player`) writer. The character carries the game it plays and, in the era
# game, the era it is in; the world scaffold itself is `config/world.yml` or `config/eras/`
# and is injected by the engine. The reputation map is seeded from the world or the era it
# starts in -- `build_reputation` or `web.eras.era_reputation_seed`.


import json


def build_reputation(kingdoms) -> dict:
    """The classic game's empty reputation map: one bucket per kingdom and guild.

    The keys here are the ones the GM will later write to (`reputation.<kingdom>.<faction>`),
    so they have to match `config/world.yml` exactly -- which is why this is rebuilt from the
    world rather than kept anywhere.
    """
    KNOWN_FACTIONS = {"Guard", "Mage", "Assassin", "Merchant", "Thief", "Alchemist's League",
                      "Ranger's Conclave", "Order of Scribes"}
    rep: dict = {}
    for kingdom in kingdoms:
        kname = kingdom.name.lower()
        rep[kname] = {"ruler": []}
        for guild in kingdom.guilds:
            for faction in KNOWN_FACTIONS:
                if guild.name.endswith(faction):
                    rep[kname][faction.lower().replace("'", "")] = []
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


def get_player_json(pc, reputation=None, era=None, arrival=None, mode=None) -> str:
    player_data = {
        "name": pc.name,
        "level": pc.level,
        "mode": mode or getattr(pc, "mode", "") or "",
        "era": era or "",
        "arrival": arrival or "",
        "xp": pc.xp,
        "gold": pc.gold,
        "character_class": pc.character_class,
        "race": pc.race,
        "background": pc.background,
        "alignment": pc.alignment,
        "gender": pc.gender,
        "age": getattr(pc, "age", 30),
        "difficulty": getattr(pc, "difficulty", "hard") or "hard",
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
        "reputation": reputation or {},
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
