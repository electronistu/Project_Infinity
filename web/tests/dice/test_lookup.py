"""lookup: era lore on demand -- the current era, another era, or one polity -- and a tool's
on-demand field reference (`ref:TOOL`).

The `lookup` tool is the one generic read-only tool. It reads the era the player is in from the
engine DB, and it is offered in BOTH games: the time-travel game fetches another era's scaffold,
and either game fetches a tool's reference. With no `era` on the save it is the classic game,
which has no eras to fetch -- an era query must be refused, never fall back to the first age and
leak the wrong world into the prompt.
"""

import unittest

from web.tests.dice import harness as H


class LookupTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_here_returns_the_current_era(self):
        # The fixture carries `era=egypt`; `here` must answer with that era.
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

    # ── the on-demand tool references (`ref:TOOL`) ─────────────────────────────

    def test_a_ref_query_returns_the_reference_body(self):
        self.assertIn("Multi-target AoE", H.ds.lookup("ref:resolve_magic"))
        self.assertIn("ability_set", H.ds.lookup("tool:update_player_list"))

    def test_an_unknown_ref_lists_the_known_references(self):
        self.assertIn("ref:update_player_list", H.ds.lookup("ref:nope"))

    def test_a_ref_query_works_with_no_era_on_the_save(self):
        # Classic offers `lookup` for the references; that path must not need an era.
        cur = H.ds.DB_CONNECTION.cursor()
        cur.execute("DELETE FROM player WHERE key = 'era'")
        H.ds.DB_CONNECTION.commit()
        self.assertIn("Multi-target AoE", H.ds.lookup("ref:resolve_magic"))

    def test_an_era_query_with_no_era_on_the_save_is_refused(self):
        # No era on the save = the classic game. `web.eras.lookup` would otherwise fall back
        # to the first age and leak the wrong world. It must be refused, not answered.
        cur = H.ds.DB_CONNECTION.cursor()
        cur.execute("DELETE FROM player WHERE key = 'era'")
        H.ds.DB_CONNECTION.commit()
        out = H.ds.lookup("here")
        self.assertIn("no eras", out)
        self.assertNotIn("polities:", out)


if __name__ == "__main__":
    unittest.main()
