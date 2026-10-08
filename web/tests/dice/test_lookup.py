"""lookup: era lore on demand -- the current era, another era, or one polity.

The `lookup` tool is the one generic retrieval tool. It reads the
era the player is in from the engine DB, so a save with no `era` must still answer.
"""

import unittest

from web.tests.dice import harness as H


class LookupTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_here_returns_the_current_era(self):
        # The fixture carries no `era`; the tool must fall back to the starting era.
        text = H.ds.lookup("here")
        self.assertIn("history:", text)
        self.assertIn("the Old Kingdom", text)

    def test_era_id_returns_that_era(self):
        text = H.ds.lookup("wallachia")
        self.assertIn("the boyars", text)
        self.assertNotIn("the Old Kingdom", text)

    def test_polity_name_returns_just_that_polity(self):
        text = H.ds.lookup("old kingdom")
        self.assertIn("the Old Kingdom", text)
        self.assertNotIn("history:", text)

    def test_a_miss_lists_the_known_eras(self):
        text = H.ds.lookup("atlantis")
        self.assertIn("Known eras", text)
        self.assertIn("egypt", text)

    def test_the_players_era_wins(self):
        with H.load(dict(H.fighter_l5(), era="wallachia")):
            text = H.ds.lookup("here")
            self.assertIn("the boyars", text)
            self.assertNotIn("the Old Kingdom", text)

    def test_the_scaffold_is_inside_the_per_era_ceiling(self):
        # tools/budgets.yml asserts this too; here it is pinned to the real tool.
        import sys
        from pathlib import Path

        repo = Path(H.__file__).resolve().parent.parent.parent.parent
        sys.path.insert(0, str(repo))
        from tools.token_budget import _counter

        _, count, exact = _counter()
        if not exact:  # pragma: no cover - the gate refuses without tiktoken
            self.skipTest("tiktoken not installed")
        self.assertLessEqual(count(H.ds.lookup("wallachia")), 450)
