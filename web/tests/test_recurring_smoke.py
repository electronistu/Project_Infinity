"""Recurring-save smoke: the whole path a live game takes, with no model in it.

Two halves:

  * the MCP level -- a real `dice_server.py` subprocess driven over stdio, exactly as the
    engine drives it, proving the new parameters are offered and a repeat save really comes
    back on the wire;
  * the engine level -- the tool result's `recurring` / `config_warning` reaching the
    Mechanics block, the GM's view and the combat roster.

Deterministic by construction: a DC-30 save cannot be passed by a +0 modifier, so the rolls
do not matter. No network, no LLM.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_recurring_smoke.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import GameSession  # noqa: E402
from web.gm_view import _gm_tool_view  # noqa: E402
from web.tests.dice import harness as H  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _player():
    return H.make_player(
        name="Power", character_class="Wizard", race="High Elf", background="Sage",
        level=20, xp=355000, proficiency_bonus=6, armor_class=15,
        total_hit_points=82, current_hit_points=82, hit_dice_count=20, hit_dice_size=6,
        stats={"str": 8, "dex": 20, "con": 10, "int": 20, "wis": 12, "cha": 10},
        spellcasting={"ability": "intelligence", "dc": 19, "attack_modifier": 11,
                      "cantrips": ["Fire Bolt"], "spells_known": [],
                      "spellbook": ["Chain Lightning"],
                      "spells_prepared": [{"name": "Chain Lightning"}],
                      "slots": {"4": 1, "6": 1}},
        inventory=["Spellbook", "Component Pouch"],
    )


GOBLIN = {"name": "Gao", "hp": 200, "ac": 12, "initiative_modifier": 2,
          "challenge_rating": 0.5, "saves": {"str": 0, "dex": 0, "con": 0, "int": 0,
                                             "wis": 0, "cha": 0},
          "attacks": [{"name": "Spear", "attack_bonus": 4, "damage_dice": "1d6",
                       "damage_modifier": 2, "damage_type": "piercing"}]}


async def _mcp() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory() as td:
        save = Path(td) / "power.player"
        save.write_text(json.dumps(_player()), encoding="utf-8")
        params = StdioServerParameters(command=sys.executable,
                                       args=["dice_server.py", str(save)], cwd=str(REPO))

        async def call(session, name, **args):
            result = await session.call_tool(name, arguments=args)
            text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
            return json.loads(text)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await call(session, "register_combatants", combatants=[GOBLIN])

                # A contradicting field must be reported, not obeyed (config wins).
                bolt = await call(session, "resolve_magic", spell_name="Fire Bolt",
                                  target_name="Gao", damage_type="cold")
                rec("the wire carries config_warning",
                    bolt.get("config_warning", {}).get("fields", {}).get("damage_type")
                    == {"provided": "cold", "config": "fire"},
                    str(bolt.get("config_warning")))

                # Eyebite takes its variant through the schema; sickened registers a repeat save.
                eyebite = await call(session, "resolve_magic", spell_name="Eyebite",
                                     slot_level=6, eyebite_effect="sickened",
                                     spell_save_dc=30, targets=[{"name": "Gao"}])
                rec("eyebite_effect is offered and registers a repeat save",
                    eyebite.get("recurring_registered") == ["Gao"],
                    str({k: eyebite.get(k) for k in ("condition", "recurring_registered")}))

                # Phantasmal Killer: initial save fails (DC 30 cannot be met), no damage yet.
                pk = await call(session, "resolve_magic", spell_name="Phantasmal Killer",
                                slot_level=4, spell_save_dc=30, targets=[{"name": "Gao"}])
                rec("the initial failure registers the repeating save",
                    pk.get("recurring_registered") == ["Gao"] and pk.get("condition") == "Frightened")

                # The target's turn: the engine rolls the repeat save and reports it.
                turn = await call(session, "perform_check", actor="Gao", dc=5,
                                  check_name="Athletics", ability="str")
                recurring = turn.get("recurring") or []
                rec("the repeat save fires on the target's turn",
                    any(e.get("name") == "Gao" and e.get("damage", 0) > 0 for e in recurring),
                    str(recurring))
                rec("the mechanics line names the save, the spell and the caster",
                    "WIS Save (Phantasmal Killer, cast by Power)" in turn.get("narrative_format", ""),
                    turn.get("narrative_format", ""))


def _engine() -> None:
    gs = GameSession(base_dir=REPO, model="test", scene_images=False)
    payload = {
        "success": True,
        "actor": "Power",
        "spell_name": "Phantasmal Killer",
        "recurring": [{"name": "Gao", "spell": "Phantasmal Killer", "save_type": "wis",
                       "save_total": 3, "save_dc": 19, "save_success": False, "damage": 22,
                       "damage_type": "psychic", "remaining_hp": 0, "killed": True}],
        "config_warning": {"spell": "Chain Lightning", "fields": {}},
        "narrative_format": "Power Phantasmal Killer — 0\n"
                            "Gao WIS Save (Phantasmal Killer): 3 vs DC 19 (Failure) (1 + 2) — 22 psychic, 0 HP (KILLED)",
        "registry_summary": [{"name": "Gao", "hp": "0/200", "ac": 12, "is_player": False,
                               "role": "hostile", "killed": True, "status": "active"}],
    }
    text = json.dumps(payload)
    lines = gs._collect_mechanics(text)
    rec("the Mechanics block carries the repeating-save line",
        any("WIS Save (Phantasmal Killer)" in line for line in lines), str(lines))
    roster = gs._combat_roster_update(text)
    rec("the combat roster shows the engine's HP",
        bool(roster) and roster[0]["hp"] == "0/200", str(roster))
    view = json.loads(_gm_tool_view("resolve_magic", text))
    rec("the GM sees `recurring` and `config_warning`",
        "recurring" in view and "config_warning" in view, str(sorted(view)))
    rec("the GM never sees narrative_format (the engine owns the block)",
        "narrative_format" not in view)


def main() -> int:
    print("=" * 72)
    print("RECURRING-SAVE SMOKE (MCP wire + engine plumbing; no model)")
    print("=" * 72)
    print("  -- MCP level (a real dice_server subprocess) --")
    asyncio.run(asyncio.wait_for(_mcp(), timeout=120))
    print("  -- engine level --")
    _engine()
    print(f"\n  RESULT: {'PASS' if all(RESULTS) else 'FAIL'}")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
