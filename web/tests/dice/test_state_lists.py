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

    def test_add_with_description_keyword_is_dict(self):
        r = H.ds.update_player_list("inventory", "the client's letter", "add",
                                    description="a folded page", weight=0)
        self.assertTrue(r["success"])
        self.assertIn({"name": "the client's letter", "description": "a folded page",
                       "weight": 0.0}, H.dbv("inventory"))

    def test_add_description_keyword_wins_over_inline(self):
        H.ds.update_player_list("inventory", "Rope: inline text", "add", description="field text")
        self.assertIn({"name": "Rope", "description": "field text"}, H.dbv("inventory"))

    def test_add_plain_entry_stays_a_string_without_description(self):
        H.ds.update_player_list("inventory", "Torch", "add")
        self.assertIn("Torch", H.dbv("inventory"))

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
    # Reputation is stored era-scoped. A polity must exist inside the era the save is in
    # (never invented) -- but the ERA's scope is created on demand from the same seed a new
    # record gets, because a jump used to leave every later era unwritable. A missing faction
    # under an existing polity is created on `add`, and a bare polity (or the legacy `misc`)
    # uses the `others` bucket.
    player_factory = staticmethod(lambda: H.make_player(
        era="egypt", reputation={"egypt": {"eldoria": {"guard": []}, "others": {}}}))

    def test_add_reputation_entry(self):
        r = H.ds.update_player_list("reputation.eldoria.guard", "Saved a patrol: earned their trust", "add")
        self.assertTrue(r["success"])
        entry = H.raw_dbv("reputation")["egypt"]["eldoria"]["guard"]
        self.assertIn({"name": "Saved a patrol", "description": "earned their trust"}, entry)

    def test_add_reputation_entry_with_description_keyword(self):
        r = H.ds.update_player_list("reputation.eldoria.guard", "Paid a bribe", "add",
                                    description="the watch looks the other way")
        self.assertTrue(r["success"])
        self.assertIn({"name": "Paid a bribe", "description": "the watch looks the other way"},
                      H.raw_dbv("reputation")["egypt"]["eldoria"]["guard"])

    def test_missing_faction_under_existing_polity_is_created(self):
        r = H.ds.update_player_list("reputation.eldoria.spies", "Bought a secret: paid in coin", "add")
        self.assertTrue(r["success"])
        self.assertIn({"name": "Bought a secret", "description": "paid in coin"},
                      H.raw_dbv("reputation")["egypt"]["eldoria"]["spies"])

    def test_bare_polity_uses_default_bucket(self):
        r = H.ds.update_player_list("reputation.others",
                                    "Awakened Convert: a willing pawn", "add")
        self.assertTrue(r["success"])
        self.assertIn({"name": "Awakened Convert", "description": "a willing pawn"},
                      H.raw_dbv("reputation")["egypt"]["others"]["others"])

    def test_the_legacy_misc_faction_folds_into_others(self):
        # `misc` was the default bucket before 2026-10-08; a GM that still says it lands in
        # the seeded one instead of creating a bucket the era's shape never shows.
        r = H.ds.update_player_list("reputation.others.misc", "Old habit: still works", "add")
        self.assertTrue(r["success"])
        self.assertIn("Old habit", [e if isinstance(e, str) else e.get("name")
                                     for e in H.raw_dbv("reputation")["egypt"]["others"]["others"]])
        self.assertNotIn("misc", H.raw_dbv("reputation")["egypt"]["others"])

    def test_the_write_lands_inside_the_era_and_not_at_the_top(self):
        # The resolver used to return a path relative to the era SCOPE while the caller wrote
        # it against the WHOLE map, so every add also created a duplicate top-level bucket.
        H.ds.update_player_list("reputation.eldoria.guard", "Saved a patrol: earned their trust", "add")
        self.assertEqual(set(H.raw_dbv("reputation")), {"egypt"})

    def test_unknown_polity_path_is_rejected(self):
        # An unknown polity is never invented, bare or with a faction.
        self.assertFalse(H.ds.update_player_list("reputation.nowhere.void", "X: Y", "add")["success"])
        self.assertFalse(H.ds.update_player_list("reputation.nowhere", "X: Y", "add")["success"])

    def test_another_eras_polity_is_rejected(self):
        # Wallachia is a real polity -- in another era. Writing to it from Egypt must fail,
        # and must not quietly create a bucket for it.
        r = H.ds.update_player_list("reputation.wallachia.ottoman_frontier", "X: Y", "add")
        self.assertFalse(r["success"])
        self.assertEqual(set(H.raw_dbv("reputation")["egypt"]), {"eldoria", "others"})

    def test_the_refusal_names_the_eras_own_polities(self):
        # The old refusal listed the map's era keys, which is not what the GM writes against.
        r = H.ds.update_player_list("reputation.nowhere.void", "X: Y", "add")
        self.assertFalse(r["success"])
        self.assertEqual(r["era"], "egypt")
        self.assertEqual(sorted(r["known_polities"]), ["eldoria", "others"])
        self.assertIn("reputation.<polity>", r["hint"])

    def test_update_and_remove_go_through_the_era(self):
        # Only `add` used to resolve through the era; update/remove fell through to the raw
        # short path and could not find the bucket it had just written.
        H.ds.update_player_list("reputation.eldoria.guard", "Saved a patrol: earned their trust", "add")
        r = H.ds.update_player_list("reputation.eldoria.guard", "Saved a patrol", "update",
                                    description="they remember the day")
        self.assertTrue(r["success"])
        self.assertEqual(H.raw_dbv("reputation")["egypt"]["eldoria"]["guard"][0]["description"],
                         "they remember the day")
        r = H.ds.update_player_list("reputation.eldoria.guard", "Saved a patrol", "remove")
        self.assertTrue(r["success"])
        self.assertEqual(H.raw_dbv("reputation")["egypt"]["eldoria"]["guard"], [])

    def test_the_dump_shows_only_the_era_you_are_in(self):
        H.ds.update_player_list("reputation.eldoria.guard", "Saved a patrol: earned their trust", "add")
        dumped = H.ds.dump_player_db()
        self.assertIn("eldoria", dumped["reputation"])
        self.assertNotIn("egypt", dumped["reputation"])  # the era level is the engine's

    def test_the_dump_offers_the_polities_of_an_era_nothing_is_written_in(self):
        # A jump used to leave the destination era with no scope at all, so the GM was shown
        # `{}` and had to invent a path. It is shown the era's real polities instead.
        with H.load(H.make_player(era="wallachia",
                                  reputation={"egypt": {"eldoria": {"guard": []}, "others": {}}})):
            dumped = H.ds.dump_player_db()["reputation"]
            self.assertEqual(sorted(dumped), ["moldavia", "others", "transylvania", "wallachia"])
            self.assertEqual(dumped["wallachia"], {"ruler": [], "boyars": [], "saxon_guilds": [],
                                                   "orthodox_church": [], "ottoman_frontier": [],
                                                   "strigoi": []})

    def test_a_scopeless_era_is_created_on_demand(self):
        # THE REGRESSION: a jump changes the era and nothing else, so the destination era had
        # no reputation scope and every write in every era after the first was refused forever.
        with H.load(H.make_player(era="wallachia",
                                  reputation={"egypt": {"eldoria": {"guard": []}, "others": {}}})):
            r = H.ds.update_player_list("reputation.wallachia.vadul", "accepted at Vadul", "add",
                                        description="fed and sheltered by the fisherfolk")
            self.assertTrue(r["success"], r)
            stored = H.raw_dbv("reputation")
            self.assertEqual(stored["wallachia"]["wallachia"]["vadul"],
                             [{"name": "accepted at Vadul",
                               "description": "fed and sheltered by the fisherfolk"}])
            self.assertIn("egypt", stored)          # the other age is kept, untouched
            self.assertEqual(set(stored), {"egypt", "wallachia"})

    def test_a_legacy_flat_map_keeps_its_buckets_and_still_receives_new_ones(self):
        # Not a migration: the flat map is left exactly as it is. It is only no longer fatal --
        # the era's scope is created under it, so the GM can write again.
        with H.load(H.make_player(era="egypt", reputation={"eldoria": {"guard": []}})):
            r = H.ds.update_player_list("reputation.eldoria.guard", "Saved a patrol", "add")
            self.assertFalse(r["success"])           # `eldoria` is not an egypt polity
            self.assertEqual(H.raw_dbv("reputation"), {"eldoria": {"guard": []}})
            self.assertEqual(sorted(H.ds.dump_player_db()["reputation"]), ["others", "the old kingdom"])
            r = H.ds.update_player_list("reputation.the old kingdom.river_boatmen",
                                        "Ferried across: paid in bread", "add")
            self.assertTrue(r["success"], r)
            stored = H.raw_dbv("reputation")
            self.assertEqual(stored["eldoria"], {"guard": []})          # left alone
            self.assertIn("Ferried across", [e.get("name") for e in
                                              stored["egypt"]["the old kingdom"]["river_boatmen"]])

    def test_remove_from_bare_polity_path_is_rejected(self):
        # 'remove' must not auto-create a bucket; the bare path is a dict.
        self.assertFalse(H.ds.update_player_list("reputation.others", "Awakened Convert", "remove")["success"])


class ReputationSaveTest(H.EngineCase):
    """The save must persist the WHOLE map; `dump_player_db` is the GM's view of it."""

    player_factory = staticmethod(lambda: H.make_player(
        era="egypt", reputation={"egypt": {"eldoria": {"guard": []}, "others": {}},
                                  "wallachia": {"wallachia": {"ruler": []}, "others": {}}}))

    def test_the_save_state_is_not_the_gms_view(self):
        dumped = H.ds.dump_player_db()["reputation"]
        saved = H.ds.dump_player_save_state()["reputation"]
        self.assertNotIn("egypt", dumped)
        self.assertEqual(set(saved), {"egypt", "wallachia"})

    def test_the_save_state_omits_the_derived_blocks(self):
        # They are recomputed wherever they are needed, and persisting them lies.
        saved = H.ds.dump_player_save_state()
        for derived in ("_carrying", "_equipment", "_passive"):
            self.assertNotIn(derived, saved)

    def test_a_saved_map_reloads_writable_in_the_era_it_was_saved_in(self):
        H.ds.update_player_list("reputation.eldoria.guard", "Saved a patrol", "add")
        saved = H.ds.dump_player_save_state()
        H.ds.update_player_list("reputation.eldoria.guard", "Saved another patrol", "add")
        self.assertEqual(len(H.raw_dbv("reputation")["egypt"]["eldoria"]["guard"]), 2)
        with H.load(saved):  # what `_write_player_atomic` + `init_player_db` do
            self.assertEqual(len(H.raw_dbv("reputation")["egypt"]["eldoria"]["guard"]), 1)
            r = H.ds.update_player_list("reputation.eldoria.guard", "Saved a third patrol", "add")
            self.assertTrue(r["success"], r)
            self.assertEqual(len(H.raw_dbv("reputation")["egypt"]["eldoria"]["guard"]), 2)


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


class UpdateEntryTest(H.EngineCase):
    """action='update': edit an item's description (and name) in place, end-state style."""

    player_factory = staticmethod(H.fighter_l5)

    def _entry(self, name):
        return next((e for e in H.dbv("inventory")
                     if isinstance(e, dict) and e.get("name") == name), None)

    def test_update_patches_the_description_without_remove_add(self):
        H.ds.update_player_list("inventory", "Wax-Sealed Letter: a folded scrap, grey seal intact", "add")
        r = H.ds.update_player_list(
            "inventory", "Wax-Sealed Letter", "update",
            description="a folded scrap, the grey seal broken; the contents read — a summons")
        self.assertTrue(r["success"])
        entry = self._entry("Wax-Sealed Letter")
        self.assertEqual(entry["description"],
                         "a folded scrap, the grey seal broken; the contents read — a summons")
        self.assertIn("Updated inventory: Wax-Sealed Letter", r["narrative_format"])
        self.assertIn("carrying", r)  # inventory updates still report the derived blocks

    def test_update_keeps_declared_stats(self):
        H.ds.update_player_list("inventory", "Iron Token: a blank disc", "add", weight=0.2)
        H.ds.update_player_list("inventory", "Iron Token", "update",
                                description="a disc stamped with a crown")
        entry = self._entry("Iron Token")
        self.assertEqual(entry["weight"], 0.2)
        self.assertEqual(entry["description"], "a disc stamped with a crown")

    def test_rename_follows_the_equipped_set(self):
        H.ds.equip_item(item="Longsword")
        ac_before = H.dbv("armor_class")
        r = H.ds.update_player_list("inventory", "Longsword", "update", new_name="Borin's Longsword")
        self.assertTrue(r["success"])
        self.assertEqual(H.dbv("equipped")["hands"][0], "Borin's Longsword")
        self.assertEqual(H.dbv("armor_class"), ac_before)
        self.assertIsNone(self._entry("Longsword"))

    def test_update_errors(self):
        self.assertEqual(
            H.ds.update_player_list("inventory", "Ghost", "update", description="x")["error"],
            "not_found")
        self.assertEqual(
            H.ds.update_player_list("inventory", "Longsword", "update")["error"],
            "nothing_to_update")
        self.assertEqual(
            H.ds.update_player_list("inventory", "Longsword", "update", new_name="Shield")["error"],
            "already_exists")

class NestedListUpdateTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_update_edits_a_nested_list_entry_in_place(self):
        H.ds.update_player_list("spellcasting.spells_known", "Shield: abjuration", "add")
        r = H.ds.update_player_list("spellcasting.spells_known", "Shield", "update",
                                    description="abjuration — a reaction barrier")
        self.assertTrue(r["success"])
        known = H.dbv("spellcasting")["spells_known"]
        self.assertIn({"name": "Shield", "description": "abjuration — a reaction barrier"}, known)


if __name__ == "__main__":
    unittest.main()
