"""Every GM-offered tool has an explicit projection decision, and the projections hold.

The projection used to be fail-open ("tools not listed pass through untouched"), so a tool added
after it was written silently sent the GM everything, and a nested `narrative_format` leaked.
This locks the fix:

  - coverage: every tool the GM is offered is in `_GM_VIEW_KEEP`, `_GM_VIEW_DROP` or
    `_GM_VIEW_FULL` -- a new tool cannot ship without a decision;
  - the always-drop keys are stripped at ANY depth (the mechanics tools embed other results);
  - `resolve_magic` / `resolve_attack` lose their embedded sub-results, and an `automatic`
    multi-projectile spell loses the per-dart list `targets[].darts` already carries;
  - an unclassified name fails closed (stripped), never dumped raw.

The MCP server is booted in-process for the tool list. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_tool_projection.py
"""

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import (  # noqa: E402
    _GM_VIEW_KEEP, _GM_VIEW_DROP, _GM_VIEW_FULL, _GM_VIEW_ALWAYS_DROP,
    _gm_tool_view, filter_tools, MODE_CLASSIC, MODE_TIME_TRAVELER,
)

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _load_token_budget():
    spec = importlib.util.spec_from_file_location("token_budget_proj", REPO / "tools" / "token_budget.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _project(name, payload):
    return json.loads(_gm_tool_view(name, json.dumps(payload, ensure_ascii=True)))


MAGIC_MISSILE = {
    "success": True, "actor": "Power", "spell_name": "Magic Missile", "target_name": "",
    "attack_type": "automatic", "slot_consumed": True, "slot_level_used": 8,
    "slot_result": {"success": True, "key": "spellcasting.slots.8", "old_value": 1, "new_value": 0,
                    "delta": -1, "remaining_slots": {"lv1": 4, "lv8": 0, "lv9": 1},
                    "narrative_format": "spellcasting.slots.8: 1 -> 0 (-1)"},
    "xp_awarded": 200,
    "xp_result": {"success": True, "key": "xp", "old_value": 355700, "new_value": 355900,
                  "delta": 200, "narrative_format": "xp: 355700 -> 355900 (+200)"},
    "projectiles": 10,
    "per_projectile": [{"target": "the knife-man", "hit": True, "crit": False, "rolls": [3],
                        "damage": d} for d in (4, 4, 3, 5, 2, 5, 4, 4, 3, 4)],
    "damage_total": 38, "damage_type": "force",
    "targets": [{"name": "the knife-man", "damage": 38, "darts": [4, 4, 3, 5, 2, 5, 4, 4, 3, 4],
                 "remaining_hp": 0, "killed": True, "xp_awarded": 200}],
    "killed_count": 1,
}
SWORD_HIT = {
    "success": True, "actor": "Power", "attack": "Longsword", "outcome": "Hit",
    "damage_total": 12, "damage_type": "slashing", "target_name": "the knife-man",
    "target_remaining_hp": 0, "target_killed": True, "challenge_rating": 2, "xp_awarded": 450,
    "hp_change": {"success": True, "key": "current_hit_points", "old_value": 12, "new_value": 0,
                  "delta": -12, "hp_status": "0/18", "narrative_format": "the knife-man HP: 0/18"},
    "xp_result": {"success": True, "key": "xp", "old_value": 355900, "new_value": 356350,
                  "delta": 450, "narrative_format": "xp: 355900 -> 356350 (+450)"},
}


def main():
    tb = _load_token_budget()
    rows = [{"function": {"name": n}} for n, _d, _s in tb.tool_listing()]

    print("\n-- coverage: every offered tool has a decision")
    classified = set(_GM_VIEW_KEEP) | set(_GM_VIEW_DROP) | set(_GM_VIEW_FULL)
    for mode in (MODE_TIME_TRAVELER, MODE_CLASSIC):
        offered = {t["function"]["name"] for t in filter_tools(rows, True, mode)}
        missing = sorted(offered - classified)
        rec(f"every tool offered in {mode} is KEEP/DROP/FULL", not missing, ", ".join(missing))
    rec("the engine-only tools are not in the maps",
        not ({"set_player_field", "dump_player_save_state", "debug_set_field", "debug_device",
              "dump_player_db"}
             & classified))

    print("\n-- the always-drop keys are stripped at any depth")
    deep = {"a": {"narrative_format": "x", "keep": 1}, "b": [{"narrative_format": "y", "keep": 2}]}
    v = _project("perform_check", deep)
    rec("a nested narrative_format is dropped",
        "narrative_format" not in json.dumps(v) and v["a"]["keep"] == 1 and v["b"][0]["keep"] == 2)

    print("\n-- resolve_magic: no embedded plumbing, and the outcome survives")
    mm = _project("resolve_magic", MAGIC_MISSILE)
    rec("slot_result / xp_result are dropped",
        "slot_result" not in mm and "xp_result" not in mm)
    rec("no narrative_format anywhere", "narrative_format" not in json.dumps(mm))
    rec("the outcome survives (targets, damage, kills, xp)",
        mm["targets"][0]["darts"] == MAGIC_MISSILE["targets"][0]["darts"]
        and mm["damage_total"] == 38 and mm["killed_count"] == 1 and mm["xp_awarded"] == 200,
        str(sorted(mm)))
    rec("an automatic spell drops the per-dart list (targets[].darts carries it)",
        "per_projectile" not in mm)
    mm_roll = _project("resolve_magic", dict(MAGIC_MISSILE, attack_type="attack_roll"))
    rec("an attack-roll spell keeps per_projectile (the rolls are the only record)",
        "per_projectile" in mm_roll)

    print("\n-- resolve_attack: no embedded plumbing")
    sw = _project("resolve_attack", SWORD_HIT)
    rec("hp_change / xp_result are dropped", "hp_change" not in sw and "xp_result" not in sw)
    rec("the outcome survives (damage, kill, xp, remaining hp)",
        sw["damage_total"] == 12 and sw["target_killed"] is True
        and sw["target_remaining_hp"] == 0 and sw["xp_awarded"] == 450)
    rec("no narrative_format anywhere", "narrative_format" not in json.dumps(sw))

    print("\n-- read_player_field: the requested fields survive; engine keys do not")
    rf = _project("read_player_field", {"fields": {"stats": {"str": 18},
                                                 "spellcasting": {"dc": 19}},
                                         "missing": []})
    rec("the requested fields pass through (wrapped)",
        rf["fields"]["stats"]["str"] == 18 and rf["fields"]["spellcasting"]["dc"] == 19)
    rf_meta = _project("read_player_field", {"fields": {"era": "tang", "difficulty": "hard",
                                                         "stats": {"str": 18}}, "missing": []})
    rec("an engine-metadata field is still stripped from the wrapper",
        "era" not in rf_meta["fields"] and "difficulty" not in rf_meta["fields"]
        and rf_meta["fields"]["stats"]["str"] == 18)

    print("\n-- fail closed: an unclassified tool is stripped, never dumped")
    unknown = {"keep_me": 1, "equipment": {"heavy": True}, "narrative_format": "x"}
    v = _project("a_tool_added_tomorrow", unknown)
    rec("an unclassified tool keeps its data but loses the always-drop keys",
        v.get("keep_me") == 1 and "equipment" not in v and "narrative_format" not in v, str(v))

    rec("the always-drop name list still covers the originals",
        {"equipment", "current_list", "registry_summary", "narrative_format"} <= set(_GM_VIEW_ALWAYS_DROP))

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
