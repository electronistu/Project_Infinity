"""The SRD 5.1 spell-stat gate: `config/spells.yml` against a frozen stat table.

`tools/srd_gate.py` proves the names. This gate proves the *mechanics* of the spells we ship:
level, resolution (attack roll vs save), save ability and half-on-save, base damage dice/type,
and the per-slot scaling (`higher_levels`). It is what would have caught Chain Lightning declared
as a 10d6 attack-roll spell instead of the SRD's 10d8 Dexterity save.

The comparison set is `tools/srd/spell-stats.json`, frozen by `tools/srd/build_spell_stats.py`
and never fetched here. Checks are deliberately conservative (only where the source data is
unambiguous) so that the gate reports real drift, not API gaps. The reviewed `EXEMPT` map below
records every remaining deliberate deviation and why.

Run:

    venv\\Scripts\\python.exe tools\\spell_stats_gate.py
    venv\\Scripts\\python.exe tools\\spell_stats_gate.py --json

Exit codes: 0 clean, 1 drift found, 2 the table is missing/invalid.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TABLE = REPO / "tools" / "srd" / "spell-stats.json"
SPELLS = REPO / "config" / "spells.yml"
_NAMES = REPO / "tools" / "srd" / "srd-5.1.json"

_DICE = re.compile(r"^\s*(\d+)d(\d+)\s*$")
_UPCAST = re.compile(r"^\+?\d+d\d+$")

# Reviewed, permanent deviations from the frozen table. Keyed by the shipped config name; the
# value is why the engine models it differently.
EXEMPT = {
    "Ray of Enfeeblement": "post-hit save: a ranged spell ATTACK, then a CON save on a hit",
    "Contagion": "post-hit save plus an end-of-turn disease ladder (3 successes/failures)",
    "Control Water": "multi-effect spell (flood/redirect/part water); only some effects save",
    "Hallow": "multi-effect ward; only the Fear variant forces a Charisma save",
    "Light": "dnd5eapi records a DEX save for the SRD 5.1 Light cantrip, which has none",
    "Storm of Vengeance": "multi-round effect with a per-round CON save, modelled round by round",
}

# Reviewed exceptions to the classification check only (the spell is still checked for every
# other field). Keyed by shipped name; the value is why the config's shape differs from the
# source's.
CLASSIFY_EXEMPT = {
    "Aid": "SRD grants +5 max/current HP; the engine models Aid as a narrative no-op and the "
           "HP increase is GM-adjudicated (C2a)",
    "Goodberry": "the 1 HP is eaten from a berry later, not restored on cast; the engine does "
                 "not model the berry consumable",
}

# dnd5eapi omits the higher-level entries for these spells -- its slot map has a single level -- so
# the `upcast` signature cannot be derived. The values were reviewed against the SRD 5.1 text (and
# cross-checked against the 5etools PHB data); the gate asserts them exactly, which is how a
# *fabricated* upcast is caught (eight rows used to grant damage the SRD never gives). "none" means
# the SRD grants no damage upcast at all.
UPCAST_EXPECTED = {
    "Sleep": "+2d8",
    "Wall of Fire": "+1d8",
    "Freezing Sphere": "+1d6",
    "Arcane Hand": "+2d8",
    "Color Spray": "+2d10",
    "Cure Wounds": "+1d8",
    "Disintegrate": "+3d6",
    "Glyph of Warding": "+1d8",
    "Healing Word": "+1d4",
    "Ice Storm": "+1d8",
    "Mass Cure Wounds": "+1d8",
    "Mass Healing Word": "+1d4",
    "Prayer of Healing": "+1d8",
    "Spirit Guardians": "+1d8",
    "Spiritual Weapon": "+1d8",
    "Flame Strike": "+1d6",
    "Arcane Sword": "none",
    "Blade Barrier": "none",
    "Finger of Death": "none",
    "Fire Storm": "none",
    "Harm": "none",
    "Sunbeam": "none",
    "Sunburst": "none",
    "Symbol": "none",
}


def _load_yaml(path: Path):
    try:
        import yaml
    except ImportError:  # pragma: no cover - the repo always has pyyaml
        print("RED -- pyyaml is required", file=sys.stderr)
        raise SystemExit(2)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _aliases() -> dict[str, str]:
    if not _NAMES.exists():
        return {}
    data = json.loads(_NAMES.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for canonical, value in (data.get("spell_aliases") or {}).items():
        for alt in (value if isinstance(value, list) else [value]):
            out[str(alt).lower()] = str(canonical).lower()
    return out


def run() -> tuple[list[str], int]:
    if not TABLE.exists():
        print(f"RED -- {TABLE.name} is missing; run tools/srd/build_spell_stats.py", file=sys.stderr)
        raise SystemExit(2)
    table = json.loads(TABLE.read_text(encoding="utf-8")).get("spells") or {}
    spells = _load_yaml(SPELLS) or []
    aliases = _aliases()

    def srd_for(name: str):
        key = name.lower()
        return table.get(key) or table.get(aliases.get(key, ""))

    issues: list[str] = []
    checked = 0
    for spell in spells:
        if not isinstance(spell, dict):
            continue
        name = spell.get("name")
        if not name:
            continue
        entry = srd_for(name)
        if entry is None:
            issues.append(f"{name}: not in the frozen SRD table")
            continue
        checked += 1
        exempt = name in EXEMPT
        if not exempt and spell.get("level") != entry.get("level"):
            issues.append(f"{name}: level {spell.get('level')} != SRD {entry.get('level')}")
        # ── Classification (frozen 2026-10-11): school, concentration, ritual, healing ──
        # `duration`, `higher_level`, `components`, `range` and `casting_time` are frozen in the
        # table for reference but NOT asserted: the config deliberately carries richer duration
        # prose, cantrip scaling is not in the source's `higher_level`, and material components
        # live in config/components.yml.
        if not exempt and name not in CLASSIFY_EXEMPT:
            want_school = entry.get("school")
            if want_school and str(spell.get("school") or "").strip().lower() != want_school:
                issues.append(f"{name}: school {spell.get('school')!r} != SRD {want_school!r}")
            if bool(spell.get("requires_concentration")) != bool(entry.get("concentration")):
                issues.append(
                    f"{name}: requires_concentration {bool(spell.get('requires_concentration'))!r}"
                    f" != SRD {bool(entry.get('concentration'))!r}")
            if bool(spell.get("ritual")) != bool(entry.get("ritual")):
                issues.append(f"{name}: ritual {bool(spell.get('ritual'))!r}"
                              f" != SRD {bool(entry.get('ritual'))!r}")
            if bool(spell.get("healing")) != (entry.get("heal") is not None):
                issues.append(f"{name}: healing {bool(spell.get('healing'))!r}"
                              f" != SRD {entry.get('heal') is not None!r}")
        srd_at = entry.get("attack_type")
        if not exempt and srd_at == "saving_throw" and spell.get("attack_type") != "saving_throw":
            issues.append(f"{name}: attack_type {spell.get('attack_type')!r} != SRD 'saving_throw'")
        if not exempt and srd_at == "attack_roll" and spell.get("attack_type") != "attack_roll":
            issues.append(f"{name}: attack_type {spell.get('attack_type')!r} != SRD 'attack_roll'")
        srd_save = entry.get("save_type")
        if not exempt and srd_save and spell.get("save_type") and spell.get("save_type") != srd_save:
            issues.append(f"{name}: save_type {spell.get('save_type')!r} != SRD {srd_save!r}")
        if (not exempt and srd_save and not spell.get("no_damage") and spell.get("save_type")
                and bool(spell.get("save_half")) != bool(entry.get("save_half"))):
            issues.append(f"{name}: save_half {spell.get('save_half')!r} != SRD {entry.get('save_half')!r}")
        # Per-slot scaling. `unjudged` means the source omits the higher levels (see the builder),
        # so only a clean '+NdM' or an explicit 'none' is asserted; the reviewed UPCAST_EXPECTED
        # map fills the source's gaps.
        upcast = entry.get("upcast")
        if upcast == "unjudged":
            upcast = UPCAST_EXPECTED.get(name, "unjudged")
        if not exempt and upcast and upcast != "unjudged":
            config_up = str(spell.get("higher_levels") or "").strip().replace(" ", "")
            if upcast == "none":
                if _UPCAST.match(config_up):
                    issues.append(f"{name}: higher_levels {spell.get('higher_levels')!r} != SRD none")
            elif config_up != upcast:
                issues.append(f"{name}: higher_levels {spell.get('higher_levels')!r} != SRD {upcast!r}")
        # Base damage: only a single-component, regex-clean, leveled spell is compared.
        if exempt or spell.get("no_damage") or spell.get("projectiles") or not entry.get("level"):
            continue
        damage = entry.get("damage") or []
        if len(damage) != 1:
            continue
        base = (damage[0].get("slot") or {}).get(str(entry["level"]))
        match = _DICE.match(base or "")
        if not match:
            continue
        want_dice = f"{match.group(1)}d{match.group(2)}"
        if str(spell.get("damage_dice") or "") != want_dice:
            issues.append(f"{name}: damage_dice {spell.get('damage_dice')!r} != SRD {want_dice!r}")
        want_type = damage[0].get("type")
        if want_type and spell.get("damage_type") and spell.get("damage_type") != want_type:
            issues.append(f"{name}: damage_type {spell.get('damage_type')!r} != SRD {want_type!r}")
    return issues, checked


def main() -> int:
    ap = argparse.ArgumentParser(description="The SRD 5.1 spell-stat gate")
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    args = ap.parse_args()

    issues, checked = run()
    if args.json:
        print(json.dumps({"ok": not issues, "checked": checked, "issues": issues}, indent=2))
    else:
        for line in issues:
            print(f"  DRIFT  {line}")
        print(f"  spells checked: {checked}  exempt (reviewed): {len(EXEMPT)}"
              f"  upcasts reviewed: {len(UPCAST_EXPECTED)}"
              f"  classify review: {len(CLASSIFY_EXEMPT)}")
        print("SPELL STATS: " + ("GREEN -- config matches the frozen SRD 5.1 stats" if not issues
                                 else f"RED -- {len(issues)} drift(s)"))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
