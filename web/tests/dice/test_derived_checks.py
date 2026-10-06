"""SRD 5.1 engine-derived ability/skill checks, Expertise, Jack of All Trades, passive scores."""

import unittest

from web.tests.dice import harness as H
import skills


class SkillMapTest(unittest.TestCase):
    def test_known_skills_map_to_abilities(self):
        self.assertEqual(skills.skill_ability("Athletics"), "str")
        self.assertEqual(skills.skill_ability("sleight of hand"), "dex")
        self.assertEqual(skills.skill_ability("  PERCEPTION "), "wis")
        self.assertIsNone(skills.skill_ability("Luck"))


class DerivedCheckTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)  # STR 18 (+4), DEX 14 (+2), WIS 12 (+1), prof +3

    def test_proficient_skill_gets_ability_and_proficiency(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Borin", dc=15, check_name="Athletics", ability="str")
        self.assertEqual(r["modifier"], 7)  # +4 STR + 3 prof
        self.assertEqual(r["check"]["skill"], "Athletics")
        self.assertTrue(r["check"]["proficient"])
        self.assertEqual(r["total"], 17)

    def test_unproficient_skill_gets_ability_only(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Borin", dc=10, check_name="Stealth")
        self.assertEqual(r["modifier"], 2)  # DEX +2, not proficient
        self.assertFalse(r["check"]["proficient"])

    def test_ability_is_derived_from_the_skill_when_omitted(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Borin", dc=10, check_name="Perception")
        self.assertEqual(r["modifier"], 4)  # WIS +1 + 3 prof
        self.assertEqual(r["check"]["ability"], "wis")

    def test_situational_modifier_adds_on_top(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Borin", dc=10, check_name="Athletics",
                                   ability="str", situational_modifier=2)
        self.assertEqual(r["modifier"], 9)

    def test_passed_modifier_is_ignored_for_a_known_skill(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(99, 10, "Athletics", actor="Borin", ability="str")
        self.assertEqual(r["modifier"], 7)

    def test_custom_check_with_no_ability_keeps_the_manual_modifier(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(modifier=4, dc=10, check_name="Luck", actor="Borin")
        self.assertEqual(r["modifier"], 4)
        self.assertNotIn("check", r)

    def test_npc_check_is_unchanged(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Goblin", modifier=5, dc=10, check_name="Athletics")
        self.assertEqual(r["modifier"], 5)
        self.assertNotIn("check", r)

    def test_save_path_is_unaffected(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Borin", dc=10, check_name="Constitution save",
                                   ability="con", save=True)
        self.assertIn("save", r)
        self.assertNotIn("check", r)
        self.assertEqual(r["modifier"], 5)  # CON +2 + 3 prof


class ExpertiseTest(H.EngineCase):
    player_factory = staticmethod(lambda: H.make_player(expertise=["Stealth"]))

    def test_expertise_doubles_the_proficiency_bonus(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Stealth")
        self.assertTrue(r["check"]["expertise"])
        self.assertEqual(r["check"]["proficiency_bonus"], 4)  # 2 x +2 prof
        self.assertEqual(r["modifier"], 6)  # DEX +2 + 4

    def test_expertise_added_at_runtime_applies(self):
        H.ds.update_player_list(key="expertise", item="Stealth", action="add")
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Stealth")
        self.assertEqual(r["modifier"], 6)


class JackOfAllTradesTest(H.EngineCase):
    player_factory = staticmethod(lambda: H.make_player(features=["Jack of All Trades"]))

    def test_half_proficiency_on_an_unproficient_check(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Stealth")
        self.assertTrue(r["check"]["jack_of_all_trades"])
        self.assertEqual(r["check"]["proficiency_bonus"], 1)  # floor(2/2)
        self.assertEqual(r["modifier"], 3)  # DEX +2 + 1

    def test_no_half_proficiency_when_already_proficient(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Athletics",
                                   ability="str")
        self.assertFalse(r["check"]["jack_of_all_trades"])
        self.assertEqual(r["modifier"], 5)  # STR +3 + 2 prof


class PassiveScoreTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)  # Perception +4, Insight +1, Investigation +0

    def test_passive_scores_are_ten_plus_modifier(self):
        cursor = H.ds.DB_CONNECTION.cursor()
        self.assertEqual(H.ds._passive_scores(cursor),
                         {"perception": 14, "investigation": 10, "insight": 11})

    def test_dump_exposes_the_passive_block(self):
        self.assertEqual(H.ds.dump_player_db()["_passive"]["perception"], 14)


if __name__ == "__main__":
    unittest.main()
