"""The engine-injected lean CHARACTER SHEET, and the companions it carries.

No network: `_sheet_body` is pure, and `_fetch_sheet` is exercised with a stubbed dump.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_sheet.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import ENGINE_ONLY_TOOLS, GameSession, _sheet_body  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


# A dump carrying EVERY field the engine can return: the narrative ones must survive, the
# combat math / engine metadata / spells must not.
DUMP = {
    "name": "Power", "race": "High Elf", "character_class": "Wizard", "level": 20,
    "background": "Criminal", "alignment": "Chaotic Neutral", "gender": "male", "age": 450,
    "languages": ["Common", "Elvish", "Orc"],
    "gold": 936, "xp": 355700,
    "current_hit_points": 82, "total_hit_points": 82, "temporary_hit_points": 0,
    "speed": 30, "conditions": ["blinded"], "concentration": "Haste",
    "active_effects": [{"name": "Mage Armor"}],
    "features": ["Spellcasting", "Overchannel"],
    "inventory": ["Spellbook", {"name": "Dagger +1", "description": "a slender blade"},
                  {"name": "Bolts", "count": 20}],
    "equipped": {"worn": ["Dark Common Clothes"], "hands": ["Dagger +1", None]},
    "consumables": {"Potion of Healing": 3},
    "_carrying": {"status": "encumbered", "items": [{"name": "Spellbook", "weight": 3}]},
    "spellcasting": {"ability": "intelligence", "dc": 19, "attack_modifier": 11,
                     "cantrips": ["Fire Bolt"], "spells_prepared": ["Shield"],
                     "always_prepared": ["Bless"], "spellbook": ["Shield"],
                     "slots": {"1": 4}},
    "reputation": {"tang": {"the tang empire": {"ruler": [], "market_guilds": []}}},
    "companions": {"xiaoyu": {"name": "Xiaoyu", "race": "human", "class": "rogue",
                              "description": "ink-stained fingers and a stolen name"}},
    # combat math / engine metadata that must NOT appear
    "stats": {"str": 8, "dex": 20}, "saves": {"dex": 5}, "skills": ["Arcana"],
    "expertise": ["Arcana"], "proficiency_bonus": 6, "armor_proficiencies": ["light"],
    "weapon_proficiencies": ["simple"], "tool_proficiencies": ["herbalism"],
    "_passive": {"perception": 12}, "armor_class": 19, "_equipment": {"base_ac": 15},
    "hit_dice_count": 20, "hit_dice_size": 6, "_active_buff_data": {"x": 1},
    "capacity_multiplier": 1.0, "item_hp_delta": 0,
    "mode": "time_traveler", "era": "tang", "arrival": "the canal", "difficulty": "hard",
    "journey": ["tang"],
}


def main() -> int:
    print("=" * 72)
    print("CHARACTER SHEET (engine-injected, lean) + companions")
    print("=" * 72)
    body = _sheet_body(DUMP)
    print(body)
    print()

    rec("identity is carried", "Power" in body and "High Elf Wizard 20" in body)
    rec("vitals, gold and languages are carried",
        "HP 82/82" in body and "936 gp" in body and "Common, Elvish, Orc" in body)
    rec("speed carries the encumbrance word", "speed 30 (encumbered)" in body)
    rec("conditions, concentration and effects are carried",
        "conditions: blinded" in body and "concentration: Haste" in body and "Mage Armor" in body)
    rec("features are carried", "features: Spellcasting, Overchannel" in body)
    rec("inventory is names only (dict -> name; no counts, no description)",
        "inventory: Spellbook, Dagger +1, Bolts" in body
        and "20" not in body.split("inventory:")[1].split("\n")[0])
    rec("equipped + consumables are carried",
        "worn Dark Common Clothes" in body and "hands Dagger +1" in body
        and '"Potion of Healing": 3' in body)
    rec("reputation (the write-target shape) is carried",
        '"the tang empire"' in body and '"market_guilds"' in body)

    excluded = ("stats", '"dex": 20', "saves", "skills", "expertise", "proficiency_bonus",
                "armor_proficiencies", "weapon_proficiencies", "tool_proficiencies",
                "_passive", "armor_class", "_equipment", "hit_dice", "_active_buff_data",
                "capacity_multiplier", "item_hp_delta", "mode", "arrival", "difficulty",
                "journey", "companions", "su lian", "355700")
    rec("no combat math, engine metadata, xp or raw companions",
        not any(tok in body for tok in excluded))
    rec("the spellcasting block is not carried",
        "cantrips" not in body and "spells_prepared" not in body
        and "slots" not in body and "Shield" not in body and "Bless" not in body)

    print("\n-- what the GM is offered")
    rec("dump_player_db is engine-only (never offered)", "dump_player_db" in ENGINE_ONLY_TOOLS)
    rec("read_player_field is the GM's per-field read (offered)",
        "read_player_field" not in ENGINE_ONLY_TOOLS)

    print("\n-- injection")
    gs = GameSession(base_dir=REPO, model="test")
    gs.classic = False
    gs.era = "tang"
    gs.timeline_path = os.path.join(tempfile.mkdtemp(), "x.timeline")

    async def fake_dump(name, args):
        return json.dumps(DUMP)

    async def fake_boom(name, args):
        raise RuntimeError("no db")

    gs._call_tool_text = fake_dump  # type: ignore[assignment]
    block = asyncio.run(gs._fetch_sheet())
    rec("_fetch_sheet builds the lean block", block.startswith("CHARACTER SHEET — Power"))
    rec("the sheet carries the party with identity and the look",
        "companions:" in block and "Xiaoyu (human rogue)" in block
        and "ink-stained fingers" in block)

    # No companions -> no block.
    no_people = {**DUMP, "companions": {}}

    async def fake_empty(name, args):
        return json.dumps(no_people)

    gs._call_tool_text = fake_empty  # type: ignore[assignment]
    rec("... with no companions line when there are none",
        "companions:" not in asyncio.run(gs._fetch_sheet()))

    gs._call_tool_text = fake_boom  # type: ignore[assignment]
    rec("_fetch_sheet is silent on failure (never sinks a session)",
        asyncio.run(gs._fetch_sheet()) == "")

    # The jump rebuilds the sheet and `_compact` re-injects it while dropping the history.
    gs.messages = [{"role": "system", "content": "the protocol"},
                   {"role": "system", "content": "places"},
                   {"role": "user", "content": "old history"}]
    gs._sheet_block = "CHARACTER SHEET — Power"
    gs._compact("egypt")
    joined = "\n".join(m.get("content", "") for m in gs.messages if isinstance(m, dict))
    rec("_compact re-injects the sheet and drops the history",
        "CHARACTER SHEET — Power" in joined and "old history" not in joined)

    print(f"\n  RESULT: {'PASS' if all(RESULTS) else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
