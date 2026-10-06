"""Tier 1 SRD conformance: player defenses + the remaining item effect consumers."""

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


def equipped(worn=None, hands=None, armor=None):
    return {"armor": armor, "hands": hands or [None, None], "worn": worn or []}


class PlayerDefenseTest(unittest.TestCase):
    def test_item_resistance_halves_and_immunity_zeroes(self):
        resist = item("Ring of Fire Resistance", kind="ring",
                      effects=[{"type": "damage_resistance", "damage": "fire"}])
        immune = item("Brooch of Shielding", kind="worn",
                      effects=[{"type": "damage_immunity", "damage": "force"}])
        p = H.make_player(inventory=[resist, immune],
                          equipped=equipped(worn=["Ring of Fire Resistance", "Brooch of Shielding"]))
        with H.load(p):
            cur = H.ds.DB_CONNECTION.cursor()
            self.assertEqual(H.ds._apply_player_damage_modifiers(cur, 20, "fire")[0], 10)
            self.assertEqual(H.ds._apply_player_damage_modifiers(cur, 20, "force")[0], 0)
            self.assertEqual(H.ds._apply_player_damage_modifiers(cur, 20, "cold")[0], 20)

    def test_spell_resistance_buff_applies(self):
        p = H.make_player()
        with H.load(p):
            cur = H.ds.DB_CONNECTION.cursor()
            H.ds._db_set(cur, "_active_buff_data", {"Protection from Energy": [
                {"kind": "resistance", "field": "damage_resistance", "value": "fire"}]})
            H.ds._db_set(cur, "active_effects", ["Protection from Energy"])
            H.ds.DB_CONNECTION.commit()
            self.assertEqual(H.ds._apply_player_damage_modifiers(cur, 12, "fire")[0], 6)
            self.assertIn("fire", equipment.defense_state(H.ds._carry_get(cur))["damage_resistances"])

    def test_petrified_player_resists_all_damage(self):
        p = H.make_player(conditions=["petrified"])
        with H.load(p):
            cur = H.ds.DB_CONNECTION.cursor()
            self.assertEqual(H.ds._apply_player_damage_modifiers(cur, 12, "slashing")[0], 6)

    def test_condition_immunity_blocks_a_condition(self):
        periapt = item("Periapt of Proof against Poison", kind="worn",
                       effects=[{"type": "condition_immunity", "condition": "poisoned"}])
        p = H.make_player(inventory=[periapt],
                          equipped=equipped(worn=["Periapt of Proof against Poison"]))
        with H.load(p):
            cur = H.ds.DB_CONNECTION.cursor()
            status, _reason = H.ds._apply_combat_condition(
                cur, H.ds._db_val(cur, "name", "Player"), "poisoned")
            self.assertEqual(status, "immune")

    def test_npc_attack_on_player_goes_through_resistance(self):
        resist = item("Ring of Fire Resistance", kind="ring",
                      effects=[{"type": "damage_resistance", "damage": "fire"}])
        p = H.make_player(inventory=[resist], equipped=equipped(worn=["Ring of Fire Resistance"]))
        with H.load(p), H.rolls_always(15):
            r = H.ds.resolve_attack(actor="Goblin", attack_modifier=5, damage_dice="2d6",
                                    damage_type="fire", target_ac=10,
                                    target_name="{player_name}", is_npc_attack=True)
            # 2d6 -> 30, halved by fire resistance
            self.assertEqual(r["damage_total"], 15)
            self.assertIn("damage_modified", r)


class SpeedTest(unittest.TestCase):
    def test_speed_minimum_and_granted_mode(self):
        boots = item("Boots of Striding and Springing", kind="boots", attunement=True,
                     effects=[{"type": "speed", "minimum": 30, "ignore_encumbrance": True},
                              {"type": "speed_grant", "mode": "swim", "value": 40}])
        p = H.make_player(speed=20, inventory=[boots],
                          equipped=equipped(worn=["Boots of Striding and Springing"]),
                          attuned=["Boots of Striding and Springing"])
        with H.load(p):
            carry = H.ds._carry_block(H.ds.DB_CONNECTION.cursor())
            self.assertEqual(carry["speed"], 30)
            self.assertEqual(carry["speeds"].get("swim"), 40)

    def test_ignore_encumbrance_zeroes_the_penalty(self):
        boots = item("Boots of Striding and Springing", kind="boots",
                     effects=[{"type": "speed", "ignore_encumbrance": True}])
        p = H.make_player(stats={"str": 8, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8},
                          speed=30, inventory=[boots, "Chain Mail"],
                          equipped=equipped(worn=["Boots of Striding and Springing"]))
        with H.load(p):
            carry = H.ds._carry_block(H.ds.DB_CONNECTION.cursor())
            self.assertEqual(carry["speed_penalty"], 0)
            self.assertEqual(carry["speed"], 30)


class HitPointEffectTest(unittest.TestCase):
    def test_hp_per_level_adds_and_reverses(self):
        axe = item("Berserker Axe", kind="weapon", attunement=True,
                   effects=[{"type": "hp_per_level", "value": 1}])
        p = H.make_player(level=4, total_hit_points=30, current_hit_points=30, hit_dice_count=4,
                          inventory=[axe], equipped=equipped(worn=["Berserker Axe"]),
                          attuned=["Berserker Axe"])
        with H.load(p):
            cur = H.ds.DB_CONNECTION.cursor()
            self.assertEqual(H.ds._db_val(cur, "total_hit_points"), 34)
            H.ds.update_player_list(key="inventory", item="Berserker Axe", action="remove")
            self.assertEqual(H.ds._db_val(cur, "total_hit_points"), 30)

    def test_hit_die_healing_multiplier_state(self):
        periapt = item("Periapt of Wound Closure", kind="worn", attunement=True,
                       effects=[{"type": "hit_die_healing_multiplier", "value": 2}])
        p = H.make_player(inventory=[periapt], equipped=equipped(worn=["Periapt of Wound Closure"]),
                          attuned=["Periapt of Wound Closure"])
        with H.load(p):
            self.assertEqual(H.ds._item_effect_state(H.ds.DB_CONNECTION.cursor())
                             ["hit_die_healing_multiplier"], 2.0)


class SaveAdvantageTest(unittest.TestCase):
    def test_scoped_advantage_vs_spells(self):
        mantle = item("Mantle of Spell Resistance", kind="cloak", attunement=True,
                      effects=[{"type": "save_advantage", "when": "vs_spells"}])
        p = H.make_player(inventory=[mantle], equipped=equipped(worn=["Mantle of Spell Resistance"]),
                          attuned=["Mantle of Spell Resistance"])
        with H.load(p):
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Dexterity save",
                                   ability="dex", save=True, against="spells")
            self.assertTrue(r["save"]["advantage"])
            self.assertEqual(r["save"]["advantage_sources"], ["Mantle of Spell Resistance"])
            plain = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Dexterity save",
                                       ability="dex", save=True)
            self.assertFalse(plain["save"]["advantage"])

    def test_resolve_magic_marks_spell_advantage(self):
        mantle = item("Mantle of Spell Resistance", kind="cloak", attunement=True,
                      effects=[{"type": "save_advantage", "when": "vs_spells"}])
        p = H.make_player(inventory=[mantle], equipped=equipped(worn=["Mantle of Spell Resistance"]),
                          attuned=["Mantle of Spell Resistance"])
        with H.load(p):
            r = H.ds.resolve_magic(spell_name="Fireball", actor="Evil Mage", is_npc_attack=True,
                                   attack_type="saving_throw", save_type="dex", spell_save_dc=14,
                                   damage_dice="8d6", damage_type="fire")
            self.assertTrue(r["save"]["advantage"])


class CheckDiceAndSkillTest(unittest.TestCase):
    def test_spell_save_dice_are_added(self):
        p = H.make_player()
        with H.load(p), H.rolls_always(4):
            cur = H.ds.DB_CONNECTION.cursor()
            H.ds._db_set(cur, "_active_buff_data", {"Bless": [
                {"kind": "dice", "field": "saving_throws", "value": "+1d4"}]})
            H.ds._db_set(cur, "active_effects", ["Bless"])
            H.ds.DB_CONNECTION.commit()
            r = H.ds.perform_check(actor="{player_name}", dc=10, check_name="Constitution save",
                                   ability="con", save=True)
            self.assertEqual(r["dice_bonus"], 4)

    def test_skill_bonus_with_context(self):
        gloves = item("Gloves of Swimming and Climbing", kind="gloves", attunement=True,
                      effects=[{"type": "skill_bonus", "skill": "athletics", "value": 5,
                                "when": "context:climbing"}])
        p = H.make_player(inventory=[gloves],
                          equipped=equipped(worn=["Gloves of Swimming and Climbing"]),
                          attuned=["Gloves of Swimming and Climbing"])
        with H.load(p):
            climb = H.ds.perform_check(actor="{player_name}", dc=10,
                                       check_name="Athletics", ability="str", context="climbing")
            # Engine-derived: STR +3, prof +2, skill_bonus +5 = +10.
            self.assertEqual(climb["modifier"], 10)
            plain = H.ds.perform_check(actor="{player_name}", dc=10,
                                       check_name="Athletics", ability="str")
            self.assertEqual(plain["modifier"], 5)


class ProficiencyAndPropertyTest(unittest.TestCase):
    def test_granted_weapon_proficiency(self):
        bracers = item("Bracers of Archery", kind="bracers", attunement=True,
                       effects=[{"type": "grant_proficiency", "category": "weapon",
                                 "value": "longbow"}])
        bow = item("Longbow", base="Longbow")
        p = H.make_player(weapon_proficiencies=["Simple weapons"],
                          inventory=[bracers, bow],
                          equipped=equipped(hands=[None, None], worn=["Bracers of Archery"]),
                          attuned=["Bracers of Archery"])
        with H.load(p):
            info = H.ds._derive_weapon_attack(H.ds.DB_CONNECTION.cursor(), "Longbow",
                                              H.ds._db_val(H.ds.DB_CONNECTION.cursor(), "inventory", []))
            self.assertTrue(info["proficient"])

    def test_sun_blade_grants_finesse(self):
        blade = item("Sun Blade", base="Longsword",
                     effects=[{"type": "weapon_property", "property": "finesse"}])
        p = H.make_player(stats={"str": 8, "dex": 16, "con": 14, "int": 10, "wis": 12, "cha": 8},
                          inventory=[blade], equipped=equipped(hands=["Sun Blade", None]))
        with H.load(p):
            info = H.ds._derive_weapon_attack(H.ds.DB_CONNECTION.cursor(), "Sun Blade", [blade])
            self.assertEqual(info["ability"], "dexterity")
            self.assertTrue(any("finesse" in str(prop).lower() for prop in info["properties"]))


class InitiativeTest(unittest.TestCase):
    def test_item_initiative_bonus_and_advantage(self):
        stone = item("Stone of Initiative", kind="worn",
                     effects=[{"type": "initiative", "bonus": 2, "advantage": True}])
        p = H.make_player(inventory=[stone], equipped=equipped(worn=["Stone of Initiative"]))
        with H.load(p):
            entry = H.ds._player_registry_entry(H.ds.DB_CONNECTION.cursor())
            self.assertEqual(entry["initiative_modifier"], 4)  # DEX +2, +2 item
            self.assertTrue(entry["initiative_advantage"])


if __name__ == "__main__":
    unittest.main()
