"""Magic Missile — per-dart resolution (SRD) and clearer breakdown.

SRD 5.1: "You create three glowing darts ... A dart deals 1d4+1 force damage to
its target. The darts all strike simultaneously, and you can direct them to hit
one creature or several."

Config is now per-projectile: damage_dice 1d4, damage_modifier 1,
projectiles 3, projectiles_per_level 1 (attack_type: automatic).
"""

import unittest

from web.tests.dice import harness as H


class MagicMissileTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_level_1_single_target_three_darts(self):
        with H.fixed_rolls([3, 3, 3]) as calls:
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna",
                                   target_name="Goblin", target_ac=15,
                                   target_current_hp=30, slot_level=1)
        self.assertEqual(calls, [(1, 4)] * 3)               # three d4s, no attack roll
        self.assertEqual(r["attack_type"], "automatic")
        self.assertEqual(r["projectiles"], 3)
        self.assertEqual([d["damage"] for d in r["per_projectile"]], [4, 4, 4])  # 3 + 1 each
        self.assertEqual(r["damage_total"], 12)
        self.assertEqual(r["damage_type"], "force")
        self.assertIn("3 darts: 4, 4, 4", r["narrative_format"])

    def test_upcast_level_2_adds_a_dart(self):
        with H.fixed_rolls([3]) as calls:
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna",
                                   target_name="Goblin", target_ac=15,
                                   target_current_hp=40, slot_level=2)
        self.assertEqual(calls, [(1, 4)] * 4)               # four darts
        self.assertEqual(r["projectiles"], 4)
        self.assertEqual(r["damage_total"], 16)             # 4 x (3+1)
        self.assertEqual(r["slot_level_used"], 2)

    def test_split_across_two_targets(self):
        with H.fixed_rolls([3]) as calls:
            r = H.ds.resolve_magic(
                spell_name="Magic Missile", actor="Senna", slot_level=1,
                targets=[{"name": "Guard 1", "darts": 1, "current_hp": 20},
                         {"name": "Guard 2", "darts": 2, "current_hp": 20}],
            )
        self.assertEqual(calls, [(1, 4)] * 3)
        by = {t["name"]: t for t in r["targets"]}
        self.assertEqual(by["Guard 1"]["damage"], 4)
        self.assertEqual(by["Guard 2"]["damage"], 8)
        self.assertEqual(by["Guard 1"]["remaining_hp"], 16)
        self.assertEqual(by["Guard 2"]["remaining_hp"], 12)
        self.assertIn("Guard 1: 4 (1 dart: 4)", r["narrative_format"])
        self.assertIn("Guard 2: 8 (2 darts: 4, 4)", r["narrative_format"])

    def test_darts_must_sum_to_projectile_count(self):
        r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                               targets=[{"name": "A", "darts": 1}, {"name": "B", "darts": 1}])
        self.assertFalse(r["success"])
        self.assertEqual(r["expected_projectiles"], 3)
        self.assertIn("must equal", r["error"])

    def test_multiple_targets_without_darts_errors(self):
        r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                               targets=[{"name": "A", "current_hp": 10},
                                        {"name": "B", "current_hp": 10}])
        self.assertFalse(r["success"])
        self.assertIn("darts", r["error"])

    def test_single_target_defaults_to_all_darts(self):
        with H.fixed_rolls([1, 1, 1]):
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                                   targets=[{"name": "Solo", "current_hp": 50}])
        self.assertEqual(r["projectiles"], 3)
        self.assertEqual([t["damage"] for t in r["targets"]], [6])  # 3 x (1+1)

    def test_simultaneous_overkill_keeps_all_darts(self):
        # A 1-HP target still takes all three darts (they strike simultaneously).
        with H.fixed_rolls([3]) as calls:
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                                   targets=[{"name": "Mook", "current_hp": 1}])
        self.assertEqual(calls, [(1, 4)] * 3)
        self.assertEqual(r["damage_total"], 12)             # not stopped at 0 HP
        self.assertTrue(r["targets"][0]["killed"])

    def test_empty_slot_is_rejected(self):
        H.ds.modify_player_numeric("spellcasting.slots.1", -4)
        r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                               target_name="Goblin", target_ac=15, target_current_hp=20)
        self.assertTrue(r.get("error"))
        self.assertEqual(H.dbv("spellcasting")["slots"]["1"], 0)

    def test_kill_awards_xp(self):
        with H.fixed_rolls([3]):
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                                   target_name="Familiar", target_current_hp=4,
                                   challenge_rating=0.125)
        self.assertTrue(r["targets"][0]["killed"])
        self.assertEqual(r["xp_awarded"], 25)


if __name__ == "__main__":
    unittest.main()
