"""The developer-only engine tools: `debug_set_field` and `debug_device`.

They must no-op unless the server was started with `--debug`, must write real JSON
(not a stringified value), and must recompute the derived blocks afterwards.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest discover -s web/tests/dice -t .
"""

import unittest

from web.tests.dice import harness as H


class DebugToolsDisabled(H.EngineCase):
    player_factory = staticmethod(H.make_player)

    def test_set_field_is_refused_without_debug(self):
        old = H.ds.DEBUG_MODE
        H.ds.DEBUG_MODE = False
        try:
            out = H.ds.debug_set_field("level", "9")
        finally:
            H.ds.DEBUG_MODE = old
        self.assertFalse(out.get("success"))
        self.assertIn("developer mode", out.get("error", ""))
        self.assertEqual(H.raw_dbv("level"), 1)  # untouched

    def test_device_is_refused_without_debug(self):
        old = H.ds.DEBUG_MODE
        H.ds.DEBUG_MODE = False
        try:
            out = H.ds.debug_device(["Escapement"])
        finally:
            H.ds.DEBUG_MODE = old
        self.assertFalse(out.get("success"))
        self.assertIn("developer mode", out.get("error", ""))


class DebugSetField(H.EngineCase):
    player_factory = staticmethod(H.make_player)

    def setUp(self):
        super().setUp()
        self._old = H.ds.DEBUG_MODE
        H.ds.DEBUG_MODE = True

    def tearDown(self):
        H.ds.DEBUG_MODE = self._old
        super().tearDown()

    def test_number_is_written_as_json_not_a_string(self):
        out = H.ds.debug_set_field("level", "5")
        self.assertTrue(out.get("success"), out)
        self.assertEqual(H.raw_dbv("level"), 5)
        self.assertIsInstance(H.raw_dbv("level"), int)

    def test_dict_is_written_whole(self):
        out = H.ds.debug_set_field("stats", '{"str": 20, "dex": 10, "con": 10, '
                                            '"int": 10, "wis": 10, "cha": 10}')
        self.assertTrue(out.get("success"), out)
        stored = H.raw_dbv("stats")
        self.assertEqual(stored["str"], 20)
        self.assertIsInstance(stored, dict)

    def test_list_is_written_whole(self):
        out = H.ds.debug_set_field("inventory", '["Dagger", "Rope"]')
        self.assertTrue(out.get("success"), out)
        self.assertEqual(H.raw_dbv("inventory"), ["Dagger", "Rope"])

    def test_string_value_stays_a_string(self):
        out = H.ds.debug_set_field("alignment", '"Chaotic Good"')
        self.assertTrue(out.get("success"), out)
        self.assertEqual(H.raw_dbv("alignment"), "Chaotic Good")

    def test_dotted_path_writes_inside_a_root(self):
        out = H.ds.debug_set_field("spellcasting.slots", '{"1": 4, "2": 2}')
        self.assertTrue(out.get("success"), out)
        self.assertEqual(H.raw_dbv("spellcasting")["slots"], {"1": 4, "2": 2})


class DebugDevice(H.EngineCase):
    player_factory = staticmethod(H.make_player)

    def setUp(self):
        super().setUp()
        self._old = H.ds.DEBUG_MODE
        H.ds.DEBUG_MODE = True

    def tearDown(self):
        H.ds.DEBUG_MODE = self._old
        super().tearDown()

    def _inv(self):
        return H.raw_dbv("inventory") or []

    def test_sets_the_device_and_exact_parts(self):
        out = H.ds.debug_device(["Escapement"], present=True)
        self.assertTrue(out.get("success"), out)
        names = [e.get("name") for e in self._inv() if isinstance(e, dict)]
        self.assertIn("The Device", names)
        self.assertIn("Escapement", names)

    def test_parts_are_canonical_and_deduped(self):
        out = H.ds.debug_device(["the escapement", "Escapement", "Compass Rose"])
        self.assertTrue(out.get("success"), out)
        recovered = [e.get("name") for e in self._inv()
                     if isinstance(e, dict) and e.get("device_part")]
        self.assertEqual(recovered.count("Escapement"), 1)
        self.assertIn("Compass Rose", recovered)

    def test_present_false_removes_everything_device(self):
        H.ds.debug_device(["Escapement"], present=True)
        out = H.ds.debug_device([], present=False)
        self.assertTrue(out.get("success"), out)
        self.assertFalse([e for e in self._inv()
                          if isinstance(e, dict) and (e.get("device") or e.get("device_part"))])

    def test_it_replaces_rather_than_appends(self):
        H.ds.debug_device(["Escapement"], present=True)
        H.ds.debug_device(["Mainspring"], present=True)
        recovered = [e.get("name") for e in self._inv()
                     if isinstance(e, dict) and e.get("device_part")]
        self.assertEqual(recovered, ["Mainspring"])

    def test_the_part_carries_the_era_it_was_found_in(self):
        H.ds.debug_device(["Escapement"])
        entry = next(e for e in self._inv()
                     if isinstance(e, dict) and e.get("device_part") == "Escapement")
        self.assertEqual(entry.get("found_in"), "egypt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
