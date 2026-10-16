"""`read_player_field`: the GM's per-field read of the player database.

The lean CHARACTER SHEET carries the common values; this is how the GM asks for one it omits --
above all the ability scores -- and for a companion's whole declared sheet. It returns exactly
the fields named, wrapped as a dict, and reports unknown names instead of refusing.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_player_field
"""

import unittest

from web.tests.dice import harness as H

from rules import state


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


class PlayerFieldTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def _set_era(self, era):
        cur = state.DB_CONNECTION.cursor()
        cur.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("era", era))
        state.DB_CONNECTION.commit()

    # ── the shape ───────────────────────────────────────────────────────────────

    def test_returns_only_the_requested_fields_wrapped(self):
        out = H.ds.read_player_field(["stats", "xp"])
        self.assertEqual(sorted(out["fields"]), ["stats", "xp"])
        self.assertEqual(out["missing"], [])
        self.assertEqual(out["fields"]["stats"], H.raw_dbv("stats"))
        self.assertEqual(out["fields"]["xp"], 6500)

    def test_an_unknown_field_is_reported_not_refused(self):
        out = H.ds.read_player_field(["stats", "not_a_field"])
        self.assertEqual(sorted(out["fields"]), ["stats"])
        self.assertEqual(out["missing"], ["not_a_field"])

    def test_an_empty_request_is_refused(self):
        out = H.ds.read_player_field([])
        self.assertFalse(out.get("success"))
        self.assertEqual(out["error"], "fields_required")

    def test_the_derived_blocks_are_readable(self):
        out = H.ds.read_player_field(["_passive", "armor_class"])
        self.assertEqual(out["fields"]["armor_class"], 17)
        self.assertIn("perception", out["fields"]["_passive"])

    def test_an_omitted_sheet_field_is_readable(self):
        # `stats` is the reason this tool exists: the injected CHARACTER SHEET never carries it.
        out = H.ds.read_player_field(["stats"])
        self.assertEqual(out["fields"]["stats"]["str"], 18)

    # ── era scoping ─────────────────────────────────────────────────────────────

    def test_reputation_matches_the_engine_view(self):
        out = H.ds.read_player_field(["reputation"])
        self.assertEqual(out["fields"]["reputation"], H.ds.dump_player_db()["reputation"])

    def test_companions_are_the_party_keyed_by_name(self):
        r = H.ds.register_companion("Maera", MAERA)
        self.assertTrue(r["success"], r)
        out = H.ds.read_player_field(["companions"])
        self.assertEqual(sorted(out["fields"]["companions"]), ["Maera"])
        sheet = out["fields"]["companions"]["Maera"]
        self.assertEqual(sheet["ac"], 15)
        self.assertEqual(sheet["max_hp"], 38)
        self.assertEqual(sheet["stats"], MAERA["stats"])
        self.assertEqual(sheet["attacks"][0]["name"], "Rapier")
        self.assertNotIn("current_hp", sheet)   # a fight's business, never stored
        self.assertNotIn("hp", sheet)           # only `max_hp`

    def test_companions_travel_and_are_read_flat(self):
        H.ds.register_companion("Maera", MAERA)
        self._set_era("tang")                             # the Device carried her
        out = H.ds.read_player_field(["companions"])
        self.assertIn("Maera", out["fields"]["companions"])
        self.assertEqual(out["fields"]["companions"]["Maera"]["max_hp"], 38)

    # ── the engine's full read is not the GM's ────────────────────────────────────

    def test_dump_player_db_still_returns_the_full_map(self):
        full = H.ds.dump_player_db()
        for key in ("name", "stats", "saves", "_carrying", "_equipment", "_passive"):
            self.assertIn(key, full)


if __name__ == "__main__":
    unittest.main()
