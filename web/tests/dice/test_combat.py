"""resolve_attack: hit/miss, crit, advantage, THP, kills, NPC attacks."""

import unittest

from web.tests.dice import harness as H


class AttackTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def _atk(self, rolls, **kw):
        base = dict(actor="Borin", attack_modifier=7, target_ac=15, damage_dice="1d8",
                    damage_modifier=4, target_name="Dummy", target_current_hp=50)
        base.update(kw)
        with H.fixed_rolls(list(rolls)):
            return H.ds.resolve_attack(**base)

    def test_hit_and_damage(self):
        r = self._atk([15, 7])
        self.assertEqual(r["outcome"], "Success")
        self.assertEqual(r["total_attack"], 22)
        self.assertEqual(r["is_crit"], False)
        self.assertEqual(r["damage_total"], 11)
        self.assertEqual(r["target_remaining_hp"], 39)

    def test_miss(self):
        r = self._atk([10], attack_modifier=0, target_ac=25)
        self.assertEqual(r["outcome"], "Failure")
        self.assertEqual(r["damage_total"], 0)
        self.assertIsNone(r["target_killed"])

    def test_natural_20_crit_doubles_primary_dice(self):
        r = self._atk([20, 7, 7])
        self.assertTrue(r["is_crit"])
        self.assertTrue(r["is_natural_20"])
        self.assertEqual(r["crit_damage_rolls"], [7])
        self.assertEqual(r["primary_damage"], 18)   # 7 + 7 + 4
        self.assertEqual(r["damage_total"], 18)

    def test_extra_dice_not_doubled_on_crit(self):
        r = self._atk([20, 7, 7, 5], extra_damage_dice="1d6", extra_damage_modifier=0)
        self.assertTrue(r["is_crit"])
        self.assertEqual(r["primary_damage"], 18)   # doubled 1d8 + mod
        self.assertEqual(r["damage_total"], 23)     # + single extra 1d6

    def test_advantage_takes_higher(self):
        r = self._atk([5, 18, 7], advantage=True)
        self.assertEqual(r["advantage_rolls"], [5, 18])
        self.assertEqual(r["attack_roll"], 18)
        self.assertEqual(r["total_attack"], 25)
        self.assertTrue(r["advantage"])

    def test_forced_crit(self):
        r = self._atk([10, 5, 6], force_crit=True)
        self.assertTrue(r["is_crit"])
        self.assertFalse(r["is_natural_20"])
        self.assertEqual(r["primary_damage"], 15)   # 5 + 6 + 4

    def test_kill_awards_xp(self):
        r = self._atk([15, 7], target_current_hp=5, challenge_rating=0.25)
        self.assertTrue(r["target_killed"])
        self.assertEqual(r["xp_awarded"], 50)

    def test_invalid_damage_notation(self):
        r = self._atk([15], damage_dice="banana")
        self.assertFalse(r.get("success", True))
        self.assertIn("error", r)


class NpcAttackTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_npc_attack_applies_to_player_hp(self):
        with H.fixed_rolls([15, 3]):
            r = H.ds.resolve_attack(actor="Goblin", attack_modifier=4, target_ac=13,
                                    damage_dice="1d6", damage_modifier=2,
                                    is_npc_attack=True, target_name="Senna", target_current_hp=13)
        self.assertIn("hp_change", r)
        self.assertEqual(r["hp_change"]["new_value"], 8)  # 13 - 5
        self.assertEqual(H.dbv("current_hit_points"), 8)

    def test_temporary_hp_absorbs_damage_first(self):
        with H.fixed_rolls([8]):
            H.ds.resolve_magic(spell_name="False Life", actor="Senna", slot_level=1, target_name="Senna")
        self.assertEqual(H.dbv("temporary_hit_points"), 12)
        with H.fixed_rolls([15, 3]):
            H.ds.resolve_attack(actor="Goblin", attack_modifier=4, target_ac=13,
                                damage_dice="1d6", damage_modifier=2,
                                is_npc_attack=True, target_name="Senna", target_current_hp=13)
        # 5 damage fully absorbed by 12 THP
        self.assertEqual(H.dbv("temporary_hit_points"), 7)
        self.assertEqual(H.dbv("current_hit_points"), 13)


if __name__ == "__main__":
    unittest.main()
