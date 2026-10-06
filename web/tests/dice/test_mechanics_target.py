"""Mechanics lines name their target (attacks + single-target spells)."""

import unittest

from web.tests.dice import harness as H


class TargetLabelTest(H.EngineCase):
    player_factory = staticmethod(H.make_player)  # name "Tester"

    def test_real_name_is_used_as_is(self):
        self.assertEqual(H.ds._target_label(H.ds.DB_CONNECTION.cursor(), "Goblin"), "Goblin")

    def test_player_placeholder_resolves_to_the_sheet_name(self):
        self.assertEqual(H.ds._target_label(H.ds.DB_CONNECTION.cursor(), "{player_name}"), "Tester")

    def test_empty_player_target_resolves_to_the_sheet_name(self):
        self.assertEqual(
            H.ds._target_label(H.ds.DB_CONNECTION.cursor(), "", is_player_target=True), "Tester")

    def test_empty_non_player_target_has_no_label(self):
        self.assertEqual(H.ds._target_label(H.ds.DB_CONNECTION.cursor(), ""), "")
        self.assertEqual(H.ds._target_suffix(H.ds.DB_CONNECTION.cursor(), ""), "")

    def test_suffix_uses_an_arrow(self):
        self.assertEqual(H.ds._target_suffix(H.ds.DB_CONNECTION.cursor(), "Goblin"),
                         " \u2192 Goblin")


class SingleTargetSpellTargetTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)  # name "Senna"

    def test_npc_save_spell_names_the_player(self):
        with H.rolls_always(4):
            r = H.ds.resolve_magic(spell_name="Zap", actor="Goblin", is_npc_attack=True,
                                   attack_type="saving_throw", save_type="dex", spell_save_dc=13,
                                   damage_dice="2d6", damage_type="fire",
                                   target_name="{player_name}")
        self.assertIn("Damage → Senna", r["narrative_format"])
        self.assertIn("Senna DEX Save", r["narrative_format"])

    def test_player_attack_spell_names_the_target(self):
        with H.rolls_always(4):
            r = H.ds.resolve_magic(spell_name="Zap", actor="Senna", attack_type="attack_roll",
                                   spell_attack_modifier=5, target_ac=8, damage_dice="2d6",
                                   damage_type="fire", target_name="Goblin")
        self.assertIn("Senna Zap Attack → Goblin", r["narrative_format"])
        self.assertIn("Damage → Goblin", r["narrative_format"])


if __name__ == "__main__":
    unittest.main()
