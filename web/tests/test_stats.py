"""Character-sheet data contract for the web client (no LLM / no network).

Locks the fix for the "[object Object]" bug: `build_stats` must return spell
lists as [{name, description}] so the browser can render them as chips with
hover tooltips, and proficiency/feature lists must stay renderable even when
the engine stored an entry as a {name, description} dict.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_stats.py
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.stats import build_stats  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def rendered_name(item):
    """Mirror app.js descTag()'s name extraction (the '[object Object]' guard)."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return item.get("name") or json.dumps(item)
    return str(item)


PAYLOAD = {
    "name": "Tester",
    "spellcasting": {
        "ability": "intelligence",
        "dc": 13,
        "attack_modifier": 5,
        "cantrips": ["Mage Hand", {"name": "Fire Bolt"}],
        "spells_known": ["Homebrew Zap"],
        "spells_prepared": [{"name": "Sleep"}, {"name": "Magic Missile"}],
        "spellbook": ["Detect Magic", {"name": "Shield", "description": "GM note: +5 AC."}],
        "slots": {"1": 3},
    },
    "skills": ["Stealth", {"name": "Arcana", "description": "GM note"}],
    "features": ["Darkvision", {"name": "Illusion Savant (Lv2)", "description": "Copying costs half."}],
    "inventory": [{"name": "Rope", "description": "50 feet"}],
    "reputation": {},
}


def main() -> bool:
    s = build_stats(PAYLOAD)
    sp = s["spellcasting"]

    for key in ("cantrips", "spells_known", "spells_prepared", "spellbook"):
        entries = sp[key]
        rec(f"{key}: all entries are dicts with a name",
            isinstance(entries, list)
            and all(isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"] for e in entries))
        rec(f"{key}: every name renders (never [object Object])",
            all(rendered_name(e) != "[object Object]" for e in entries))

    rec("prepared names preserved",
        [e["name"] for e in sp["spells_prepared"]] == ["Sleep", "Magic Missile"])
    sleep = next(e for e in sp["spells_prepared"] if e["name"] == "Sleep")
    rec("description looked up from config/spells.yml", bool(sleep["description"].strip()),
        sleep["description"][:52])
    shield = next(e for e in sp["spellbook"] if e["name"] == "Shield")
    rec("entry description wins over spells.yml", shield["description"] == "GM note: +5 AC.")

    # One list on the sheet: the spellbook with prepared entries flagged, then any
    # prepared spell the book does not hold (prepared casters have no spellbook).
    rec("spellbook merges prepared spells (book order first)",
        [e["name"] for e in sp["spellbook"]] == ["Detect Magic", "Shield", "Sleep", "Magic Missile"],
        str([e["name"] for e in sp["spellbook"]]))
    flags = {e["name"]: e["prepared"] for e in sp["spellbook"]}
    rec("book-only entries are not flagged prepared",
        flags["Detect Magic"] is False and flags["Shield"] is False, str(flags))
    rec("prepared entries are flagged (in or out of the book)",
        flags["Sleep"] is True and flags["Magic Missile"] is True, str(flags))
    rec("has_spellbook reports the character's own book", sp["has_spellbook"] is True)
    rec("spellbook item missing -> list marked unavailable",
        sp["spellbook_missing"] is True, str(sp["spellbook_missing"]))
    with_book = build_stats({**PAYLOAD, "inventory": PAYLOAD["inventory"] + [{"name": "Spellbook"}]})
    rec("spellbook item present -> list usable",
        with_book["spellcasting"]["spellbook_missing"] is False)
    prepared_only = build_stats({"spellcasting": {"spells_prepared": ["Bless"]}})
    rec("no spellbook at all -> prepared-only list, never flagged missing",
        prepared_only["spellcasting"]["has_spellbook"] is False
        and prepared_only["spellcasting"]["spellbook_missing"] is False
        and [e["name"] for e in prepared_only["spellcasting"]["spellbook"]] == ["Bless"],
        str(prepared_only["spellcasting"]))
    rec("unknown spell -> empty description", sp["spells_known"][0]["description"] == "")
    rec("mixed string/dict cantrips handled",
        [e["name"] for e in sp["cantrips"]] == ["Mage Hand", "Fire Bolt"])

    s2 = build_stats({"spellcasting": {"spells_known": None, "spellbook": "nope"}})
    rec("None / non-list spell inputs -> []",
        s2["spellcasting"]["spells_known"] == [] and s2["spellcasting"]["spellbook"] == [])

    # Proficiencies/features render as chips in app.js; both shapes must survive.
    p = s["proficiencies"]
    rec("skills keep string and dict shapes",
        [rendered_name(x) for x in p["skills"]] == ["Stealth", "Arcana"])
    rec("features keep string and dict shapes",
        rendered_name(p["features"][1]) == "Illusion Savant (Lv2)")

    # Spell slots: max comes from level_up's tables, remaining from the DB.
    def slots_for(cls, level, slots):
        d = build_stats({"character_class": cls, "level": level,
                         "spellcasting": {"slots": slots}})["spellcasting"]
        return [(x["level"], x["remaining"], x["max"]) for x in d["slot_levels"]]

    rec("wizard L3: max from the slot table",
        slots_for("Wizard", 3, {"1": 3, "2": 2}) == [(1, 3, 4), (2, 2, 2)])
    rec("warlock pact magic", slots_for("Warlock", 3, {"2": 1}) == [(2, 1, 2)])
    rec("non-caster -> no slots", slots_for("Fighter", 5, {}) == [])
    rec("all-spent level still listed",
        slots_for("Wizard", 3, {"1": 0, "2": 0}) == [(1, 0, 4), (2, 0, 2)])
    rec("unknown class falls back to remaining",
        slots_for("Homebrew", 3, {"1": 2}) == [(1, 2, 2)])

    # Config-sourced descriptions + weapon name normalization.
    cfg = build_stats({
        "name": "T", "race": "High Elf", "character_class": "Wizard", "level": 3,
        "background": "Criminal",
        "weapon_proficiencies": ["Light crossbows", "Daggers", "Simple weapons"],
        "features": ["Darkvision", "Spellcasting", {"name": "Custom", "description": "gm"}],
        "inventory": ["Dagger", {"name": "Magic Dagger", "description": "gm text"}],
    })
    ch = cfg["character"]
    rec("race tooltip from config",
        "Fey Ancestry" in ch["race_desc"] and "Elf Weapon Training" in ch["race_desc"])
    rec("class tooltip from config",
        "Hit die: d6" in ch["character_class_desc"] and "Spellcasting" in ch["character_class_desc"])
    rec("background tooltip from config", "Criminal Contact" in ch["background_desc"])
    wp = cfg["proficiencies"]["weapons"]
    rec("weapon names canonicalized",
        [w["name"] for w in wp] == ["Light Crossbow", "Dagger", "Simple weapons"])
    rec("weapon description from config", "piercing" in wp[0]["description"])
    feats = {f["name"]: f["description"] for f in cfg["proficiencies"]["features"]}
    rec("feature desc from race trait", "superior vision" in feats["Darkvision"].lower())
    rec("feature desc from class", bool(feats["Spellcasting"]))
    rec("GM feature desc wins", feats["Custom"] == "gm")
    inv = {i["name"]: i["description"] for i in cfg["inventory"]}
    rec("inventory weapon gets config desc", bool(inv["Dagger"]))
    rec("GM inventory desc is kept, stat line appended",
        inv["Magic Dagger"].startswith("gm text") and "piercing" in inv["Magic Dagger"])

    # Shared vs per-item icon keys: exact catalog/weapon match is shared; a
    # specific/modified item gets its own key (generated later at runtime).
    invi = {i["name"]: i.get("icon") for i in cfg["inventory"]}
    rec("inventory Dagger reuses the weapon icon", invi.get("Dagger") == "weapon/dagger")
    rec("inventory Magic Dagger is per-item", invi.get("Magic Dagger") == "item/magic-dagger")
    shared_inv = build_stats({"inventory": ["Spellbook"]})["inventory"]
    rec("starting item uses the shared catalog", shared_inv[0].get("icon") == "item/spellbook")
    # Fighter-style proficiencies: categories must produce keys, not text.
    fighter = build_stats({
        "armor_proficiencies": ["All armor", "Shields"],
        "weapon_proficiencies": ["Martial weapons", "Simple weapons"],
        "tool_proficiencies": ["Jeweler's Tools", "Vehicles (land)"],
    })["proficiencies"]
    f_armor = {e["name"]: e.get("icon") for e in fighter["armor"]}
    f_weapons = {e["name"]: e.get("icon") for e in fighter["weapons"]}
    f_tools = {e["name"]: e.get("icon") for e in fighter["tools"]}
    rec("fighter armor categories keyed",
        f_armor.get("All armor") == "armor/all-armor" and f_armor.get("Shields") == "armor/shield")
    rec("fighter weapon categories keyed",
        f_weapons.get("Martial weapons") == "weapon/martial-weapons"
        and f_weapons.get("Simple weapons") == "weapon/simple-weapons")
    rec("fighter vehicle tool keyed", f_tools.get("Vehicles (land)") == "tool/vehicles-land")
    cons = build_stats({"consumables": {"Potion of Healing": 2}})
    rec("Potion of Healing is a shared icon",
        cons["consumable_icons"]["Potion of Healing"] == "item/potion-of-healing")

    # Numeric/emblem stat icons.
    ab = build_stats({"stats": {"str": 16, "dex": 14}})
    rec("ability icons keyed by full name",
        ab["stats"][0]["icon"] == "ability/strength" and ab["stats"][0]["name"] == "Strength")
    sv = build_stats({"saves": ["Strength", "Wisdom"]})
    rec("saves become {name, icon}",
        sv["proficiencies"]["saves"][0] == {"name": "Strength", "icon": "save/strength"})
    cm = build_stats({"level": 4, "gold": 9, "xp": 100, "current_hit_points": 20,
                      "total_hit_points": 30, "armor_class": 15, "speed": 30})
    rec("stat icon keys present",
        cm["character"]["level_icon"] == "stat/level" and cm["combat"]["ac_icon"] == "stat/ac")
    rec("character exposes gender (for the portrait payload)",
        build_stats({"gender": "Female"})["character"]["gender"] == "Female")

    # Active effects: string entries (spells) and {name, description} dicts
    # (update_player_list stores a dict when the item text has a colon). The
    # dict shape used to crash build_stats with "unhashable type: 'dict'".
    ae = build_stats({
        "active_effects": [
            "Bless",
            {"name": "Bracers of Defense", "description": "+2 AC while unarmored."},
        ],
        "_active_buff_data": {"Bless": [{"field": "armor_class", "delta": 1},
                                         {"field": "saving_throw_bonus", "delta": 1}]},
    })["active_effects"]
    by_name = {e["name"]: e for e in ae}
    rec("active_effects: string entry keeps its buff rows",
        by_name["Bless"]["rows"] == [{"field": "armor_class", "value": "+1"},
                                      {"field": "saving_throw_bonus", "value": "+1"}])
    rec("active_effects: dict entry does not crash (unhashable dict)",
        "Bracers of Defense" in by_name)
    rec("active_effects: dict entry keeps its description",
        by_name["Bracers of Defense"]["description"] == "+2 AC while unarmored.")
    rec("active_effects: dict entry without buff data -> empty rows",
        by_name["Bracers of Defense"]["rows"] == [])

    # A dict entry whose name matches a buff still gets its rows.
    ae2 = build_stats({
        "active_effects": [{"name": "Shield", "description": "gm"}],
        "_active_buff_data": {"Shield": [{"field": "armor_class", "delta": 5}]},
    })["active_effects"]
    rec("active_effects: dict entry matched to buff_data by name",
        ae2[0]["rows"] == [{"field": "armor_class", "value": "+5"}])

    # Reputation: the engine auto-creates a faction bucket for a bare kingdom;
    # the sheet must render category/faction/entries either way.
    rep = build_stats({"reputation": {
        "others": {"misc": [{"name": "Awakened Convert", "description": "a willing pawn"}]},
    }})["reputation"]
    rec("reputation: auto-created bucket renders",
        len(rep) == 1 and rep[0]["category"] == "Others" and rep[0]["faction"] == "Misc"
        and rep[0]["entries"][0]["name"] == "Awakened Convert")

    # Equipped items (SRD 5.1): the sheet flags what is worn/wielded like prepared
    # spells, reports the hands, and carries the armour-class breakdown.
    es = build_stats({
        "name": "Tester", "character_class": "Fighter", "features": [],
        "stats": {"str": 16, "dex": 14, "con": 14},
        "armor_class": 18,
        "inventory": ["Longsword", "Chain Mail", "Shield", "Rope"],
        "equipped": {"armor": "Chain Mail", "hands": ["Longsword", "Shield"]},
    })
    inv = {e["name"]: e for e in es["inventory"]}
    rec("equipment: worn armour is flagged with its slot",
        inv["Chain Mail"]["equipped"] and inv["Chain Mail"]["slot"] == "Worn")
    rec("equipment: a held weapon is flagged with its hand",
        inv["Longsword"]["equipped"] and inv["Longsword"]["slot"] == "Main hand")
    rec("equipment: the off hand too", inv["Shield"]["slot"] == "Off hand")
    rec("equipment: carried items are not flagged",
        not inv["Rope"]["equipped"] and inv["Rope"]["slot"] is None)
    rec("equipment: hands + AC breakdown reach the sheet",
        es["equipment"]["hands_free"] == 0
        and es["combat"]["ac_breakdown"].startswith("18")
        and "Chain Mail" in es["combat"]["ac_breakdown"])
    rec("equipment: a payload without `equipped` claims no breakdown",
        build_stats({"stats": {"dex": 14}, "inventory": ["Rope"]})["combat"]["ac_breakdown"] == "")

    # Worn (non-hand) items — cloaks, rings, boots — and attunement.
    ew = build_stats({
        "character_class": "Fighter", "stats": {"str": 16, "dex": 14, "con": 14},
        "inventory": [{"name": "Cloak of Warding", "kind": "cloak", "attunement": True},
                      "Chain Mail"],
        "equipped": {"armor": "Chain Mail", "hands": [None, None], "worn": ["Cloak of Warding"]},
        "attuned": ["Cloak of Warding"], "armor_class": 16,
    })
    winv = {e["name"]: e for e in ew["inventory"]}
    rec("equipment: a worn item is flagged Worn like the armour",
        winv["Cloak of Warding"]["equipped"] and winv["Cloak of Warding"]["slot"] == "Worn")
    rec("equipment: the attuned list reaches the sheet",
        ew["equipment"]["attuned"] == ["Cloak of Warding"]
        and ew["equipment"]["attunement_slots_free"] == 2
        and ew["equipment"]["worn"][0]["name"] == "Cloak of Warding")

    # Ability-score items and engine-derived saves reach the sheet.
    eff = build_stats({
        "character_class": "Fighter", "proficiency_bonus": 2,
        "stats": {"str": 10, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8},
        "saves": ["Strength", "Constitution"],
        "inventory": [{"name": "Belt of Hill Giant Strength", "kind": "belt",
                       "set_str": 21, "attunement": True},
                      {"name": "Ring of Protection", "kind": "ring",
                       "ac_bonus": 1, "save_bonus": 1, "attunement": True}],
        "equipped": {"armor": None, "hands": [None, None],
                     "worn": ["Belt of Hill Giant Strength", "Ring of Protection"]},
        "attuned": ["Belt of Hill Giant Strength", "Ring of Protection"],
    })
    abilities = {a["key"]: a for a in eff["stats"]}
    rec("equipment: a set-score item raises the sheet's STR and names its source",
        abilities["STR"]["value"] == 21
        and abilities["STR"]["source"] == "Belt of Hill Giant Strength")
    saves = {s["key"]: s for s in eff["saves"]}
    rec("equipment: derived saves include ability + proficiency + item",
        saves["CON"]["total"] == 5 and saves["DEX"]["total"] == 3)

    # GM-declared item stats reach the inventory tooltip (declared wins; flavour text kept).
    dt = build_stats({
        "inventory": [
            {"name": "Voidfang", "base": "Dagger", "damage_dice": "1d6",
             "damage_type": "necrotic", "properties": ["Finesse", "Light"],
             "attack_bonus": 2, "damage_bonus": 1, "description": "a cold blade"},
            {"name": "Plate of the Dawn", "ac": 18, "dex_cap": 0, "strength_req": 15},
            {"name": "Ring of Warding", "kind": "ring", "ac_bonus": 1, "save_bonus": 1,
             "attunement": True},
            "Shield", "Rope",
            {"name": "the client's letter", "description": "a folded page", "weight": 0},
        ],
        "equipped": {"armor": None, "hands": [None, None], "worn": []},
    })
    dinv = {e["name"]: e["description"] for e in dt["inventory"]}
    rec("declared weapon: flavour text first, declared stats after",
        dinv["Voidfang"].startswith("a cold blade")
        and "1d6 necrotic" in dinv["Voidfang"] and "Finesse" in dinv["Voidfang"])
    rec("declared weapon: declared dice win over the Dagger base",
        "1d4" not in dinv["Voidfang"])
    rec("declared weapon: bonuses listed with a wielded qualifier",
        "+2 to attack rolls" in dinv["Voidfang"] and "+1 to damage rolls" in dinv["Voidfang"]
        and "while wielded" in dinv["Voidfang"])
    rec("declared armour: AC/DEX/Str from the declaration",
        "AC 18" in dinv["Plate of the Dawn"] and "no DEX bonus" in dinv["Plate of the Dawn"]
        and "Str 15" in dinv["Plate of the Dawn"])
    rec("declared shield shows its AC bonus", "+2 AC" in dinv["Shield"])
    rec("worn magic: kind + bonuses, qualified while attuned",
        dinv["Ring of Warding"].startswith("ring:")
        and "+1 AC" in dinv["Ring of Warding"]
        and "+1 to saving throws" in dinv["Ring of Warding"]
        and "while attuned" in dinv["Ring of Warding"])
    rec("plain loot keeps an empty tooltip", dinv["Rope"] == "")
    rec("a plain entry with only a description renders it",
        dinv["the client's letter"] == "a folded page")

    dt2 = build_stats({
        "inventory": [{"name": "Ring of Warding", "kind": "ring", "ac_bonus": 1,
                       "save_bonus": 1, "attunement": True}],
        "equipped": {"armor": None, "hands": [None, None], "worn": ["Ring of Warding"]},
        "attuned": ["Ring of Warding"],
    })
    ring2 = dt2["inventory"][0]["description"]
    rec("worn magic: no qualifier once worn and attuned",
        "+1 AC" in ring2 and "while" not in ring2)

    # Regression against the real save, if present.
    real = REPO / "output" / "electronistu.player"
    if real.exists():
        rs = build_stats(json.loads(real.read_text(encoding="utf-8")))
        all_spells = []
        for key in ("cantrips", "spells_known", "spells_prepared", "spellbook"):
            all_spells += rs["spellcasting"].get(key, [])
        rec("real save: no spell renders as [object Object]",
            all(rendered_name(e) != "[object Object]" for e in all_spells),
            f"{len(all_spells)} spells, e.g. {all_spells[0]['name'] if all_spells else '-'}")

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
