"""SRD 5.1 carrying capacity: weights, encumbrance bands and engine enforcement.

Run from the repo root:  venv\\Scripts\\python.exe -m unittest web.tests.dice.test_carrying
"""

import unittest

import carrying
from web.tests.dice import harness as H


def get_of(payload):
    """A `get(key, default)` reader over a plain dict, as carrying.carry_state wants."""
    return lambda key, default=None: payload.get(key, default)


class CarryingMathTest(unittest.TestCase):
    def test_capacity_thresholds_and_push_drag_lift(self):
        state = carrying.carry_state(get_of({"stats": {"str": 8}, "race": "Human", "gold": 0}))
        self.assertEqual(state["capacity"], 120)          # 8 x 15
        self.assertEqual(state["push_drag_lift"], 240)    # twice the capacity
        self.assertEqual(state["thresholds"]["encumbered"], 40)            # 5 x STR
        self.assertEqual(state["thresholds"]["heavily_encumbered"], 80)    # 10 x STR
        self.assertEqual(state["size"], "Medium")
        self.assertEqual(state["status"], "unencumbered")

    def test_size_rules(self):
        self.assertEqual(carrying.size_for_race("Halfling"), "Small")
        self.assertEqual(carrying.size_for_race("Human"), "Medium")
        self.assertEqual(carrying.size_for_race("Nonsense Race"), "Medium")
        self.assertEqual(carrying.size_multiplier("Tiny"), 0.5)
        self.assertEqual(carrying.size_multiplier("Large"), 2.0)
        self.assertEqual(carrying.size_multiplier("Huge"), 4.0)
        self.assertEqual(carrying.size_multiplier("Medium", 2), 2.0)

    def test_bands_are_strictly_exceeded(self):
        base = {"stats": {"str": 10}, "race": "Human", "gold": 0}
        # exactly 5x STR = 50 lb -> still unencumbered
        under = dict(base, inventory=["Iron Spikes (10)"] * 10)  # 10 x 5 lb = 50
        self.assertEqual(carrying.carry_state(get_of(under))["status"], "unencumbered")
        # just over 5x STR -> encumbered
        over = dict(base, inventory=["Iron Spikes (10)"] * 10 + ["Dagger"])
        state = carrying.carry_state(get_of(over))
        self.assertEqual(state["status"], "encumbered")
        self.assertEqual(state["speed_penalty"], 10)
        # just over 10x STR -> heavily encumbered
        heavy = dict(base, inventory=["Chain Mail", "Plate Armor"])  # 55 + 65 = 120 > 100
        state = carrying.carry_state(get_of(heavy))
        self.assertEqual(state["status"], "heavily_encumbered")
        self.assertEqual(state["speed_penalty"], 20)
        self.assertTrue(state["heavy_encumbered"])

    def test_effective_speed_follows_the_penalty(self):
        state = carrying.carry_state(get_of({"stats": {"str": 10}, "speed": 30,
                                             "inventory": ["Chain Mail", "Plate Armor"]}))
        self.assertEqual(state["speed"], 10)  # 30 - 20

    def test_catalog_weights_and_aliases(self):
        self.assertEqual(carrying.weight_for("Dagger"), 1)
        self.assertEqual(carrying.weight_for("a crowbar"), 5)
        self.assertEqual(carrying.weight_for("Rope"), 10)            # alias of hempen rope
        self.assertEqual(carrying.weight_for("Explorer's Pack"), 59)  # sum of contents
        self.assertEqual(carrying.weight_for("Magic Dagger (+1)"), 1)  # magic variants still weigh
        self.assertIsNone(carrying.weight_for("Void Crystal of Harrow"))

    def test_declared_weight_beats_the_catalog(self):
        self.assertEqual(carrying.weight_for("Dagger", 4), 4)
        state = carrying.carry_state(get_of({"stats": {"str": 10},
                                             "inventory": [{"name": "Void Crystal", "weight": 12.5}]}))
        self.assertEqual(state["carried"], 12.5)
        self.assertEqual(state["items"][0]["source"], "declared")
        self.assertEqual(state["unweighed"], [])

    def test_unknown_items_count_zero_and_are_flagged(self):
        state = carrying.carry_state(get_of({"stats": {"str": 10},
                                             "inventory": ["Mystery Orb", "Dagger"]}))
        self.assertEqual(state["carried"], 1)
        self.assertEqual(state["unweighed"], ["Mystery Orb"])

    def test_consumables_count_units_and_coins_weigh(self):
        # The Forge stores "20 Arrows" as {"Arrows": 20}: a UNIT count, so the
        # catalog's bundle weight (1 lb per 20 arrows) is divided by 20.
        state = carrying.carry_state(get_of({"stats": {"str": 10}, "gold": 100,
                                             "consumables": {"Arrows": 20}}))
        self.assertEqual(state["carried"], 3)         # 20 arrows = 1 lb + 2 lb of coins
        self.assertEqual(state["coin_weight"], 2.0)
        bolts = carrying.carry_state(get_of({"stats": {"str": 10}, "gold": 0,
                                           "consumables": {"Bolts": 18}}))
        self.assertEqual(bolts["carried"], 1.35)      # 18 bolts of 1.5 lb per 20

    def test_bundle_and_unit_weights(self):
        self.assertEqual(carrying.bundle_size_for("Arrows"), 20)
        self.assertEqual(carrying.bundle_size_for("Bolts"), 20)
        self.assertEqual(carrying.bundle_size_for("Blowgun Needles"), 50)
        self.assertEqual(carrying.bundle_size_for("Dagger"), 1)
        self.assertEqual(carrying.bundle_size_for("Void Crystal"), 1)
        self.assertEqual(carrying.unit_weight_for("Arrow"), 0.05)
        self.assertEqual(carrying.unit_weight_for("Magic Dagger (+1)"), 1)  # per 1
        self.assertIsNone(carrying.unit_weight_for("Void Crystal"))

    def test_plural_consumable_names_resolve(self):
        # Names the Forge writes as consumables (from quantities in starting gear).
        for name, weight in [("Daggers", 1), ("Handaxes", 2), ("Javelins", 2),
                             ("Shortswords", 2), ("Darts", 0.25)]:
            self.assertEqual(carrying.weight_for(name), weight, name)

    def test_forged_gear_names_resolve(self):
        # Renamed/flavoured starting gear (see config/backgrounds.yml, classes.yml).
        self.assertEqual(carrying.weight_for("Dark Common Clothes"), 3)     # Criminal
        self.assertEqual(carrying.weight_for("Leather Tunic"), 10)          # Bard/Druid/Rogue/Warlock
        self.assertEqual(carrying.weight_for("Wooden Shield"), 6)           # Druid
        self.assertEqual(carrying.weight_for("Bottle of Black Ink"), 0)     # Sage
        self.assertEqual(carrying.weight_for("Bone Dice Set"), 0)           # Soldier
        self.assertEqual(carrying.weight_for("5 Sticks of Incense"), 0)     # Acolyte
        self.assertEqual(carrying.weight_for("Insignia of Rank"), 0)
        self.assertEqual(carrying.weight_for("Trophy from a Fallen Enemy"), 0)
        self.assertEqual(carrying.weight_for("Letter from a Dead Colleague"), 0)
        self.assertEqual(carrying.weight_for("Small Knife"), 0)

    def test_capacity_multiplier_doubles_capacity_and_bands(self):
        payload = {"stats": {"str": 10}, "gold": 0,
                   "inventory": ["Chain Mail", "Plate Armor"]}  # 120 lb
        # bands without the effect: 50 lb encumbered / 100 lb heavily -> heavy
        self.assertEqual(carrying.carry_state(get_of(payload))["status"], "heavily_encumbered")
        doubled = dict(payload, capacity_multiplier=2)
        state = carrying.carry_state(get_of(doubled))
        self.assertEqual(state["capacity"], 300)                  # 150 x 2
        self.assertEqual(state["thresholds"]["encumbered"], 100)
        self.assertEqual(state["thresholds"]["heavily_encumbered"], 200)
        self.assertEqual(state["push_drag_lift"], 600)
        self.assertEqual(state["status"], "encumbered")           # 120 lb: over 100, under 200

    def test_heavy_encumbrance_only_hits_the_physical_abilities(self):
        payload = {"stats": {"str": 10}, "inventory": ["Plate Armor", "Chain Mail"]}
        self.assertEqual(carrying.heavy_encumbrance(get_of(payload), "str"), (True, ["heavily encumbered"]))
        self.assertEqual(carrying.heavy_encumbrance(get_of(payload), "dex"), (True, ["heavily encumbered"]))
        self.assertEqual(carrying.heavy_encumbrance(get_of(payload), "con"), (True, ["heavily encumbered"]))
        self.assertEqual(carrying.heavy_encumbrance(get_of(payload), "wis"), (False, []))
        self.assertEqual(carrying.heavy_encumbrance(get_of(payload)), (True, ["heavily encumbered"]))
        light = {"stats": {"str": 10}, "inventory": ["Dagger"]}
        self.assertEqual(carrying.heavy_encumbrance(get_of(light), "str"), (False, []))


def heavy_fighter():
    """STR 10 fighter hauling 129 lb of armour: heavily encumbered (over 10x STR)."""
    return H.make_player(
        name="Borin", character_class="Fighter", level=3,
        stats={"str": 10, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8},
        inventory=["Plate Armor", "Chain Mail", "Shield", "Longsword"],  # 129 lb
        gold=0,
    )


class EncumbranceEnforcementTest(H.EngineCase):
    player_factory = staticmethod(heavy_fighter)

    def test_attack_rolls_get_disadvantage(self):
        with H.fixed_rolls([18, 4]) as calls:  # rolled twice, takes the lower
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=6, target_ac=12,
                                    damage_dice="1d8", damage_modifier=3,
                                    target_name="Goblin", target_current_hp=20)
        self.assertEqual([c for c in calls if c == (1, 20)], [(1, 20)] * 2)
        self.assertEqual(r["attack_roll"], 4)
        self.assertEqual(r["disadvantage"], True)
        self.assertEqual(r["disadvantage_sources"], ["heavily encumbered"])
        self.assertEqual(r["outcome"], "Failure")

    def test_gm_advantage_cancels_encumbrance_disadvantage(self):
        with H.fixed_rolls([18]) as calls:
            r = H.ds.resolve_attack(actor="Borin", attack_modifier=6, target_ac=10,
                                    damage_dice="1d8", target_name="Goblin",
                                    target_current_hp=20, advantage=True)
        self.assertEqual([c for c in calls if c == (1, 20)], [(1, 20)])  # one straight roll
        self.assertEqual(r["attack_roll"], 18)
        self.assertEqual(r["advantage_cancelled"], True)

    def test_ability_checks_and_saves_get_disadvantage(self):
        with H.fixed_rolls([19, 3]):
            r = H.ds.perform_check(modifier=5, dc=15, check_name="Athletics", actor="{player_name}",
                                   ability="str")
        self.assertEqual(r["base_roll"], 3)
        self.assertEqual(r["disadvantage_sources"], ["heavily encumbered"])
        self.assertEqual(r["disadvantage_rolls"], [19, 3])

    def test_mental_checks_are_unaffected(self):
        with H.fixed_rolls([19]) as calls:
            r = H.ds.perform_check(modifier=1, dc=13, check_name="Arcana", actor="{player_name}",
                                   ability="int")
        self.assertEqual([c for c in calls if c == (1, 20)], [(1, 20)])
        self.assertEqual(r["base_roll"], 19)
        self.assertNotIn("disadvantage_sources", r)

    def test_enemy_attacks_are_not_affected(self):
        with H.fixed_rolls([19]) as calls:
            r = H.ds.resolve_attack(actor="Goblin", attack_modifier=4, target_ac=13,
                                    damage_dice="1d6", target_name="Borin",
                                    target_current_hp=20, is_npc_attack=True)
        self.assertEqual([c for c in calls if c == (1, 20)], [(1, 20)])
        self.assertNotIn("disadvantage_sources", r)

    def test_inventory_add_returns_the_carrying_block(self):
        r = H.ds.update_player_list(key="inventory", item="Rope", action="add")
        self.assertTrue(r["success"])
        self.assertEqual(r["carrying"]["status"], "heavily_encumbered")
        self.assertEqual(r["carrying"]["carried"], 139)

    def test_unknown_items_warn_until_a_weight_is_declared(self):
        r = H.ds.update_player_list(key="inventory", item="Void Crystal: a humming shard", action="add")
        self.assertEqual(r["unweighed_item"], "Void Crystal")
        self.assertIn("counts as 0 lb", r["warning"])
        H.ds.update_player_list(key="inventory", item="Void Crystal", action="remove")
        r2 = H.ds.update_player_list(key="inventory", item="Void Crystal: a humming shard",
                                     action="add", weight=2.5)
        self.assertNotIn("unweighed_item", r2)
        self.assertEqual(r2["carrying"]["carried"], 131.5)

    def test_dump_player_db_carries_the_block(self):
        block = H.db()["_carrying"]
        self.assertEqual(block["capacity"], 150)
        self.assertEqual(block["status"], "heavily_encumbered")

    def test_long_rest_resets_a_capacity_multiplier(self):
        H.ds.modify_player_numeric(key="capacity_multiplier", delta=1)  # 1 -> 2
        self.assertEqual(H.dbv("capacity_multiplier"), 2)
        self.assertEqual(H.db()["_carrying"]["capacity"], 300)
        r = H.ds.rest(rest_type="long")
        self.assertIn("capacity_multiplier", r["changes"])
        self.assertEqual(H.dbv("capacity_multiplier"), 1)
        self.assertEqual(H.db()["_carrying"]["capacity"], 150)


class StartingGearCatalogTest(unittest.TestCase):
    """Every item the Forge can grant at character creation must have a weight.

    Walks the real class/background configs the way the Forge does (splitting comma
    bundles, stripping quantities) and fails when a new creation item is missing from
    config/weights.yml. Homebrew/loot is out of scope — the GM declares those.
    """

    # Resolved by `resolve_equipment_choice` into a concrete item before storage.
    CHOICE_PLACEHOLDERS = {
        "Any Martial Melee Weapon", "Any Simple Melee Weapon", "Any Simple Weapon",
        "Two Martial Weapons", "Two Simple Melee Weapons", "Artisan's Tools",
        "Any Musical Instrument",
    }
    _QUANTITY_WORDS = (r"^(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
                       r"twelve|fifteen|twenty|fifty)\s+")

    @classmethod
    def _creation_items(cls):
        import os

        import yaml

        items = set()
        for filename in ("classes.yml", "backgrounds.yml"):
            with open(os.path.join(carrying.CONFIG_DIR, filename), encoding="utf-8") as fh:
                for entry in yaml.safe_load(fh) or []:
                    for group in (entry.get("starting_equipment_options") or []):
                        if not isinstance(group, dict):
                            continue
                        for key in ("fixed_items", "choose_one_from"):
                            items.update(str(i) for i in (group.get(key) or []))
        return items

    def test_creation_gear_is_all_weighted(self):
        import re

        missing = []
        for raw in self._creation_items():
            if raw in self.CHOICE_PLACEHOLDERS:
                continue
            for part in str(raw).split(","):
                part = re.sub(r"^\d+\s+", "", part.strip())            # "20 Arrows"
                part = re.sub(self._QUANTITY_WORDS, "", part, flags=re.IGNORECASE)  # "Two Daggers"
                if part and carrying.weight_for(part) is None:
                    missing.append(f"{part} (from {raw!r})")
        self.assertEqual(missing, [], f"creation gear with no weight: {missing}")


if __name__ == "__main__":
    unittest.main()
