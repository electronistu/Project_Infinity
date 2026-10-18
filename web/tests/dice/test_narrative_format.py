"""narrative_format on state-mutation tools (so the GM need not parse the raw JSON)."""

import unittest

from web.tests.dice import harness as H
from rules.sheet import _ac_note


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
        self.assertNotIn("Encumbrance", r["narrative_format"])  # 1 lb stays unencumbered

    def test_update_player_list_reports_encumbrance_only_when_it_happens(self):
        # STR 18 -> encumbered over 90 lb; the fighter carries ~64 lb of gear.
        r = H.ds.update_player_list(key="inventory", item="Anvil: a heavy block of iron",
                                    action="add", weight=40.0)
        self.assertEqual(r["carrying"]["status"], "encumbered")
        self.assertIn("Encumbrance: encumbered", r["narrative_format"])
        # Already encumbered -> adding more must NOT repeat the line.
        r2 = H.ds.update_player_list(key="inventory", item="Pebble: a small stone",
                                     action="add", weight=0.1)
        self.assertNotIn("Encumbrance", r2["narrative_format"])

    def test_update_player_list_remove_equipped_notes_unequip(self):
        H.ds.equip_item(item="Shield", action="equip")
        r = H.ds.update_player_list(key="inventory", item="Shield", action="remove")
        self.assertTrue(r.get("unequipped"))
        self.assertIn("unequipped", r["narrative_format"])


class EquipNarrativeTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_ac_note_only_when_the_ac_changed(self):
        self.assertEqual(_ac_note(13, 13), "")
        self.assertEqual(_ac_note(13, 15), " — AC 13 → 15")

    def test_equipping_armour_reports_the_ac_change(self):
        r = H.ds.equip_item(item="Chain Mail")
        self.assertNotEqual(r["armor_class_before"], r["armor_class_after"])
        self.assertIn("AC", r["narrative_format"])

    def test_equipping_a_weapon_omits_the_unchanged_ac(self):
        H.ds.equip_item(item="Chain Mail")  # establishes the equipped set + derived AC
        H.ds.update_player_list(key="inventory", item="Dagger", action="add")
        r = H.ds.equip_item(item="Dagger")
        self.assertEqual(r["armor_class_before"], r["armor_class_after"])
        self.assertIn("Dagger equipped (main hand)", r["narrative_format"])
        self.assertNotIn("AC", r["narrative_format"])


class GmOnlyRefusalTest(H.EngineCase):
    """A GM-correctable error must never compose a player-facing Mechanics line.

    `web/session_turn.py::_collect_mechanics` turns every `narrative_format` into one, and
    `_action_failure`'s contract says shape errors (a missing parameter, an invalid notation,
    an unknown registry name) must NOT set it. Live 2026-10-18 a `register_combatants`
    refusal ("Refused: a combatant cannot carry both `name` and `names`") was shown in the
    Mechanics block while the GM retried the call correctly.
    """

    player_factory = staticmethod(H.fighter_l5)

    def test_register_combatants_shape_refusals_are_gm_only(self):
        cases = [
            ({"name": "Neferhotep", "names": ["Night Watch 1"]}, "name_and_names"),
            ({"names": []}, "empty_names"),
            ({"names": ["Night Watch 1", "   "]}, "blank_name"),
            ({"names": ["Night Watch 1", "night watch 1"]}, "duplicate_name"),
        ]
        for entry, error in cases:
            with self.subTest(error=error):
                r = H.ds.register_combatants([entry])
                self.assertFalse(r.get("success"), r)
                self.assertEqual(r.get("error"), error, r)
                self.assertNotIn("narrative_format", r, r)

    def test_update_combatant_invalid_status_is_gm_only(self):
        H.ds.register_combatants([{"name": "Goblin", "hp": 7, "ac": 15}])
        r = H.ds.update_combatant(name="Goblin", status="banana")
        self.assertFalse(r.get("success"), r)
        self.assertEqual(r.get("error"), "invalid_status")
        self.assertNotIn("narrative_format", r)

    def test_a_fiction_refusal_still_reaches_the_block(self):
        # The other half of the convention: a spent or refused ACTION is a fact of the fiction
        # and must still tell the player what happened.
        H.ds.modify_player_numeric("current_hit_points", -44)  # exactly 0
        r = H.ds.rest("long")
        self.assertFalse(r.get("success"), r)
        self.assertIn("0 HP", r.get("narrative_format", ""))


if __name__ == "__main__":
    unittest.main()
