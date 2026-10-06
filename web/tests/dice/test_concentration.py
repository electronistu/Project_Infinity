"""SRD 5.1 concentration: tracking, the damage CON save, and how it ends."""

import unittest

from web.tests.dice import harness as H


def _cast_bless():
    return H.ds.resolve_magic(spell_name="Bless", actor="Father Aldric", slot_level=1)


class ConcentrationTrackingTest(H.EngineCase):
    player_factory = staticmethod(H.cleric_l5)

    def test_concentration_stored_on_cast(self):
        _cast_bless()
        conc = H.ds._get_concentration(H.ds.DB_CONNECTION.cursor())
        self.assertIsNotNone(conc)
        self.assertEqual(conc["spell"], "Bless")

    def test_non_concentration_spell_does_not_set_it(self):
        H.ds.resolve_magic(spell_name="Cure Wounds", actor="Father Aldric", slot_level=1)
        self.assertIsNone(H.ds._get_concentration(H.ds.DB_CONNECTION.cursor()))

    def test_second_concentration_spell_replaces_the_first(self):
        _cast_bless()
        r = H.ds.resolve_magic(spell_name="Shield of Faith", actor="Father Aldric", slot_level=1)
        self.assertEqual(r["concentration"], "Shield of Faith")
        self.assertEqual(r["concentration_replaced"]["spell"], "Bless")
        self.assertEqual(
            H.ds._get_concentration(H.ds.DB_CONNECTION.cursor())["spell"], "Shield of Faith")
        self.assertNotIn("Bless", H.dbv("active_effects"))

    def test_long_rest_clears_concentration(self):
        _cast_bless()
        H.ds.rest("long")
        self.assertIsNone(H.ds._get_concentration(H.ds.DB_CONNECTION.cursor()))


class ConcentrationSaveTest(H.EngineCase):
    player_factory = staticmethod(H.cleric_l5)  # CON +2

    def test_failed_save_ends_the_spell(self):
        _cast_bless()
        with H.rolls_always(1):
            r = H.ds.modify_player_numeric("current_hit_points", -5)
        self.assertIn("concentration_check", r)
        self.assertFalse(r["concentration_check"]["success"])
        self.assertEqual(r["concentration_check"]["dc"], 10)
        self.assertIsNone(H.ds._get_concentration(H.ds.DB_CONNECTION.cursor()))
        self.assertNotIn("Bless", H.dbv("active_effects"))
        self.assertIn("concentration_broken", r)

    def test_successful_save_keeps_the_spell(self):
        _cast_bless()
        with H.rolls_always(15):
            r = H.ds.modify_player_numeric("current_hit_points", -5)
        self.assertTrue(r["concentration_check"]["success"])
        self.assertEqual(
            H.ds._get_concentration(H.ds.DB_CONNECTION.cursor())["spell"], "Bless")

    def test_dc_scales_with_damage(self):
        _cast_bless()
        with H.rolls_always(15):
            r = H.ds.modify_player_numeric("current_hit_points", -30)
        self.assertEqual(r["concentration_check"]["dc"], 15)

    def test_no_save_when_not_concentrating(self):
        with H.rolls_always(1):
            r = H.ds.modify_player_numeric("current_hit_points", -5)
        self.assertNotIn("concentration_check", r)


class ConcentrationBreakTest(H.EngineCase):
    player_factory = staticmethod(H.cleric_l5)

    def test_incapacitating_condition_breaks_it(self):
        _cast_bless()
        cursor = H.ds.DB_CONNECTION.cursor()
        H.ds._apply_combat_condition(cursor, "Father Aldric", "unconscious")
        self.assertIsNone(H.ds._get_concentration(cursor))

    def test_dropping_to_zero_breaks_it(self):
        _cast_bless()
        H.ds.modify_player_numeric("current_hit_points", -38)  # exactly 0
        self.assertIsNone(H.ds._get_concentration(H.ds.DB_CONNECTION.cursor()))

    def test_manual_remove_clears_concentration(self):
        _cast_bless()
        H.ds.update_player_list(key="active_effects", item="Bless", action="remove")
        self.assertIsNone(H.ds._get_concentration(H.ds.DB_CONNECTION.cursor()))


if __name__ == "__main__":
    unittest.main()
