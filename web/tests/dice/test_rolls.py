"""roll_dice: notation, modifiers, bounds, narrative format."""

import unittest

from web.tests.dice import harness as H


class RollDiceTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_single_die_and_actor_default(self):
        with H.fixed_rolls([13]) as calls:
            r = H.ds.roll_dice("1d20")
        self.assertEqual(calls, [(1, 20)])
        self.assertEqual(r["rolls"], [13])
        self.assertEqual(r["total"], 13)
        self.assertEqual(r["actor"], "Senna")  # "{player_name}" resolved
        self.assertEqual(r["notation"], "1d20")
        self.assertIn("13", r["narrative_format"])

    def test_multi_die_with_modifier_exact(self):
        with H.fixed_rolls([4, 2, 2]) as calls:
            r = H.ds.roll_dice("3d4", 3, "Senna")
        self.assertEqual(calls, [(1, 4)] * 3)
        self.assertEqual(r["rolls"], [4, 2, 2])
        self.assertEqual(r["modifier"], 3)
        self.assertEqual(r["total"], 11)
        self.assertEqual(r["narrative_format"], "Senna 3d4: 11 (4 + 2 + 2 + 3)")

    def test_no_modifier_omits_plus(self):
        with H.fixed_rolls([3, 5]):
            r = H.ds.roll_dice("2d6", 0, "Goblin")
        self.assertEqual(r["narrative_format"], "Goblin 2d6: 8 (3 + 5)")

    def test_minimum_and_maximum_bounds(self):
        with H.rolls_always(1):
            self.assertEqual(H.ds.roll_dice("2d6", 0)["total"], 2)
        with H.rolls_always(6):
            self.assertEqual(H.ds.roll_dice("2d6", 5)["total"], 17)

    def test_negative_modifier(self):
        with H.fixed_rolls([6, 6]):
            r = H.ds.roll_dice("2d6", -3)
        self.assertEqual(r["total"], 9)
        self.assertIn("-3", r["narrative_format"])

    def test_invalid_notation(self):
        self.assertIn("error", H.ds.roll_dice("banana"))
        self.assertIn("error", H.ds.roll_dice("2d"))
        self.assertIn("error", H.ds.roll_dice("0d6"))

    def test_implicit_one_die(self):
        with H.fixed_rolls([10]):
            self.assertEqual(H.ds.roll_dice("d20")["rolls"], [10])


if __name__ == "__main__":
    unittest.main()
