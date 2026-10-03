"""Combatant stat blocks: full registration, derived lookups, NPC attacks, saves,
damage types and conditions.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_combatants
"""

import unittest

from web.tests.dice import harness as H


GOBLIN = {
    "name": "Goblin", "hp": 7, "ac": 15, "initiative_modifier": 2, "challenge_rating": 0.25,
    "role": "hostile", "speed": 30,
    "saves": {"str": -1, "dex": 2, "con": 0, "int": 0, "wis": -1, "cha": -1},
    "condition_immunities": ["poisoned"],
    "attacks": [
        {"name": "Scimitar", "attack_bonus": 4, "damage_dice": "1d6", "damage_modifier": 2,
         "damage_type": "slashing", "reach": 5, "properties": ["Finesse", "Light"]},
        {"name": "Shortbow", "base": "Shortbow", "attack_bonus": 4, "damage_dice": "1d6",
         "damage_modifier": 2, "damage_type": "piercing", "range": "80/320"},
    ],
}
OGRE = {
    "name": "Ogre", "hp": 59, "ac": 11, "initiative_modifier": -1, "challenge_rating": 2,
    "damage_resistances": ["fire"], "damage_immunities": ["poison"],
    "damage_vulnerabilities": ["radiant"], "multiattack": 1,
    "attacks": [{"name": "Greatclub", "attack_bonus": 6, "damage_dice": "2d8",
                 "damage_modifier": 4, "damage_type": "bludgeoning"}],
}
MAGE = {
    "name": "Evil Mage", "hp": 22, "ac": 12, "initiative_modifier": 2, "challenge_rating": 6,
    "spellcasting": {"ability": "int", "save_dc": 14, "attack_modifier": 5},
}


class CombatantTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def _register(self, npcs, rolls=None):
        with H.fixed_rolls(rolls or ([15] + [6] * len(npcs))):
            return H.ds.register_combatants(npcs)

    def test_full_stat_block_is_stored_and_summarized(self):
        r = self._register([GOBLIN])
        entry = H.ds._COMBAT_REGISTRY["Goblin"]
        self.assertEqual(entry["saves"]["dex"], 2)
        self.assertEqual(entry["role"], "hostile")
        self.assertEqual(entry["attacks"][0]["name"], "Scimitar")
        self.assertEqual(entry["condition_immunities"], ["poisoned"])
        by = {e["name"]: e for e in r["registry_summary"]}
        self.assertEqual(by["Goblin"]["role"], "hostile")

    def test_player_entry_derives_saves_speed_and_role(self):
        self._register([GOBLIN])
        player = H.ds._COMBAT_REGISTRY["Borin"]
        # STR 18 (+4) proficient (+3), CON 14 (+2) proficient (+3), DEX 14 (+2)
        self.assertEqual(player["saves"], {"str": 7, "dex": 2, "con": 5, "int": 0, "wis": 1, "cha": -1})
        self.assertEqual(player["role"], "ally")
        self.assertEqual(player["speed"], 30)


class AutoLookupTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_target_ac_comes_from_the_registry(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([10, 4]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d8",
                                    target_name="Goblin")  # no target_ac
        self.assertEqual(r["target_ac"], 15)

    def test_explicit_target_ac_still_wins(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([10, 4]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, target_ac=25,
                                    damage_dice="1d8", target_name="Goblin")
        self.assertEqual(r["target_ac"], 25)


class NpcAttackTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_declared_attack_is_derived(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([12, 4]):
            r = H.ds.resolve_attack(actor="Goblin", attack="Scimitar", target_name="Borin",
                                    is_npc_attack=True)
        self.assertTrue(r["success"])
        self.assertEqual(r["used_attack"], "Scimitar")
        self.assertEqual(r["attack_modifier"], 4)
        self.assertEqual(r["damage_dice"], "1d6")
        self.assertEqual(r["damage_type"], "slashing")
        self.assertEqual(r["multiattack"], 1)

    def test_base_fills_missing_damage(self):
        npc = {"name": "Bandit", "hp": 11, "ac": 12, "initiative_modifier": 1,
               "attacks": [{"name": "Blade", "base": "Scimitar", "attack_bonus": 3}]}
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([npc])
        with H.fixed_rolls([12, 4]):
            r = H.ds.resolve_attack(actor="Bandit", attack="Blade", target_name="Borin",
                                    is_npc_attack=True)
        self.assertEqual(r["damage_dice"], "1d6")
        self.assertEqual(r["damage_type"], "slashing")

    def test_undeclared_attack_is_refused(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([GOBLIN])
        r = H.ds.resolve_attack(actor="Goblin", attack="Bite", target_name="Borin",
                                is_npc_attack=True)
        self.assertEqual(r["error"], "attack_not_declared")
        self.assertEqual(r["declared_attacks"], ["Scimitar", "Shortbow"])

    def test_unregistered_actor_is_refused(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([GOBLIN])
        r = H.ds.resolve_attack(actor="Ghost", attack="Touch", target_name="Borin")
        self.assertEqual(r["error"], "actor_not_registered")


class InitiativeAdvantageTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_advantage_takes_the_best_of_two(self):
        npc = {"name": "Scout", "hp": 9, "ac": 13, "initiative_modifier": 2,
               "initiative_advantage": True}
        with H.fixed_rolls([15, 3, 18]):  # player 15, scout rolls 3 then 18
            r = H.ds.register_combatants([npc])
        scout = next(e for e in r["initiative"] if e["name"] == "Scout")
        self.assertEqual(scout["roll"], 18)


class SaveLookupTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_registry_per_ability_save_is_used(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([10]):
            r = H.ds.resolve_magic(spell_name="Burning Hands", actor="Borin", spell_save_dc=15,
                                   targets=[{"name": "Goblin"}])
        self.assertEqual(r["targets"][0]["save_modifier"], 2)   # registry DEX save

    def test_legacy_single_save_modifier_is_used(self):
        guard = {"name": "Guard", "hp": 11, "ac": 16, "initiative_modifier": 1, "save_modifier": 3}
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([guard])
        with H.fixed_rolls([10]):
            r = H.ds.resolve_magic(spell_name="Burning Hands", actor="Borin", spell_save_dc=15,
                                   save_type="dex", targets=[{"name": "Guard"}])
        self.assertEqual(r["targets"][0]["save_modifier"], 3)

    def test_npc_caster_dc_and_player_save_are_derived(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([MAGE])
        with H.fixed_rolls([10]):
            r = H.ds.resolve_magic(spell_name="Lightning Bolt", actor="Evil Mage", is_npc_attack=True,
                                   attack_type="saving_throw", save_type="dex", damage_dice="8d6",
                                   target_name="Borin")
        self.assertEqual(r["save_dc"], 14)          # from the registry spellcasting block
        self.assertEqual(r["save_modifier"], 2)     # player's derived DEX save


class DamageTypeTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_resistance_halves_damage(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([OGRE])
        with H.fixed_rolls([15, 5, 5]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="2d6",
                                    damage_type="fire", target_name="Ogre")
        self.assertEqual(r["damage_total"], 5)      # 10 -> 5
        self.assertIn("fire", r["damage_modified"])

    def test_immunity_zeroes_damage(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([OGRE])
        with H.fixed_rolls([15, 5]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d6",
                                    damage_type="poison", target_name="Ogre")
        self.assertEqual(r["damage_total"], 0)

    def test_vulnerability_doubles_damage(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([OGRE])
        with H.fixed_rolls([15, 3]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d6",
                                    damage_type="radiant", target_name="Ogre")
        self.assertEqual(r["damage_total"], 6)      # 3 -> 6


class ConditionTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def _setup(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([GOBLIN])

    def test_prone_target_gives_advantage(self):
        self._setup()
        H.ds.update_combatant("Goblin", conditions_add=["prone"])
        with H.fixed_rolls([3, 18, 4]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d8",
                                    damage_modifier=4, target_name="Goblin")
        self.assertEqual(r["outcome"], "Success")   # took the 18
        self.assertEqual(r["conditions"]["target"], ["prone"])

    def test_paralyzed_target_is_hit_critically_and_auto_fails_saves(self):
        self._setup()
        H.ds.update_combatant("Goblin", conditions_add=["paralyzed"])
        with H.fixed_rolls([18, 3]):
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=7, damage_dice="1d8",
                                    damage_modifier=4, target_name="Goblin")
        self.assertTrue(r["forced_crit"])
        with H.fixed_rolls([10]):
            s = H.ds.resolve_magic(spell_name="Burning Hands", actor="Borin", spell_save_dc=15,
                                   targets=[{"name": "Goblin"}])
        self.assertTrue(s["targets"][0]["save_auto_failed"])
        self.assertFalse(s["targets"][0]["save_success"])

    def test_condition_immunity_blocks_and_is_reported(self):
        self._setup()
        r = H.ds.update_combatant("Goblin", conditions_add=["poisoned"])
        self.assertEqual(r["blocked_conditions"], [{"condition": "poisoned", "reason": "immune"}])
        self.assertEqual(r["conditions"], [])

    def test_player_conditions_are_stored_and_drive_checks(self):
        self._setup()
        H.ds.update_combatant("Borin", conditions_add=["poisoned"])
        self.assertEqual(H.dbv("conditions"), ["poisoned"])
        r = H.ds.perform_check(modifier=5, dc=10, check_name="Athletics", ability="str",
                               actor="Borin")
        self.assertEqual(r["disadvantage_sources"], ["poisoned"])

    def test_removing_a_condition(self):
        self._setup()
        H.ds.update_combatant("Goblin", conditions_add=["prone", "restrained"])
        r = H.ds.update_combatant("Goblin", conditions_remove=["prone"])
        self.assertEqual(r["conditions"], ["restrained"])


class UpdateCombatantTest(H.EngineCase):
    player_factory = staticmethod(H.fighter_l5)

    def test_hp_delta_clamps_and_kills(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([GOBLIN])
        r = H.ds.update_combatant("Goblin", hp_delta=-100)
        self.assertEqual(r["hp"], "0/7")
        self.assertTrue(H.ds._COMBAT_REGISTRY["Goblin"]["killed"])

    def test_player_hp_is_not_edited_here(self):
        with H.fixed_rolls([15, 6, 7]):
            H.ds.register_combatants([GOBLIN])
        r = H.ds.update_combatant("Borin", hp_delta=-5)
        self.assertTrue(r["success"])
        self.assertIn("modify_player_numeric", r["note"])
        self.assertEqual(H.dbv("current_hit_points"), 44)

    def test_unknown_combatant(self):
        r = H.ds.update_combatant("Nobody", conditions_add=["prone"])
        self.assertEqual(r["error"], "not_registered")


if __name__ == "__main__":
    unittest.main()
