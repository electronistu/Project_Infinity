"""Spell resolution contract: registry AC fallback, config-vs-GM conflicts, and the
rule that a targets[] list is never silently dropped.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_spell_resolution
"""

import unittest

from web.tests.dice import harness as H


GOBLIN = {
    "name": "Goblin", "hp": 7, "ac": 15, "initiative_modifier": 2, "challenge_rating": 0.25,
    "role": "hostile", "saves": {"str": -1, "dex": 2, "con": 0, "int": 0, "wis": -1, "cha": -1},
}
MAGE = {
    "name": "Evil Mage", "hp": 22, "ac": 12, "initiative_modifier": 2, "challenge_rating": 6,
    "spellcasting": {"ability": "int", "save_dc": 14, "attack_modifier": 5},
}


class RegistryAcLookupTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)  # Senna, knows Fire Bolt (cantrip, +5)

    def test_attack_spell_uses_registry_ac_when_omitted(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([10, 7]):
            r = H.ds.resolve_magic(spell_name="Fire Bolt", actor="Senna", target_name="Goblin")
        self.assertTrue(r["success"])
        self.assertEqual(r["target_ac"], 15)  # from the registry, not passed by the GM
        self.assertEqual(r["attack_roll"], 10)
        self.assertEqual(r["total_attack"], 15)

    def test_explicit_target_ac_still_wins(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([10, 7]):
            r = H.ds.resolve_magic(spell_name="Fire Bolt", actor="Senna", target_ac=25,
                                   target_name="Goblin")
        self.assertEqual(r["target_ac"], 25)

    def test_unregistered_target_still_needs_target_ac(self):
        r = H.ds.resolve_magic(spell_name="Fire Bolt", actor="Senna", target_name="Ghost")
        self.assertFalse(r["success"])
        self.assertIn("target_ac", r["error"])


class TargetsNeverDroppedTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_attack_roll_spell_with_targets_resolves_each(self):
        a = dict(GOBLIN, name="Goblin A")
        b = dict(GOBLIN, name="Goblin B")
        with H.fixed_rolls([15, 6, 6]):
            H.ds.register_combatants([a, b])
        with H.fixed_rolls([18, 6, 18, 6]):  # attack, damage, attack, damage
            r = H.ds.resolve_magic(
                spell_name="Ray Volley", actor="Senna", attack_type="attack_roll",
                damage_dice="1d6", damage_type="force",
                targets=[{"name": "Goblin A"}, {"name": "Goblin B"}])
        self.assertTrue(r["success"])
        self.assertEqual(len(r["targets"]), 2)
        self.assertEqual([t["damage"] for t in r["targets"]], [6, 6])
        self.assertEqual([t["remaining_hp"] for t in r["targets"]], [1, 1])
        self.assertEqual(H.ds._COMBAT_REGISTRY["Goblin A"]["current_hp"], 1)

    def test_automatic_spell_with_targets_is_refused_not_dropped(self):
        r = H.ds.resolve_magic(spell_name="Mage Armor", actor="Senna",
                               targets=[{"name": "Senna"}])
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "targets_unsupported")


class ConfigWinsWarnTest(H.EngineCase):
    player_factory = staticmethod(H.cleric_l5)  # Father Aldric has 3rd-level slots

    def test_explicit_fields_that_contradict_config_are_reported(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([10, 5, 5, 5, 5, 5, 5]):
            r = H.ds.resolve_magic(
                spell_name="Lightning Bolt", actor="Father Aldric", spell_save_dc=15,
                attack_type="saving_throw", save_type="dex", damage_dice="6d6",
                targets=[{"name": "Goblin"}])
        self.assertTrue(r["success"])
        self.assertIn("config_warning", r)
        self.assertEqual(r["config_warning"]["fields"]["damage_dice"],
                         {"provided": "6d6", "config": "8d6"})

    def test_matching_fields_produce_no_warning(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([10, 5, 5, 5, 5, 5, 5]):
            r = H.ds.resolve_magic(
                spell_name="Lightning Bolt", actor="Father Aldric", spell_save_dc=15,
                attack_type="saving_throw", save_type="dex", damage_dice="8d6",
                targets=[{"name": "Goblin"}])
        self.assertNotIn("config_warning", r)


def power_l20():
    return H.make_player(
        name="Power", character_class="Wizard", race="High Elf", background="Sage",
        level=20, xp=355000, proficiency_bonus=6, armor_class=15,
        total_hit_points=82, current_hit_points=82, hit_dice_count=20, hit_dice_size=6,
        stats={"str": 8, "dex": 20, "con": 10, "int": 20, "wis": 12, "cha": 10},
        spellcasting={"ability": "intelligence", "dc": 19, "attack_modifier": 11,
                      "cantrips": ["Fire Bolt"], "spells_known": [],
                      "spellbook": ["Chain Lightning"],
                      "spells_prepared": [{"name": "Chain Lightning"}],
                      "slots": {"4": 1, "5": 1, "6": 1}},
        inventory=["Spellbook", "Component Pouch"],
    )


class ChainLightningIncidentTest(H.EngineCase):
    """The live incident: Chain Lightning must roll a DEX save per target and apply HP+XP —
    not a single phantom attack roll with the target list dropped."""

    player_factory = staticmethod(power_l20)

    def test_chain_lightning_is_a_dex_save_aoe(self):
        with H.fixed_rolls([20, 5, 5]):
            H.ds.register_combatants([
                {"name": "Corporal Gao", "hp": 22, "ac": 17, "initiative_modifier": 2,
                 "challenge_rating": 0.5, "saves": {"dex": 2}},
                {"name": "Post-Road Trooper 1", "hp": 11, "ac": 16,
                 "initiative_modifier": 1, "challenge_rating": 0.125,
                 "saves": {"dex": 1}},
            ])
        with H.fixed_rolls([8] * 10 + [1, 1]):  # 10d8 damage, then one DEX save per target
            r = H.ds.resolve_magic(
                spell_name="Chain Lightning", spell_save_dc=19,
                targets=[{"name": "Corporal Gao"}, {"name": "Post-Road Trooper 1"}])
        self.assertTrue(r["success"])
        self.assertEqual(r["attack_type"], "saving_throw")
        self.assertEqual(len(r["targets"]), 2)
        self.assertTrue(all(not t["save_success"] for t in r["targets"]))
        self.assertEqual(r["killed_count"], 2)
        self.assertEqual(r["xp_awarded"], 125)
        self.assertIn("Corporal Gao DEX Save", r["narrative_format"])
        self.assertEqual(H.ds._COMBAT_REGISTRY["Corporal Gao"]["current_hp"], 0)


class RecurringSaveTest(H.EngineCase):
    """Repeating saves: the engine owns the per-turn roll, and a skipped turn still fires."""

    player_factory = staticmethod(power_l20)

    def _register(self, extra=None):
        with H.fixed_rolls([20, 5, 1]):
            return H.ds.register_combatants([
                {"name": "Gao", "hp": 25, "ac": 17, "initiative_modifier": 2,
                 "challenge_rating": 0.5, "saves": {"wis": 2, "con": 2},
                 "attacks": [{"name": "Spear", "attack_bonus": 4, "damage_dice": "1d6",
                              "damage_modifier": 2, "damage_type": "piercing"}]},
                *(extra or []),
            ])

    def _cast_pk(self):
        with H.fixed_rolls([1]):  # Gao's initial WIS save: 1 + 2 = 3 < 19
            return H.ds.resolve_magic(spell_name="Phantasmal Killer", slot_level=4,
                                       targets=[{"name": "Gao"}])

    def test_initial_failure_registers_but_deals_no_damage(self):
        self._register()
        r = self._cast_pk()
        self.assertEqual(r["condition"], "Frightened")
        self.assertEqual(r["recurring_registered"], ["Gao"])
        self.assertEqual(H.ds._COMBAT_REGISTRY["Gao"]["current_hp"], 25)  # no damage yet
        self.assertEqual(len(H.ds._COMBAT_REGISTRY["Gao"]["recurring"]), 1)

    def test_repeat_save_fires_on_the_target_turn(self):
        self._register()
        self._cast_pk()
        with H.fixed_rolls([1, 10, 10, 10, 10]):  # WIS save fails, then 4d10 psychic
            r = H.ds.resolve_attack(actor="Gao", attack="Spear", target_name="Power",
                                    is_npc_attack=True)
        self.assertIn("WIS Save (Phantasmal Killer)", r["narrative_format"])
        self.assertEqual(r["recurring"][0]["damage"], 40)
        self.assertEqual(r["recurring"][0]["remaining_hp"], 0)
        self.assertTrue(r["recurring"][0]["killed"])
        self.assertEqual(H.ds._COMBAT_REGISTRY["Gao"]["recurring"], [])  # consumed on the kill

    def test_a_skipped_turn_still_fires_when_the_cursor_passes_it(self):
        self._register(extra=[
            {"name": "Trooper", "hp": 11, "ac": 16, "initiative_modifier": 1,
             "challenge_rating": 0.125,
             "attacks": [{"name": "Spear", "attack_bonus": 3, "damage_dice": "1d6",
                          "damage_modifier": 1, "damage_type": "piercing"}]},
        ])
        self._cast_pk()
        before = H.ds._COMBAT_REGISTRY["Gao"]["current_hp"]
        # Gao's turn is skipped; the Trooper acts next and the cursor passes Gao.
        with H.fixed_rolls([1, 10, 10, 10, 10, 1]):
            r = H.ds.resolve_attack(actor="Trooper", attack="Spear", target_name="Power",
                                    is_npc_attack=True)
        self.assertLess(H.ds._COMBAT_REGISTRY["Gao"]["current_hp"], before)
        self.assertTrue(any(x["name"] == "Gao" for x in r["recurring"]))

    def test_perform_check_advances_the_cursor(self):
        self._register(extra=[
            {"name": "Trooper", "hp": 11, "ac": 16, "initiative_modifier": 1,
             "challenge_rating": 0.125,
             "attacks": [{"name": "Spear", "attack_bonus": 3, "damage_dice": "1d6",
                          "damage_modifier": 1, "damage_type": "piercing"}]},
        ])
        self._cast_pk()
        before = H.ds._COMBAT_REGISTRY["Gao"]["current_hp"]
        # The Trooper's turn is advanced with a check; the cursor passes Gao.
        with H.fixed_rolls([15, 1, 10, 10, 10, 10]):
            r = H.ds.perform_check(actor="Trooper", dc=10, check_name="Athletics", ability="str")
        self.assertTrue(any(x["name"] == "Gao" for x in r.get("recurring", [])))
        self.assertLess(H.ds._COMBAT_REGISTRY["Gao"]["current_hp"], before)

    def test_multiattack_does_not_double_roll(self):
        self._register()
        self._cast_pk()
        with H.fixed_rolls([1, 4, 4, 4, 4]):
            H.ds.resolve_attack(actor="Gao", attack="Spear", target_name="Power",
                                is_npc_attack=True)
        hp_after_first = H.ds._COMBAT_REGISTRY["Gao"]["current_hp"]
        with H.fixed_rolls([1, 4, 4, 4, 4]):
            second = H.ds.resolve_attack(actor="Gao", attack="Spear", target_name="Power",
                                          is_npc_attack=True)
        self.assertNotIn("recurring", second)  # same turn: no second repeat save
        self.assertEqual(H.ds._COMBAT_REGISTRY["Gao"]["current_hp"], hp_after_first)

    def test_flesh_to_stone_ladder_petrifies_after_three_failures(self):
        self._register()
        with H.fixed_rolls([1]):  # initial CON save fails -> restrained + recurring
            first = H.ds.resolve_magic(spell_name="Flesh to Stone", slot_level=6,
                                       targets=[{"name": "Gao"}])
        self.assertEqual(first["condition"], "Restrained")
        for _ in range(3):
            H.ds._advance_turn("Power")          # wrap the round back to the top
            with H.fixed_rolls([1]):             # end-of-turn CON save fails three times
                H.ds._advance_turn("Gao")
        self.assertIn("petrified", H.ds._COMBAT_REGISTRY["Gao"]["conditions"])
        self.assertTrue(H.ds._COMBAT_REGISTRY["Gao"]["recurring"][0]["locked"])

    def test_contagion_lands_on_a_hit_and_starts_its_ladder(self):
        self._register()
        with H.fixed_rolls([10]):  # spell attack 10 + 11 = 21 vs AC 17
            r = H.ds.resolve_magic(spell_name="Contagion", slot_level=6, target_name="Gao")
        self.assertTrue(r["success"])
        self.assertEqual(r["condition"], "Diseased")
        self.assertEqual(r["recurring_registered"], ["Gao"])


class SpellDamageModifierTest(H.EngineCase):
    """Spiritual Weapon's ability modifier and per-two-level upcast."""

    player_factory = staticmethod(H.cleric_l5)  # WIS 18 -> +4, spell attack +7, DC 15

    def test_spiritual_weapon_adds_the_spellcasting_modifier(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([8, 4]):  # attack 8 + 7 = 15 (hit), damage 1d8 = 4
            r = H.ds.resolve_magic(spell_name="Spiritual Weapon", actor="Father Aldric",
                                   slot_level=2, target_name="Goblin")
        self.assertEqual(r["damage_total"], 8)  # 4 + WIS +4


class PostHitSaveTest(H.EngineCase):
    """Ray of Enfeeblement: the condition needs a failed save AFTER the hit."""

    player_factory = staticmethod(H.cleric_l5)

    def test_condition_only_on_a_failed_post_hit_save(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([8, 5]):  # hit 8 + 7; CON save 5 + 0 < 15
            r = H.ds.resolve_magic(spell_name="Ray of Enfeeblement", actor="Father Aldric",
                                   slot_level=2, target_name="Goblin")
        self.assertTrue(r["success"])
        self.assertEqual(r["condition"], "Weakened")
        self.assertEqual(r["conditions_applied"], ["Goblin"])

    def test_a_successful_post_hit_save_leaves_the_target_unaffected(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([8, 20]):  # hit, then a natural 20 on the CON save
            r = H.ds.resolve_magic(spell_name="Ray of Enfeeblement", actor="Father Aldric",
                                   slot_level=2, target_name="Goblin")
        self.assertNotIn("conditions_applied", r)


def warpriest():
    return H.make_player(
        name="Father Aldric", character_class="Cleric", level=7, xp=23000,
        proficiency_bonus=3, armor_class=18, total_hit_points=52, current_hit_points=52,
        hit_dice_count=7, hit_dice_size=8,
        stats={"str": 12, "dex": 10, "con": 14, "int": 10, "wis": 18, "cha": 12},
        spellcasting={"ability": "wisdom", "dc": 15, "attack_modifier": 7,
                      "cantrips": [], "spellbook": [], "spells_prepared": [],
                      "slots": {"2": 2, "4": 1, "6": 1, "7": 1}},
        inventory=["Mace", "Chain Mail", "Shield"],
    )


class UpcastStyleTest(H.EngineCase):
    """Flame Strike's per-cast choice and Delayed Blast Fireball's accumulated rounds."""

    player_factory = staticmethod(warpriest)

    def test_spiritual_weapon_upcasts_per_two_levels(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([8, 4, 2]):  # slot 4 = two above 2 -> +1d8; 4 + 2 + WIS 4
            r = H.ds.resolve_magic(spell_name="Spiritual Weapon", actor="Father Aldric",
                                   slot_level=4, target_name="Goblin")
        self.assertEqual(r["damage_total"], 10)  # 2d8 (6) + 4

    def test_flame_strike_choice_defaults_to_the_primary_component(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([6] * 10):  # fire 5d6, radiant 4d6, then the DEX save
            r = H.ds.resolve_magic(spell_name="Flame Strike", actor="Father Aldric",
                                   slot_level=6, targets=[{"name": "Goblin"}])
        self.assertEqual(r["primary_damage"], 30)   # 5d6 fire
        self.assertEqual(r["extra_damage"], 24)     # 4d6 radiant

    def test_flame_strike_choice_can_empower_radiant(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([6] * 10):
            r = H.ds.resolve_magic(spell_name="Flame Strike", actor="Father Aldric",
                                   slot_level=6, boost_damage_type="radiant",
                                   targets=[{"name": "Goblin"}])
        self.assertEqual(r["primary_damage"], 24)   # 4d6 fire
        self.assertEqual(r["extra_damage"], 30)     # 5d6 radiant

    def test_multi_component_resistance_is_applied_per_type(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([{
                "name": "Frostling", "hp": 40, "ac": 10, "initiative_modifier": 1,
                "challenge_rating": 1, "damage_resistances": ["cold"], "saves": {"dex": 0}}])
        # Ice Storm: 2d8 bludgeoning (4+4) + 4d6 cold (6x4 = 24 -> 12, resistant), fail save
        with H.fixed_rolls([4, 4, 6, 6, 6, 6, 1]):
            r = H.ds.resolve_magic(spell_name="Ice Storm", actor="Father Aldric",
                                   slot_level=4, targets=[{"name": "Frostling"}])
        self.assertEqual(r["targets"][0]["damage"], 20)  # 8 bludgeoning + 12 cold

    def test_acid_arrow_miss_damage_scales_with_the_slot(self):
        # Acid Arrow at 4th level (two above 2nd): 4d4 hit, and the "later" miss damage
        # grows from 2d4 to 4d4 (SRD: the damage, both initial and later, increases by 1d4).
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([1, 2, 2, 2, 2]):  # natural 1 misses, then 4d4
            r = H.ds.resolve_magic(spell_name="Acid Arrow", actor="Father Aldric",
                                   slot_level=4, target_name="Goblin")
        self.assertEqual(r["miss_damage"], 8)

    def test_delayed_blast_fireball_accumulates_rounds(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([1] * 16):  # base 12d6 + 3 rounds = 15d6, then the DEX save
            r = H.ds.resolve_magic(spell_name="Delayed Blast Fireball", actor="Father Aldric",
                                   slot_level=7, delayed_rounds=3,
                                   targets=[{"name": "Goblin"}])
        self.assertEqual(r["targets"][0]["damage"], 15)


class OneShotBuffTest(H.EngineCase):
    """Branding Smite: a typed (+2d6 radiant) buff that is spent on the next hit."""

    player_factory = staticmethod(warpriest)

    def test_branding_smite_lands_once_then_is_spent(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        H.ds.resolve_magic(spell_name="Branding Smite", actor="Father Aldric",
                           slot_level=2, target_name="Father Aldric")
        self.assertIn("Branding Smite", H.raw_dbv("_active_buff_data") or {})
        with H.fixed_rolls([10, 4, 3, 3]):  # attack, weapon 1d6, radiant 2d6
            r = H.ds.resolve_attack(actor="Father Aldric", attack_modifier=7, damage_dice="1d6",
                                    damage_type="bludgeoning", target_ac=5, target_name="Goblin")
        self.assertIn(r["outcome"], ("Success", "Critical Success"))
        self.assertEqual(r["damage_dice_bonus"], 6)
        self.assertNotIn("Branding Smite", H.raw_dbv("_active_buff_data") or {})

    def test_branding_smite_upcast_grows_the_buff(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        H.ds.resolve_magic(spell_name="Branding Smite", actor="Father Aldric",
                           slot_level=4, target_name="Father Aldric")  # +2 levels -> +2d6
        with H.fixed_rolls([10, 4, 1, 1, 1, 1]):  # weapon 1d6, then 4d6 radiant of ones
            r = H.ds.resolve_attack(actor="Father Aldric", attack_modifier=7, damage_dice="1d6",
                                    damage_type="bludgeoning", target_ac=5, target_name="Goblin")
        self.assertEqual(r["damage_dice_bonus"], 4)  # +2d6 base + 2d6 upcast, all ones


class EyebiteVariantTest(H.EngineCase):
    """Eyebite's effect is chosen per cast; only Sickened carries a repeating save."""

    player_factory = staticmethod(warpriest)  # has a 6th-level slot

    def _cast(self, effect):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([1]):  # Goblin's WIS save: 1 - 1 = 0 < 15
            return H.ds.resolve_magic(spell_name="Eyebite", actor="Father Aldric",
                                      slot_level=6, eyebite_effect=effect,
                                      targets=[{"name": "Goblin"}])

    def test_sickened_registers_a_repeat_save(self):
        r = self._cast("sickened")
        self.assertEqual(r["condition"], "Sickened")
        self.assertEqual(r["recurring_registered"], ["Goblin"])

    def test_asleep_does_not_repeat(self):
        r = self._cast("asleep")
        self.assertEqual(r["condition"], "Unconscious")
        self.assertNotIn("recurring_registered", r)

    def test_damage_wakes_an_asleep_target_and_reports_it(self):
        self._cast("asleep")
        entry = H.ds._COMBAT_REGISTRY["Goblin"]
        self.assertIn("unconscious", entry["conditions"])
        self.assertEqual(entry["conditions_end_on_damage"], ["unconscious"])
        with H.fixed_rolls([10, 4]):  # 10 + 7 vs AC 15 hits; 1d6 bludgeoning
            hit = H.ds.resolve_attack(actor="Father Aldric", attack_modifier=7,
                                      damage_dice="1d6", damage_type="bludgeoning",
                                      target_ac=5, target_name="Goblin")
        self.assertEqual([w["name"] for w in hit["conditions_woke"]], ["Goblin"])
        self.assertNotIn("unconscious", H.ds._COMBAT_REGISTRY["Goblin"]["conditions"])
        self.assertIn("wakes", hit["narrative_format"])

    def test_update_combatant_damage_also_wakes(self):
        self._cast("asleep")
        with H.fixed_rolls([1]):
            r = H.ds.update_combatant(name="Goblin", hp_delta=-2)
        self.assertEqual([w["name"] for w in r["conditions_woke"]], ["Goblin"])
        self.assertNotIn("unconscious", H.ds._COMBAT_REGISTRY["Goblin"]["conditions"])

    def test_omitted_variant_keeps_the_generic_condition(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([1]):
            r = H.ds.resolve_magic(spell_name="Eyebite", actor="Father Aldric", slot_level=6,
                                   targets=[{"name": "Goblin"}])
        self.assertEqual(r["condition"], "Asleep, Panicked, or Sickened")
        self.assertNotIn("recurring_registered", r)


class SleepWakeOnDamageTest(H.EngineCase):
    """Sleep's Unconscious ends on damage (SRD), driven by the spell's `ends_on_damage` flag."""

    player_factory = staticmethod(H.wizard_l3)  # Senna knows Sleep and has 1st-level slots

    def test_sleep_victim_wakes_on_damage(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        with H.fixed_rolls([2, 2, 2, 2, 2]):  # 5d8 = 10 >= Goblin's 7 HP
            cast = H.ds.resolve_magic(spell_name="Sleep", actor="Senna",
                                      targets=[{"name": "Goblin"}])
        self.assertEqual(cast["condition"], "Unconscious")
        self.assertEqual(cast["conditions_applied"], ["Goblin"])
        self.assertEqual(H.ds._COMBAT_REGISTRY["Goblin"]["conditions_end_on_damage"],
                         ["unconscious"])
        with H.fixed_rolls([10, 4]):  # hit vs AC 5; 1d6 bludgeoning
            hit = H.ds.resolve_attack(actor="Senna", attack_modifier=5, damage_dice="1d6",
                                      damage_type="bludgeoning", target_ac=5,
                                      target_name="Goblin")
        self.assertEqual([w["name"] for w in hit["conditions_woke"]], ["Goblin"])
        self.assertNotIn("unconscious", H.ds._COMBAT_REGISTRY["Goblin"]["conditions"])
        self.assertIn("wakes", hit["narrative_format"])

    def test_gm_declared_unconscious_does_not_wake_on_damage(self):
        """A condition no tool marked `ends_on_damage` is the GM's to remove."""
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([GOBLIN])
        H.ds.update_combatant(name="Goblin", conditions_add=["Unconscious"])
        with H.fixed_rolls([10, 4]):
            hit = H.ds.resolve_attack(actor="Senna", attack_modifier=5, damage_dice="1d6",
                                      damage_type="bludgeoning", target_ac=5,
                                      target_name="Goblin")
        self.assertNotIn("conditions_woke", hit)
        self.assertIn("unconscious", H.ds._COMBAT_REGISTRY["Goblin"]["conditions"])

    def test_unregistered_player_sleep_wakes_on_damage(self):
        """Sleep's AoE can catch the player with no registry entry to hang the mark on."""
        with H.fixed_rolls([8, 8, 8, 8, 8]):  # 5d8 = 40 >= Senna's 13 HP
            cast = H.ds.resolve_magic(spell_name="Sleep", actor="Senna",
                                      targets=[{"name": "Senna", "current_hp": 13}])
        self.assertEqual(cast["conditions_applied"], ["Senna"])
        self.assertIn("unconscious", H.raw_dbv("conditions") or [])
        self.assertEqual(H.ds._SHEET_WAKE_CONDITIONS.get("Senna"), {"unconscious"})
        with H.fixed_rolls([15, 4]):  # NPC hit on the player: 15 + 5 vs AC 13
            hit = H.ds.resolve_attack(actor="Goblin", attack_modifier=5, damage_dice="1d6",
                                      damage_type="slashing", target_ac=13,
                                      target_name="Senna", target_current_hp=13,
                                      is_npc_attack=True)
        self.assertEqual([w["name"] for w in hit["conditions_woke"]], ["Senna"])
        self.assertNotIn("unconscious", H.raw_dbv("conditions") or [])
        self.assertNotIn("Senna", H.ds._SHEET_WAKE_CONDITIONS)


class MaintainSpellTest(H.EngineCase):
    """Eyebite re-targeting: no slot, and a creature that already saved is refused."""

    player_factory = staticmethod(warpriest)  # has a 6th-level slot

    def _register(self):
        with H.fixed_rolls([15, 6, 6]):
            H.ds.register_combatants([GOBLIN, dict(GOBLIN, name="Orc")])

    def test_maintain_retargets_without_a_slot_and_refuses_a_saved_target(self):
        self._register()
        with H.fixed_rolls([1]):  # Goblin fails its WIS save
            first = H.ds.resolve_magic(spell_name="Eyebite", actor="Father Aldric",
                                       slot_level=6, eyebite_effect="sickened",
                                       spell_save_dc=15, targets=[{"name": "Goblin"}])
        self.assertEqual(first["slot_level_used"], 6)
        with H.fixed_rolls([20]):  # Orc saves against the same casting
            second = H.ds.resolve_magic(spell_name="Eyebite", actor="Father Aldric",
                                        maintain_spell=True, eyebite_effect="sickened",
                                        spell_save_dc=15, targets=[{"name": "Orc"}])
        self.assertEqual(second["slot_consumed"], "maintain")
        self.assertNotIn("slot_level_used", second)
        third = H.ds.resolve_magic(spell_name="Eyebite", actor="Father Aldric",
                                   maintain_spell=True, eyebite_effect="sickened",
                                   spell_save_dc=15, targets=[{"name": "Orc"}])
        self.assertEqual(third["error"], "already_saved_target")
        self.assertEqual(third["targets"], ["Orc"])

    def test_maintain_without_concentration_is_refused(self):
        self._register()
        r = H.ds.resolve_magic(spell_name="Eyebite", actor="Father Aldric",
                               maintain_spell=True, target_name="Goblin")
        self.assertEqual(r["error"], "not_concentrating")


class DamagingConditionTest(H.EngineCase):
    player_factory = staticmethod(H.wizard_l3)

    def test_failed_save_applies_condition_even_when_the_spell_deals_damage(self):
        # An NPC casts Sunbeam (6d8 radiant + Blinded on a failed CON save) at Senna.
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([MAGE])
        with H.fixed_rolls([4, 4, 4, 4, 4, 4, 5]):  # 6d8 damage, then save d20=5 -> fail
            r = H.ds.resolve_magic(spell_name="Sunbeam", actor="Evil Mage", is_npc_attack=True,
                                   target_name="Senna")
        self.assertTrue(r["success"])
        self.assertEqual(r["condition"], "Blinded")
        self.assertEqual(r.get("conditions_applied"), ["Senna"])
        self.assertEqual(r["condition_targets"], ["Senna"])

    def test_successful_save_does_not_apply_the_condition(self):
        with H.fixed_rolls([15, 6]):
            H.ds.register_combatants([MAGE])
        with H.fixed_rolls([4, 4, 4, 4, 4, 4, 20]):  # 6d8 damage, then save d20=20 -> success
            r = H.ds.resolve_magic(spell_name="Sunbeam", actor="Evil Mage", is_npc_attack=True,
                                   target_name="Senna")
        self.assertTrue(r["save_success"])
        self.assertNotIn("conditions_applied", r)


if __name__ == "__main__":
    unittest.main()
