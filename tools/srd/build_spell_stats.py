"""Freeze the SRD 5.1 spell stat table for `tools/spell_stats_gate.py`.

`tools/srd_gate.py` proves every *name* we ship is an SRD 5.1 name. It cannot see a wrong
damage die or a misdeclared save, which is how the Chain Lightning row (10d6 attack-roll
instead of 10d8 DEX save) shipped. This build script fetches the structured SRD 5.1 / 2014
spell data once and writes a frozen table; the gate compares `config/spells.yml` against the
committed table and never touches the network.

Source: `https://www.dnd5eapi.co/api/2014/spells` (SRD 5.1, CC-BY-4.0). The API's own data has
known gaps, so the gate keeps a small reviewed `EXEMPT` map for real deviations.

Run:

    venv\\Scripts\\python.exe tools\\srd\\build_spell_stats.py
    venv\\Scripts\\python.exe tools\\srd\\build_spell_stats.py --check   # table is current
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
OUT = REPO / "tools" / "srd" / "spell-stats.json"
BASE = "https://www.dnd5eapi.co"
LIST = BASE + "/api/2014/spells"
DETAIL = BASE + "/api/2014/spells/"
_DICE = re.compile(r"^\s*(\d+)d(\d+)\s*$")


def _fetch(url: str, tries: int = 4):
    err = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as fh:
                return json.load(fh)
        except Exception as exc:  # noqa: BLE001 - network flake, retry
            err = exc
            time.sleep(0.5 * (attempt + 1))
    raise err


def _upcast_sig(damage: list, level: int) -> str:
    """The per-slot damage scaling signature: '+NdM', 'none', or 'unjudged'.

    The source omits the higher-level entries for a number of spells (Harm, Sunbeam, Wall of
    Fire, Blade Barrier, Fire Storm, Sunburst, Arcane Sword, Sleep, ...), so a map with fewer
    than two levels is never judged -- otherwise the gate would report the API's gaps as drift.
    """
    if level == 0 or len(damage) != 1:
        return "unjudged"
    slot = damage[0].get("slot") or {}
    keys = sorted(slot, key=lambda k: int(k)) if slot else []
    if len(keys) < 2:
        return "unjudged"
    values = []
    for key in keys:
        match = _DICE.match(str(slot[key]))
        if not match:
            return "unjudged"
        values.append((int(match.group(1)), int(match.group(2))))
    if all(v == values[0] for v in values):
        return "none"
    deltas = {(values[i + 1][0] - values[i][0], values[i + 1][1] - values[i][1])
              for i in range(len(values) - 1)}
    if len(deltas) == 1:
        (count, size_delta), = deltas
        if count and size_delta == 0:
            return f"+{count}d{values[0][1]}"
    return "unjudged"


def _normalize(detail: dict) -> dict:
    dc = detail.get("dc") or {}
    if dc:
        attack_type = "saving_throw"
    elif detail.get("attack_type") in ("ranged", "melee"):
        attack_type = "attack_roll"
    else:
        attack_type = "automatic"
    damage = []
    for dmg in detail.get("damage") or []:
        damage.append({
            "type": (dmg.get("damage_type") or {}).get("index"),
            "slot": dmg.get("damage_at_slot_level") or {},
            "char": dmg.get("damage_at_character_level") or {},
        })
    area = detail.get("area_of_effect") or None
    if area:
        area = {"type": area.get("type"), "size": area.get("size")}
    heal = detail.get("heal_at_slot_level") or None
    return {
        "level": detail.get("level"),
        "school": (detail.get("school") or {}).get("index"),
        "attack_type": attack_type,
        "save_type": (dc.get("dc_type") or {}).get("index"),
        "save_half": dc.get("dc_success") == "half",
        "damage": damage,
        "heal": heal,
        "area": area,
        "upcast": _upcast_sig(damage, detail.get("level") or 0),
        # Classification fields (Phase 1 of the spell-mechanics verification). Frozen for the
        # gate; `components`/`range`/`casting_time` are recorded but not asserted (the config
        # keeps material components in config/components.yml and does not carry range).
        "concentration": bool(detail.get("concentration")),
        "ritual": bool(detail.get("ritual")),
        "duration": detail.get("duration"),
        "higher_level": bool(detail.get("higher_level")),
        "components": detail.get("components") or [],
        "range": detail.get("range"),
        "casting_time": detail.get("casting_time"),
    }


def build() -> dict:
    listing = _fetch(LIST)
    indexes = [r["index"] for r in listing["results"]]

    def one(index: str):
        return index, _fetch(DETAIL + index)

    spells: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for index, detail in pool.map(one, indexes):
            spells[str(detail["name"]).lower()] = _normalize(detail)
    return {
        "_meta": {
            "document": "System Reference Document 5.1 (\"SRD 5.1\"), Wizards of the Coast LLC",
            "license": "CC-BY-4.0",
            "source": LIST,
            "method": "Structured stats fetched from dnd5eapi (2014/SRD 5.1), normalised to "
                      "level / school / attack_type / save_type / save_half / damage / heal / "
                      "area / upcast / concentration / ritual / duration / higher_level / "
                      "components / range / casting_time. Reviewed by "
                      "tools/spell_stats_gate.py, which carries the EXEMPT map.",
            "count": len(spells),
        },
        "spells": spells,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build tools/srd/spell-stats.json")
    ap.add_argument("--check", action="store_true",
                    help="verify the committed table matches a fresh fetch (needs network)")
    args = ap.parse_args()

    fresh = build()
    if args.check:
        if not OUT.exists():
            print(f"RED -- {OUT.name} is missing", file=sys.stderr)
            return 1
        current = json.loads(OUT.read_text(encoding="utf-8"))
        if current.get("spells") == fresh.get("spells"):
            print("GREEN -- spell-stats.json matches the source")
            return 0
        print("RED -- spell-stats.json is stale; rerun without --check", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fresh, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)} -- {fresh['_meta']['count']} spells")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
