"""resolve_magic: slots, cantrips, saves, AoE, healing, THP, buffs, scrolls."""

import unittest

from web.tests.dice import harness as H


class SpellCastingTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_cantrip_consumes_no_slot(self):
        with H.fixed_rolls([15, 5]):
            r = H.ds.resolve_magic(spell_name="Fire Bolt", actor="Senna", spell_attack_modifier=5,
                                   target_ac=12, target_name="G", target_current_hp=30)
        self.assertEqual(r["slot_consumed"], "cantrip")
        self.assertEqual(H.dbv("spellcasting")["slots"]["1"], 4)

    def test_slot_consumed(self):
        with H.fixed_rolls([4, 2, 2]):
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                                   target_ac=12, target_name="G", target_current_hp=30)
        self.assertEqual(r["slot_result"]["old_value"], 4)
        self.assertEqual(r["slot_result"]["new_value"], 3)
        self.assertEqual(H.dbv("spellcasting")["slots"]["1"], 3)

    def test_unknown_spell_without_overrides_errors(self):
        r = H.ds.resolve_magic(spell_name="Nonexistent Spell", actor="Senna", slot_level=1)
        self.assertFalse(r.get("success", True))
        self.assertIn("error", r)

    def test_scroll_cast_uses_no_slot(self):
        with H.fixed_rolls([4, 2, 2]):
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                                   target_ac=12, target_name="G", target_current_hp=30, is_scroll=True)
        self.assertEqual(r["slot_consumed"], "scroll")
        self.assertEqual(H.dbv("spellcasting")["slots"]["1"], 4)  # unchanged
        self.assertIn("Scroll", r["narrative_format"])

    def test_no_slot_failure_carries_a_mechanics_line(self):
        H.ds.modify_player_numeric("spellcasting.slots.1", -4)
        r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1)
        self.assertFalse(r["success"])
        self.assertEqual(r["slot_level_needed"], 1)
        self.assertIn("not cast: no 1st-level slot remaining", r["narrative_format"])
        self.assertIn("No level 1 spell slots", r["error"])

    def test_spellbook_missing_failure_carries_a_mechanics_line(self):
        H.ds.update_player_list(key="inventory", item="Spellbook", action="remove")
        r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                               target_ac=12, target_name="G", target_current_hp=30)
        self.assertFalse(r["success"])
        self.assertIn("not cast: the spellbook is missing", r["narrative_format"])
        self.assertIn("spellbook is missing", r["error"])

    def test_scroll_fizzle_carries_a_mechanics_line(self):
        with H.rolls_always(1):  # 1 + INT 3 = 4 vs DC 13
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=3,
                                   is_scroll=True, target_ac=12, target_name="G",
                                   target_current_hp=30)
        self.assertFalse(r["success"])
        self.assertIn("the scroll fizzles", r["narrative_format"])
        self.assertIn("Scroll ability check failed", r["error"])


class SavingThrowSpellTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_failed_save_takes_full_damage(self):
        with H.fixed_rolls([1, 2, 3, 1]):
            r = H.ds.resolve_magic(spell_name="Burning Hands", actor="Senna", spell_save_dc=13,
                                   slot_level=1, target_name="G", target_current_hp=40,
                                   target_save_modifier=0)
        self.assertFalse(r["save_success"])
        self.assertEqual(r["damage_total"], 6)

    def test_successful_save_halves_damage(self):
        with H.fixed_rolls([1, 2, 3, 20]):
            r = H.ds.resolve_magic(spell_name="Burning Hands", actor="Senna", spell_save_dc=13,
                                   slot_level=1, target_name="G", target_current_hp=40,
                                   target_save_modifier=0)
        self.assertTrue(r["save_success"])
        self.assertEqual(r["damage_total"], 3)


class AoeSpellTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_one_roll_one_slot_per_target_saves(self):
        with H.fixed_rolls([4, 4, 4, 10, 10]):
            r = H.ds.resolve_magic(
                spell_name="Burning Hands", actor="Senna", spell_save_dc=13, slot_level=1,
                targets=[{"name": "A", "hp": 20, "ac": 12, "save_modifier": 0},
                         {"name": "B", "hp": 20, "ac": 12, "save_modifier": 5}],
            )
        self.assertEqual(len(r["targets"]), 2)
        self.assertEqual(r["targets"][0]["damage"], 12)   # failed save
        self.assertEqual(r["targets"][1]["damage"], 6)    # saved -> half
        self.assertEqual(r["slot_result"]["old_value"], 4)  # exactly one slot for the AoE


class HealingAndBuffTest(H.EngineCase):
    player_factory = staticmethod(H.cleric_l5)  # WIS 18 -> +4

    def test_cure_wounds_adds_spellcasting_modifier(self):
        with H.fixed_rolls([4]):
            r = H.ds.resolve_magic(spell_name="Cure Wounds", actor="Father Aldric", slot_level=1,
                                   target_name="Father Aldric", healing=True)
        self.assertEqual(r["healing_total"], 8)  # 1d8 (4) + WIS (+4)
        self.assertIn("Healing → Father Aldric: 8", r["narrative_format"])

    def test_healing_word_also_adds_modifier(self):
        with H.fixed_rolls([3]):
            r = H.ds.resolve_magic(spell_name="Healing Word", actor="Father Aldric", slot_level=1,
                                   target_name="Father Aldric", healing=True)
        self.assertEqual(r["healing_total"], 7)  # 1d4 (3) + WIS (+4)


def wounded_cleric_l5():
    """Father Aldric at 37/38 HP — one point missing, so healing must cap at 38."""
    p = H.cleric_l5()
    p["current_hit_points"] = 37
    return p


class HealingCapTest(H.EngineCase):
    """Healing is capped at max HP and always declares the ceiling automatically."""

    player_factory = staticmethod(wounded_cleric_l5)  # WIS 18 -> +4

    def test_healing_declares_max_hp_and_caps_at_it(self):
        with H.fixed_rolls([6]):
            r = H.ds.resolve_magic(spell_name="Cure Wounds", actor="Father Aldric", slot_level=1,
                                   target_name="Father Aldric")
        self.assertEqual(r["healing_total"], 10)   # 1d8 (6) + WIS (+4)
        self.assertEqual(r["healing_applied"], 1)  # only 1 HP was missing
        healed = r["target_healed"]
        self.assertEqual(healed["max_hp"], 38)
        self.assertEqual(healed["remaining_hp"], 38)
        self.assertEqual(H.dbv("current_hit_points"), 38)

    def test_healing_at_full_hp_reports_zero_applied(self):
        with H.fixed_rolls([6]):
            H.ds.resolve_magic(spell_name="Cure Wounds", actor="Father Aldric", slot_level=1,
                               target_name="Father Aldric")
        with H.fixed_rolls([6]):
            r = H.ds.resolve_magic(spell_name="Cure Wounds", actor="Father Aldric", slot_level=1,
                                   target_name="Father Aldric")
        self.assertEqual(r["healing_total"], 10)
        self.assertEqual(r["healing_applied"], 0)
        self.assertEqual(r["target_healed"]["remaining_hp"], 38)
        self.assertEqual(r["target_healed"]["max_hp"], 38)

    def test_gm_max_hp_override_for_a_wounded_target(self):
        # A creature the engine has no sheet for: the GM supplies current + max.
        with H.fixed_rolls([6]):
            r = H.ds.resolve_magic(spell_name="Cure Wounds", actor="Father Aldric", slot_level=1,
                                   target_name="Wounded Peasant",
                                   target_current_hp=4, target_max_hp=20)
        healed = r["target_healed"]
        self.assertEqual(healed["max_hp"], 20)
        self.assertEqual(healed["remaining_hp"], 14)
        self.assertEqual(healed["healing_applied"], 10)
        self.assertIn("warning", healed)  # not in the registry -> written nowhere

    def test_unknown_npc_heal_never_touches_the_player(self):
        before = H.dbv("current_hit_points")
        with H.fixed_rolls([6]):
            H.ds.resolve_magic(spell_name="Cure Wounds", actor="Father Aldric", slot_level=1,
                               target_name="Absent Peasant")
        self.assertEqual(H.dbv("current_hit_points"), before)


class TempHpTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_false_life_grants_temporary_hp(self):
        with H.rolls_always(3):   # 1d4 + 4 -> 3 + 4
            r = H.ds.resolve_magic(spell_name="False Life", actor="Senna", slot_level=1,
                                   target_name="Senna")
        self.assertEqual(H.dbv("temporary_hit_points"), 7)
        self.assertIn("False Life", H.dbv("active_effects"))

    def test_false_life_does_not_heal_real_hp(self):
        H.ds.modify_player_numeric(key="current_hit_points", delta=-9)  # 13 -> 4
        before = H.dbv("current_hit_points")
        with H.rolls_always(3):   # 1d4 + 4 -> 7 temporary HP
            r = H.ds.resolve_magic(spell_name="False Life", actor="Senna", slot_level=1,
                                   target_name="Senna")
        self.assertEqual(H.dbv("current_hit_points"), before)  # real HP untouched
        self.assertEqual(H.dbv("temporary_hit_points"), 7)
        self.assertTrue(r["temporary_hp_only"])
        self.assertEqual(r["healing_applied"], 0)

    def test_temporary_hp_may_exceed_max_hp(self):
        with H.rolls_always(3):
            H.ds.resolve_magic(spell_name="False Life", actor="Senna", slot_level=1,
                               target_name="Senna")
        self.assertEqual(H.dbv("current_hit_points"), 13)      # already at max
        self.assertEqual(H.dbv("temporary_hit_points"), 7)    # pool sits ABOVE max


class BuffTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_mage_armor_applies_buff(self):
        with H.fixed_rolls([10]):
            r = H.ds.resolve_magic(spell_name="Mage Armor", actor="Senna", slot_level=1,
                                   target_name="Senna")
        self.assertTrue(r["success"])
        self.assertIn("Mage Armor", H.dbv("active_effects"))
        self.assertEqual(H.dbv("armor_class"), 15)

    def test_duplicate_buff_rejected_without_consuming_slot(self):
        with H.fixed_rolls([10]):
            H.ds.resolve_magic(spell_name="Mage Armor", actor="Senna", slot_level=1, target_name="Senna")
        slots_before = H.dbv("spellcasting")["slots"]["1"]
        r = H.ds.resolve_magic(spell_name="Mage Armor", actor="Senna", slot_level=1, target_name="Senna")
        self.assertFalse(r["success"])
        self.assertIn("already active", r["error"])
        self.assertIn("already active, not recast", r["narrative_format"])
        self.assertEqual(H.dbv("spellcasting")["slots"]["1"], slots_before)


class SleepPoolTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_sleep_drains_pool_in_ascending_hp_order(self):
        with H.fixed_rolls([5, 5, 5, 5, 5]):  # 5d8 -> pool 25
            r = H.ds.resolve_magic(
                spell_name="Sleep", actor="Senna", slot_level=1,
                targets=[{"name": "A", "current_hp": 20, "ac": 12},
                         {"name": "B", "current_hp": 10, "ac": 12}],
            )
        self.assertTrue(r["hp_pool"])
        self.assertEqual(r["hp_pool_total"], 25)
        self.assertEqual([t["name"] for t in r["targets_affected"]], ["B"])    # 10 <= 25
        self.assertEqual([t["name"] for t in r["targets_unaffected"]], ["A"])  # 20 > 15
        self.assertEqual(r["hp_pool_remaining"], 15)
        self.assertEqual(r["condition"], "Unconscious")

    def test_target_without_current_hp_or_registry_counts_as_zero(self):
        # Documents the trap: the engine reads 'current_hp' (or the registry),
        # NOT 'hp'. A target carrying only 'hp' is treated as 0 HP and is always
        # affected without draining the pool.
        with H.fixed_rolls([1, 1, 1, 1, 1]):  # pool 5
            r = H.ds.resolve_magic(
                spell_name="Sleep", actor="Senna", slot_level=1,
                targets=[{"name": "X", "hp": 99, "ac": 12}],
            )
        self.assertEqual([t["name"] for t in r["targets_affected"]], ["X"])
        self.assertEqual(r["hp_pool_remaining"], 5)


class ConditionAutoApplyTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_sleep_applies_unconscious_to_affected_targets(self):
        with H.rolls_always(10):
            H.ds.register_combatants([{"name": "Peck", "hp": 5, "ac": 11},
                                      {"name": "Boss", "hp": 40, "ac": 15}])
        with H.fixed_rolls([5, 5, 5, 5, 5]):  # pool 25
            r = H.ds.resolve_magic(spell_name="Sleep", actor="Senna", slot_level=1,
                                   targets=[{"name": "Peck", "current_hp": 5},
                                            {"name": "Boss", "current_hp": 40}])
        self.assertEqual(r["conditions_applied"], ["Peck"])
        self.assertIn("unconscious", H.ds._COMBAT_REGISTRY["Peck"]["conditions"])
        self.assertIn("Peck", r["condition_reminder"])
        self.assertIn("Peck: Affected", r["narrative_format"])
        self.assertIn("Boss: Unaffected", r["narrative_format"])
        self.assertNotIn("(HP ", r["narrative_format"])   # HP lives in the tooltip now

    def test_immune_target_refuses_the_condition(self):
        with H.rolls_always(10):
            H.ds.register_combatants([{"name": "Construct", "hp": 5, "ac": 10,
                                       "condition_immunities": ["unconscious"]}])
        with H.fixed_rolls([5, 5, 5, 5, 5]):
            r = H.ds.resolve_magic(spell_name="Sleep", actor="Senna", slot_level=1,
                                   targets=[{"name": "Construct", "current_hp": 5}])
        self.assertNotIn("conditions_applied", r)
        self.assertEqual(r.get("conditions_refused"), ["Construct"])
        self.assertEqual(H.ds._COMBAT_REGISTRY["Construct"]["conditions"], [])


class ScorchingRayTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)  # has 2nd-level slots

    def test_three_rays_each_roll_to_hit(self):
        # ray1: 15 -> hit, 2d6 = 3+4 = 7 ; ray2: 15 -> hit, 2d6 = 1+1 = 2 ; ray3: nat 1 miss
        with H.fixed_rolls([15, 3, 4, 15, 1, 1]):
            r = H.ds.resolve_magic(spell_name="Scorching Ray", actor="Senna",
                                   spell_attack_modifier=5, target_ac=12,
                                   target_name="Goblin", target_current_hp=40, slot_level=2)
        self.assertEqual(r["attack_type"], "attack_roll")
        self.assertEqual(r["projectiles"], 3)
        self.assertEqual([d["hit"] for d in r["per_projectile"]], [True, True, False])
        self.assertEqual(r["damage_total"], 9)
        self.assertIn("3 rays", r["narrative_format"])

    def test_ray_crit_doubles_that_ray_only(self):
        # ray1: nat 20 -> crit (2d6 doubled: 1+1 + 1+1 = 4)
        with H.fixed_rolls([20, 1, 1, 1, 1]):
            r = H.ds.resolve_magic(spell_name="Scorching Ray", actor="Senna",
                                   spell_attack_modifier=5, target_ac=12,
                                   target_name="Goblin", target_current_hp=60, slot_level=2)
        first = r["per_projectile"][0]
        self.assertTrue(first["crit"])
        self.assertEqual(first["damage"], 4)   # (1+1) + crit (1+1)


class ScorchingRayUpcastTest(H.EngineCase):
    player_factory = staticmethod(lambda: H.make_player(
        name="Senna", character_class="Wizard", level=5,
        spellcasting={"ability": "intelligence", "dc": 13, "attack_modifier": 5,
                      "slots": {"2": 2, "3": 1}}))

    def test_upcast_adds_a_ray(self):
        with H.fixed_rolls([15, 1, 1] * 4):
            r = H.ds.resolve_magic(spell_name="Scorching Ray", actor="Senna",
                                   spell_attack_modifier=5, target_ac=12,
                                   target_name="Goblin", target_current_hp=80, slot_level=3)
        self.assertEqual(r["projectiles"], 4)  # 3 base + 1 upcast
        self.assertEqual(r["damage_total"], 8)  # 4 rays x (1+1)


class EldritchBlastTest(unittest.TestCase):
    def test_beams_scale_with_character_level(self):
        for level, beams in [(1, 1), (5, 2), (11, 3), (17, 4)]:
            with self.subTest(level=level):
                player = H.make_player(name="Vex", character_class="Warlock", level=level)
                with H.load(player):
                    with H.fixed_rolls([15, 5] * beams) as calls:
                        r = H.ds.resolve_magic(spell_name="Eldritch Blast", actor="Vex",
                                               spell_attack_modifier=5, target_ac=12,
                                               target_name="Goblin", target_current_hp=999)
                    self.assertEqual(r["projectiles"], beams)
                    self.assertEqual(r["damage_total"], 5 * beams)
                    self.assertEqual(calls, [(1, 20), (1, 10)] * beams)
                    self.assertEqual(r["slot_consumed"], "cantrip")


class SpellbookItemTest(H.EngineCase):
    """A wizard without their spellbook item: levels blocked, cantrips/scrolls still work."""

    player_factory = staticmethod(H.wizard_l3)

    def _steal_book(self):
        r = H.ds.update_player_list(key="inventory", item="Spellbook", action="remove")
        self.assertTrue(r.get("success", True), r)
        self.assertNotIn("Spellbook", H.dbv("inventory"))

    def test_leveled_spell_blocked_and_no_slot_consumed(self):
        self._steal_book()
        r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                               target_ac=12, target_name="G", target_current_hp=30)
        self.assertFalse(r.get("success", True))
        self.assertIn("spellbook", r["error"].lower())
        self.assertEqual(H.dbv("spellcasting")["slots"]["1"], 4)  # untouched

    def test_cantrip_still_works(self):
        self._steal_book()
        with H.fixed_rolls([15, 5]):
            r = H.ds.resolve_magic(spell_name="Fire Bolt", actor="Senna", spell_attack_modifier=5,
                                   target_ac=12, target_name="G", target_current_hp=30)
        self.assertEqual(r["slot_consumed"], "cantrip")
        self.assertTrue(r.get("success", True))

    def test_scroll_still_works(self):
        self._steal_book()
        with H.fixed_rolls([4, 2, 2]):
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                                   target_ac=12, target_name="G", target_current_hp=30, is_scroll=True)
        self.assertEqual(r["slot_consumed"], "scroll")

    def test_book_present_casts_normally(self):
        with H.fixed_rolls([4, 2, 2]):
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Senna", slot_level=1,
                                   target_ac=12, target_name="G", target_current_hp=30)
        self.assertEqual(r["slot_consumed"], 1)

    def test_cannot_prepare_spells_without_the_book(self):
        self._steal_book()
        r = H.ds.rest(rest_type="long", prepared_spells=["Magic Missile", "Sleep"])
        err = r["changes"]["prepared_spells_error"]
        self.assertEqual(err["error"], "Spellbook missing.")
        self.assertNotIn("prepared_spells", r["changes"])

    def test_preparing_still_works_with_the_book(self):
        r = H.ds.rest(rest_type="long", prepared_spells=["Magic Missile", "Sleep"])
        self.assertEqual(r["changes"]["prepared_spells"]["count"], 2)


class PreparedCasterNotGatedTest(H.EngineCase):
    """Clerics/Druids have no spellbook list and are never gated on the item."""

    player_factory = staticmethod(H.cleric_l5)

    def test_cleric_can_prepare_without_a_spellbook_item(self):
        self.assertNotIn("Spellbook", H.dbv("inventory"))
        r = H.ds.rest(rest_type="long", prepared_spells=["Cure Wounds"])
        self.assertNotIn("prepared_spells_error", r["changes"])


if __name__ == "__main__":
    unittest.main()
