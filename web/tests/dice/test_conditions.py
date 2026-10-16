"""The condition mechanics matrix (C1 of the SRD spell-mechanics verification).

Every flag in `CONDITION_EFFECTS` is proven to reach its consumer: how others attack the bearer,
how the bearer attacks, auto-failed STR/DEX saves, DEX-save disadvantage, check disadvantage, and
petrified's resistance to all damage. Two integration checks drive a registered target.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_conditions
"""

import unittest

from rules import state
from rules.conditions import (_apply_damage_modifiers, _condition_attack_effects,
                              _condition_check_disadvantage, _condition_save_effects)
from rules.vitals import _apply_combat_condition

from web.tests.dice import harness as H

OGRE = {
    "name": "Ogre", "hp": 30, "ac": 11, "initiative_modifier": 0, "challenge_rating": 2,
    "role": "hostile",
    "saves": {"str": 4, "dex": -1, "con": 3, "int": -2, "wis": -1, "cha": -2},
}


class ConditionEffectsMatrixTest(unittest.TestCase):
    """Pure-function matrix over `_condition_attack_effects` / `_condition_save_effects`."""

    def test_attackers_have_advantage(self):
        for cond in ("blinded", "paralyzed", "petrified", "restrained", "stunned", "unconscious"):
            with self.subTest(condition=cond):
                adv, dis, crit, src = _condition_attack_effects(set(), {cond})
                self.assertTrue(adv, src)

    def test_invisible_gives_attackers_disadvantage(self):
        adv, dis, crit, src = _condition_attack_effects(set(), {"invisible"})
        self.assertTrue(dis, src)

    def test_prone_melee_advantage_ranged_disadvantage(self):
        melee = _condition_attack_effects(set(), {"prone"}, ranged=False)
        ranged = _condition_attack_effects(set(), {"prone"}, ranged=True)
        self.assertTrue(melee[0] and not melee[1])
        self.assertTrue(ranged[1])

    def test_melee_crit_only_in_melee(self):
        for cond in ("paralyzed", "petrified", "unconscious"):
            with self.subTest(condition=cond):
                self.assertTrue(_condition_attack_effects(set(), {cond}, ranged=False)[2])
                self.assertFalse(_condition_attack_effects(set(), {cond}, ranged=True)[2])

    def test_self_attacks_with_disadvantage(self):
        for cond in ("blinded", "frightened", "poisoned", "prone", "restrained"):
            with self.subTest(condition=cond):
                adv, dis, crit, src = _condition_attack_effects({cond}, set())
                self.assertTrue(dis, src)

    def test_invisible_attacker_has_advantage(self):
        adv, dis, crit, src = _condition_attack_effects({"invisible"}, set())
        self.assertTrue(adv, src)

    def test_auto_fail_str_and_dex_saves(self):
        for cond in ("paralyzed", "petrified", "stunned", "unconscious"):
            for ability in ("str", "dex"):
                with self.subTest(condition=cond, ability=ability):
                    self.assertTrue(_condition_save_effects({cond}, ability)[1])
            self.assertFalse(_condition_save_effects({cond}, "wis")[1])

    def test_restrained_gives_dex_save_disadvantage(self):
        self.assertTrue(_condition_save_effects({"restrained"}, "dex")[0])
        self.assertFalse(_condition_save_effects({"restrained"}, "str")[0])

    def test_check_disadvantage(self):
        for cond in ("frightened", "poisoned"):
            with self.subTest(condition=cond):
                self.assertTrue(_condition_check_disadvantage({cond})[0])

    def test_petrified_resists_all_damage(self):
        entry = {"conditions": ["petrified"], "damage_resistances": [],
                 "damage_immunities": [], "damage_vulnerabilities": []}
        damage, note = _apply_damage_modifiers(entry, 20, "slashing")
        self.assertEqual(damage, 10)  # resistance to every type
        self.assertIn("slashing", note)

    def test_flat_conditions_have_no_mechanical_effect(self):
        for cond in ("grappled", "incapacitated"):
            with self.subTest(condition=cond):
                adv, dis, crit, _ = _condition_attack_effects(set(), {cond})
                self.assertFalse(adv or dis or crit)


class ConditionIntegrationTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)  # Borin, +7 to hit, Longsword 1d8

    def test_stunned_target_is_attacked_with_advantage_and_melee_crit(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([OGRE])
        H.ds.update_combatant(name="Ogre", conditions_add=["Stunned"])
        with H.fixed_rolls([1, 20, 5]):  # advantage keeps the 20 -> crit
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d8",
                                    target_ac=11, target_name="Ogre")
        self.assertEqual(r["attack_roll"], 20)
        self.assertEqual(r["outcome"], "Critical Success")

    def test_frightened_attacker_has_disadvantage(self):
        cursor = state.DB_CONNECTION.cursor()
        _apply_combat_condition(cursor, "Borin", "frightened")
        with H.fixed_rolls([1, 20, 4]):  # disadvantage keeps the 1 -> critical failure
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d8",
                                    target_ac=5, target_name="Goblin")
        self.assertEqual(r["attack_roll"], 1)
        self.assertEqual(r["outcome"], "Critical Failure")


if __name__ == "__main__":
    unittest.main()
