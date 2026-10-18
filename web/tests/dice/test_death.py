"""SRD 5.1 instant death and death saving throws."""

import unittest

from web.tests.dice import harness as H


class InstantDeathTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)  # 44 HP

    def test_drop_to_zero_survives_to_death_saves(self):
        r = H.ds.modify_player_numeric("current_hit_points", -44)  # exactly 0
        self.assertNotIn("instant_death", r)
        self.assertTrue(r["death_saves"])

    def test_overshoot_equal_to_max_is_instant_death(self):
        r = H.ds.modify_player_numeric("current_hit_points", -88)  # overshoot 44
        self.assertTrue(r["instant_death"])
        self.assertTrue(r["dead"])
        self.assertEqual(r["status"], "Dead")
        self.assertEqual(r["death_cause"], "massive damage")
        self.assertEqual(H.dbv("dead"), 1)

    def test_instant_death_cannot_be_healed(self):
        H.ds.modify_player_numeric("current_hit_points", -88)
        r = H.ds.modify_player_numeric("current_hit_points", 20)
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "dead")
        self.assertEqual(H.dbv("current_hit_points"), 0)

    def test_overshoot_below_max_survives(self):
        r = H.ds.modify_player_numeric("current_hit_points", -87)  # overshoot 43
        self.assertNotIn("instant_death", r)
        self.assertTrue(r["death_saves"])


class DeathSaveTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def _down(self):
        H.ds.modify_player_numeric("current_hit_points", -44)  # exactly 0, no overshoot

    def test_requires_zero_hp(self):
        r = H.ds.make_death_save()
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "not_dying")

    def test_success_can_stabilise(self):
        self._down()
        with H.fixed_rolls([12, 12, 12]):
            H.ds.make_death_save()
            H.ds.make_death_save()
            r = H.ds.make_death_save()
        self.assertEqual(r["outcome"], "stable")
        self.assertTrue(r["stable"])
        self.assertEqual(H.dbv("death_save_successes"), 0)
        self.assertEqual(H.dbv("death_save_failures"), 0)
        self.assertEqual(H.dbv("stable"), 1)

    def test_stable_makes_no_further_saves(self):
        self._down()
        with H.fixed_rolls([12, 12, 12]):
            H.ds.make_death_save()
            H.ds.make_death_save()
            H.ds.make_death_save()
        # No roll, no counter move: stable is a state, not a cleared counter.
        r = H.ds.make_death_save()
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "stable")
        self.assertTrue(r["stable"])
        self.assertEqual(H.dbv("death_save_successes"), 0)
        self.assertEqual(H.dbv("death_save_failures"), 0)

    def test_damage_while_stable_starts_a_new_dying_episode(self):
        self._down()
        with H.fixed_rolls([12, 12, 12]):
            H.ds.make_death_save()
            H.ds.make_death_save()
            H.ds.make_death_save()
        r = H.ds.modify_player_numeric("current_hit_points", -1)
        self.assertEqual(r["death_save_failures"], 1)
        self.assertEqual(H.dbv("stable"), 0)
        self.assertEqual(H.dbv("death_save_failures"), 1)

    def test_three_failures_kill(self):
        self._down()
        with H.fixed_rolls([5, 5, 5]):
            H.ds.make_death_save()
            H.ds.make_death_save()
            r = H.ds.make_death_save()
        self.assertEqual(r["outcome"], "dead")
        self.assertTrue(r["dead"])
        self.assertEqual(r["failures"], 3)
        self.assertEqual(r["death_cause"], "third death save failure")
        self.assertEqual(H.dbv("dead"), 1)
        self.assertEqual(H.dbv("stable"), 0)
        self.assertEqual(H.ds.make_death_save()["error"], "already_dead")

    def test_healing_cannot_revive_the_dead(self):
        self._down()
        with H.fixed_rolls([5, 5, 5]):
            H.ds.make_death_save()
            H.ds.make_death_save()
            H.ds.make_death_save()
        r = H.ds.modify_player_numeric("current_hit_points", 5)
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "dead")
        self.assertEqual(H.dbv("current_hit_points"), 0)
        self.assertEqual(H.dbv("dead"), 1)

    def test_rest_cannot_revive_the_dead(self):
        self._down()
        with H.fixed_rolls([5, 5, 5]):
            H.ds.make_death_save()
            H.ds.make_death_save()
            H.ds.make_death_save()
        for rest_type in ("short", "long"):
            r = H.ds.rest(rest_type)
            self.assertFalse(r["success"], rest_type)
            self.assertEqual(H.dbv("current_hit_points"), 0, rest_type)
            self.assertEqual(H.dbv("dead"), 1, rest_type)

    def test_natural_20_revives_with_one_hp(self):
        self._down()
        with H.fixed_rolls([20]):
            r = H.ds.make_death_save()
        self.assertEqual(r["outcome"], "revived")
        self.assertEqual(H.dbv("current_hit_points"), 1)
        self.assertEqual(H.dbv("stable"), 0)

    def test_natural_1_is_two_failures(self):
        self._down()
        with H.fixed_rolls([1]):
            r = H.ds.make_death_save()
        self.assertEqual(r["failures"], 2)

    def test_damage_at_zero_is_a_failure(self):
        self._down()
        r = H.ds.modify_player_numeric("current_hit_points", -3)
        self.assertEqual(r["death_save_failures"], 1)

    def test_healing_clears_counters(self):
        self._down()
        H.ds.modify_player_numeric("current_hit_points", -3)  # 1 failure
        H.ds.modify_player_numeric("current_hit_points", 5)   # any real HP ends death saves
        self.assertEqual(H.dbv("death_save_failures"), 0)
        self.assertEqual(H.dbv("death_save_successes"), 0)


if __name__ == "__main__":
    unittest.main()
