"""The combat registry's forgiving name lookup, and the refusals that replace the old
fabricated defaults (AC 10 / spell-attack +0 / save-target 0 HP and +0 save).

An unregistered or misspelled name is found by case/whitespace-insensitive match when it IS
registered; when it is not, the engine refuses rather than inventing a number -- but only when
the caller supplied no explicit value.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_registry_names
"""

import unittest

from web.tests.dice import harness as H

from rules import state
from rules.combat import _registry_entry, _registry_hp

GOBLIN = {
    "name": "Goblin", "hp": 7, "ac": 15, "initiative_modifier": 2, "challenge_rating": 0.25,
    "role": "hostile", "saves": {"str": -1, "dex": 2, "con": 0, "int": 0, "wis": -1, "cha": -1},
}


class NameLookupTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def _register(self):
        with H.fixed_rolls([15, 6]):
            return H.ds.register_combatants([GOBLIN])

    def test_case_and_whitespace_still_find_the_combatant(self):
        self._register()
        self.assertIs(_registry_entry("  gObLiN "), state._COMBAT_REGISTRY["Goblin"])
        self.assertEqual(_registry_hp("GOBLIN"), 7)

    def test_registration_keeps_the_declared_spelling(self):
        self._register()
        self.assertIn("Goblin", state._COMBAT_REGISTRY)
        self.assertNotIn("goblin", state._COMBAT_REGISTRY)  # the index is additive, not a rename

    def test_attack_finds_a_misspelled_registered_target(self):
        self._register()
        with H.fixed_rolls([15, 3]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d8",
                                    target_name=" goblin ")
        self.assertTrue(r["success"], str(r))
        self.assertEqual(r["target_ac"], 15)          # from the registry, not guessed
        self.assertEqual(_registry_hp("Goblin"), 4)   # 7 - 3

    def test_update_combatant_finds_a_misspelled_name(self):
        self._register()
        r = H.ds.update_combatant(name="  GOBLIN ", conditions_add=["prone"])
        self.assertTrue(r["success"], str(r))
        self.assertIn("prone", state._COMBAT_REGISTRY["Goblin"]["conditions"])


class AttackRefusalTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_unregistered_target_is_refused(self):
        with H.fixed_rolls([15, 7]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d8",
                                    target_name="Nobody Registered")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "target_not_registered")
        self.assertEqual(r["target_name"], "Nobody Registered")
        self.assertTrue(r["reason"] and r["gm_instruction"])

    def test_no_target_and_no_ac_is_refused(self):
        r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d8")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "target_ac_required")
        self.assertIn("target_ac", r["reason"])
        self.assertTrue(r["gm_instruction"])

    def test_explicit_target_ac_still_resolves(self):
        with H.fixed_rolls([15, 3]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d8",
                                    target_ac=12, target_name="Nobody Registered")
        self.assertTrue(r["success"], str(r))
        self.assertEqual(r["target_ac"], 12)


class MagicRefusalTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)   # Senna

    def test_unregistered_npc_spell_attack_is_refused(self):
        r = H.ds.resolve_magic(spell_name="Fire Bolt", actor="Evil Mage",
                               is_npc_attack=True, target_name="Senna")
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "spell_attack_modifier_required")
        self.assertTrue(r["reason"] and r["gm_instruction"])

    def test_registered_npc_spell_attack_uses_its_own_bonus(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([
                {"name": "Evil Mage", "hp": 22, "ac": 12, "initiative_modifier": 2,
                 "challenge_rating": 6, "role": "hostile",
                 "spellcasting": {"ability": "int", "save_dc": 14, "attack_modifier": 5}}])
        with H.fixed_rolls([10]):                      # the spell attack roll
            r = H.ds.resolve_magic(spell_name="Fire Bolt", actor="Evil Mage",
                                   is_npc_attack=True, target_name="Senna")
        self.assertTrue(r["success"], str(r))
        self.assertEqual(r["total_attack"], 15)        # 10 + the registry's +5, not +0

    def test_unregistered_save_target_is_refused(self):
        r = H.ds.resolve_magic(spell_name="Burning Hands", actor="Senna", spell_save_dc=13,
                               slot_level=1, target_name="Nobody Registered",
                               target_current_hp=20)
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "save_target_not_registered")
        self.assertEqual(r["missing"], ["save_modifier"])

    def test_unregistered_multi_target_save_is_refused(self):
        r = H.ds.resolve_magic(spell_name="Burning Hands", actor="Senna", spell_save_dc=13,
                               slot_level=1, targets=[{"name": "Nobody Registered"}])
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "save_target_not_registered")
        self.assertEqual(r["missing"], ["current_hp", "save_modifier"])

    def test_unregistered_single_target_with_no_hp_is_refused(self):
        r = H.ds.resolve_magic(spell_name="Burning Hands", actor="Senna", spell_save_dc=13,
                               slot_level=1, target_name="Nobody Registered",
                               target_save_modifier=0)
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "save_target_not_registered")
        self.assertEqual(r["missing"], ["current_hp"])

    def test_explicit_single_target_values_still_resolve(self):
        with H.fixed_rolls([3, 3, 3]):
            r = H.ds.resolve_magic(spell_name="Burning Hands", actor="Senna", spell_save_dc=13,
                                   slot_level=1, target_name="Nobody Registered",
                                   target_current_hp=20, target_save_modifier=0)
        self.assertTrue(r["success"], str(r))
        self.assertEqual(r["damage_total"], 9)           # 3d6, failed save: no halving
        self.assertEqual(r["target_remaining_hp"], 11)   # 20 - 9

    def test_explicit_multi_target_values_still_resolve(self):
        with H.fixed_rolls([3, 3, 3, 1, 20]):
            r = H.ds.resolve_magic(
                spell_name="Burning Hands", actor="Senna", spell_save_dc=13, slot_level=1,
                targets=[{"name": "Nobody Registered", "current_hp": 20, "save_modifier": 0}])
        self.assertTrue(r["success"], str(r))
        self.assertEqual(len(r["targets"]), 1)


if __name__ == "__main__":
    unittest.main()
