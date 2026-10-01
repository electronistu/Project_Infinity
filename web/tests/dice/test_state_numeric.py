"""modify_player_numeric: gold/xp/hp/slots/consumables + level-up."""

import unittest

from web.tests.dice import harness as H


class ModifyNumericTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_gold_delta(self):
        r = H.ds.modify_player_numeric("gold", 50)
        self.assertEqual((r["old_value"], r["new_value"], r["delta"]), (120, 170, 50))
        self.assertEqual(H.dbv("gold"), 170)

    def test_unknown_root_key_errors(self):
        r = H.ds.modify_player_numeric("nonsense", 1)
        self.assertFalse(r["success"])
        self.assertIn("available_keys", r)

    def test_hp_clamps_at_zero_and_flags_death(self):
        r = H.ds.modify_player_numeric("current_hit_points", -999)
        self.assertTrue(r["clamped"])
        self.assertEqual(r["new_value"], 0)
        self.assertEqual(r["status"], "Unconscious")
        self.assertTrue(r["death_saves"])
        self.assertEqual(H.dbv("current_hit_points"), 0)

    def test_hp_clamps_at_max(self):
        H.ds.modify_player_numeric("current_hit_points", -20)
        r = H.ds.modify_player_numeric("current_hit_points", +100)
        self.assertTrue(r["clamped"])
        self.assertEqual(r["new_value"], 44)

    def test_hp_status_reported(self):
        r = H.ds.modify_player_numeric("current_hit_points", -14)  # 44 -> 30 (~68%)
        self.assertIn("hp_status", r)
        self.assertEqual(r["new_value"], 30)


class ConsumablesTest(H.EngineCase):
    player_factory = staticmethod(lambda: H.make_player(consumables={"Arrows": 5}))

    def test_decrement(self):
        r = H.ds.modify_player_numeric("consumables.Arrows", -2)
        self.assertEqual(r["new_value"], 3)
        self.assertEqual(H.dbv("consumables")["Arrows"], 3)

    def test_depletes_and_removes(self):
        r = H.ds.modify_player_numeric("consumables.Arrows", -5)
        self.assertTrue(r.get("item_depleted"))
        self.assertNotIn("Arrows", H.dbv("consumables"))

    def test_auto_creates_missing_consumable(self):
        H.ds.modify_player_numeric("consumables.Torches", 3)
        self.assertEqual(H.dbv("consumables")["Torches"], 3)


class SlotNumericTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_slot_decrement_reports_remaining(self):
        r = H.ds.modify_player_numeric("spellcasting.slots.1", -1)
        self.assertEqual(r["remaining_slots"]["lv1"], 3)
        self.assertEqual(H.dbv("spellcasting")["slots"]["1"], 3)

    def test_slot_drain_below_zero_is_not_clamped_by_modify(self):
        # Documents current behaviour: modify_player_numeric does NOT clamp a slot
        # drain; casting-time validation in resolve_magic is what blocks use.
        r = H.ds.modify_player_numeric("spellcasting.slots.1", -10)
        self.assertEqual(r["new_value"], -6)


class XpLevelUpTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)  # xp 1310, level 3

    def test_crossing_threshold_levels_up(self):
        with H.fixed_rolls([4, 4, 4]):
            r = H.ds.modify_player_numeric("xp", 1890)  # 3200 -> level 4
        self.assertTrue(r.get("level_up"), r)
        self.assertEqual(r["old_level"], 3)
        self.assertEqual(r["new_level"], 4)
        self.assertEqual(H.dbv("level"), 4)
        self.assertEqual(H.dbv("proficiency_bonus"), 2)  # level 4 prof is still +2

    def test_no_level_up_below_threshold(self):
        r = H.ds.modify_player_numeric("xp", 100)
        self.assertNotIn("level_up", r)
        self.assertEqual(H.dbv("level"), 3)


if __name__ == "__main__":
    unittest.main()
