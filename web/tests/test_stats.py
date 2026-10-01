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
