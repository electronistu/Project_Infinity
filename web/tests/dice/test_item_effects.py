"""The general item `effects` model + engine-derived saving throws (SRD 5.1).

Covers: set/flat/capped ability scores, save/check bonuses, proficiency-bonus grants,
conditional AC (`ac_set`, `no_armor_or_shield`), CON -> max HP (retroactive), INT -> spell
DC/attack recompute, STR -> carry, engine-derived saves (Option B), and the
`unmodelled_effects` warning (nothing declared is silently lost).
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import equipment  # noqa: E402
from web.tests.dice import harness as H  # noqa: E402


def item(name, **fields):
    return {"name": name, **fields}


def worn_payload(**overrides):
    base = H.make_player(**overrides)
    return base


class EffectNormalizationTest(unittest.TestCase):
    def test_flat_fields_and_explicit_effects_normalize(self):
        declared = {
            "ac_bonus": 1, "save_bonus": 1, "check_bonus": 1, "set_con": 19,
            "str_bonus": 2, "str_bonus_max": 20, "proficiency_bonus": 1,
            "effects": [{"type": "damage_resistance", "damage": "cold"}],
        }
        types = [e["type"] for e in equipment.item_effects(declared)]
        self.assertEqual(types.count("ability_set"), 1)
        self.assertIn("ac_bonus", types)
        self.assertIn("save_bonus", types)
        self.assertIn("check_bonus", types)
        self.assertIn("proficiency_bonus", types)
        self.assertIn("damage_resistance", types)
        bonus = next(e for e in equipment.item_effects(declared) if e["type"] == "ability_bonus")
        self.assertEqual((bonus["ability"], bonus["value"], bonus["max"]), ("str", 2, 20))

    def test_worn_kinds_include_belt(self):
        import dice_server as ds
        self.assertIn("belt", ds.WORN_KINDS)
        self.assertTrue(ds._is_worn_slot("Belt of Giant Strength", {"kind": "belt"}))


class AbilityScoreItemTest(unittest.TestCase):
    def test_set_score_and_capped_bonus(self):
        amulet = item("Amulet of Health", base="Amulet of Health", kind="amulet",
                      set_con=19, attunement=True)
        belt = item("Belt of Dwarvenkind", base="Belt of Dwarvenkind", kind="belt",
                    con_bonus=2, con_bonus_max=20, attunement=True)
        p = H.make_player(stats={"str": 16, "dex": 14, "con": 18, "int": 10, "wis": 12, "cha": 8},
                          inventory=[amulet, belt],
                          equipped={"armor": None, "hands": [None, None],
                                    "worn": ["Amulet of Health", "Belt of Dwarvenkind"]},
                          attuned=["Amulet of Health", "Belt of Dwarvenkind"])
        with H.load(p):
            # 18 + 2 capped at 20 -> 20, then Amulet's floor 19 -> 20.
            self.assertEqual(H.ds._effective_stats(H.ds.DB_CONNECTION.cursor())["con"], 20)

    def test_set_score_does_not_lower_a_higher_score(self):
        amulet = item("Amulet of Health", base="Amulet of Health", kind="amulet",
                      set_con=19, attunement=True)
        p = H.make_player(stats={"str": 16, "dex": 14, "con": 20, "int": 10, "wis": 12, "cha": 8},
                          inventory=[amulet],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Amulet of Health"]},
                          attuned=["Amulet of Health"])
        with H.load(p):
            self.assertEqual(H.ds._effective_stats(H.ds.DB_CONNECTION.cursor())["con"], 20)

    def test_unattuned_set_score_does_nothing(self):
        amulet = item("Amulet of Health", base="Amulet of Health", kind="amulet",
                      set_con=19, attunement=True)
        p = H.make_player(inventory=[amulet],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Amulet of Health"]})
        with H.load(p):
            self.assertEqual(H.ds._effective_stats(H.ds.DB_CONNECTION.cursor())["con"], 14)

    def test_belt_strength_changes_carry_capacity(self):
        belt = item("Belt of Hill Giant Strength", base="Belt of Giant Strength", kind="belt",
                    set_str=21, attunement=True)
        p = H.make_player(stats={"str": 10, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8},
                          inventory=[belt],
                          equipped={"armor": None, "hands": [None, None],
                                    "worn": ["Belt of Hill Giant Strength"]},
                          attuned=["Belt of Hill Giant Strength"])
        with H.load(p):
            self.assertEqual(H.ds._carry_block(H.ds.DB_CONNECTION.cursor())["capacity"], 315.0)


class DerivedSaveTest(unittest.TestCase):
    def test_engine_derives_the_player_save(self):
        p = H.make_player()  # CON 14 (+2), proficient in CON saves, prof +2
        with H.load(p):
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Constitution save",
                                   ability="con", save=True)
            self.assertEqual(r["modifier"], 4)  # +2 ability +2 proficiency
            self.assertEqual(r["save"]["ability_modifier"], 2)
            self.assertEqual(r["save"]["proficiency_bonus"], 2)

    def test_non_proficient_save_has_no_proficiency(self):
        p = H.make_player()  # DEX 14 is not a Fighter save proficiency
        with H.load(p):
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Dexterity save",
                                   ability="dex", save=True)
            self.assertEqual(r["modifier"], 2)

    def test_ring_of_protection_adds_one_to_saves_and_ac(self):
        ring = item("Ring of Protection", base="Ring of Protection", kind="ring",
                    ac_bonus=1, save_bonus=1, attunement=True)
        p = H.make_player(inventory=[ring],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Ring of Protection"]},
                          attuned=["Ring of Protection"])
        with H.load(p):
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Constitution save",
                                   ability="con", save=True)
            self.assertEqual(r["save"]["item_bonus"], 1)
            self.assertEqual(r["modifier"], 5)
            self.assertEqual(H.ds._db_val(H.ds.DB_CONNECTION.cursor(), "armor_class"), 13)

    def test_unattuned_ring_grants_nothing(self):
        ring = item("Ring of Protection", base="Ring of Protection", kind="ring",
                    ac_bonus=1, save_bonus=1, attunement=True)
        p = H.make_player(inventory=[ring],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Ring of Protection"]})
        with H.load(p):
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Constitution save",
                                   ability="con", save=True)
            self.assertEqual(r["modifier"], 4)
            self.assertEqual(H.ds._db_val(H.ds.DB_CONNECTION.cursor(), "armor_class"), 12)

    def test_situational_modifier_adds_on_top(self):
        p = H.make_player()
        with H.load(p):
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Constitution save",
                                   ability="con", save=True, situational_modifier=3)
            self.assertEqual(r["modifier"], 7)
            self.assertEqual(r["save"]["situational"], 3)

    def test_stone_of_good_luck_bonuses_checks_and_saves(self):
        stone = item("Stone of Good Luck", kind="other", check_bonus=1, save_bonus=1,
                     attunement=True)
        p = H.make_player(inventory=[stone],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Stone of Good Luck"]},
                          attuned=["Stone of Good Luck"])
        with H.load(p):
            check = H.ds.perform_check(actor="{player_name}", dc=10,
                                       check_name="Athletics", ability="str")
            # Engine-derived: STR +3 (16), prof +2, item +1 = +6.
            self.assertEqual(check["modifier"], 6)
            save = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Constitution save",
                                      ability="con", save=True)
            self.assertEqual(save["modifier"], 5)

    def test_registry_saves_include_item_bonus(self):
        ring = item("Ring of Protection", base="Ring of Protection", kind="ring",
                    ac_bonus=1, save_bonus=1, attunement=True)
        p = H.make_player(inventory=[ring],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Ring of Protection"]},
                          attuned=["Ring of Protection"])
        with H.load(p):
            entry = H.ds._player_registry_entry(H.ds.DB_CONNECTION.cursor())
            self.assertEqual(entry["saves"]["con"], 5)  # +2 ability +2 prof +1 item

    def test_proficiency_bonus_item_raises_save_and_spell_dc(self):
        mastery = item("Ioun Stone of Mastery", kind="other", proficiency_bonus=1, attunement=True)
        p = H.wizard_l3()
        p["inventory"] = list(p["inventory"]) + [mastery]
        p["equipped"] = {"armor": None, "hands": [None, None], "worn": ["Ioun Stone of Mastery"]}
        p["attuned"] = ["Ioun Stone of Mastery"]
        with H.load(p):
            cur = H.ds.DB_CONNECTION.cursor()
            self.assertEqual(H.ds._effective_prof_bonus(cur), 3)
            self.assertEqual(H.ds._db_val(cur, "spellcasting")["dc"], 14)  # 8 + 3 + 3

    def test_resolve_magic_derives_the_player_save(self):
        p = H.make_player()
        with H.load(p):
            r = H.ds.resolve_magic(spell_name="Fireball", actor="Evil Mage", is_npc_attack=True,
                                   attack_type="saving_throw", save_type="dex", spell_save_dc=14,
                                   damage_dice="8d6", damage_type="fire")
            self.assertEqual(r["save_modifier"], 2)  # DEX 14, not proficient
            self.assertIn("save", r)

    def test_multi_target_player_save_is_derived(self):
        p = H.make_player()
        with H.load(p):
            r = H.ds.resolve_magic(
                spell_name="Fireball", actor="Evil Mage", is_npc_attack=True,
                attack_type="saving_throw", save_type="dex", spell_save_dc=14,
                targets=[{"name": "{player_name}", "is_player": True, "current_hp": 12},
                         {"name": "Goblin", "current_hp": 7, "save_modifier": 2}],
                damage_dice="8d6", damage_type="fire")
            by_name = {t["name"]: t for t in r["targets"]}
            self.assertEqual(by_name["{player_name}"]["save_modifier"], 2)
            self.assertIn("save", by_name["{player_name}"])
            self.assertEqual(by_name["Goblin"]["save_modifier"], 2)


class HitPointRecomputeTest(unittest.TestCase):
    def test_con_setter_changes_max_hp_retroactively(self):
        amulet = item("Amulet of Health", base="Amulet of Health", kind="amulet",
                      set_con=19, attunement=True)
        p = H.make_player(level=4, total_hit_points=30, current_hit_points=30, hit_dice_count=4,
                          inventory=[amulet],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Amulet of Health"]},
                          attuned=["Amulet of Health"])
        with H.load(p):
            cur = H.ds.DB_CONNECTION.cursor()
            # CON 14 (+2) -> 19 (+4): +2 x 4 levels = +8
            self.assertEqual(H.ds._db_val(cur, "total_hit_points"), 38)
            self.assertEqual(H.ds._db_val(cur, "current_hit_points"), 38)
            self.assertEqual(H.ds._db_val(cur, "item_hp_delta"), 8)

    def test_removing_the_item_reverses_the_hp_change(self):
        amulet = item("Amulet of Health", base="Amulet of Health", kind="amulet",
                      set_con=19, attunement=True)
        p = H.make_player(level=4, total_hit_points=30, current_hit_points=25, hit_dice_count=4,
                          inventory=[amulet],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Amulet of Health"]},
                          attuned=["Amulet of Health"])
        with H.load(p):
            cur = H.ds.DB_CONNECTION.cursor()
            self.assertEqual(H.ds._db_val(cur, "total_hit_points"), 38)
            H.ds.update_player_list(key="inventory", item="Amulet of Health", action="remove")
            self.assertEqual(H.ds._db_val(cur, "total_hit_points"), 30)
            self.assertEqual(H.ds._db_val(cur, "current_hit_points"), 25)
            self.assertEqual(H.ds._db_val(cur, "item_hp_delta"), 0)


class ArmourClassItemTest(unittest.TestCase):
    def test_ac_set_formula_when_unarmoured(self):
        robe = item("Robe of the Archmagi", kind="worn", attunement=True,
                    effects=[{"type": "ac_set", "base": 15, "plus_dex": True, "when": "no_armor"}])
        p = H.make_player(inventory=[robe],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Robe of the Archmagi"]},
                          attuned=["Robe of the Archmagi"])
        with H.load(p):
            cur = H.ds.DB_CONNECTION.cursor()
            self.assertEqual(H.ds._db_val(cur, "armor_class"), 17)  # 15 + DEX 2

    def test_conditional_ac_bonus_needs_no_armour_or_shield(self):
        bracers = item("Bracers of Defense", base="Bracers of Defense", kind="bracers",
                       attunement=True,
                       effects=[{"type": "ac_bonus", "value": 2, "when": "no_armor_or_shield"}])
        p = H.make_player(inventory=[bracers],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Bracers of Defense"]},
                          attuned=["Bracers of Defense"])
        with H.load(p):
            self.assertEqual(H.ds._db_val(H.ds.DB_CONNECTION.cursor(), "armor_class"), 14)  # 10+2+2

    def test_conditional_ac_bonus_withheld_with_armour(self):
        bracers = item("Bracers of Defense", base="Bracers of Defense", kind="bracers",
                       attunement=True,
                       effects=[{"type": "ac_bonus", "value": 2, "when": "no_armor_or_shield"}])
        leather = item("Leather Armor", base="Leather Armor")
        p = H.make_player(inventory=[bracers, leather],
                          equipped={"armor": "Leather Armor", "hands": [None, None],
                                    "worn": ["Bracers of Defense"]},
                          attuned=["Bracers of Defense"])
        with H.load(p):
            # Leather 11 + DEX 2 = 13; the bracers grant nothing while armoured.
            self.assertEqual(H.ds._db_val(H.ds.DB_CONNECTION.cursor(), "armor_class"), 13)


class UnmodelledEffectTest(unittest.TestCase):
    def test_declared_but_unapplied_effect_is_reported(self):
        ring = item("Ring of Regeneration", kind="ring", attunement=True,
                    effects=[{"type": "regeneration", "dice": "1d6", "period": "10 minutes"}])
        p = H.make_player(inventory=[ring],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Ring of Regeneration"]},
                          attuned=["Ring of Regeneration"])
        with H.load(p):
            block = H.ds._equipment_block(H.ds.DB_CONNECTION.cursor())
            self.assertEqual(block["unmodelled_effects"][0]["type"], "regeneration")
            self.assertEqual(block["unmodelled_effects"][0]["reason"], "not_yet_applied")
            self.assertTrue(any(w["code"] == "unmodelled_effect" for w in block["warnings"]))

    def test_unknown_predicate_is_inactive_and_reported(self):
        ring = item("Strange Ring", kind="ring", attunement=True,
                    effects=[{"type": "ac_bonus", "value": 5, "when": "while_moon_is_full"}])
        p = H.make_player(inventory=[ring],
                          equipped={"armor": None, "hands": [None, None], "worn": ["Strange Ring"]},
                          attuned=["Strange Ring"])
        with H.load(p):
            cur = H.ds.DB_CONNECTION.cursor()
            self.assertEqual(H.ds._db_val(cur, "armor_class"), 12)  # bonus NOT applied
            self.assertEqual(H.ds._equipment_block(cur)["unmodelled_effects"][0]["reason"],
                             "unknown_predicate")


class UpdatePlayerListAuthoringTest(unittest.TestCase):
    def test_new_sugar_and_effects_are_stored(self):
        p = H.make_player()
        with H.load(p):
            r = H.ds.update_player_list(
                key="inventory", item="Amulet of Health: jade serpent", action="add",
                base="Amulet of Health", kind="amulet", set_con=19, save_bonus=1, attunement=True,
                effects=[{"type": "ability_set", "ability": "con", "value": 19}])
            self.assertTrue(r["success"])
            entry = H.ds._inventory_entry(H.ds._db_val(H.ds.DB_CONNECTION.cursor(), "inventory", []),
                                          "Amulet of Health")
            self.assertEqual(entry["set_con"], 19)
            self.assertEqual(entry["save_bonus"], 1)
            self.assertEqual(entry["effects"][0]["type"], "ability_set")

    def test_worn_effect_without_kind_is_rejected(self):
        p = H.make_player()
        with H.load(p):
            r = H.ds.update_player_list(key="inventory", item="Ring of Warding", action="add",
                                        save_bonus=1)
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], "worn_kind_missing")


if __name__ == "__main__":
    unittest.main()
