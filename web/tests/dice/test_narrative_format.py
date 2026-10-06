"""narrative_format on state-mutation tools (so the GM need not parse the raw JSON)."""

import unittest

from web.tests.dice import harness as H


class NarrativeFormatTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_modify_player_numeric_has_narrative_format(self):
        r = H.ds.modify_player_numeric(key="gold", delta=5)
        self.assertEqual(r["old_value"], 120)
        self.assertEqual(r["new_value"], 125)
        self.assertIn("125", r["narrative_format"])
        self.assertIn("+5", r["narrative_format"])

    def test_update_player_list_add_has_narrative_format(self):
        r = H.ds.update_player_list(key="inventory", item="Dagger", action="add")
        self.assertIn("Dagger", r["narrative_format"])
        self.assertIn("inventory", r["narrative_format"])
        self.assertIn("Encumbrance", r["narrative_format"])

    def test_update_player_list_remove_equipped_notes_unequip(self):
        H.ds.equip_item(item="Shield", action="equip")
        r = H.ds.update_player_list(key="inventory", item="Shield", action="remove")
        self.assertTrue(r.get("unequipped"))
        self.assertIn("unequipped", r["narrative_format"])


if __name__ == "__main__":
    unittest.main()
