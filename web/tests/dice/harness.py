"""Shared fixtures and deterministic-roll helpers for the dice_server tests.

The engine (`dice_server.py`) keeps its state in module globals:
  - `DB_CONNECTION`  : in-memory SQLite, (re)built by `init_player_db(path)`
  - `_COMBAT_REGISTRY`: the active battle registry

Every test must start from a clean player DB and an empty registry — use the
`load()` context manager. Rolls use `random.randint`; use `fixed_rolls()` /
`rolls_always()` to script them for exact-value assertions.
"""

import contextlib
import copy
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import dice_server as ds  # noqa: E402


# ── player fixtures ───────────────────────────────────────────────────────

def make_player(**overrides):
    """A complete, valid `.player` dict with sane Fighter-1 defaults."""
    base = {
        "name": "Tester",
        "character_class": "Fighter",
        "race": "Human",
        "background": "Soldier",
        "alignment": "True Neutral",
        "gender": "Unknown",
        "level": 1,
        "xp": 0,
        "gold": 10,
        "armor_class": 16,
        "speed": 30,
        "proficiency_bonus": 2,
        "stats": {"str": 16, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8},
        "skills": ["Athletics", "Perception"],
        "expertise": [],
        "saves": ["Strength", "Constitution"],
        "armor_proficiencies": ["Light armor", "Medium armor", "Shields"],
        "weapon_proficiencies": ["Simple weapons", "Martial weapons"],
        "tool_proficiencies": [],
        "languages": ["Common"],
        "features": [],
        "reputation": {},
        "consumables": {},
        "inventory": [],
        "temporary_hit_points": 0,
        "total_hit_points": 12,
        "current_hit_points": 12,
        "hit_dice_count": 1,
        "hit_dice_size": 10,
        "active_effects": [],
        "_active_buff_data": {},
    }
    base.update(overrides)
    return base


def fighter_l5():
    return make_player(
        name="Borin", character_class="Fighter", level=5, xp=6500, gold=120,
        proficiency_bonus=3, armor_class=17, total_hit_points=44,
        current_hit_points=44, hit_dice_count=5, hit_dice_size=10,
        stats={"str": 18, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8},
        inventory=["Longsword", "Chain Mail", "Shield"],
    )


def wizard_l3():
    return make_player(
        name="Senna", character_class="Wizard", race="High Elf", background="Sage",
        level=3, xp=1310, gold=200, proficiency_bonus=2, armor_class=13,
        total_hit_points=13, current_hit_points=13, hit_dice_count=3, hit_dice_size=6,
        stats={"str": 8, "dex": 14, "con": 8, "int": 16, "wis": 10, "cha": 10},
        weapon_proficiencies=["Dagger", "Light Crossbow", "Quarterstaff"],
        spellcasting={
            "ability": "intelligence", "dc": 13, "attack_modifier": 5,
            "cantrips": ["Fire Bolt", "Mage Hand"],
            "spells_known": [],
            "spellbook": ["Magic Missile", "Sleep", "Shield", "Mage Armor", "Misty Step"],
            "spells_prepared": [{"name": "Magic Missile"}, {"name": "Sleep"}],
            "slots": {"1": 4, "2": 2},
        },
        inventory=["Spellbook", "Component Pouch"],
    )


def cleric_l5():
    return make_player(
        name="Father Aldric", character_class="Cleric", background="Acolyte",
        level=5, xp=6500, gold=80, proficiency_bonus=3, armor_class=18,
        total_hit_points=38, current_hit_points=38, hit_dice_count=5, hit_dice_size=8,
        stats={"str": 12, "dex": 10, "con": 14, "int": 10, "wis": 18, "cha": 12},
        spellcasting={
            "ability": "wisdom", "dc": 15, "attack_modifier": 7,
            "cantrips": ["Sacred Flame", "Guidance"],
            "spellbook": [],
            "spells_prepared": [{"name": "Cure Wounds"}, {"name": "Bless"}],
            "slots": {"1": 4, "2": 3, "3": 2},
        },
        armor_proficiencies=["Light armor", "Medium armor", "Shields"],
        inventory=["Mace", "Chain Mail", "Shield"],
    )


def warlock_l3():
    return make_player(
        name="Vex", character_class="Warlock", background="Charlatan",
        level=3, xp=900, gold=40, proficiency_bonus=2, armor_class=14,
        total_hit_points=24, current_hit_points=24, hit_dice_count=3, hit_dice_size=8,
        stats={"str": 8, "dex": 16, "con": 14, "int": 10, "wis": 12, "cha": 17},
        spellcasting={
            "ability": "charisma", "dc": 13, "attack_modifier": 5,
            "cantrips": ["Eldritch Blast"],
            "spells_known": [{"name": "Hex"}, {"name": "Shield"}],
            "spells_prepared": [],
            "slots": {"2": 2},
        },
        inventory=["Leather Armor", "Dagger"],
    )


def rogue_l1():
    return make_player(
        name="Pip", character_class="Rogue", background="Criminal", level=1, xp=0,
        gold=25, proficiency_bonus=2, armor_class=14, total_hit_points=9,
        current_hit_points=9, hit_dice_count=1, hit_dice_size=8,
        stats={"str": 10, "dex": 17, "con": 12, "int": 14, "wis": 10, "cha": 13},
        skills=["Stealth", "Sleight of Hand", "Perception", "Deception"],
        inventory=["Shortsword", "Dagger", "Leather Armor", "Thieves' Tools"],
    )


# ── state fixtures ────────────────────────────────────────────────────────

@contextlib.contextmanager
def load(payload):
    """Install `payload` as the engine's in-memory player DB; clean up after."""
    ds.DB_CONNECTION = None
    ds._COMBAT_REGISTRY = {}
    fd, path = tempfile.mkstemp(suffix=".player")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        status = ds.init_player_db(path)
        assert status.startswith("Database initialized"), status
        yield payload
    finally:
        ds.DB_CONNECTION = None
        ds._COMBAT_REGISTRY = {}
        try:
            os.unlink(path)
        except OSError:
            pass


def db():
    """Full engine DB as a dict."""
    return ds.dump_player_db()


def dbv(key, default=None):
    return db().get(key, default)


# ── deterministic rolls ───────────────────────────────────────────────────

@contextlib.contextmanager
def fixed_rolls(values):
    """Patch `random.randint` to return `values` in order (last value repeats).

    Yields the list of (low, high) bounds the engine actually asked for, so a
    test can assert the dice it requested as well as the value it produced.
    """
    seq = list(values)
    calls = []

    def fake(low, high):
        calls.append((low, high))
        if len(seq) > 1:
            return seq.pop(0)
        return seq[0] if seq else low

    original = random.randint
    random.randint = fake
    try:
        yield calls
    finally:
        random.randint = original


def rolls_always(value):
    """Every `random.randint` returns `value` (e.g. 20 for a forced crit)."""
    return fixed_rolls([value])


# ── base test case ────────────────────────────────────────────────────────

class EngineCase(unittest.TestCase):
    """Installs a fresh engine DB before each test and cleans up after.

    Override `player_factory` (a zero-arg callable returning a `.player` dict).
    """

    player_factory = staticmethod(make_player)

    def setUp(self):
        self.ctx = load(self.player_factory())
        self.player = self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)
