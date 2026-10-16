"""Spell coverage report: which shipped spells are exercised, and by which resolution shape.

Phase 0 of the SRD spell-mechanics verification. This is a **report, not a gate** -- it exits 0
even when coverage is low. It answers, for every spell in `config/spells.yml`:

  * its resolution *shape* (attack_roll / saving_throw / automatic, plus hp_pool, projectiles,
    healing, buffs, condition, repeat_save, ...);
  * whether the frozen SRD stat table (`tools/srd/spell-stats.json`) can *judge* it (the same
    conservative checks `tools/spell_stats_gate.py` makes);
  * whether any test under `web/tests/` references it by name.

The full matrix is in `--json`; the text mode prints the shape roll-up and the unexercised list.

Run:

    venv\\Scripts\\python.exe tools\\spell_coverage.py
    venv\\Scripts\\python.exe tools\\spell_coverage.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPELLS = REPO / "config" / "spells.yml"
TABLE = REPO / "tools" / "srd" / "spell-stats.json"
TESTS = REPO / "web" / "tests"
_DICE = re.compile(r"^\s*(\d+)d(\d+)\s*$")


def _load_spells() -> list[dict]:
    import yaml
    data = yaml.safe_load(SPELLS.read_text(encoding="utf-8"))
    spells = data["spells"] if isinstance(data, dict) and "spells" in data else data
    return list(spells)


def _load_table() -> dict:
    return json.loads(TABLE.read_text(encoding="utf-8"))["spells"]


def _test_blob() -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in TESTS.rglob("*.py"))


def _test_refs(name: str, blob: str) -> bool:
    return re.search(r'["\']' + re.escape(name) + r'["\']', blob) is not None


def _shape(spell: dict) -> str:
    tags = []
    if spell.get("hp_pool"):
        tags.append("hp_pool")
    if spell.get("projectiles") or spell.get("cantrip_projectile_scaling"):
        tags.append("projectiles")
    if spell.get("healing") or spell.get("flat_healing"):
        tags.append("healing")
    if spell.get("temporary_hp"):
        tags.append("temp_hp")
    if spell.get("instant_kill"):
        # A real engine mechanic since Power Word Kill was wired (`_resolve_instant_kill`): an
        # HP-threshold instant death, not a GM-adjudicated tag.
        tags.append("instant_kill")
    if spell.get("no_damage"):
        tags.append("no_damage")
    if spell.get("condition"):
        tags.append("condition")
    if spell.get("repeat_save"):
        tags.append("repeat_save")
    if spell.get("post_hit_save"):
        tags.append("post_hit_save")
    if spell.get("apply_condition_on_hit"):
        tags.append("cond_on_hit")
    if spell.get("eyebite_variants"):
        tags.append("variants")
    if spell.get("damage_on_miss"):
        tags.append("damage_on_miss")
    if spell.get("add_spellcasting_mod"):
        tags.append("add_mod")
    if spell.get("delayed_bonus_dice"):
        tags.append("delayed")
    if spell.get("buffs"):
        tags.append("buffs")
    base = str(spell.get("attack_type") or "?")
    return base + ("+" + ",".join(tags) if tags else "")


def _judged(spell: dict, entry: dict) -> bool:
    """Could the frozen stat table rule on this spell's damage? Mirrors the gate's skips."""
    if spell.get("no_damage") or spell.get("projectiles") or not entry.get("level"):
        return False
    damage = entry.get("damage") or []
    if len(damage) != 1:
        return False
    base = (damage[0].get("slot") or {}).get(str(entry["level"]))
    return bool(_DICE.match(base or ""))


def build() -> dict:
    spells = _load_spells()
    table = _load_table()
    blob = _test_blob()
    rows = []
    for spell in spells:
        name = spell.get("name", "")
        entry = table.get(name.lower()) or {}
        rows.append({
            "name": name,
            "shape": _shape(spell),
            "judged": _judged(spell, entry),
            "tested": _test_refs(name, blob),
            "condition": spell.get("condition"),
            "buffs": sorted((spell.get("buffs") or {}).keys()),
        })
    shapes: dict[str, dict] = {}
    for row in rows:
        bucket = shapes.setdefault(row["shape"], {"total": 0, "tested": 0})
        bucket["total"] += 1
        bucket["tested"] += int(row["tested"])
    return {
        "total": len(rows),
        "tested": sum(1 for r in rows if r["tested"]),
        "judged": sum(1 for r in rows if r["judged"]),
        "shapes": shapes,
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Spell coverage report (Phase 0)")
    ap.add_argument("--json", action="store_true", help="machine-readable matrix")
    args = ap.parse_args()
    report = build()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"spells: {report['total']}   referenced by a test: {report['tested']}"
          f"   judged by the SRD table: {report['judged']}")
    print(f"shapes: {len(report['shapes'])}\n")
    print(f"  {'tested/total':>12}  shape")
    for shape, bucket in sorted(report["shapes"].items(),
                                key=lambda kv: (-kv[1]["total"], kv[0])):
        print(f"  {bucket['tested']:>5}/{bucket['total']:<6}  {shape}")
    untested = [r["name"] for r in report["rows"] if not r["tested"]]
    print(f"\nunexercised spells ({len(untested)}):")
    print("  " + ", ".join(untested))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
