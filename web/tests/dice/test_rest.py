"""rest: short/long recovery, hit dice, slots, effects, prepared replacement."""

import unittest

from web.tests.dice import harness as H


class ShortRestTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_short_rest_spends_hit_dice_and_heals(self):
        H.ds.modify_player_numeric("current_hit_points", -8)  # 13 -> 5
        with H.fixed_rolls([4, 4, 4]):
            r = H.ds.rest("short")
        self.assertTrue(r["success"])
        self.assertEqual(r["changes"]["hit_dice"]["spent"], 3)
        self.assertEqual(r["changes"]["hit_dice"]["new"], 0)
        self.assertEqual(H.dbv("current_hit_points"), 13)
        self.assertEqual(r["changes"]["hp"]["max_hp"], 13)
        self.assertEqual(r["changes"]["hp"]["healing_applied"], 8)  # rolled 9, capped at max

    def test_short_rest_arcane_recovery_for_wizard(self):
        H.ds.modify_player_numeric("spellcasting.slots.1", -3)  # 1st-level 4 -> 1
        with H.fixed_rolls([4, 4, 4]):
            r = H.ds.rest("short")
        self.assertIn("arcane_recovery", r["changes"])
        self.assertEqual(H.dbv("spellcasting")["slots"]["1"], 3)  # +2 recovered


class LongRestTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_long_rest_restores_hp_slots_and_effects(self):
        H.ds.modify_player_numeric("current_hit_points", -5)
        H.ds.modify_player_numeric("spellcasting.slots.1", -3)
        with H.fixed_rolls([10]):
            H.ds.resolve_magic(spell_name="Mage Armor", actor="Senna", slot_level=1, target_name="Senna")
        r = H.ds.rest("long")
        self.assertTrue(r["success"])
        self.assertEqual(H.dbv("current_hit_points"), 13)
        self.assertEqual(r["changes"]["hp"]["healing_applied"], 5)
        self.assertEqual(r["changes"]["hp"]["max_hp"], 13)
        self.assertEqual(H.dbv("spellcasting")["slots"]["1"], 4)
        self.assertEqual(H.dbv("active_effects"), [])
        self.assertEqual(H.dbv("armor_class"), 13)  # buff reverted

    def test_long_rest_rejected_at_zero_hp(self):
        H.ds.modify_player_numeric("current_hit_points", -999)
        r = H.ds.rest("long")
        self.assertFalse(r["success"])
        self.assertIn("0 HP", r["error"])
        self.assertIn("Long rest — impossible while at 0 HP", r["narrative_format"])

    def test_long_rest_replaces_prepared_spells(self):
        r = H.ds.rest("long", prepared_spells=["Shield", "Sleep"])
        self.assertTrue(r["success"])
        self.assertEqual([p["name"] for p in H.dbv("spellcasting")["spells_prepared"]], ["Shield", "Sleep"])

    def test_long_rest_rejects_over_capacity_prepared(self):
        r = H.ds.rest("long", prepared_spells=["A", "B", "C", "D", "E", "F", "G"])
        self.assertIn("prepared_spells_error", r["changes"])


class WarlockShortRestTest(H.EngineCase):
    player_factory = staticmethod(H.warlock_l3)  # pact magic {"2": 2}

    def test_pact_slots_restored_on_short_rest(self):
        # Warlock has no hit-dice damage here; just drain a slot and rest.
        H.ds.modify_player_numeric("spellcasting.slots.2", -1)
        self.assertEqual(H.dbv("spellcasting")["slots"]["2"], 1)
        H.ds.rest("short")
        self.assertEqual(H.dbv("spellcasting")["slots"]["2"], 2)


if __name__ == "__main__":
    unittest.main()
