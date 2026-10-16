"""Behavioural tests for the active-spell buffs wired 2026-10-11 (Increment 3).

Covers the fields the generic delta/dice/defense paths did not: scoped save advantage (Haste,
Beacon of Hope), one-shot save dice (Resistance), a flat skill bonus (Pass Without Trace), psychic
immunity (Mind Blank), Foresight's four advantages, and the non-`+` speed forms. Also covers the
multi-field buff fix (Haste's speed + AC).

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_spell_buffs
"""

import unittest

import equipment

from web.tests.dice import harness as H

from rules import state
from rules.db import _carry_get
from rules.sheet import _derived_check, _derived_save
from rules.spell_effects import _store_active_effect


def buff_caster():
    return H.make_player(
        name="Buff", character_class="Wizard", race="Human", background="Sage",
        level=17, xp=225000, proficiency_bonus=6, armor_class=13,
        total_hit_points=100, current_hit_points=100, hit_dice_count=17, hit_dice_size=6,
        stats={"str": 10, "dex": 14, "con": 14, "int": 20, "wis": 12, "cha": 10},
        spellcasting={
            "ability": "intelligence", "dc": 19, "attack_modifier": 11,
            "cantrips": ["Resistance"], "spells_known": [], "spellbook": [],
            "spells_prepared": [], "slots": {str(n): 9 for n in range(1, 10)},
        },
        inventory=["Spellbook", "Component Pouch"],
    )


class ActiveSpellBuffTest(H.EngineCase):
    player_factory = staticmethod(buff_caster)

    def _cast_self(self, name, level=None):
        return H.ds.resolve_magic(spell_name=name, actor="Buff", target_name="Buff",
                                  slot_level=level)

    def _get(self):
        return _carry_get(state.DB_CONNECTION.cursor())

    def test_haste_doubles_speed_and_grants_dex_save_advantage(self):
        self._cast_self("Haste", 3)
        self.assertEqual(H.dbv("speed"), 60)  # 30 -> doubled
        save = _derived_save(state.DB_CONNECTION.cursor(), "dex")
        self.assertTrue(save["advantage"])
        self.assertIn("Haste", H.raw_dbv("active_effects") or [])

    def test_haste_second_numeric_field_still_applies(self):
        # Multi-field fix: Haste's +2 AC used to be blocked by its own speed entry.
        self._cast_self("Haste", 3)
        self.assertEqual(H.dbv("armor_class"), 15)  # 13 + 2

    def test_mind_blank_grants_psychic_immunity(self):
        self._cast_self("Mind Blank", 8)
        defenses = equipment.defense_state(self._get())
        self.assertIn("psychic", defenses["damage_immunities"])
        with H.fixed_rolls([15, 4]):
            r = H.ds.resolve_attack(actor="Goblin", attack_modifier=10, damage_dice="1d6",
                                    damage_type="psychic", is_npc_attack=True,
                                    target_name="Buff", target_ac=13, target_current_hp=100)
        self.assertEqual(H.dbv("current_hit_points"), 100)  # immune
        self.assertIn("immun", str(r.get("damage_modified") or "").lower())

    def test_beacon_of_hope_grants_wis_save_advantage(self):
        self._cast_self("Beacon of Hope", 3)
        save = _derived_save(state.DB_CONNECTION.cursor(), "wis")
        self.assertTrue(save["advantage"])

    def test_beacon_of_hope_registers_death_save_advantage(self):
        self._cast_self("Beacon of Hope", 3)
        self.assertTrue(
            equipment.active_advantage_for(self._get(), "save_advantage", "death")[0])

    def test_death_save_advantage_takes_the_higher_roll(self):
        # Beacon of Hope is concentration, so it ends at 0 HP (SRD: incapacitated). The death-save
        # advantage mechanism is exercised with a non-concentration effect instead.
        cursor = state.DB_CONNECTION.cursor()
        _store_active_effect(cursor, "Blessing", "save_advantage", "death_saves", "death")
        H.ds.modify_player_numeric(key="current_hit_points", delta=-100)  # to 0
        with H.fixed_rolls([5, 15]):  # disadvantage would keep 5; advantage takes 15
            r = H.ds.make_death_save()
        self.assertTrue(r["success"])
        self.assertEqual(r["roll"], 15)
        self.assertEqual(r["outcome"], "success")

    def test_resistance_grants_a_one_shot_save_die(self):
        self._cast_self("Resistance")  # cantrip
        with H.fixed_rolls([4, 10]):  # d4 dice first, then the d20
            r = H.ds.perform_check(actor="Buff", check_name="Dexterity save", dc=15,
                                   save=True, ability="dex")
        self.assertEqual(r["dice_bonus"], 4)
        with H.fixed_rolls([10]):  # the charge is spent
            r2 = H.ds.perform_check(actor="Buff", check_name="Dexterity save", dc=15,
                                    save=True, ability="dex")
        self.assertNotIn("dice_bonus", r2)

    def test_pass_without_trace_grants_a_stealth_bonus(self):
        self._cast_self("Pass Without Trace", 2)
        chk = _derived_check(state.DB_CONNECTION.cursor(), "Stealth")
        self.assertGreaterEqual(chk["item_bonus"], 10)
        self.assertTrue(any("+10" in s for s in chk["item_sources"]))

    def test_fly_grants_a_fly_speed(self):
        self._cast_self("Fly", 3)
        state = equipment.speed_state(self._get())
        self.assertEqual(state["modes"].get("fly"), 60)

    def test_foresight_grants_advantage_and_attackers_disadvantage(self):
        self._cast_self("Foresight", 9)
        get = self._get()
        self.assertTrue(equipment.active_advantage_for(get, "attack_advantage")[0])
        self.assertTrue(equipment.active_advantage_for(get, "attackers_disadvantage")[0])
        self.assertTrue(equipment.active_advantage_for(get, "save_advantage", "dex")[0])
        self.assertTrue(equipment.active_advantage_for(get, "check_advantage", "stealth")[0])
        # The player attacks with advantage (two d20s, keeps the 20).
        with H.fixed_rolls([1, 20, 4]):
            r = H.ds.resolve_attack(actor="Buff", attack_modifier=5, damage_dice="1d6",
                                    damage_type="slashing", target_ac=10, target_name="Goblin")
        self.assertEqual(r["attack_roll"], 20)
        self.assertEqual(r["outcome"], "Critical Success")


if __name__ == "__main__":
    unittest.main()
