"""Every shipped spell resolves through the real engine (Phase 2 of the SRD spell-mechanics
verification).

This is the "all 319" guarantee: for every spell in `config/spells.yml` the harness synthesises the
minimal valid call from the spell's own row (slot level, save DC, a target of the right shape for
its resolution path) and runs `resolve_magic` in a fresh engine. It asserts the spell *runs* --
`success is True`, no exception, a non-empty `narrative_format`. Whether the effect is *correct*
is Phase 1 (the stat gate) and Phase 3 (the behavioural suites), not here.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_spell_smoke
"""

import unittest
from pathlib import Path

import yaml

from web.tests.dice import harness as H

REPO = Path(__file__).resolve().parent.parent.parent.parent
SPELLS = REPO / "config" / "spells.yml"

ACTOR = "Smoke"
TARGET = "Training Dummy"


def config_spells() -> list[dict]:
    data = yaml.safe_load(SPELLS.read_text(encoding="utf-8"))
    spells = data["spells"] if isinstance(data, dict) and "spells" in data else data
    return list(spells)


def smoke_caster():
    """A level-20 caster with a Spellbook, a Component Pouch, empty hands and slots for every
    level -- so a spell fails only on its own resolution, never on a missing resource."""
    return H.make_player(
        name=ACTOR, character_class="Wizard", race="Human", background="Sage",
        level=20, xp=355000, proficiency_bonus=6, armor_class=15,
        total_hit_points=100, current_hit_points=100, hit_dice_count=20, hit_dice_size=6,
        stats={"str": 10, "dex": 14, "con": 14, "int": 20, "wis": 12, "cha": 10},
        spellcasting={
            "ability": "intelligence", "dc": 19, "attack_modifier": 11,
            "cantrips": [], "spells_known": [], "spellbook": [],
            "spells_prepared": [], "slots": {str(n): 9 for n in range(1, 10)},
        },
        inventory=["Spellbook", "Component Pouch"],
    )


def call_for(spell: dict) -> dict:
    """The minimal `resolve_magic` call for this spell's declared resolution shape."""
    kwargs: dict = {"spell_name": spell["name"], "actor": ACTOR}
    level = int(spell.get("level") or 0)
    if level > 0:
        kwargs["slot_level"] = level
    is_projectile = bool(spell.get("projectiles") or spell.get("cantrip_projectile_scaling"))
    is_attack = spell.get("attack_type") == "attack_roll"
    is_save = spell.get("attack_type") == "saving_throw"
    heals = bool(spell.get("healing") or spell.get("flat_healing") or spell.get("temporary_hp"))
    if is_save:
        kwargs["spell_save_dc"] = 15
    if spell.get("hp_pool"):
        kwargs["targets"] = [{"name": TARGET, "current_hp": 50}]
    elif is_projectile or is_attack:
        kwargs["target_name"] = TARGET
        kwargs["target_current_hp"] = 100
        kwargs["target_ac"] = 10
        kwargs["spell_attack_modifier"] = 11
    elif heals:
        kwargs["target_name"] = ACTOR
    elif is_save:
        kwargs["target_name"] = TARGET
        kwargs["target_current_hp"] = 100
    elif spell.get("instant_kill"):
        kwargs["target_name"] = TARGET
        kwargs["target_current_hp"] = 10
    elif spell.get("buffs"):
        kwargs["target_name"] = ACTOR
    return kwargs


class EverySpellSmokeTest(unittest.TestCase):
    def test_every_spell_resolves(self):
        spells = config_spells()
        self.assertGreater(len(spells), 300)
        for spell in spells:
            name = spell["name"]
            with self.subTest(spell=name):
                with H.load(smoke_caster()):
                    result = H.ds.resolve_magic(**call_for(spell))
                self.assertIsInstance(result, dict)
                self.assertTrue(result.get("success", False),
                                f"{name} did not resolve: {result}")
                self.assertTrue(result.get("narrative_format"),
                                f"{name} produced no mechanics line")


if __name__ == "__main__":
    unittest.main()
