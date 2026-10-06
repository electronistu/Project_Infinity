"""SRD 5.1 exhaustion: levels, speed, attack/check/save disadvantage, HP max, death."""

import unittest

from web.tests.dice import harness as H


class ExhaustionLevelTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)  # 44 HP, speed 30

    def _eff(self):
        return H.ds._exhaustion_effects(H.dbv("exhaustion", 0))

    def test_level_1_imposes_check_disadvantage_only(self):
        H.ds.modify_exhaustion(1)
        eff = self._eff()
        self.assertTrue(eff["check_disadvantage"])
        self.assertFalse(eff["attack_disadvantage"])
        self.assertEqual(eff["speed_multiplier"], 1.0)

    def test_level_2_halves_speed(self):
        H.ds.modify_exhaustion(2)
        entry = H.ds._player_registry_entry(H.ds.DB_CONNECTION.cursor())
        self.assertEqual(entry["speed"], 15)

    def test_level_3_disadvantages_attacks_and_saves(self):
        H.ds.modify_exhaustion(3)
        eff = self._eff()
        self.assertTrue(eff["attack_disadvantage"])
        self.assertTrue(eff["save_disadvantage"])

    def test_level_4_halves_hp_max_and_reverses(self):
        H.ds.modify_exhaustion(4)
        self.assertEqual(H.dbv("total_hit_points"), 22)
        self.assertEqual(H.dbv("current_hit_points"), 22)
        H.ds.modify_exhaustion(-4)
        self.assertEqual(H.dbv("total_hit_points"), 44)

    def test_level_5_sets_speed_zero(self):
        H.ds.modify_exhaustion(5)
        entry = H.ds._player_registry_entry(H.ds.DB_CONNECTION.cursor())
        self.assertEqual(entry["speed"], 0)

    def test_level_6_is_death(self):
        r = H.ds.modify_exhaustion(6)
        self.assertTrue(r["effects"]["dead"])
        self.assertEqual(r["exhaustion"], 6)
        self.assertIn("dead", r["narrative_format"])
        self.assertEqual(H.dbv("current_hit_points"), 0)

    def test_clamped_between_0_and_6(self):
        self.assertEqual(H.ds.modify_exhaustion(99)["exhaustion"], 6)
        self.assertEqual(H.ds.modify_exhaustion(-99)["exhaustion"], 0)


class ExhaustionDisadvantageTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_ability_check_disadvantage_at_level_1(self):
        H.ds.modify_exhaustion(1)
        with H.fixed_rolls([10, 10]):
            r = H.ds.perform_check(actor="Borin", modifier=5, check_name="Athletics",
                                   ability="str", dc=10)
        self.assertIn("disadvantage_rolls", r)

    def test_saving_throw_disadvantage_at_level_3(self):
        H.ds.modify_exhaustion(3)
        with H.fixed_rolls([10, 10]):
            r = H.ds.perform_check(actor="Borin", check_name="Constitution save",
                                   ability="con", save=True, dc=10)
        self.assertIn("disadvantage_rolls", r)

    def test_attack_disadvantage_at_level_3(self):
        H.ds.modify_exhaustion(3)
        with H.fixed_rolls([10, 10, 5]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=5, damage_dice="1d8",
                                    target_name="Dummy", damage_type="slashing")
        self.assertTrue(r["disadvantage"])

    def test_no_disadvantage_at_level_0(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Borin", modifier=5, check_name="Athletics",
                                   ability="str", dc=10)
        self.assertNotIn("disadvantage_rolls", r)

    def test_spell_save_disadvantage_at_level_3(self):
        H.ds.modify_exhaustion(3)
        with H.rolls_always(10):
            r = H.ds.resolve_magic(spell_name="Burning Hands", actor="Goblin",
                                   is_npc_attack=True, spell_save_dc=13, target_name="Borin",
                                   damage_dice="3d6", damage_type="fire")
        self.assertTrue(r["save_disadvantage"])
        self.assertTrue(any("exhaustion" in s for s in r["save_disadvantage_sources"]))


class ExhaustionSourceTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_long_rest_removes_one_level(self):
        H.ds.modify_exhaustion(3)
        r = H.ds.rest("long")
        self.assertEqual(r["changes"]["exhaustion"], {"old": 3, "new": 2})
        self.assertEqual(H.dbv("exhaustion"), 2)

    def test_update_combatant_adds_a_level(self):
        H.ds.register_combatants([])
        r = H.ds.update_combatant(name="Borin", exhaustion_delta=1)
        self.assertEqual(r["exhaustion"], 1)
        self.assertEqual(H.dbv("exhaustion"), 1)

    def test_condition_add_exhaustion_is_ignored(self):
        # `exhaustion` is a level, not a condition — use exhaustion_delta.
        H.ds.register_combatants([])
        H.ds.update_combatant(name="Borin", conditions_add=["exhaustion"])
        self.assertIsNone(H.dbv("exhaustion"))
        self.assertNotIn("exhaustion", H.ds._COMBAT_REGISTRY["Borin"]["conditions"])


if __name__ == "__main__":
    unittest.main()
