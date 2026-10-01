"""Private helpers + spell/derived data in dice_server.py."""

import unittest

from web.tests.dice import harness as H


class PureHelpersTest(unittest.TestCase):
    def test_parse_and_roll_dice(self):
        with H.fixed_rolls([6, 3, 1]) as calls:
            die, rolls, total = H.ds._parse_and_roll_dice("3d6")
        self.assertEqual(die, 6)
        self.assertEqual(rolls, [6, 3, 1])
        self.assertEqual(total, 10)
        self.assertEqual(calls, [(1, 6)] * 3)

    def test_parse_and_roll_dice_plain_number(self):
        self.assertEqual(H.ds._parse_and_roll_dice("7"), (0, [], 7))
        self.assertEqual(H.ds._parse_and_roll_dice(5), (0, [], 5))

    def test_parse_and_roll_dice_invalid(self):
        self.assertEqual(H.ds._parse_and_roll_dice("banana"), (None, [], 0))

    def test_combine_dice(self):
        self.assertEqual(H.ds._combine_dice("3d4", "1d4"), "4d4")
        self.assertEqual(H.ds._combine_dice("1d8", ""), "1d8")
        self.assertEqual(H.ds._combine_dice("1d8", "2d6"), "1d8+2d6")

    def test_multiply_dice_notation(self):
        self.assertEqual(H.ds._multiply_dice_notation("1d6", 3), "3d6")
        self.assertEqual(H.ds._multiply_dice_notation("2d4", 2), "4d4")
        self.assertEqual(H.ds._multiply_dice_notation("1d6", 1), "1d6")

    def test_parse_higher_levels(self):
        self.assertEqual(H.ds._parse_higher_levels("+1d4+1"), ("dice", 1, 4, 1))
        self.assertEqual(H.ds._parse_higher_levels("+2d6"), ("dice", 2, 6, 0))
        self.assertEqual(H.ds._parse_higher_levels("+1"), ("flat", 1, 0, 0))
        self.assertIsNone(H.ds._parse_higher_levels("garbage"))

    def test_get_level_for_xp(self):
        for xp, lvl in [(0, 1), (299, 1), (300, 2), (899, 2), (900, 3), (6500, 5), (400000, 20)]:
            self.assertEqual(H.ds.get_level_for_xp(xp), lvl, f"xp={xp}")

    def test_hp_status_tags(self):
        self.assertEqual(H.ds._hp_status_tag(10, 10), "Healthy")
        self.assertEqual(H.ds._hp_status_tag(7, 10), "Wounded")   # 70%
        self.assertEqual(H.ds._hp_status_tag(4, 10), "Bloodied")  # 40%
        self.assertEqual(H.ds._hp_status_tag(2, 10), "Critical")  # 20%
        self.assertEqual(H.ds._hp_status_tag(0, 10), "Unconscious")
        self.assertEqual(H.ds._hp_status_tag(0, 0), "Unknown")

    def test_compute_spell_damage_upcast(self):
        spell = {"damage_dice": "3d6", "damage_modifier": 2, "level": 3, "higher_levels": "+1d6"}
        self.assertEqual(H.ds._compute_spell_damage(spell, 5, 3), ("3d6", 2, None))
        self.assertEqual(H.ds._compute_spell_damage(spell, 5, 4), ("4d6", 2, None))
        self.assertEqual(H.ds._compute_spell_damage(spell, 5, 5), ("5d6", 2, None))

    def test_compute_spell_damage_flat_upcast(self):
        spell = {"damage_dice": "1d8", "damage_modifier": 0, "level": 1, "higher_levels": "+1"}
        self.assertEqual(H.ds._compute_spell_damage(spell, 1, 3), ("1d8", 2, None))

    def test_compute_spell_damage_cantrip_scaling(self):
        fb = H.ds._load_spells()["fire bolt"]
        for char_level, dice in [(1, "1d10"), (4, "1d10"), (5, "2d10"), (10, "2d10"),
                                 (11, "3d10"), (16, "3d10"), (17, "4d10")]:
            self.assertEqual(H.ds._compute_spell_damage(fb, char_level, None)[0], dice, f"lvl {char_level}")

    def test_spell_db_is_cached(self):
        self.assertIs(H.ds._load_spells(), H.ds._load_spells())
        self.assertIn("magic missile", H.ds._load_spells())


class PreparedSpellHelpersTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def _cursor(self):
        return H.ds.DB_CONNECTION.cursor()

    def test_max_prepared_wizard(self):
        # INT 16 -> +3, level 3 -> 3 + 3 = 6
        self.assertEqual(H.ds.get_max_prepared_spells(self._cursor()), 6)

    def test_prepared_info(self):
        info = H.ds.build_prepared_spells_info(self._cursor())
        self.assertEqual(info["current_count"], 2)
        self.assertEqual(info["max_count"], 6)
        self.assertEqual(info["available_slots"], 4)
        self.assertFalse(info["at_capacity"])


class PreparedSpellHelpersClericTest(H.EngineCase):
    player_factory = staticmethod(H.cleric_l5)

    def test_max_prepared_cleric(self):
        # WIS 18 -> +4, level 5 -> 4 + 5 = 9
        self.assertEqual(H.ds.get_max_prepared_spells(H.ds.DB_CONNECTION.cursor()), 9)


if __name__ == "__main__":
    unittest.main()
