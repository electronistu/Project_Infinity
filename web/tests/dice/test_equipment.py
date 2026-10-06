"""SRD 5.1 equipped items: armour catalog, hands, derived armour class, and the equip tool.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_equipment
"""

import unittest

import equipment
from web.tests.dice import harness as H


# STR 16, DEX 14 (+2), CON 14 (+2), WIS 12 (+1)
STATS = {"str": 16, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8}


def get_of(payload):
    """A `get(key, default)` reader over a plain dict, as equipment_state wants."""
    return lambda key, default=None: payload.get(key, default)


def ac(armor=None, hands=None, features=(), cls="Fighter", inventory=None, stats=None):
    return equipment.armor_class(stats or STATS, cls, {"armor": armor, "hands": hands or [None, None]},
                                  features, inventory)


class ArmorCatalogTest(unittest.TestCase):
    def test_catalog_matches_the_project_spellings(self):
        self.assertEqual(equipment.ARMOR_DATA["Leather Tunic"],
                         {"ac": 11, "dex_cap": None, "type": "light"})
        self.assertEqual(equipment.ARMOR_DATA["Plate"]["ac"], 18)
        self.assertIn("Wooden Shield", equipment.SHIELD_NAMES)
        self.assertTrue(equipment.is_shield("Wooden Shield"))
        self.assertTrue(equipment.is_armor("Padded"))
        self.assertFalse(equipment.is_armor("Shield"))
        self.assertTrue(equipment.is_weapon("Longsword"))
        self.assertIsNone(equipment.armor_entry("Rope"))

    def test_declared_base_resolves_homebrew_items(self):
        self.assertTrue(equipment.is_weapon("Voidfang", "Dagger"))
        self.assertTrue(equipment.is_armor("Voidmail", "Chain Mail"))
        self.assertEqual(equipment.armor_entry("Voidmail", "Chain Mail")["ac"], 16)


class ArmorClassTest(unittest.TestCase):
    def test_worn_armour_and_dexterity_cap(self):
        self.assertEqual(ac("Leather Tunic"), 13)          # 11 + 2 DEX
        self.assertEqual(ac("Chain Mail"), 16)             # 16 + 0 (heavy)
        self.assertEqual(ac("Chain Shirt"), 15)            # 13 + min(2, DEX 2)
        high_dex = dict(STATS, dex=20)                      # +5 DEX, still capped at 2
        self.assertEqual(equipment.armor_class(high_dex, "Fighter",
                                                {"armor": "Chain Shirt", "hands": [None, None]}), 15)

    def test_shield_adds_its_bonus_in_a_hand(self):
        self.assertEqual(ac("Chain Mail", ["Longsword", "Shield"]), 18)
        self.assertEqual(ac("Chain Mail", ["Longsword", None]), 16)
        self.assertEqual(ac(None, ["Shield", None]), 14)   # unarmoured 12 + 2
        self.assertEqual(ac(None, ["Shield", None], cls="Monk"), 13)  # monks gain nothing

    def test_unarmoured_defense_and_armour_suppresses_it(self):
        self.assertEqual(ac(), 12)                          # 10 + 2 DEX
        self.assertEqual(ac(cls="Barbarian"), 14)           # 10 + 2 + 2 CON
        self.assertEqual(ac(cls="Monk"), 13)                # 10 + 2 + 1 WIS
        # A spare suit never applies, and worn armour replaces the formula.
        self.assertEqual(ac("Plate Armor", cls="Barbarian"), 18)

    def test_defense_fighting_style_needs_armour(self):
        defense = ["Fighting Style: Defense"]
        self.assertEqual(ac("Chain Mail", features=defense), 17)
        self.assertEqual(ac(None, features=defense), 12)

    def test_breakdown_reads_without_a_double_sign(self):
        state = equipment.equipment_state(get_of({
            "stats": STATS, "character_class": "Fighter", "features": [],
            "inventory": [], "equipped": {"armor": None, "hands": [None, None]},
        }))
        self.assertEqual(state["ac_breakdown"], "12 = unarmoured 10 + DEX +2")
        self.assertNotIn("++", state["ac_breakdown"])
        weak = equipment.equipment_state(get_of({
            "stats": dict(STATS, dex=8), "character_class": "Fighter", "features": [],
            "inventory": [], "equipped": {"armor": None, "hands": [None, None]},
        }))
        self.assertEqual(weak["ac_breakdown"], "9 = unarmoured 10 + DEX -1")

    def test_magic_bonuses_are_explicit_not_parsed_from_the_name(self):
        inventory = [{"name": "Voidmail", "description": "a humming cuirass",
                      "base": "Chain Mail", "ac_bonus": 1}]
        self.assertEqual(ac("Voidmail", inventory=inventory), 17)   # 16 + 1
        inventory = [{"name": "Warded Shield", "base": "Shield", "ac_bonus": 1}]
        self.assertEqual(ac(None, ["Longsword", "Warded Shield"], inventory=inventory), 15)


class HandsTest(unittest.TestCase):
    def test_two_handed_weapon_takes_both_hands_to_attack_only(self):
        inventory = ["Greatsword"]
        self.assertEqual(equipment.hands_used(["Greatsword", None], inventory), 2)
        self.assertEqual(equipment.hands_used(["Greatsword", None], inventory, casting=True), 1)
        self.assertEqual(equipment.hands_used(["Longsword", "Shield"], inventory), 2)

    def test_equipment_state_reports_hands_and_breakdown(self):
        state = equipment.equipment_state(get_of({
            "stats": STATS, "character_class": "Fighter", "features": [],
            "inventory": ["Greatsword", "Chain Mail"],
            "equipped": {"armor": "Chain Mail", "hands": ["Greatsword", None]},
        }))
        self.assertTrue(state["derived_from_equipped"])
        self.assertEqual(state["base_ac"], 16)
        self.assertEqual(state["hands_free"], 0)            # two-handed weapon
        self.assertEqual(state["hands_free_casting"], 1)    # held in one hand while casting
        self.assertEqual(state["hands"][0]["slot"], "main_hand")
        self.assertTrue(state["hands"][0]["two_handed"])
        self.assertTrue(state["ac_breakdown"].startswith("16"))
        self.assertEqual(state["warnings"], [])

    def test_two_handed_conflict_warns(self):
        state = equipment.equipment_state(get_of({
            "stats": STATS, "character_class": "Fighter", "features": [],
            "inventory": ["Greatsword", "Shield"],
            "equipped": {"armor": None, "hands": ["Greatsword", "Shield"]},
        }))
        self.assertIn("two_handed_conflict", [w["code"] for w in state["warnings"]])

    def test_unknown_armour_archetype_warns(self):
        state = equipment.equipment_state(get_of({
            "stats": STATS, "character_class": "Fighter", "features": [],
            "inventory": ["Mystery Cuirass"],
            "equipped": {"armor": "Mystery Cuirass", "hands": [None, None]},
        }))
        self.assertIn("unknown_archetype", [w["code"] for w in state["warnings"]])

    def test_payload_without_equipped_claims_nothing(self):
        state = equipment.equipment_state(get_of({"stats": STATS, "inventory": ["Chain Mail"]}))
        self.assertFalse(state["derived_from_equipped"])
        self.assertIsNone(state["armor"])
        self.assertEqual(state["hands_free"], 2)


class StartingEquipmentTest(unittest.TestCase):
    def test_best_armour_and_hands_are_chosen(self):
        equipped = equipment.pick_starting_equipped(
            ["Longsword", "Chain Mail", "Shield", "Explorer's Pack"])
        self.assertEqual(equipped, {"armor": "Chain Mail", "hands": ["Longsword", "Shield"],
                                    "worn": []})

    def test_two_handed_weapon_leaves_the_off_hand_empty(self):
        equipped = equipment.pick_starting_equipped(["Greatsword", "Chain Mail", "Shield"])
        self.assertEqual(equipped["hands"], ["Greatsword", None])

    def test_a_shield_alone_occupies_a_hand(self):
        equipped = equipment.pick_starting_equipped(["Shield"])
        self.assertEqual(equipped, {"armor": None, "hands": ["Shield", None], "worn": []})

    def test_clothing_is_worn_as_the_base_layer(self):
        # The Forge files clothing under generic `gear`, so it is worn by name.
        equipped = equipment.pick_starting_equipped(["Spellbook", "Crowbar", "Dark Common Clothes"])
        self.assertEqual(equipped["worn"], ["Dark Common Clothes"])
        self.assertEqual(equipped["hands"], [None, None])
        self.assertIsNone(equipped["armor"])
        self.assertTrue(equipment.is_clothing("Common Clothes"))
        self.assertTrue(equipment.is_clothing("Vestments"))
        self.assertFalse(equipment.is_clothing("Explorer's Pack"))

    def test_nothing_to_equip(self):
        self.assertEqual(equipment.pick_starting_equipped(["Rope", "Torch"]),
                         {"armor": None, "hands": [None, None], "worn": []})


def fighters_equipped(**overrides):
    payload = {
        "character_class": "Fighter",
        "stats": STATS,
        "features": [],
        "inventory": ["Longsword", "Chain Mail", "Shield", "Rope"],
        "equipped": {"armor": "Chain Mail", "hands": ["Longsword", "Shield"]},
    }
    payload.update(overrides)
    return H.make_player(**payload)


class EquipmentEngineTest(unittest.TestCase):
    def test_init_recomputes_armour_class_from_the_equipped_set(self):
        # The file lied (99); the engine derives 16 + shield 2 from what is worn.
        with H.load(fighters_equipped(armor_class=99)):
            self.assertEqual(H.dbv("armor_class"), 18)

    def test_spare_armour_does_not_raise_the_armour_class(self):
        payload = fighters_equipped(armor_class=99)
        payload["inventory"] = ["Longsword", "Chain Mail", "Shield", "Plate Armor"]
        with H.load(payload):
            self.assertEqual(H.dbv("armor_class"), 18)  # wearing Chain Mail, not the spare Plate

    def test_dump_carries_the_equipment_block(self):
        with H.load(fighters_equipped()):
            block = H.dbv("_equipment")
        self.assertTrue(block["derived_from_equipped"])
        self.assertEqual(block["hands_free"], 0)
        self.assertEqual(block["base_ac"], 18)
        self.assertEqual(block["armor"], "Chain Mail")

    def test_payload_without_equipped_keeps_its_armour_class(self):
        with H.load(H.make_player(armor_class=15)):
            self.assertEqual(H.dbv("armor_class"), 15)
            self.assertFalse(H.dbv("_equipment")["derived_from_equipped"])


def fighter(**overrides):
    payload = {
        "character_class": "Fighter",
        "stats": STATS,
        "features": [],
        "armor_proficiencies": ["Light armor", "Medium armor", "Heavy armor", "Shields"],
        "inventory": ["Longsword", "Chain Mail", "Shield", "Greatsword", "Rope"],
        "equipped": {"armor": None, "hands": [None, None]},
    }
    payload.update(overrides)
    return H.make_player(**payload)


class EquipToolTest(unittest.TestCase):
    def test_armour_is_worn_and_armour_class_recomputed(self):
        with H.load(fighter()):
            r = H.ds.equip_item("Chain Mail")
            self.assertTrue(r["success"])
            self.assertEqual(r["armor_class_before"], 12)
            self.assertEqual(r["armor_class_after"], 16)
            self.assertEqual(H.dbv("equipped")["armor"], "Chain Mail")
            self.assertEqual(H.dbv("armor_class"), 16)
            self.assertEqual(r["equipment"]["armor"], "Chain Mail")

    def test_shield_prefers_the_off_hand_and_weapons_the_main_hand(self):
        with H.load(fighter()):
            H.ds.equip_item("Shield")
            self.assertEqual(H.dbv("equipped")["hands"], [None, "Shield"])
            H.ds.equip_item("Longsword")
            self.assertEqual(H.dbv("equipped")["hands"], ["Longsword", "Shield"])
            self.assertEqual(H.dbv("armor_class"), 14)  # unarmoured 12 + shield 2

    def test_two_handed_weapon_needs_both_hands(self):
        with H.load(fighter()):
            H.ds.equip_item("Shield")
            r = H.ds.equip_item("Greatsword")
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "two_handed_needs_both_hands")
            r = H.ds.equip_item("Greatsword", replace=True)
            self.assertTrue(r["success"])
            self.assertEqual(H.dbv("equipped")["hands"], ["Greatsword", None])
            self.assertEqual(H.dbv("_equipment")["hands_free"], 0)

    def test_armour_slot_is_single_and_replace_stows(self):
        with H.load(fighter(inventory=["Chain Mail", "Plate Armor"])):
            H.ds.equip_item("Chain Mail")
            r = H.ds.equip_item("Plate Armor")
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "armor_slot_occupied")
            self.assertTrue(H.ds.equip_item("Plate Armor", replace=True)["success"])
            self.assertEqual(H.dbv("equipped")["armor"], "Plate Armor")

    def test_unequip_keeps_the_item_in_the_inventory(self):
        with H.load(fighter()):
            H.ds.equip_item("Longsword")
            r = H.ds.equip_item("Longsword", action="unequip")
            self.assertTrue(r["success"])
            self.assertEqual(H.dbv("equipped")["hands"], [None, None])
            names = [e if isinstance(e, str) else e.get("name") for e in H.dbv("inventory")]
            self.assertIn("Longsword", names)

    def test_clothes_are_routed_to_the_worn_container(self):
        with H.load(fighter(inventory=["Dark Common Clothes", "Chain Mail"])):
            r = H.ds.equip_item("Dark Common Clothes")
            self.assertTrue(r["success"])
            self.assertEqual(H.dbv("equipped")["worn"], ["Dark Common Clothes"])
            self.assertEqual(H.dbv("equipped")["hands"], [None, None])  # not held

    def test_removing_an_item_from_the_inventory_unequips_it(self):
        with H.load(fighter()):
            H.ds.equip_item("Chain Mail")
            r = H.ds.update_player_list(key="inventory", item="Chain Mail", action="remove")
            self.assertIsNone(H.dbv("equipped")["armor"])
            self.assertEqual(H.dbv("armor_class"), 12)  # back to unarmoured
            self.assertEqual(r["unequipped"], "Chain Mail")
            self.assertIn("equip_item", r["note"])

    def test_item_must_be_carried_first(self):
        with H.load(fighter()):
            r = H.ds.equip_item("Excalibur")
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "not_in_inventory")

    def test_declared_weapon_stats_are_stored(self):
        with H.load(fighter()):
            r = H.ds.update_player_list(key="inventory", item="Voidfang: a humming black dagger",
                                        action="add", base="Dagger", damage_dice="1d4",
                                        damage_type="piercing", attack_bonus=1, damage_bonus=1)
            self.assertTrue(r["success"])
            entry = H.dbv("inventory")[-1]
            self.assertEqual(entry["base"], "Dagger")
            self.assertEqual(entry["damage_dice"], "1d4")
            self.assertEqual(entry["attack_bonus"], 1)
            # The declared base supplies the weight the catalogs do not know.
            self.assertEqual(r["equipment"]["hands_free"], 2)
            self.assertEqual(H.dbv("_carrying")["unweighed"], [])

    def test_unknown_base_without_stats_is_rejected(self):
        with H.load(fighter()):
            r = H.ds.update_player_list(key="inventory", item="Warp Blade: odd", action="add",
                                        base="Bananasword")
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "unknown_base_no_stats")
            self.assertIn("update_player_list", r["gm_instruction"])

    def test_unknown_base_with_explicit_stats_is_accepted(self):
        with H.load(fighter()):
            r = H.ds.update_player_list(key="inventory", item="Warp Blade: odd", action="add",
                                        base="Bananasword", damage_dice="1d6",
                                        damage_type="slashing")
            self.assertTrue(r["success"])

    def test_homebrew_worn_item_with_unknown_base_is_accepted(self):
        # base names the magic archetype; kind + ac_bonus is enough for a worn item.
        with H.load(fighter()):
            r = H.ds.update_player_list(key="inventory", item="Ring of Warding: cold silver",
                                        action="add", base="Ring of Protection", kind="ring",
                                        ac_bonus=1, attunement=True)
            self.assertTrue(r["success"])

    def test_weapon_stats_missing_is_rejected(self):
        with H.load(fighter()):
            r = H.ds.update_player_list(key="inventory", item="Warp Spear", action="add",
                                        properties=["Thrown"])
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "weapon_stats_missing")
            self.assertIn("damage_dice", r["gm_instruction"])

    def test_worn_bonus_without_kind_is_rejected(self):
        with H.load(fighter()):
            r = H.ds.update_player_list(key="inventory", item="Band of Vigour", action="add",
                                        ac_bonus=1, attunement=True)
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "worn_kind_missing")
            self.assertIn("kind='ring'", r["gm_instruction"])


def swordsman(**overrides):
    payload = fighter(inventory=["Longsword", "Dagger", "Shortbow", "Chain Mail", "Shield"],
                      equipped={"armor": "Chain Mail", "hands": ["Longsword", "Shield"]})
    payload.update(overrides)
    return payload


class WeaponAttackTest(unittest.TestCase):
    def test_attack_and_damage_are_derived_from_the_equipped_weapon(self):
        with H.load(swordsman()):
            r = H.ds.resolve_attack(actor="Tester", weapon="Longsword", target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertTrue(r["success"])
            self.assertEqual(r["attack_modifier"], 5)   # STR 16 (+3) + proficiency 2
            self.assertEqual(r["used_item"], "Longsword")
            self.assertEqual(r["attack_ability"], "strength")
            self.assertEqual(r["damage_type"], "slashing")
            self.assertEqual(r["damage_modifier"], 3)

    def test_finesse_uses_the_better_of_str_and_dex(self):
        rogue = swordsman(stats={"str": 10, "dex": 17, "con": 12, "int": 10, "wis": 10, "cha": 10},
                          equipped={"armor": "Leather Tunic", "hands": ["Dagger", None]},
                          weapon_proficiencies=["Simple weapons", "Martial weapons"])
        with H.load(rogue):
            r = H.ds.resolve_attack(actor="Tester", weapon="Dagger", target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertEqual(r["attack_ability"], "dexterity")
            self.assertEqual(r["attack_modifier"], 5)   # DEX 17 (+3) + proficiency 2

    def test_ranged_uses_dexterity(self):
        with H.load(swordsman(equipped={"armor": "Chain Mail", "hands": ["Shortbow", None]})):
            r = H.ds.resolve_attack(actor="Tester", weapon="Shortbow", target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertEqual(r["attack_ability"], "dexterity")
            self.assertEqual(r["attack_modifier"], 4)   # DEX 14 (+2) + proficiency 2

    def test_declared_magic_bonuses_apply(self):
        payload = swordsman(equipped={"armor": "Chain Mail", "hands": ["Voidfang", None]})
        payload["inventory"] = [{"name": "Voidfang", "description": "a humming black dagger",
                                 "base": "Dagger", "damage_dice": "1d4", "damage_type": "piercing",
                                 "attack_bonus": 1, "damage_bonus": 1}, "Chain Mail"]
        with H.load(payload):
            r = H.ds.resolve_attack(actor="Tester", weapon="Voidfang", target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertEqual(r["attack_modifier"], 6)   # STR +3, proficiency +2, magic +1
            self.assertEqual(r["damage_modifier"], 4)   # STR +3, magic +1

    def test_worn_ring_bonuses_apply_to_attacks(self):
        payload = swordsman(equipped={"armor": "Chain Mail", "hands": ["Longsword", None],
                                      "worn": ["Ring of Accuracy"]})
        payload["inventory"] = [
            "Longsword", "Dagger", "Shortbow", "Chain Mail", "Shield",
            {"name": "Ring of Accuracy", "kind": "ring", "attack_bonus": 1, "damage_bonus": 1,
             "attunement": True},
        ]
        with H.load(payload):
            # Withheld until attuned.
            r = H.ds.resolve_attack(actor="Tester", weapon="Longsword", target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertEqual(r["attack_modifier"], 5)   # STR +3, proficiency +2
            self.assertEqual(r["damage_modifier"], 3)
            H.ds.attune_item("Ring of Accuracy")
            r = H.ds.resolve_attack(actor="Tester", weapon="Longsword", target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertEqual(r["attack_modifier"], 6)   # +1 from the worn ring
            self.assertEqual(r["damage_modifier"], 4)
            self.assertIn("Ring of Accuracy", r["item_bonus"]["sources"])

    def test_attacking_with_an_unequipped_weapon_is_refused(self):
        with H.load(swordsman()):
            with H.fixed_rolls([20]) as calls:
                r = H.ds.resolve_attack(actor="Tester", weapon="Dagger", target_ac=5,
                                        target_name="Goblin", target_current_hp=20)
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "item_not_equipped")
            self.assertTrue(r["turn_lost"])
            self.assertIn("Longsword", r["reason"])        # says what is actually held
            self.assertNotIn("attack_roll", r)
            self.assertEqual(calls, [])                     # no dice were rolled

    def test_attacking_with_an_uncarried_weapon_is_refused(self):
        with H.load(swordsman()):
            r = H.ds.resolve_attack(actor="Tester", weapon="Excalibur", target_ac=5)
            self.assertEqual(r["error"], "item_not_carried")
            self.assertTrue(r["turn_lost"])

    def test_a_weapon_with_no_stats_is_refused(self):
        payload = swordsman(inventory=["Rusty Spork", "Chain Mail"],
                            equipped={"armor": "Chain Mail", "hands": ["Rusty Spork", None]})
        with H.load(payload):
            r = H.ds.resolve_attack(actor="Tester", weapon="Rusty Spork", target_ac=5)
            self.assertEqual(r["error"], "weapon_stats_unknown")
            self.assertTrue(r["turn_lost"])

    def test_npc_attacks_are_untouched(self):
        with H.load(swordsman()):
            r = H.ds.resolve_attack(actor="Goblin", attack_modifier=4, target_ac=13,
                                    damage_dice="1d6", damage_modifier=2,
                                    target_name="Tester", is_npc_attack=True)
            self.assertTrue(r["success"])
            self.assertNotIn("used_item", r)


CLERIC = {
    "ability": "wisdom", "dc": 15, "attack_modifier": 7,
    "cantrips": ["Guidance"],
    "spells_prepared": [{"name": "Cure Wounds"}, {"name": "Healing Word"}, {"name": "Bless"}],
    "slots": {"1": 4},
}


def busy_cleric(**overrides):
    payload = H.make_player(
        character_class="Cleric",
        stats={"str": 12, "dex": 10, "con": 14, "int": 10, "wis": 18, "cha": 12},
        inventory=["Mace", "Scale Mail", "Shield", "Component Pouch"],
        equipped={"armor": "Scale Mail", "hands": ["Mace", "Shield"]},
        armor_proficiencies=["Light armor", "Medium armor", "Shields"],
        spellcasting=dict(CLERIC))
    payload.update(overrides)
    return payload


class SpellHandsTest(unittest.TestCase):
    def test_somatic_spell_with_both_hands_busy_is_refused(self):
        with H.load(busy_cleric()):
            r = H.ds.resolve_magic(spell_name="Cure Wounds", actor="Tester",
                                   target_name="Tester", healing=True)
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "both_hands_occupied")
            self.assertTrue(r["turn_lost"])
            self.assertEqual(r["components"], "VS")
            self.assertIn("No spell slot", r["note"])
            self.assertEqual(H.dbv("spellcasting")["slots"], {"1": 4})  # nothing spent

    def test_verbal_only_spell_needs_no_hand(self):
        with H.load(busy_cleric()):
            r = H.ds.resolve_magic(spell_name="Healing Word", actor="Tester",
                                   target_name="Tester", healing=True)
            self.assertTrue(r["success"])
            self.assertEqual(H.dbv("spellcasting")["slots"], {"1": 3})

    def test_a_free_hand_allows_the_spell(self):
        free = busy_cleric(equipped={"armor": "Scale Mail", "hands": ["Mace", None]})
        with H.load(free):
            self.assertTrue(H.ds.resolve_magic(spell_name="Cure Wounds", actor="Tester",
                                                target_name="Tester", healing=True)["success"])

    def test_material_only_spell_is_covered_by_a_held_focus(self):
        # A component pouch occupies the second hand; M with no S is satisfied by holding it.
        focus = busy_cleric(equipped={"armor": "Scale Mail", "hands": ["Mace", "Component Pouch"]})
        with H.load(focus):
            r = H.ds.resolve_magic(spell_name="Cure Wounds", actor="Tester", components="M",
                                   target_name="Tester", healing=True)
            self.assertTrue(r["success"])

    def test_components_override_lets_the_gm_declare_a_homebrew_spell(self):
        with H.load(busy_cleric()):
            r = H.ds.resolve_magic(spell_name="Word of the Void", actor="Tester", components="V",
                                   attack_type="automatic", damage_dice="1d6", target_name="Goblin",
                                   target_current_hp=10)
            self.assertTrue(r["success"])

    def test_no_equipped_payload_skips_the_check(self):
        with H.load(H.make_player(character_class="Wizard", armor_class=13,
                                   spellcasting={"ability": "intelligence", "dc": 13,
                                                 "attack_modifier": 5, "cantrips": ["Fire Bolt"],
                                                 "spells_known": [], "spells_prepared": [],
                                                 "slots": {"1": 2}})):
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Tester",
                                    target_name="Goblin", target_current_hp=10)
            self.assertTrue(r["success"])


if __name__ == "__main__":
    unittest.main()
