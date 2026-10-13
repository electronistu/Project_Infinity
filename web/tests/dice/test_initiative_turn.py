"""The informational turn cursor: the next actor, the upcoming round, and skipped turns.

The engine does NOT enforce the turn sequence (the Game Master adjudicates timing); it only
tracks, for the client's combat bar, who last acted, who is next in line, the round that next
turn belongs to, and whose turns the cursor crossed without acting. Neutrals sit the fight
out; the killed, fled or surrendered are passed over when picking the next actor.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_initiative_turn
"""

import unittest

from web.tests.dice import harness as H


GOBLIN = {"name": "Goblin", "hp": 7, "ac": 15, "initiative_modifier": 2, "role": "hostile"}
SCOUT = {"name": "Scout", "hp": 12, "ac": 14, "initiative_modifier": 0, "role": "ally"}
MERCHANT = {"name": "Merchant", "hp": 9, "ac": 11, "initiative_modifier": 0, "role": "neutral"}


class InitiativeTurnTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def _register(self):
        # player first, then the declared combatants (roll order = declaration order)
        with H.fixed_rolls([10, 15, 5, 20]):
            return H.ds.register_combatants([GOBLIN, SCOUT, MERCHANT])

    # -- the payload ----------------------------------------------------------

    def test_registration_payload_points_at_the_first_actor(self):
        r = self._register()
        self.assertEqual(r["combat_turn"],
                         {"round": 1, "actor": "", "next": "Goblin", "skipped": []})

    def test_attach_roster_adds_summary_and_cursor(self):
        self._register()
        H.ds._advance_turn("Borin")
        r = H.ds._attach_roster({"success": True})
        self.assertIn("registry_summary", r)
        self.assertEqual(r["combat_turn"],
                         {"round": 1, "actor": "Borin", "next": "Scout",
                          "skipped": ["Goblin"]})

    # -- the derivation -------------------------------------------------------

    def test_neutrals_are_excluded_from_the_turn_order(self):
        self._register()
        order = H.ds._combat_turn_order()
        self.assertNotIn("Merchant", order)
        self.assertIn("Goblin", order)
        self.assertIn("Borin", order)

    def test_next_is_the_successor_and_the_round_wraps(self):
        self._register()
        # turn order (neutrals aside): Goblin 17, Borin 12, Scout 5.
        H.ds._advance_turn("Borin")          # Borin acted; Scout is next, same round
        self.assertEqual(H.ds._combat_turn_state(),
                         {"round": 1, "actor": "Borin", "next": "Scout",
                          "skipped": ["Goblin"]})
        H.ds._advance_turn("Scout")          # Scout acted; the round wraps to Goblin
        self.assertEqual(H.ds._combat_turn_state(),
                         {"round": 2, "actor": "Scout", "next": "Goblin", "skipped": []})

    def test_the_next_actor_skips_the_killed(self):
        self._register()
        H.ds._advance_turn("Borin")
        # Kill Scout (next in line): the marker walks past the corpse and wraps to Goblin.
        H.ds._COMBAT_REGISTRY["Scout"]["current_hp"] = 0
        H.ds._COMBAT_REGISTRY["Scout"]["killed"] = True
        self.assertEqual(H.ds._combat_turn_state()["next"], "Goblin")
        self.assertEqual(H.ds._combat_turn_state()["round"], 2)

    def test_the_next_actor_skips_the_fled(self):
        self._register()
        H.ds._advance_turn("Borin")
        H.ds._COMBAT_REGISTRY["Scout"]["status"] = "fled"
        self.assertEqual(H.ds._combat_turn_state()["next"], "Goblin")

    def test_a_neutral_acting_never_advances_the_cursor(self):
        self._register()
        H.ds._advance_turn("Borin")
        H.ds._advance_turn("Merchant")
        self.assertEqual(H.ds._combat_turn_state(),
                         {"round": 1, "actor": "Borin", "next": "Scout",
                          "skipped": ["Goblin"]})

    def test_all_out_leaves_no_next(self):
        self._register()
        for name in ("Goblin", "Borin", "Scout"):
            H.ds._COMBAT_REGISTRY[name]["killed"] = True
        state = H.ds._combat_turn_state()
        self.assertEqual(state["next"], "")

    def test_end_combat_clears_the_cursor(self):
        self._register()
        H.ds._advance_turn("Borin")
        H.ds.end_combat()
        self.assertEqual(H.ds._combat_turn_state(),
                         {"round": 0, "actor": "", "next": "", "skipped": []})

    def test_an_arrival_resyncs_the_cursor(self):
        self._register()                     # turn order: Goblin 17, Borin 12, Scout 5
        H.ds._advance_turn("Borin")          # the cursor is on Borin
        with H.fixed_rolls([20]):
            H.ds.register_combatants(
                [{"name": "Late", "hp": 5, "ac": 12, "initiative_modifier": 0}],
                add_to_existing=True)
        # Late rolled 20 and now leads the order; the cursor still tracks Borin.
        self.assertEqual(H.ds._COMBAT_ACTOR, "Borin")
        self.assertEqual(H.ds._combat_next_actor()[1], "Scout")


if __name__ == "__main__":
    unittest.main()
