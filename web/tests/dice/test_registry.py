"""register_combatants: initiative, registry, HP carry-forward, add_to_existing."""

import unittest

from web.tests.dice import harness as H

NPCS = [
    {"name": "Goblin", "hp": 7, "ac": 15, "initiative_modifier": 2, "challenge_rating": 0.25},
    {"name": "Ogre", "hp": 59, "ac": 11, "initiative_modifier": -1, "challenge_rating": 2},
]
OGRE_ONLY = [{"name": "Ogre", "hp": 59, "ac": 11, "initiative_modifier": -1, "challenge_rating": 2}]


class RegistryTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def _register(self, npcs=None, rolls=(15, 6, 7), add=False):
        with H.fixed_rolls(list(rolls)):
            return H.ds.register_combatants(npcs if npcs is not None else NPCS, add_to_existing=add)

    def test_player_auto_registered_first(self):
        r = self._register()
        self.assertTrue(r["success"])
        self.assertEqual(r["initiative"][0]["name"], "Borin")
        self.assertTrue(r["initiative"][0]["is_player"])

    def test_initiative_order_is_sorted(self):
        r = self._register()
        # Borin 15+2=17, Goblin 6+2=8, Ogre 7-1=6
        self.assertEqual(r["initiative_order"], ["Borin", "Goblin", "Ogre"])

    def test_registry_summary_hp(self):
        r = self._register()
        by = {e["name"]: e for e in r["registry_summary"]}
        self.assertEqual(by["Borin"]["hp"], "44/44")
        self.assertEqual(by["Goblin"]["hp"], "7/7")
        self.assertEqual(by["Goblin"]["ac"], 15)

    def test_hp_carries_forward_between_attacks(self):
        self._register(npcs=OGRE_ONLY, rolls=[10])
        with H.fixed_rolls([15, 7]):
            a1 = H.ds.resolve_attack(actor="Borin", attack_modifier=7, target_ac=11,
                                     damage_dice="1d8", damage_modifier=4, target_name="Ogre")
        with H.fixed_rolls([15, 3]):
            a2 = H.ds.resolve_attack(actor="Borin", attack_modifier=7, target_ac=11,
                                     damage_dice="1d8", damage_modifier=4, target_name="Ogre")
        self.assertEqual(a1["target_remaining_hp"], 48)  # 59 - 11
        self.assertEqual(a2["target_remaining_hp"], 41)  # 48 - 7

    def test_kill_sets_registry_hp_zero(self):
        self._register()
        with H.fixed_rolls([15, 7]):
            a = H.ds.resolve_attack(actor="Borin", attack_modifier=7, target_ac=15,
                                    damage_dice="1d8", damage_modifier=4, target_name="Goblin")
        self.assertTrue(a["target_killed"])
        self.assertEqual(H.ds._registry_hp("Goblin"), 0)

    def test_add_to_existing_preserves_hp_and_skips_initiative(self):
        self._register(npcs=OGRE_ONLY, rolls=[10])
        with H.fixed_rolls([15, 7]):
            H.ds.resolve_attack(actor="Borin", attack_modifier=7, target_ac=11,
                                damage_dice="1d8", damage_modifier=4, target_name="Ogre")
        hp_before = H.ds._registry_hp("Ogre")
        with H.fixed_rolls([1]):
            r = H.ds.register_combatants([{"name": "Wolf", "hp": 11, "ac": 13}], add_to_existing=True)
        self.assertTrue(r["success"])
        self.assertEqual(H.ds._registry_hp("Ogre"), hp_before)
        self.assertEqual(H.ds._registry_hp("Wolf"), 11)

    def test_reregistering_wipes_registry(self):
        self._register()
        with H.fixed_rolls([15, 7]):
            H.ds.resolve_attack(actor="Borin", attack_modifier=7, target_ac=15,
                                damage_dice="1d8", damage_modifier=4, target_name="Goblin")
        self._register()  # no add_to_existing
        self.assertEqual(H.ds._registry_hp("Goblin"), 7)  # back to full

    def test_register_narrative_is_minimal_and_sheets_are_structured(self):
        with H.fixed_rolls([15, 6, 7]):
            r = H.ds.register_combatants([
                {"name": "Goblin", "hp": 7, "ac": 15, "initiative_modifier": 2,
                 "saves": {"dex": 2, "con": 0}, "role": "hostile",
                 "attacks": [{"name": "Scimitar", "attack_bonus": 4, "damage_dice": "1d6",
                              "damage_modifier": 2, "damage_type": "slashing"}],
                 "traits": ["Sneaky"]},
            ])
        n = r["narrative_format"]
        self.assertEqual(n, "Combatants registered (2 total). Initiative Order")
        self.assertNotIn("HP ", n)                      # no HP anywhere in the block
        self.assertNotIn("Goblin — hostile", n)         # no inline sheet
        # The full sheet rides the structured payload instead.
        goblin = next(s for s in r["sheets"] if s["name"] == "Goblin")
        self.assertEqual(goblin["lines"][0], "Goblin — hostile")
        self.assertIn("DEX +2", "\n".join(goblin["lines"]))
        self.assertIn("Scimitar +4, 1d6+2 slashing", "\n".join(goblin["lines"]))
        self.assertNotIn("Borin", [s["name"] for s in r["sheets"]])   # player excluded
        self.assertEqual([e["name"] for e in r["registry_summary"]],
                         ["Borin", "Goblin"])

    def test_add_to_existing_returns_only_the_new_sheets(self):
        self._register(npcs=OGRE_ONLY, rolls=[10])
        with H.fixed_rolls([1]):
            r = H.ds.register_combatants([{"name": "Wolf", "hp": 11, "ac": 13}],
                                         add_to_existing=True)
        self.assertEqual(r["narrative_format"],
                         "Combatants registered (3 total). Added to existing registry: Wolf")
        self.assertEqual([s["name"] for s in r["sheets"]], ["Wolf"])   # only the arrival
        self.assertIn("HP 11/11  AC 13", "\n".join(r["sheets"][0]["lines"]))

    def test_attack_narrative_no_longer_restates_hp(self):
        self._register(npcs=OGRE_ONLY, rolls=[10])
        with H.fixed_rolls([1]):  # natural 1 -> automatic miss
            miss = H.ds.resolve_attack(actor="Borin", attack_modifier=7, target_ac=11,
                                       damage_dice="1d8", damage_modifier=4, target_name="Ogre")
        self.assertNotIn("HP:", miss["narrative_format"])
        with H.fixed_rolls([15, 7]):
            hit = H.ds.resolve_attack(actor="Borin", attack_modifier=7, target_ac=11,
                                      damage_dice="1d8", damage_modifier=4, target_name="Ogre")
        self.assertNotIn("Ogre HP:", hit["narrative_format"])
        self.assertEqual(hit["target_remaining_hp"], 48)  # JSON still carries it (tooltip)

    # ── in combat: only a living, hostile, active non-player counts ──────────

    def test_a_living_hostile_is_in_combat(self):
        self._register(OGRE_ONLY, rolls=[10])
        self.assertTrue(H.ds._in_active_combat())

    def test_killing_the_last_hostile_ends_combat(self):
        self._register(OGRE_ONLY, rolls=[10])
        H.ds.update_combatant("Ogre", hp_delta=-99)
        self.assertEqual(H.ds._registry_hp("Ogre"), 0)
        self.assertFalse(H.ds._in_active_combat())

    def test_status_fled_takes_a_hostile_out_of_combat(self):
        self._register(OGRE_ONLY, rolls=[10])
        r = H.ds.update_combatant("Ogre", status="fled")
        self.assertTrue(r["success"])
        self.assertEqual(r["status"], "fled")
        self.assertIn("fled the fight", r["narrative_format"])
        self.assertFalse(H.ds._in_active_combat())

    def test_status_surrendered_and_back_to_active(self):
        self._register(OGRE_ONLY, rolls=[10])
        H.ds.update_combatant("Ogre", status="surrendered")
        self.assertFalse(H.ds._in_active_combat())
        r = H.ds.update_combatant("Ogre", status="active")
        self.assertIn("back in the fight", r["narrative_format"])
        self.assertTrue(H.ds._in_active_combat())

    def test_invalid_status_is_refused_and_changes_nothing(self):
        self._register(OGRE_ONLY, rolls=[10])
        r = H.ds.update_combatant("Ogre", status="panicked")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "invalid_status")
        self.assertTrue(H.ds._in_active_combat())
        self.assertEqual(H.ds._COMBAT_REGISTRY["Ogre"]["status"], "active")

    def test_allies_and_neutrals_never_keep_combat_on(self):
        with H.fixed_rolls([10, 11, 12]):
            H.ds.register_combatants([
                {"name": "Scout", "hp": 11, "ac": 13, "initiative_modifier": 1,
                 "role": "ally"},
                {"name": "Merchant", "hp": 9, "ac": 10, "initiative_modifier": 0,
                 "role": "neutral"},
            ])
        self.assertFalse(H.ds._in_active_combat())

    def test_registry_summary_carries_killed_and_status(self):
        self._register(OGRE_ONLY, rolls=[10])
        H.ds.update_combatant("Ogre", status="surrendered")
        by = {e["name"]: e for e in H.ds._registry_summary_list()}
        self.assertEqual(by["Ogre"]["status"], "surrendered")
        self.assertFalse(by["Ogre"]["killed"])
        self.assertEqual(by["Borin"]["status"], "active")

    def test_player_status_is_not_tracked(self):
        self._register(OGRE_ONLY, rolls=[10])
        r = H.ds.update_combatant("Borin", status="fled")
        self.assertTrue(r["success"])
        self.assertIn("Player status is not tracked", r.get("note") or "")
        self.assertTrue(H.ds._in_active_combat())  # the Ogre is still fighting


if __name__ == "__main__":
    unittest.main()
