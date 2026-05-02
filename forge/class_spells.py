# forge/class_spells.py
# Full SRD 5.1 class spell lists — used for interactive spell selection in the World Forge

CLASS_CANTRIPS = {
    "Bard": ["Dancing Lights", "Light", "Mage Hand", "Mending", "Message", "Minor Illusion", "Prestidigitation",
             "True Strike", "Vicious Mockery"],
    "Cleric": ["Guidance", "Light", "Mending", "Resistance", "Sacred Flame", "Spare the Dying", "Thaumaturgy"],
    "Druid": ["Druidcraft", "Guidance", "Mending", "Poison Spray", "Produce Flame", "Resistance", "Shillelagh",
              "Thorn Whip"],
    "Sorcerer": ["Acid Splash", "Blade Ward", "Chill Touch", "Dancing Lights", "Fire Bolt", "Friends", "Light",
                 "Mage Hand", "Mending", "Message", "Minor Illusion", "Poison Spray", "Prestidigitation",
                 "Ray of Frost", "Shocking Grasp", "True Strike"],
    "Warlock": ["Blade Ward", "Chill Touch", "Eldritch Blast", "Friends", "Mage Hand", "Minor Illusion",
                "Poison Spray", "Prestidigitation", "True Strike"],
    "Wizard": ["Acid Splash", "Blade Ward", "Chill Touch", "Dancing Lights", "Fire Bolt", "Friends", "Light",
               "Mage Hand", "Mending", "Message", "Minor Illusion", "Poison Spray", "Prestidigitation",
               "Ray of Frost", "Shocking Grasp", "True Strike"],
}

LEVEL_1_SPELLS = {
    "Bard": ["Animal Friendship", "Bane", "Charm Person", "Comprehend Languages", "Cure Wounds", "Detect Magic",
             "Disguise Self", "Faerie Fire", "Healing Word", "Heroism", "Identify", "Illusory Script",
             "Longstrider", "Silent Image", "Sleep", "Speak with Animals", "Tasha's Hideous Laughter",
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
                  "Mage Armor", "Magic Missile", "Shield", "Silent Image", "Sleep", "Tasha's Hideous Laughter",
                  "Thunderwave", "Witch Bolt"],
    "Warlock": ["Armor of Agathys", "Arms of Hadar", "Charm Person", "Comprehend Languages", "Expeditious Retreat",
                 "Hellish Rebuke", "Hex", "Illusory Script", "Protection from Evil and Good",
                 "Tasha's Hideous Laughter", "Unseen Servant", "Witch Bolt"],
    "Wizard": ["Alarm", "Burning Hands", "Charm Person", "Color Spray", "Comprehend Languages", "Detect Magic",
               "Disguise Self", "Expeditious Retreat", "False Life", "Feather Fall", "Find Familiar", "Floating Disk",
               "Fog Cloud", "Grease", "Identify", "Illusory Script", "Jump", "Longstrider", "Mage Armor",
               "Magic Missile", "Protection from Evil and Good", "Shield", "Silent Image", "Sleep",
               "Tasha's Hideous Laughter", "Thunderwave", "Unseen Servant", "Witch Bolt"],
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
