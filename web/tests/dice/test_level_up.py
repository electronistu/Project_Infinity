"""level_up.py tables + apply_level_up through dice_server."""

import json
import unittest

import level_up

from web.tests.dice import harness as H


class ApplyLevelUpTest(unittest.TestCase):
    def _data(self, **over):
        data = {
            "level": "5", "total_hit_points": "44", "current_hit_points": "44",
            "proficiency_bonus": "3", "hit_dice_count": "5", "hit_dice_size": "10",
            "spellcasting": "{}", "character_class": "Fighter",
            "stats": json.dumps({"str": 18, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8}),
        }
        data.update(over)
        return data

    def test_fighter_5_to_6_gains_hp_and_hit_die(self):
        with H.fixed_rolls([6]):
            changes, summary = H.ds.apply_level_up("Fighter", 5, 6, self._data())
        self.assertEqual(changes["level"], "6")
        self.assertEqual(changes["hit_dice_count"], "6")
        self.assertEqual(changes["total_hit_points"], "52")  # 44 + 6 (d10) + 2 CON
        self.assertEqual(changes["current_hit_points"], "52")

    def test_wizard_gains_new_slot_level(self):
        data = self._data(
            character_class="Wizard", level="4", total_hit_points="26", current_hit_points="26",
            proficiency_bonus="2", hit_dice_count="4", hit_dice_size="6",
            stats=json.dumps({"str": 8, "dex": 14, "con": 8, "int": 16, "wis": 10, "cha": 10}),
            spellcasting=json.dumps({"ability": "intelligence", "dc": 13, "attack_modifier": 5,
                                     "slots": {"1": 4, "2": 3}}),
        )
        with H.fixed_rolls([3]):
            changes, _ = H.ds.apply_level_up("Wizard", 4, 5, data)
        self.assertEqual(changes["level"], "5")
        self.assertEqual(json.loads(changes["spellcasting"])["slots"]["3"], 2)


class SlotTableTest(unittest.TestCase):
    def test_full_caster(self):
        self.assertEqual(level_up.FULL_CASTER_SPELL_SLOTS[1], {1: 2})
        self.assertEqual(level_up.FULL_CASTER_SPELL_SLOTS[3], {1: 4, 2: 2})
        self.assertEqual(level_up.FULL_CASTER_SPELL_SLOTS[5], {1: 4, 2: 3, 3: 2})

    def test_half_caster(self):
        self.assertEqual(level_up.HALF_CASTER_SPELL_SLOTS[1], {})
        self.assertEqual(level_up.HALF_CASTER_SPELL_SLOTS[5], {1: 4, 2: 2})

    def test_warlock_pact_magic(self):
        self.assertEqual(level_up.WARLOCK_SPELL_SLOTS[1], {1: 1})
        self.assertEqual(level_up.WARLOCK_SPELL_SLOTS[3], {2: 2})
        self.assertEqual(level_up.WARLOCK_SPELL_SLOTS[11], {5: 3})

    def test_compute_spell_slots_keeps_expended_levels(self):
        sc = {"ability": "intelligence", "dc": 13, "attack_modifier": 5, "slots": {"1": 1}}
        out = level_up.compute_spell_slots("Wizard", 5, sc)
        self.assertEqual(out["slots"], {"1": 1, "2": 3, "3": 2})

    def test_unknown_class_returns_none(self):
        self.assertIsNone(level_up.compute_spell_slots("Commoner", 1, None))


if __name__ == "__main__":
    unittest.main()
