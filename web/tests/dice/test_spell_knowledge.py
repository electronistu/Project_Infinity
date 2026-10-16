"""The spell-knowledge gate: a player casts only what the character has.

SRD precedent (i): a miss casts nothing and SPENDS NO ACTION -- the engine hands the GM the
castable lists so the player can choose again. A scroll and an NPC caster are not gated.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\dice\\test_spell_knowledge.py
"""

import unittest

from web.tests.dice import harness as H


class SpellKnowledgeTest(H.EngineCase):
    """``Senna`` is a wizard whose sheet, here, holds exactly what the tests give it."""

    @staticmethod
    def _senna(cantrips=(), prepared=(), known=(), always=(), book=()):
        p = H.make_player(name="Senna", character_class="Wizard", level=5, armor_class=13,
                          total_hit_points=30, current_hit_points=30, hit_dice_count=5,
                          hit_dice_size=6,
                          stats={"str": 8, "dex": 14, "con": 10, "int": 16, "wis": 10, "cha": 10},
                          inventory=["Spellbook", "Component Pouch"],
                          spellcasting={"ability": "intelligence", "dc": 14, "attack_modifier": 6,
                                        "cantrips": list(cantrips), "spells_prepared": list(prepared),
                                        "spells_known": list(known), "spellbook": list(book),
                                        "always_prepared": list(always),
                                        "slots": {"1": 4, "2": 3, "3": 2}})
        return p

    def test_a_prepared_spell_casts_case_insensitively(self):
        with H.load(self._senna(prepared=["Magic Missile"])):
            r = H.ds.resolve_magic(spell_name="magic missile", actor="Senna",
                                   target_name="Goblin", target_current_hp=10)
            self.assertTrue(r["success"])
            self.assertEqual(r["slot_level_used"], 1)

    def test_an_unprepared_spell_is_refused_with_the_lists_and_no_turn_lost(self):
        with H.load(self._senna(cantrips=["Fire Bolt"], prepared=["Magic Missile"])):
            r = H.ds.resolve_magic(spell_name="Fireball", actor="Senna", slot_level=3,
                                   target_name="Goblin")
            self.assertFalse(r["success"])
            self.assertIn("not prepared or known", r["error"])
            self.assertEqual(r["known_cantrips"], ["Fire Bolt"])
            self.assertEqual(r["known_spells"], ["Magic Missile"])
            self.assertNotIn("turn_lost", r)          # SRD: the cast simply does not happen
            self.assertNotIn("action_consumed", r)

    def test_a_cantrip_must_be_in_the_cantrip_list(self):
        with H.load(self._senna(prepared=["Magic Missile"])):
            r = H.ds.resolve_magic(spell_name="Fire Bolt", actor="Senna",
                                   target_name="Goblin", target_current_hp=10)
            self.assertFalse(r["success"])
            self.assertEqual(r["known_cantrips"], [])

    def test_a_spellbook_spell_needs_a_ritual(self):
        with H.load(self._senna(prepared=["Magic Missile"], book=["Detect Magic"])):
            refused = H.ds.resolve_magic(spell_name="Detect Magic", actor="Senna", slot_level=1)
            self.assertFalse(refused["success"])
            self.assertEqual(refused["ritual_spells"], ["Detect Magic"])
            allowed = H.ds.resolve_magic(spell_name="Detect Magic", actor="Senna", slot_level=1,
                                         ritual=True)
            self.assertTrue(allowed["success"])

    def test_an_always_prepared_domain_spell_casts(self):
        # A Devotion paladin's Sanctuary is off the paladin list but always prepared.
        p = H.make_player(name="Aldric", character_class="Paladin", level=5, armor_class=18,
                          total_hit_points=40, current_hit_points=40, hit_dice_count=5,
                          hit_dice_size=10,
                          stats={"str": 16, "dex": 10, "con": 14, "int": 10, "wis": 10, "cha": 14},
                          spellcasting={"ability": "charisma", "dc": 13, "attack_modifier": 5,
                                        "cantrips": [], "spells_prepared": ["Bless"],
                                        "always_prepared": ["Protection from Evil and Good",
                                                            "Sanctuary"],
                                        "slots": {"1": 4}})
        with H.load(p):
            r = H.ds.resolve_magic(spell_name="Sanctuary", actor="Aldric", slot_level=1,
                                   target_name="Aldric", target_save_modifier=0)
            self.assertTrue(r["success"])

    def test_a_scroll_is_not_gated(self):
        with H.load(self._senna(prepared=["Magic Missile"])):
            r = H.ds.resolve_magic(spell_name="Fireball", actor="Senna", slot_level=3,
                                   is_scroll=True, target_name="Goblin", target_current_hp=20,
                                   target_save_modifier=0)
            self.assertTrue(r["success"])

    def test_a_caster_with_no_spellcasting_row_is_not_gated(self):
        p = H.make_player(name="Senna", character_class="Wizard", level=5)
        p.pop("spellcasting", None)
        with H.load(p):
            r = H.ds.resolve_magic(spell_name="Fire Bolt", actor="Senna",
                                   spell_attack_modifier=6, target_ac=12,
                                   target_name="Goblin", target_current_hp=10)
            self.assertTrue(r["success"])

    def test_an_npc_caster_is_not_gated(self):
        with H.load(self._senna(prepared=["Magic Missile"])):
            r = H.ds.resolve_magic(spell_name="Fireball", actor="Evil Mage", is_npc_attack=True,
                                   spell_save_dc=14, attack_type="saving_throw", save_type="dex",
                                   damage_dice="8d6", damage_type="fire",
                                   target_name="{player_name}")
            self.assertTrue(r["success"])


if __name__ == "__main__":
    unittest.main()
