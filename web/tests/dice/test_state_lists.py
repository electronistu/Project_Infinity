"""update_player_list: inventory / reputation / prepared capacity / buff revert."""

import unittest

from web.tests.dice import harness as H


class InventoryListTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_add_plain_entry_is_string(self):
        r = H.ds.update_player_list("inventory", "Rope", "add")
        self.assertTrue(r["success"])
        self.assertIn("Rope", H.dbv("inventory"))

    def test_add_with_description_is_dict(self):
        H.ds.update_player_list("inventory", "Rope: 50 feet of hempen rope", "add")
        self.assertIn({"name": "Rope", "description": "50 feet of hempen rope"}, H.dbv("inventory"))

    def test_duplicate_rejected(self):
        H.ds.update_player_list("inventory", "Rope", "add")
        r = H.ds.update_player_list("inventory", "Rope", "add")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "already_exists")

    def test_duplicate_matches_dict_entry_by_name(self):
        H.ds.update_player_list("inventory", "Rope: 50 feet", "add")
        r = H.ds.update_player_list("inventory", "Rope", "add")
        self.assertEqual(r["error"], "already_exists")

    def test_remove_by_name(self):
        r = H.ds.update_player_list("inventory", "Longsword", "remove")
        self.assertTrue(r["success"])
        self.assertNotIn("Longsword", H.dbv("inventory"))

    def test_remove_missing(self):
        r = H.ds.update_player_list("inventory", "Excalibur", "remove")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "not_found")

    def test_remove_dict_entry_by_name(self):
        H.ds.update_player_list("inventory", "Rope: 50 feet", "add")
        r = H.ds.update_player_list("inventory", "Rope", "remove")
        self.assertTrue(r["success"])
        self.assertNotIn("Rope", [i if isinstance(i, str) else i.get("name") for i in H.dbv("inventory")])

    def test_unknown_key(self):
        self.assertFalse(H.ds.update_player_list("nonsense", "x", "add")["success"])

    def test_bad_action(self):
        self.assertFalse(H.ds.update_player_list("inventory", "x", "frobnicate")["success"])


class ReputationListTest(H.EngineCase):
    # Kingdoms must exist (never invented), but a missing faction under an
    # existing kingdom is created, and a bare kingdom uses the 'misc' bucket.
    player_factory = staticmethod(lambda: H.make_player(reputation={"eldoria": {"guard": []}, "others": {}}))

    def test_add_reputation_entry(self):
        r = H.ds.update_player_list("reputation.eldoria.guard", "Saved a patrol: earned their trust", "add")
        self.assertTrue(r["success"])
        entry = H.dbv("reputation")["eldoria"]["guard"]
        self.assertIn({"name": "Saved a patrol", "description": "earned their trust"}, entry)

    def test_missing_faction_under_existing_kingdom_is_created(self):
        r = H.ds.update_player_list("reputation.eldoria.spies", "Bought a secret: paid in coin", "add")
        self.assertTrue(r["success"])
        self.assertIn({"name": "Bought a secret", "description": "paid in coin"},
                      H.dbv("reputation")["eldoria"]["spies"])

    def test_bare_kingdom_uses_default_bucket(self):
        r = H.ds.update_player_list("reputation.others",
                                    "Awakened Convert: a willing pawn", "add")
        self.assertTrue(r["success"])
        self.assertIn({"name": "Awakened Convert", "description": "a willing pawn"},
                      H.dbv("reputation")["others"]["misc"])

    def test_unknown_kingdom_path_is_rejected(self):
        # An unknown kingdom is never invented, bare or with a faction.
        self.assertFalse(H.ds.update_player_list("reputation.nowhere.void", "X: Y", "add")["success"])
        self.assertFalse(H.ds.update_player_list("reputation.nowhere", "X: Y", "add")["success"])

    def test_remove_from_bare_kingdom_path_is_rejected(self):
        # 'remove' must not auto-create a bucket; the bare path is a dict.
        self.assertFalse(H.ds.update_player_list("reputation.others", "Awakened Convert", "remove")["success"])


class PreparedCapacityTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)  # max prepared = 6, starts with 2

    def test_fill_to_capacity_then_reject(self):
        for name in ("A", "B", "C", "D"):
            self.assertTrue(H.ds.update_player_list("spellcasting.spells_prepared", name, "add")["success"])
        r = H.ds.update_player_list("spellcasting.spells_prepared", "Overflow", "add")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "spells_prepared_at_capacity")
        self.assertIn("spells_prepared_info", r)
        self.assertEqual(r["spells_prepared_info"]["at_capacity"], True)

    def test_removing_frees_a_slot(self):
        for name in ("A", "B", "C", "D"):
            H.ds.update_player_list("spellcasting.spells_prepared", name, "add")
        H.ds.update_player_list("spellcasting.spells_prepared", "A", "remove")
        self.assertTrue(H.ds.update_player_list("spellcasting.spells_prepared", "E", "add")["success"])


class ActiveEffectRevertTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def _mage_armor(self):
        with H.fixed_rolls([10]):
            H.ds.resolve_magic(spell_name="Mage Armor", actor="Senna", slot_level=1, target_name="Senna")

    def test_removing_buff_reverts_stat(self):
        self._mage_armor()
        self.assertEqual(H.dbv("armor_class"), 15)  # 13 + 2
        r = H.ds.update_player_list("active_effects", "Mage Armor", "remove")
        self.assertTrue(r["success"])
        self.assertIn("reverted", r)
        self.assertEqual(H.dbv("armor_class"), 13)

    def test_removing_buff_also_drops_it_from_active_effects(self):
        self._mage_armor()
        H.ds.update_player_list("active_effects", "Mage Armor", "remove")
        self.assertNotIn("Mage Armor", H.dbv("active_effects"))


if __name__ == "__main__":
    unittest.main()
