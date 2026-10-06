"""Phase 4 equipped-item rules: proficiency, Versatile, loading, two-weapon fighting,
attunement/pairs, and the don/doff gate.

Run from the repo root:
    venv\\Scripts\\python.exe -m unittest web.tests.dice.test_equipped_rules
"""

import unittest

import equipment
from web.tests.dice import harness as H


STATS = {"str": 16, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8}


def fighter(**overrides):
    payload = {
        "character_class": "Fighter",
        "stats": STATS,
        "features": [],
        "armor_proficiencies": ["Light armor", "Medium armor", "Heavy armor", "Shields"],
        "weapon_proficiencies": ["Simple weapons", "Martial weapons"],
        "inventory": ["Longsword", "Shortsword", "Dagger", "Hand Crossbow",
                      "Chain Mail", "Plate Armor", "Shield"],
        "equipped": {"armor": None, "hands": [None, None]},
    }
    payload.update(overrides)
    return H.make_player(**payload)


def in_combat():
    H.ds._COMBAT_REGISTRY = {"Goblin": {"is_player": False, "killed": False}}


class HeavyArmorDexterityTest(unittest.TestCase):
    """SRD: heavy armour adds no Dexterity — and does not penalise a negative one either."""

    def test_heavy_armour_ignores_negative_dexterity(self):
        weak_dex = dict(STATS, dex=8)   # -1
        self.assertEqual(equipment.armor_class(weak_dex, "Fighter",
                                                {"armor": "Plate Armor", "hands": [None, None]}), 18)

    def test_light_and_medium_still_apply_negative_dexterity(self):
        weak_dex = dict(STATS, dex=8)   # -1
        self.assertEqual(equipment.armor_class(weak_dex, "Fighter",
                                                {"armor": "Chain Shirt", "hands": [None, None]}), 12)
        self.assertEqual(equipment.armor_class(weak_dex, "Rogue",
                                                {"armor": "Leather Armor", "hands": [None, None]}), 10)


class ArmorProficiencyTest(unittest.TestCase):
    def test_non_proficient_armour_disadvantages_str_and_dex_checks_only(self):
        payload = fighter(armor_proficiencies=["Light armor"],  # no heavy armour
                          equipped={"armor": "Chain Mail", "hands": [None, None]})
        with H.load(payload):
            self.assertEqual(H.ds.perform_check(modifier=3, dc=10, check_name="Athletics",
                                                ability="str", actor="Tester")["disadvantage_sources"],
                             ["not proficient with Chain Mail"])
            self.assertEqual(H.ds.perform_check(modifier=3, dc=10, check_name="Dexterity save",
                                                ability="dex", actor="Tester")["disadvantage_sources"],
                             ["not proficient with Chain Mail"])
            self.assertIsNone(H.ds.perform_check(modifier=3, dc=10, check_name="Arcana",
                                                 ability="int", actor="Tester").get("disadvantage_sources"))

    def test_non_proficient_armour_disadvantages_attacks(self):
        payload = fighter(armor_proficiencies=["Light armor"],
                          equipped={"armor": "Chain Mail", "hands": ["Longsword", None]})
        with H.load(payload):
            r = H.ds.resolve_attack(actor="Tester", weapon="Longsword", target_ac=10,
                                    target_name="Goblin", target_current_hp=20)
            self.assertEqual(r["disadvantage_sources"], ["not proficient with Chain Mail"])

    def test_non_proficient_armour_blocks_spellcasting(self):
        payload = H.make_player(
            character_class="Wizard", armor_proficiencies=["Light armor"], armor_class=16,
            inventory=["Chain Mail"], equipped={"armor": "Chain Mail", "hands": [None, None]},
            spellcasting={"ability": "intelligence", "dc": 13, "attack_modifier": 5,
                          "cantrips": ["Fire Bolt"], "spells_known": [], "spells_prepared": [],
                          "slots": {"1": 2}})
        with H.load(payload):
            r = H.ds.resolve_magic(spell_name="Magic Missile", actor="Tester",
                                   target_name="Goblin", target_current_hp=10)
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "armor_not_proficient")
            self.assertTrue(r["turn_lost"])
            self.assertEqual(H.dbv("spellcasting")["slots"], {"1": 2})  # nothing spent


class VersatileTest(unittest.TestCase):
    def test_two_handed_grip_uses_the_larger_die(self):
        payload = fighter(equipped={"armor": "Chain Mail", "hands": ["Longsword", None]})
        with H.load(payload):
            r = H.ds.resolve_attack(actor="Tester", weapon="Longsword", target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertEqual(r["damage_dice"], "1d10")   # Versatile 1d10, other hand free

    def test_one_handed_grip_uses_the_base_die(self):
        payload = fighter(equipped={"armor": "Chain Mail", "hands": ["Longsword", "Shield"]})
        with H.load(payload):
            r = H.ds.resolve_attack(actor="Tester", weapon="Longsword", target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertEqual(r["damage_dice"], "1d8")


class LoadingTest(unittest.TestCase):
    def test_one_handed_loading_weapon_needs_a_free_hand(self):
        payload = fighter(equipped={"armor": "Chain Mail", "hands": ["Hand Crossbow", "Dagger"]})
        with H.load(payload):
            r = H.ds.resolve_attack(actor="Tester", weapon="Hand Crossbow", target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertEqual(r["error"], "cannot_reload")
            self.assertTrue(r["turn_lost"])

    def test_loading_weapon_fires_with_a_free_hand(self):
        payload = fighter(equipped={"armor": "Chain Mail", "hands": ["Hand Crossbow", None]})
        with H.load(payload):
            r = H.ds.resolve_attack(actor="Tester", weapon="Hand Crossbow", target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertTrue(r["success"])


class TwoWeaponTest(unittest.TestCase):
    def test_bonus_attack_takes_no_ability_modifier(self):
        payload = fighter(equipped={"armor": "Chain Mail", "hands": ["Shortsword", "Dagger"]})
        with H.load(payload):
            r = H.ds.resolve_attack(actor="Tester", weapon="Dagger", off_hand=True, target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertTrue(r["success"])
            self.assertEqual(r["damage_dice"], "1d4")
            self.assertEqual(r["damage_modifier"], 0)     # STR +3 is not added
            self.assertTrue(r["two_weapon"])
            self.assertTrue(r["bonus_action_consumed"])

    def test_requires_a_light_melee_weapon_in_each_hand(self):
        payload = fighter(equipped={"armor": "Chain Mail", "hands": ["Longsword", "Dagger"]})
        with H.load(payload):
            r = H.ds.resolve_attack(actor="Tester", weapon="Dagger", off_hand=True, target_ac=5,
                                    target_name="Goblin", target_current_hp=20)
            self.assertEqual(r["error"], "two_weapon_requires_light_melee")
            self.assertTrue(r["turn_lost"])

    def test_off_hand_needs_a_second_weapon(self):
        payload = fighter(equipped={"armor": "Chain Mail", "hands": ["Dagger", None]})
        with H.load(payload):
            r = H.ds.resolve_attack(actor="Tester", weapon="Dagger", off_hand=True, target_ac=5)
            self.assertEqual(r["error"], "off_hand_needs_another_weapon")


class GrappleTest(unittest.TestCase):
    def test_grapple_needs_a_free_hand(self):
        payload = fighter(equipped={"armor": "Chain Mail", "hands": ["Longsword", "Shield"]})
        with H.load(payload):
            r = H.ds.perform_check(modifier=5, dc=12, check_name="Grapple", ability="str",
                                   actor="Tester", grapple=True)
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "no_free_hand")
            self.assertTrue(r["turn_lost"])

    def test_grapple_with_a_free_hand_rolls(self):
        payload = fighter(equipped={"armor": "Chain Mail", "hands": ["Longsword", None]})
        with H.load(payload):
            with H.fixed_rolls([18]):
                r = H.ds.perform_check(modifier=5, dc=12, check_name="Grapple", ability="str",
                                       actor="Tester", grapple=True)
            self.assertIn("outcome", r)
            self.assertEqual(r["outcome"], "Success")

    def test_escaping_a_grapple_is_not_flagged(self):
        payload = fighter(equipped={"armor": "Chain Mail", "hands": ["Longsword", "Shield"]})
        with H.load(payload):
            r = H.ds.perform_check(modifier=5, dc=12, check_name="Escape a grapple",
                                   ability="str", actor="Tester")
            self.assertIn("outcome", r)


MAGIC = [
    {"name": "Voidmail", "base": "Chain Mail", "ac_bonus": 1, "attunement": True, "kind": "armor"},
    {"name": "Cloak of Warding", "kind": "cloak", "ac_bonus": 1, "attunement": True},
    {"name": "Ring of Shielding", "kind": "ring", "ac_bonus": 1, "attunement": True},
    {"name": "Amulet of Vigour", "kind": "amulet", "ac_bonus": 1, "attunement": True},
]


class AttunementTest(unittest.TestCase):
    def test_magic_bonus_applies_only_while_attuned(self):
        payload = fighter(inventory=[*MAGIC, "Chain Mail"],
                          equipped={"armor": "Voidmail", "hands": [None, None]})
        with H.load(payload):
            self.assertEqual(H.dbv("armor_class"), 16)          # 16 + 1 withheld
            self.assertIn("not_attuned", [w["code"] for w in H.dbv("_equipment")["warnings"]])
            H.ds.attune_item("Voidmail")
            self.assertEqual(H.dbv("armor_class"), 17)
            H.ds.attune_item("Voidmail", action="unattune")
            self.assertEqual(H.dbv("armor_class"), 16)

    def test_amulets_that_do_not_require_attunement_are_rejected(self):
        with H.load(fighter()):
            r = H.ds.attune_item("Chain Mail")
            self.assertEqual(r["error"], "attunement_not_required")

    def test_at_most_three_items(self):
        payload = fighter(inventory=[*MAGIC, "Chain Mail"])
        with H.load(payload):
            for name in ("Voidmail", "Cloak of Warding", "Ring of Shielding"):
                self.assertTrue(H.ds.attune_item(name)["success"])
            r = H.ds.attune_item("Amulet of Vigour")
            self.assertEqual(r["error"], "attunement_full")
            self.assertEqual(r["attunement_slots_free"], 0)

    def test_two_copies_of_the_same_item_cannot_both_be_attuned(self):
        # SRD 5.1: "a creature can't attune to more than one copy of an item".
        payload = fighter(inventory=[
            {"name": "Ring of Protection", "base": "Ring of Protection", "kind": "ring",
             "ac_bonus": 1, "attunement": True},
            {"name": "Ring of Protection (2)", "base": "Ring of Protection", "kind": "ring",
             "ac_bonus": 1, "attunement": True},
            "Chain Mail"])
        with H.load(payload):
            self.assertTrue(H.ds.attune_item("Ring of Protection")["success"])
            r = H.ds.attune_item("Ring of Protection (2)")
            self.assertEqual(r["error"], "attunement_duplicate_item")
            self.assertEqual(r["conflicting_item"], "Ring of Protection")

    def test_different_items_of_the_same_kind_can_both_be_attuned(self):
        # SRD's "one of each kind" is a wearing limit, not an attunement limit.
        payload = fighter(inventory=[
            {"name": "Cloak of Warding", "kind": "cloak", "ac_bonus": 1, "attunement": True},
            {"name": "Cloak of Elvenkind", "kind": "cloak", "ac_bonus": 1, "attunement": True},
            "Chain Mail"])
        with H.load(payload):
            self.assertTrue(H.ds.attune_item("Cloak of Warding")["success"])
            self.assertTrue(H.ds.attune_item("Cloak of Elvenkind")["success"])

    def test_class_prerequisite_is_enforced(self):
        payload = fighter(inventory=[
            {"name": "Holy Avenger", "base": "Longsword", "kind": "other", "attack_bonus": 3,
             "attunement": True, "attunement_by": "by a paladin"}, "Chain Mail"])
        with H.load(payload):
            r = H.ds.attune_item("Holy Avenger")
            self.assertEqual(r["error"], "attunement_prerequisite_unmet")


class WornSlotTest(unittest.TestCase):
    def test_only_one_pair_of_boots_can_be_worn(self):
        payload = fighter(inventory=[
            {"name": "Boots of Elvenkind", "kind": "boots", "attunement": True},
            {"name": "Boots of Speed", "kind": "boots", "attunement": True}])
        with H.load(payload):
            self.assertTrue(H.ds.equip_item("Boots of Elvenkind")["success"])
            r = H.ds.equip_item("Boots of Speed")
            self.assertEqual(r["error"], "worn_slot_taken")
            self.assertIn("replace=True", r["gm_instruction"])
            self.assertTrue(H.ds.equip_item("Boots of Speed", replace=True)["success"])
            self.assertEqual([w["name"] for w in H.dbv("_equipment")["worn"]],
                             ["Boots of Speed"])

    def test_gloves_and_gauntlets_share_the_hands_slot(self):
        payload = fighter(inventory=[
            {"name": "Gloves of Thievery", "kind": "gloves"},
            {"name": "Gauntlets of Ogre Power", "kind": "gauntlets"}])
        with H.load(payload):
            self.assertTrue(H.ds.equip_item("Gloves of Thievery")["success"])
            self.assertEqual(H.ds.equip_item("Gauntlets of Ogre Power")["error"],
                             "worn_slot_taken")

    def test_two_rings_can_both_be_worn(self):
        payload = fighter(inventory=[
            {"name": "Ring of Shielding", "kind": "ring", "ac_bonus": 1, "attunement": True},
            {"name": "Ring of Warmth", "kind": "ring", "attunement": True}])
        with H.load(payload):
            self.assertTrue(H.ds.equip_item("Ring of Shielding")["success"])
            self.assertTrue(H.ds.equip_item("Ring of Warmth")["success"])
            self.assertEqual(len(H.dbv("_equipment")["worn"]), 2)

    def test_amulet_kind_is_worn_and_its_bonus_applies(self):
        payload = fighter(inventory=[
            {"name": "Amulet of Vigour", "kind": "amulet", "ac_bonus": 1, "attunement": True}])
        with H.load(payload):
            self.assertTrue(H.ds.equip_item("Amulet of Vigour")["success"])
            self.assertEqual([w["name"] for w in H.dbv("_equipment")["worn"]],
                             ["Amulet of Vigour"])
            H.ds.attune_item("Amulet of Vigour")
            self.assertEqual(H.dbv("armor_class"), 13)  # unarmoured 12 + 1


PAIR = [
    {"name": "Gloves of Thievery (L)", "kind": "gloves", "base": "Gloves of Thievery",
     "pair": True, "ac_bonus": 1},
    {"name": "Gloves of Thievery (R)", "kind": "gloves", "base": "Gloves of Thievery",
     "pair": True, "ac_bonus": 1},
]


class PairedItemsTest(unittest.TestCase):
    def test_one_half_grants_nothing(self):
        payload = fighter(inventory=[*PAIR, "Chain Mail"],
                          equipped={"armor": "Chain Mail", "hands": [None, None],
                                    "worn": ["Gloves of Thievery (L)"]})
        with H.load(payload):
            self.assertEqual(H.dbv("armor_class"), 16)
            self.assertIn("pair_incomplete", [w["code"] for w in H.dbv("_equipment")["warnings"]])

    def test_both_halves_grant_the_bonus(self):
        payload = fighter(inventory=[*PAIR, "Chain Mail"],
                          equipped={"armor": "Chain Mail", "hands": [None, None],
                                    "worn": ["Gloves of Thievery (L)", "Gloves of Thievery (R)"]})
        with H.load(payload):
            self.assertEqual(H.dbv("armor_class"), 18)   # both halves: +1 each
            self.assertNotIn("pair_incomplete", [w["code"] for w in H.dbv("_equipment")["warnings"]])


class WonItemsTest(unittest.TestCase):
    def test_worn_items_land_in_the_worn_container(self):
        payload = fighter(inventory=[*MAGIC, "Chain Mail"])
        with H.load(payload):
            r = H.ds.equip_item("Cloak of Warding")
            self.assertTrue(r["success"])
            self.assertEqual([w["name"] for w in H.dbv("_equipment")["worn"]], ["Cloak of Warding"])
            self.assertTrue(H.ds.equip_item("Cloak of Warding"))
            H.ds.attune_item("Cloak of Warding")
            self.assertEqual(H.dbv("armor_class"), 13)  # unarmoured 12 + 1

    def test_worn_items_are_unequipped_when_removed(self):
        payload = fighter(inventory=[*MAGIC, "Chain Mail"],
                          equipped={"armor": None, "hands": [None, None],
                                    "worn": ["Cloak of Warding"]})
        with H.load(payload):
            H.ds.update_player_list(key="inventory", item="Cloak of Warding", action="remove")
            self.assertEqual(H.dbv("equipped")["worn"], [])


class DonDoffTest(unittest.TestCase):
    def test_armour_cannot_be_donned_in_combat(self):
        with H.load(fighter()):
            in_combat()
            r = H.ds.equip_item("Plate Armor")
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "cannot_change_armor_in_combat")
            self.assertTrue(r["turn_lost"])
            self.assertIn("10 minutes", r["reason"])

    def test_instant_allows_it(self):
        with H.load(fighter()):
            in_combat()
            r = H.ds.equip_item("Plate Armor", instant=True)
            self.assertTrue(r["success"])
            self.assertEqual(r["time_cost"], "10 minutes")

    def test_armour_can_be_donned_out_of_combat_and_reports_the_time(self):
        with H.load(fighter()):
            r = H.ds.equip_item("Plate Armor")
            self.assertTrue(r["success"])
            self.assertEqual(r["time_cost"], "10 minutes")

    def test_shields_only_cost_an_action(self):
        with H.load(fighter()):
            in_combat()
            r = H.ds.equip_item("Shield")
            self.assertTrue(r["success"])
            self.assertEqual(r["time_cost"], "1 action")

    def test_attuning_needs_a_short_rest(self):
        payload = fighter(inventory=[*MAGIC, "Chain Mail"])
        with H.load(payload):
            in_combat()
            r = H.ds.attune_item("Voidmail")
            self.assertEqual(r["error"], "requires_short_rest")
            self.assertTrue(H.ds.attune_item("Voidmail", instant=True)["success"])

    def test_a_dead_registry_is_not_combat(self):
        with H.load(fighter()):
            H.ds._COMBAT_REGISTRY = {"Goblin": {"is_player": False, "killed": True}}
            self.assertTrue(H.ds.equip_item("Plate Armor")["success"])


if __name__ == "__main__":
    unittest.main()
