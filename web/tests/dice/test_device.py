"""The Device and its five parts: the closed, engine-owned inventory vocabulary.

Covers the guards (the Device cannot be invented or edited; a part only by its exact
name, engine-described and stamped with the age it was recovered in) and the verbs that
must refuse a Device item (equip / attune / update) while `remove` stays allowed.
"""

import unittest

from web.tests.dice import harness as H


class DeviceAddTest(H.EngineCase):
    player_factory = staticmethod(H.make_player)

    def test_device_seeded_at_creation_is_engine_owned(self):
        r = H.ds.update_player_list("inventory", "The Device", "add")
        self.assertTrue(r["success"])
        entry = next(e for e in H.raw_dbv("inventory") if isinstance(e, dict))
        self.assertEqual(entry["name"], "The Device")
        self.assertTrue(entry["device"])
        self.assertTrue(entry["description"])
        self.assertEqual(entry["weight"], 0)

    def test_the_five_parts_and_their_powers(self):
        # One part = one power, and the fifth part (the Vernier) owns the place.
        self.assertEqual(H.ds.device.part_total(), 5)
        self.assertEqual(H.ds.device.part_names()[-1], "Vernier")
        self.assertEqual(H.ds.device.canonical_part("the vernier"), "Vernier")
        self.assertEqual(len(H.ds.device.POWERS), 5)
        self.assertEqual(len(set(H.ds.device.POWERS.values())), 5)
        self.assertEqual(H.ds.device.POWERS["Vernier"], H.ds.device.PLACE)
        self.assertEqual(H.ds.device.POWERS["Mainspring"], H.ds.device.CHARGE)
        self.assertEqual(H.ds.device.POWERS["Escapement"], H.ds.device.RELEASE)

    def test_the_observed_gm_call_is_refused(self):
        # The exact call seen in play: the GM inventing the Device with its own flavour.
        H.ds.update_player_list("inventory", "The Device", "add")
        r = H.ds.update_player_list(
            "inventory", "the Device", "add",
            description="a broken device of the far future", weight=1)
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "device_engine_owned")
        self.assertIn("already in the inventory", r["gm_instruction"])
        # ... and it did not mint a second one.
        inventory = H.raw_dbv("inventory")
        self.assertEqual(sum(1 for e in inventory
                             if isinstance(e, dict) and e.get("name") == "The Device"), 1)

    def test_device_alias_is_refused_when_present(self):
        H.ds.update_player_list("inventory", "The Device", "add")
        r = H.ds.update_player_list("inventory", "Horologe", "add")
        self.assertEqual(r["error"], "device_engine_owned")

    def test_part_added_by_exact_name_gets_the_engine_description(self):
        r = H.ds.update_player_list(
            "inventory", "Escapement", "add", description="a cog I made up")
        self.assertTrue(r["success"])
        entry = H.ds.device.entry_for_part(H.raw_dbv("inventory"), "Escapement")
        self.assertEqual(entry["name"], "Escapement")
        self.assertEqual(entry["device_part"], "Escapement")
        self.assertEqual(entry["found_in"], "egypt")
        self.assertNotIn("made up", entry["description"])
        self.assertIn("toothed wheel", entry["description"])
        self.assertEqual(entry["weight"], 0)
        # The engine description means no "unweighed item" nudge.
        self.assertNotIn("unweighed_item", r)

    def test_part_records_the_age_it_was_recovered_in(self):
        H.ds.set_player_field("era", "tang")
        H.ds.update_player_list("inventory", "Compass Rose", "add")
        entry = H.ds.device.entry_for_part(H.raw_dbv("inventory"), "Compass Rose")
        self.assertEqual(entry["found_in"], "tang")

    def test_part_name_is_case_insensitive(self):
        r = H.ds.update_player_list("inventory", "escapement", "add")
        self.assertTrue(r["success"])
        self.assertIsNotNone(H.ds.device.entry_for_part(H.raw_dbv("inventory"), "Escapement"))

    def test_duplicate_part_is_rejected(self):
        H.ds.update_player_list("inventory", "Regulator", "add")
        r = H.ds.update_player_list("inventory", "Regulator", "add")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "already_exists")

    def test_duplicate_part_is_rejected_case_insensitively(self):
        H.ds.update_player_list("inventory", "Escapement", "add")
        r = H.ds.update_player_list("inventory", "escapement", "add")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "already_exists")
        self.assertEqual(len(H.ds.device.recovered_parts(H.raw_dbv("inventory"))), 1)

    def test_part_add_returns_the_canonical_name(self):
        r = H.ds.update_player_list("inventory", "eScApEmEnT", "add")
        self.assertTrue(r["success"])
        self.assertEqual(r["item"], "Escapement")

    def test_part_is_refused_in_a_classic_save(self):
        H.ds.set_player_field("era", "")
        r = H.ds.update_player_list("inventory", "Escapement", "add")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "device_not_in_this_game")

    def test_a_non_device_name_still_adds_normally(self):
        r = H.ds.update_player_list("inventory", "Cog", "add")
        self.assertTrue(r["success"])
        self.assertIsNotNone(H.ds.device.canonical_part("Escapement"))
        self.assertIsNone(H.ds.device.canonical_part("Cog"))


class DeviceVerbTest(H.EngineCase):
    player_factory = staticmethod(H.make_player)

    def setUp(self):
        super().setUp()
        H.ds.update_player_list("inventory", "The Device", "add")
        H.ds.update_player_list("inventory", "Escapement", "add")

    def test_update_a_device_entry_is_refused(self):
        r = H.ds.update_player_list("inventory", "Escapement", "update",
                                    description="now fitted")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "device_engine_owned")

    def test_rename_a_device_entry_is_refused(self):
        r = H.ds.update_player_list("inventory", "The Device", "update",
                                    new_name="The Horologe")
        self.assertEqual(r["error"], "device_engine_owned")

    def test_equip_the_device_is_refused(self):
        r = H.ds.equip_item(item="The Device")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "device_not_equippable")

    def test_attune_a_part_is_refused(self):
        r = H.ds.attune_item(item="Escapement")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "device_not_attunable")

    def test_remove_the_device_is_allowed(self):
        r = H.ds.update_player_list("inventory", "The Device", "remove")
        self.assertTrue(r["success"])
        self.assertFalse(H.ds.device.has_device(H.raw_dbv("inventory")))

    def test_removing_a_part_is_allowed(self):
        r = H.ds.update_player_list("inventory", "Escapement", "remove")
        self.assertTrue(r["success"])
        self.assertEqual(H.ds.device.recovered_parts(H.raw_dbv("inventory")), [])

    def test_removed_device_can_be_restored_engine_described(self):
        H.ds.update_player_list("inventory", "The Device", "remove")
        r = H.ds.update_player_list("inventory", "Horologe", "add",
                                    description="my own text")
        self.assertTrue(r["success"])
        inventory = H.raw_dbv("inventory")
        self.assertTrue(H.ds.device.has_device(inventory))
        entry = next(e for e in inventory if isinstance(e, dict) and e.get("device"))
        self.assertNotIn("my own text", entry["description"])


if __name__ == "__main__":
    unittest.main()
