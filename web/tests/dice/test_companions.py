"""Declared companions: a durable, era-bound sheet the engine fills in by name.

A companion is a person of the age it was found in and cannot travel in time, so its sheet is
stored per era, at most three per era. Current HP is never stored -- registering the companion
by name alone fills the stat block, at full HP unless the call states one.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_companions
"""

import unittest

from web.tests.dice import harness as H


MAERA = {
    "description": "a grey-braided dwarf woman in a leather apron",
    "race": "dwarf", "class": "rogue",
    "stats": {"str": 14, "dex": 16, "con": 12, "int": 12, "wis": 10, "cha": 8},
    "ac": 15, "max_hp": 38, "speed": 25, "initiative_modifier": 3,
    "saves": {"str": 2, "dex": 6, "con": 3, "int": 1, "wis": 1, "cha": 0},
    "attacks": [{"name": "Rapier", "attack_bonus": 6, "damage_dice": "1d8",
                 "damage_modifier": 4, "damage_type": "piercing"}],
    "advantages": ["Sneak Attack 2d6."],
    "skills": ["Stealth", "Insight"],
    "proficiency_bonus": 3,
}
GOBLIN = {
    "name": "Goblin", "hp": 7, "ac": 15, "initiative_modifier": 2, "challenge_rating": 0.25,
    "attacks": [{"name": "Scimitar", "attack_bonus": 4, "damage_dice": "1d6",
                 "damage_modifier": 2, "damage_type": "slashing"}],
}


class CompanionTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def _set_era(self, era):
        cur = H.ds.DB_CONNECTION.cursor()
        cur.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("era", era))
        H.ds.DB_CONNECTION.commit()

    def _register(self, npcs, rolls=None):
        with H.fixed_rolls(rolls or ([15] + [6] * len(npcs))):
            return H.ds.register_combatants(npcs)

    # ── declaring ─────────────────────────────────────────────────────────────

    def test_declare_stores_a_static_sheet_scoped_to_the_era(self):
        r = H.ds.register_companion("Maera", MAERA)
        self.assertTrue(r["success"])
        self.assertEqual(r["status"], "declared")
        self.assertEqual((r["era"], r["count"]), ("egypt", 1))
        stored = H.raw_dbv("companions")["egypt"]["maera"]
        self.assertEqual(stored["ac"], 15)
        self.assertEqual(stored["max_hp"], 38)
        self.assertEqual(stored["role"], "ally")
        self.assertEqual(stored["description"], MAERA["description"])
        self.assertEqual(stored["attacks"][0]["name"], "Rapier")
        self.assertEqual(stored["stats"]["dex"], 16)
        self.assertEqual(stored["advantages"], ["Sneak Attack 2d6."])
        self.assertNotIn("traits", stored)          # renamed: combat advantages only
        self.assertNotIn("challenge_rating", stored)  # an ally earns no XP
        self.assertNotIn("current_hp", stored)      # current HP is a fight's business
        self.assertNotIn("conditions", stored)

    def test_an_incomplete_sheet_is_refused(self):
        r = H.ds.register_companion("Maera", {"race": "dwarf"})
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "incomplete_sheet")
        self.assertIsNone(H.raw_dbv("companions"))

    def test_the_ability_scores_are_required(self):
        no_stats = {k: v for k, v in MAERA.items() if k != "stats"}
        r = H.ds.register_companion("Maera", no_stats)
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "incomplete_sheet")
        partial = {**MAERA, "stats": {"str": 14, "dex": 16}}
        self.assertFalse(H.ds.register_companion("Bram", partial)["success"])

    def test_a_sheet_stored_before_the_rename_still_registers(self):
        # A sheet written when the field was called `traits` keeps them as advantages.
        cur = H.ds.DB_CONNECTION.cursor()
        H.ds._save_companions(cur, {"egypt": {"maera": {
            "name": "Maera", "ac": 15, "max_hp": 38, "traits": ["Sneak Attack 2d6."]}}})
        with H.fixed_rolls([10, 6]):
            r = H.ds.register_combatants([{"name": "Maera"}])
        self.assertEqual(H.ds._COMBAT_REGISTRY["Maera"]["traits"], ["Sneak Attack 2d6."])
        self.assertIn("Sneak Attack", r["companions"][0]["line"])

    def test_re_declaring_a_name_updates_it(self):
        H.ds.register_companion("Maera", MAERA)
        H.ds.register_companion("Maera", {**MAERA, "ac": 16})
        stored = H.raw_dbv("companions")["egypt"]["maera"]
        self.assertEqual(stored["ac"], 16)
        self.assertEqual(H.ds.register_companion("Maera", MAERA)["count"], 1)

    def test_the_cap_is_three_per_era(self):
        for n in ("Ana", "Bram", "Cora"):
            self.assertTrue(H.ds.register_companion(n, MAERA)["success"])
        r = H.ds.register_companion("Dain", MAERA)
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "companion_limit")
        self.assertIn("decide which companion to remove", r["reason"])
        self.assertEqual(sorted(r["companions"]), ["Ana", "Bram", "Cora"])
        # Updating one already declared is always allowed, and forgetting frees a slot.
        self.assertTrue(H.ds.register_companion("Ana", MAERA)["success"])
        self.assertTrue(H.ds.register_companion("Bram", action="forget")["success"])
        self.assertTrue(H.ds.register_companion("Dain", MAERA)["success"])

    def test_the_cap_is_per_era_not_global(self):
        for n in ("Ana", "Bram", "Cora"):
            H.ds.register_companion(n, MAERA)
        self._set_era("wallachia")
        self.assertTrue(H.ds.register_companion("Maera", MAERA)["success"])
        self.assertEqual(sorted(H.raw_dbv("companions")), ["egypt", "wallachia"])

    def test_forget_removes_the_sheet(self):
        H.ds.register_companion("Maera", MAERA)
        r = H.ds.register_companion("Maera", action="forget")
        self.assertTrue(r["success"])
        self.assertEqual(r["status"], "forgotten")
        self.assertNotIn("companions", H.raw_dbv("companions", {}))
        self.assertFalse(self._register([{"name": "Maera"}])["success"])

    def test_an_unknown_action_is_refused(self):
        r = H.ds.register_companion("Maera", MAERA, action="rename")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "invalid_action")

    def test_the_description_is_world_neutral_not_era_only(self):
        # Classic has no ages, so the tool's always-on text must read correctly there too.
        # The unit is the world (one in classic, one per age in the era game) and the cap is
        # stated by the RETURN -- `limit` + the refusal -- not restated every turn.
        doc = (H.ds.register_companion.__doc__ or "").lower()
        for phrase in ("time travel", "cannot travel", "in an age", "per age", "each age"):
            self.assertNotIn(phrase, doc)
        self.assertNotIn("at most 3", doc)
        r = H.ds.register_companion("Maera", MAERA)
        self.assertEqual(r["limit"], 3)

    # ── registering the companion in a fight ──────────────────────────────────

    def test_a_name_alone_is_filled_from_the_sheet_at_full_hp(self):
        H.ds.register_companion("Maera", MAERA)
        r = self._register([{"name": "Maera"}, GOBLIN])
        self.assertTrue(r["success"])
        entry = H.ds._COMBAT_REGISTRY["Maera"]
        self.assertEqual(entry["role"], "ally")
        self.assertEqual(entry["current_hp"], 38)   # default: full
        self.assertEqual(entry["max_hp"], 38)
        self.assertEqual(entry["ac"], 15)
        self.assertEqual(entry["attacks"][0]["name"], "Rapier")
        self.assertEqual(entry["saves"]["dex"], 6)
        self.assertEqual(entry["traits"], ["Sneak Attack 2d6."])   # `advantages` maps here
        self.assertEqual(len(r["companions"]), 1)
        self.assertIn("Maera", r["companions"][0]["line"])
        self.assertEqual(r["companions"][0]["hp"], "38/38")

    def test_an_hp_in_the_registration_is_the_current_hp(self):
        H.ds.register_companion("Maera", MAERA)
        self._register([{"name": "Maera", "hp": 17}, GOBLIN])
        self.assertEqual(H.ds._COMBAT_REGISTRY["Maera"]["current_hp"], 17)
        self.assertEqual(H.ds._COMBAT_REGISTRY["Maera"]["max_hp"], 38)

    def test_an_explicit_block_is_never_shadowed(self):
        H.ds.register_companion("Maera", MAERA)
        self._register([{"name": "Maera", "hp": 9, "ac": 12, "initiative_modifier": 1}])
        entry = H.ds._COMBAT_REGISTRY["Maera"]
        self.assertEqual((entry["ac"], entry["current_hp"]), (12, 9))

    def test_a_name_alone_that_is_no_companion_is_refused(self):
        r = self._register([{"name": "Nobody"}])
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "not_declared")
        self.assertIn("register_companion", r["gm_instruction"])

    def test_a_companion_of_another_age_cannot_be_registered(self):
        H.ds.register_companion("Maera", MAERA)          # found in egypt
        self._set_era("wallachia")
        r = self._register([{"name": "Maera"}])
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "companion_not_here")
        self.assertIn("Egypt", r["reason"])

    def test_a_companion_can_join_an_existing_fight(self):
        H.ds.register_companion("Maera", MAERA)
        self._register([GOBLIN])
        with H.fixed_rolls([11]):
            r = H.ds.register_combatants([{"name": "Maera"}], add_to_existing=True)
        self.assertTrue(r["success"])
        self.assertEqual(H.ds._COMBAT_REGISTRY["Maera"]["current_hp"], 38)

    def test_a_companion_never_keeps_combat_alive(self):
        H.ds.register_companion("Maera", MAERA)
        self._register([{"name": "Maera"}, GOBLIN])
        H.ds.update_combatant("Goblin", hp_delta=-99)
        self.assertFalse(H.ds._in_active_combat())

    # ── persistence ───────────────────────────────────────────────────────────

    def test_the_sheet_survives_a_save_and_load(self):
        H.ds.register_companion("Maera", MAERA)
        rows = H.ds.dump_player_save_state()
        self.assertIn("companions", rows)
        with H.load(rows):
            cursor = H.ds.DB_CONNECTION.cursor()
            store = H.ds._companions_all(cursor)
            self.assertEqual(store["egypt"]["maera"]["max_hp"], 38)
            self.assertEqual(store["egypt"]["maera"]["description"], MAERA["description"])


if __name__ == "__main__":
    unittest.main()


class CompanionCheckTest(H.EngineCase):
    """A companion's own checks and saves: the engine derives them from her sheet, exactly as
    it derives the player's -- never from a number the Game Master computed."""

    player_factory = staticmethod(H.fighter_l5)

    def setUp(self):
        super().setUp()
        H.ds.register_companion("Maera", MAERA)

    def test_a_skill_check_derives_ability_plus_proficiency(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Maera", dc=15, check_name="Stealth")
        self.assertEqual(r["modifier"], 6)          # DEX +3, proficient +3
        self.assertEqual(r["total"], 16)
        self.assertEqual(r["check"]["ability"], "dex")
        self.assertIn("prof +3", r["narrative_format"])

    def test_an_unproficient_skill_is_the_ability_alone(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Maera", dc=15, check_name="Athletics")
        self.assertEqual(r["modifier"], 2)          # STR +2, not proficient

    def test_a_bare_ability_check_uses_the_ability(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Maera", dc=15, check_name="Raw strength",
                                   ability="str")
        self.assertEqual(r["modifier"], 2)

    def test_expertise_doubles_the_proficiency(self):
        H.ds.register_companion("Maera", {**MAERA, "expertise": ["Stealth"]})
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Maera", dc=15, check_name="Stealth")
        self.assertEqual(r["modifier"], 9)          # DEX +3, expertise +6
        self.assertTrue(r["check"]["expertise"])

    def test_a_save_reads_her_declared_saves(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Maera", dc=15, check_name="Dexterity save",
                                   ability="dex", save=True)
        self.assertEqual(r["modifier"], 6)          # her sheet's dex save
        self.assertEqual(r["total"], 16)

    def test_proficiency_bonus_defaults_to_two(self):
        H.ds.register_companion("Maera", {k: v for k, v in MAERA.items()
                                          if k != "proficiency_bonus"})
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Maera", dc=15, check_name="Stealth")
        self.assertEqual(r["modifier"], 5)          # DEX +3, the default +2

    def test_situational_modifier_still_applies(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Maera", dc=15, check_name="Stealth",
                                   situational_modifier=-2)
        self.assertEqual(r["modifier"], 4)

    def test_no_stats_falls_back_to_the_given_modifier(self):
        cur = H.ds.DB_CONNECTION.cursor()
        H.ds._save_companions(cur, {"egypt": {"maera": {
            "name": "Maera", "ac": 15, "max_hp": 38}}})
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Maera", dc=15, check_name="Stealth", modifier=4)
        self.assertEqual(r["modifier"], 4)
        self.assertEqual(r["total"], 14)

    def test_a_companion_of_another_world_is_refused(self):
        cur = H.ds.DB_CONNECTION.cursor()
        cur.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                    ("era", "wallachia"))
        H.ds.DB_CONNECTION.commit()
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="Maera", dc=15, check_name="Stealth")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "companion_not_here")
        self.assertIn("Egypt", r["narrative_format"])

    def test_an_unknown_npc_still_uses_the_passed_modifier(self):
        with H.fixed_rolls([10]):
            r = H.ds.perform_check(actor="A Bystander", dc=15, check_name="Perception",
                                   modifier=3)
        self.assertEqual(r["modifier"], 3)
