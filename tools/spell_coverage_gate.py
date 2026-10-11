"""The spell-coverage gate (Phase 4 of the SRD spell-mechanics verification).

Phase 1's `spell_stats_gate.py` proves the *numbers*. This gate proves the *vocabulary* is
accounted for: every shipped spell is in the frozen table, every resolution type is one the engine
knows, and every `condition` / `buffs` field a spell declares is either a real engine mechanic or a
reviewed, named entry (a pending wiring, or a GM-adjudicated tag). A new spell that invents a
condition or a buffs field fails here until it is classified on purpose.

`_meta.method` of `tools/srd/spell-stats.json` and `tools/spell_stats_gate.py` carry the numeric
side; the smoke suite `web/tests/dice/test_spell_smoke.py` runs every spell. This is the piece that
keeps the vocabulary from drifting.

Run:

    venv\\Scripts\\python.exe tools\\spell_coverage_gate.py
    venv\\Scripts\\python.exe tools\\spell_coverage_gate.py --json

Exit codes: 0 clean, 1 an unclassified spell/condition/buffs field, 2 the inputs are missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPELLS = REPO / "config" / "spells.yml"
TABLE = REPO / "tools" / "srd" / "spell-stats.json"

RESOLUTION_TYPES = {"attack_roll", "saving_throw", "automatic"}

# The 15 SRD 5.1 conditions. 13 are engine conditions (`CONDITION_EFFECTS`); `charmed` and
# `deafened` are declared in config but not yet wired -- Increment 3 of the verification plan.
ENGINE_CONDITIONS = {"blinded", "exhaustion", "frightened", "grappled", "incapacitated",
                     "invisible", "paralyzed", "petrified", "poisoned", "prone", "restrained",
                     "stunned", "unconscious"}
PENDING_CONDITIONS = {"charmed", "deafened"}
# `sickened` is an engine tag (Eyebite's third variant), not an SRD condition.
ENGINE_TAGS = {"sickened"}

# Reviewed map: every `condition` string in config/spells.yml -> the engine condition(s) it
# corresponds to ([] = a GM-adjudicated tag with no engine mechanic yet). A new string fails the
# gate until it is added here on purpose.
CONDITION_MAP = {
    # Real SRD conditions, written Title Case in config (the engine lowercases on apply).
    "Blinded": ["blinded"],
    "Frightened": ["frightened"],
    "Incapacitated": ["incapacitated"],
    "Invisible": ["invisible"],
    "Paralyzed": ["paralyzed"],
    "Prone": ["prone"],
    "Restrained": ["restrained"],
    "Stunned": ["stunned"],
    "Unconscious": ["unconscious"],
    # Aliases, compounds and GM-adjudicated tags.
    "Altered Memories": [],                                   # Modify Memory
    "Asleep, Panicked, or Sickened": ["unconscious", "frightened", "sickened"],  # Eyebite
    "Banished": [],                                           # Banishment, Plane Shift
    "Banished to Maze": [],                                   # Maze
    "Blinded or Deafened": ["blinded", "deafened"],           # Blindness/Deafness
    "Bound": [],                                              # Planar Binding
    "Charmed": ["charmed"],                                   # Animal Friendship, Charm Person
    "Charmed (Incapacitated)": ["charmed", "incapacitated"],  # Hypnotic Pattern
    "Charmed (Suggested)": ["charmed"],                       # Suggestion, Mass Suggestion
    "Commanded": [],                                          # Command
    "Confused": [],                                           # Confusion
    "Cursed": [],                                             # Bestow Curse
    "Deafened, Blinded, Stunned, or killed based on current HP": ["deafened", "blinded", "stunned"],
    "Diseased": [],                                           # Contagion
    "Dominated": ["charmed"],                                 # Dominate *
    "Feebleminded": [],                                       # Feeblemind
    "Frightened (Antipathy) or Charmed (Sympathy)": ["frightened", "charmed"],
    "Gaseous": [],                                            # Gaseous Form
    "Geased": [],                                             # Geas
    "Illusory Duplicates": [],                                # Mirror Image
    "Imprisoned": [],                                         # Imprisonment
    "Insane": [],                                             # Contact Other Plane
    "Levitating": [],                                         # Levitate
    "Magic Suppressed": [],                                   # Antimagic Field
    "Outlined": [],                                           # Faerie Fire
    "Polymorphed": [],                                        # Polymorph
    "Polymorphed (Permanent)": [],                            # True Polymorph
    "Possessed": [],                                          # Magic Jar
    "Prone and Incapacitated": ["prone", "incapacitated"],    # Hideous Laughter
    "Slowed": [],                                             # Slow
    "Spell Interrupted": [],                                  # Counterspell
    "Teleported": [],                                         # Misty Step, Dimension Door, Teleport
    "Time Stopped": [],                                       # Time Stop
    "Trapped": [],                                            # Resilient Sphere, Forcecage
    "Varies by symbol type": [],                              # Symbol
    "Weakened": [],                                           # Ray of Enfeeblement
}

# `buffs` fields the engine consumes today (in `_finalize_spell_result` via `_apply_special_buff`,
# `equipment.active_dice` / `active_advantage_for` / `active_check_bonus_for`, `speed_state`, or
# `defense_state`).
BUFFS_CONSUMED = {
    "armor_class", "speed", "hp_temporary",
    "damage_resistance", "damage_immunity", "damage_vulnerability", "condition_immunity",
    "saving_throws", "ability_checks", "attack_rolls", "damage_rolls",
    # wired 2026-10-11 (Increment 3)
    "psychic_immunity", "dexterity_saves", "wisdom_saves", "death_saves",
    "saving_throws_once", "stealth_checks", "foresight",
}
# Declared and reviewed, but with no engine consumer: action economy and divination scrying are
# GM-adjudicated (the typed note still rides `active_effects` so the GM and sheet can see it).
BUFFS_GM_ADJUDICATED = {"divination_immunity", "extra_action"}


def _load_spells() -> list[dict]:
    import yaml
    data = yaml.safe_load(SPELLS.read_text(encoding="utf-8"))
    spells = data["spells"] if isinstance(data, dict) and "spells" in data else data
    return list(spells)


def run() -> tuple[list[str], dict]:
    if not SPELLS.exists() or not TABLE.exists():
        raise FileNotFoundError("config/spells.yml or tools/srd/spell-stats.json is missing")
    spells = _load_spells()
    table = json.loads(TABLE.read_text(encoding="utf-8"))["spells"]
    issues: list[str] = []
    unknown_conditions: set[str] = set()
    unknown_buffs: set[str] = set()
    known = ENGINE_CONDITIONS | PENDING_CONDITIONS | ENGINE_TAGS
    for condition, mapped in CONDITION_MAP.items():
        for want in mapped:
            if want not in known:
                issues.append(f"CONDITION_MAP[{condition!r}] -> unknown condition {want!r}")
    for spell in spells:
        name = spell.get("name", "?")
        if name.lower() not in table:
            issues.append(f"{name}: not in the frozen SRD spell table")
        if spell.get("attack_type") not in RESOLUTION_TYPES:
            issues.append(f"{name}: unknown attack_type {spell.get('attack_type')!r}")
        condition = spell.get("condition")
        if condition and condition not in CONDITION_MAP:
            unknown_conditions.add(condition)
            issues.append(f"{name}: unclassified condition {condition!r}")
        for field in (spell.get("buffs") or {}):
            if field not in BUFFS_CONSUMED and field not in BUFFS_GM_ADJUDICATED:
                unknown_buffs.add(field)
                issues.append(f"{name}: unclassified buffs field {field!r}")
    stats = {
        "spells": len(spells),
        "conditions": len(CONDITION_MAP),
        "buffs_consumed": len(BUFFS_CONSUMED),
        "buffs_gm": len(BUFFS_GM_ADJUDICATED),
        "new_conditions": sorted(unknown_conditions),
        "new_buffs": sorted(unknown_buffs),
    }
    return issues, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="The spell-coverage gate")
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    args = ap.parse_args()
    try:
        issues, stats = run()
    except FileNotFoundError as exc:
        print(f"RED -- {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": not issues, "stats": stats, "issues": issues}, indent=2))
        return 0 if not issues else 1
    for line in issues:
        print(f"  DRIFT  {line}")
    print(f"  spells: {stats['spells']}  conditions classified: {stats['conditions']}"
          f"  buffs consumed: {stats['buffs_consumed']}"
          f"  buffs GM-adjudicated: {stats['buffs_gm']}")
    print("SPELL COVERAGE: " + ("GREEN -- every spell, condition and buffs field is accounted for"
                                if not issues else f"RED -- {len(issues)} unclassified item(s)"))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
