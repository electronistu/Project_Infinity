# forge/class_spells.py
# Full SRD 5.1 class spell lists — used for interactive spell selection in the World Forge

from pathlib import Path

CLASS_CANTRIPS = {
    "Bard": ["Dancing Lights", "Light", "Mage Hand", "Mending", "Message", "Minor Illusion", "Prestidigitation",
             "True Strike", "Vicious Mockery"],
    "Cleric": ["Guidance", "Light", "Mending", "Resistance", "Sacred Flame", "Spare the Dying", "Thaumaturgy"],
    "Druid": ["Druidcraft", "Guidance", "Mending", "Poison Spray", "Produce Flame", "Resistance", "Shillelagh"],
    "Sorcerer": ["Acid Splash", "Chill Touch", "Dancing Lights", "Fire Bolt", "Light",
                 "Mage Hand", "Mending", "Message", "Minor Illusion", "Poison Spray", "Prestidigitation",
                 "Ray of Frost", "Shocking Grasp", "True Strike"],
    "Warlock": ["Chill Touch", "Eldritch Blast", "Mage Hand", "Minor Illusion",
                "Poison Spray", "Prestidigitation", "True Strike"],
    "Wizard": ["Acid Splash", "Chill Touch", "Dancing Lights", "Fire Bolt", "Light",
               "Mage Hand", "Mending", "Message", "Minor Illusion", "Poison Spray", "Prestidigitation",
               "Ray of Frost", "Shocking Grasp", "True Strike"],
}

LEVEL_1_SPELLS = {
    "Bard": ["Animal Friendship", "Bane", "Charm Person", "Comprehend Languages", "Cure Wounds", "Detect Magic",
             "Disguise Self", "Faerie Fire", "Healing Word", "Heroism", "Identify", "Illusory Script",
             "Longstrider", "Silent Image", "Sleep", "Speak with Animals", "Hideous Laughter",
             "Thunderwave", "Unseen Servant"],
    "Cleric": ["Bane", "Bless", "Command", "Create or Destroy Water", "Cure Wounds", "Detect Evil and Good",
               "Detect Magic", "Detect Poison and Disease", "Guiding Bolt", "Healing Word", "Inflict Wounds",
               "Protection from Evil and Good", "Purify Food and Drink", "Sanctuary", "Shield of Faith"],
    "Druid": ["Animal Friendship", "Charm Person", "Create or Destroy Water", "Cure Wounds", "Detect Magic",
              "Detect Poison and Disease", "Entangle", "Faerie Fire", "Fog Cloud", "Goodberry", "Healing Word",
              "Jump", "Longstrider", "Purify Food and Drink", "Speak with Animals", "Thunderwave"],
    "Paladin": ["Bless", "Command", "Cure Wounds", "Detect Evil and Good", "Detect Magic", "Detect Poison and Disease",
                "Divine Favor", "Heroism", "Protection from Evil and Good", "Purify Food and Drink", "Shield of Faith"],
    "Ranger": ["Alarm", "Animal Friendship", "Cure Wounds", "Detect Magic", "Detect Poison and Disease", "Fog Cloud",
               "Goodberry", "Hunter's Mark", "Jump", "Longstrider", "Speak with Animals"],
    "Sorcerer": ["Burning Hands", "Charm Person", "Color Spray", "Comprehend Languages", "Detect Magic",
                  "Disguise Self", "Expeditious Retreat", "False Life", "Feather Fall", "Fog Cloud", "Jump",
                  "Mage Armor", "Magic Missile", "Shield", "Silent Image", "Sleep", "Hideous Laughter",
                  "Thunderwave"],
    "Warlock": ["Charm Person", "Comprehend Languages", "Expeditious Retreat",
                 "Hellish Rebuke", "Illusory Script", "Protection from Evil and Good",
                 "Hideous Laughter", "Unseen Servant"],
    "Wizard": ["Alarm", "Burning Hands", "Charm Person", "Color Spray", "Comprehend Languages", "Detect Magic",
               "Disguise Self", "Expeditious Retreat", "False Life", "Feather Fall", "Find Familiar", "Floating Disk",
               "Fog Cloud", "Grease", "Identify", "Illusory Script", "Jump", "Longstrider", "Mage Armor",
               "Magic Missile", "Protection from Evil and Good", "Shield", "Silent Image", "Sleep",
               "Hideous Laughter", "Thunderwave", "Unseen Servant"],
}

CANTRIP_COUNTS = {
    "Bard": 2, "Cleric": 3, "Druid": 2, "Sorcerer": 4,
    "Warlock": 2, "Wizard": 3,
}

KNOWN_SPELL_COUNTS = {
    "Bard": 4, "Sorcerer": 2, "Warlock": 2, "Ranger": 0,
}

PREPARED_SPELL_COUNTS_BASE = {
    "Cleric": 1, "Druid": 1, "Paladin": 0, "Wizard": 1,
}


def _filter_available(spell_list: list, available_spells: set) -> list:
    return sorted([s for s in spell_list if s in available_spells])


def get_available_cantrips(class_name: str, available_spell_names: set) -> list:
    full = CLASS_CANTRIPS.get(class_name, [])
    return _filter_available(full, available_spell_names)


def get_available_level1_spells(class_name: str, available_spell_names: set) -> list:
    full = LEVEL_1_SPELLS.get(class_name, [])
    return _filter_available(full, available_spell_names)


# ── SRD domain / oath / circle spells (always prepared) ─────────────────────────
# Data: config/subclasses.yml. The SRD carries one subclass per class, so there is no
# subclass *choice* -- only the Circle of the Land chooses a terrain. These spells are
# always prepared and never count against the number the caster can prepare.

_SUBCLASSES = None


def load_subclasses() -> list:
    global _SUBCLASSES
    if _SUBCLASSES is None:
        import yaml
        path = Path(__file__).resolve().parent.parent / "config" / "subclasses.yml"
        _SUBCLASSES = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return _SUBCLASSES


def subclass_for(class_name: str):
    """The subclass entry that grants this class its always-prepared spells, or None."""
    for s in load_subclasses():
        if s.get("class") == class_name:
            return s
    return None


def get_circle_terrains() -> list:
    """The Circle of the Land terrains (empty for everything else)."""
    s = subclass_for("Druid")
    return list((s or {}).get("always_prepared_by_terrain") or {})


def get_subclass_name(class_name: str, terrain: str = "") -> str:
    """The SRD subclass name, with the terrain appended for the Circle of the Land."""
    s = subclass_for(class_name)
    if not s:
        return ""
    name = str(s.get("name") or "")
    if s.get("terrain") and terrain:
        return f"{name} ({terrain})"
    return name


def get_always_prepared(class_name: str, level: int, terrain: str = "") -> list:
    """Every always-prepared spell granted at or below `level` (in tier order, deduped)."""
    s = subclass_for(class_name)
    if not s:
        return []
    table = s.get("always_prepared") or {}
    if s.get("terrain"):
        table = (s.get("always_prepared_by_terrain") or {}).get(terrain) or {}
    out: list = []
    for tier in sorted(table, key=lambda k: int(k)):
        if int(tier) > int(level or 1):
            continue
        for spell in table[tier]:
            if spell not in out:
                out.append(spell)
    return out
