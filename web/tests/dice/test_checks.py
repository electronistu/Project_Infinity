"""perform_check: success/failure, natural 20/1, narrative format."""

import unittest

from web.tests.dice import harness as H


class PerformCheckTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_success(self):
        with H.fixed_rolls([18]) as calls:
            r = H.ds.perform_check(5, 15, "Athletics")
        self.assertEqual(calls, [(1, 20)])
        self.assertEqual(r["base_roll"], 18)
        self.assertEqual(r["total"], 23)
        self.assertEqual(r["outcome"], "Success")
        self.assertEqual(r["actor"], "Borin")
        self.assertEqual(r["dc_to_beat"], 15)
        self.assertEqual(r["narrative_format"], "Borin Athletics: 23 vs DC 15 (Success) (18 + 5)")

    def test_failure(self):
        with H.fixed_rolls([3]):
            r = H.ds.perform_check(2, 15, "Stealth")
        self.assertEqual(r["total"], 5)
        self.assertEqual(r["outcome"], "Failure")

    def test_exact_dc_succeeds(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(5, 15, "Perception")  # 15 >= 15
        self.assertEqual(r["outcome"], "Success")

    def test_natural_twenty_is_critical_even_below_dc(self):
        with H.fixed_rolls([20]):
            r = H.ds.perform_check(-5, 99, "Luck")
        self.assertEqual(r["outcome"], "Critical Success")
        self.assertEqual(r["total"], 15)  # low total, but nat 20 still wins

    def test_natural_one_is_critical_failure_even_above_dc(self):
        with H.fixed_rolls([1]):
            r = H.ds.perform_check(50, 5, "Sure Thing")
        self.assertEqual(r["outcome"], "Critical Failure")

    def test_negative_modifier(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(-3, 8, "Clumsy")
        self.assertEqual(r["total"], 7)
        self.assertIn("-3", r["narrative_format"])


if __name__ == "__main__":
    unittest.main()
