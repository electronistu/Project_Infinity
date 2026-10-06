"""SRD 5.1 equipped-items model: the armour catalog, hands, and derived AC.

Shared by the Forge (`forge/character_creator.py`), the dice engine
(`dice_server.py`, the rules authority) and the web client's sheet builder
(`web/stats.py`), so the three can never disagree — the same arrangement as
`carrying.py`.

Rules implemented (SRD 5.1):

* there are no equipment slots — only **hands** (two of them). A shield is simply
  an item held in one hand and no more than one shield benefits a creature at a
  time; a *two-handed* weapon needs both hands **when you attack with it**, but is
  held in one hand otherwise (so its wielder can still cast with the free hand);
* worn armour: AC = the armour's base + min(DEX modifier, its Dexterity cap);
* only one suit of armour can be worn (the `armor` slot holds at most one item);
* a shield adds its AC bonus (a Monk gains no benefit from one);
* unarmoured: AC = 10 + DEX (Barbarian: + CON, Monk: + WIS) — but only while no
  armour is worn;
* the Defense fighting-style adds +1 only while wearing armour;
* magic variants declare a `base` (their SRD archetype) plus explicit bonuses
  (`ac_bonus`), never a "+1" parsed out of the name.

The `equipped` value in the player database is a dict::

    {"armor": "Chain Mail", "hands": ["Longsword", "Shield"]}

`hands` is a two-element list (main hand, off hand) whose entries may be null.
The inventory entry an equipped name points at may carry `base`, `ac_bonus`,
`damage_dice`, ... — see `dice_server.update_player_list`.
"""

from __future__ import annotations

import json
import os
import re

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced as "nothing known"
    yaml = None

try:  # the carrying module already owns name normalisation (repo root)
    from carrying import normalize
except ImportError:  # pragma: no cover - package-relative import
    from .carrying import normalize

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")

_MAGIC_PREFIX = re.compile(r"^magic\s+")

_ARMOR: dict | None = None
_SHIELDS: set | None = None
_WEAPONS: dict | None = None


def _config(filename: str):
    path = os.path.join(CONFIG_DIR, filename)
    if yaml is None or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or []


def _key(name, table) -> str | None:
    """Normalized catalog key for a name: exact, magic variant, then singular."""
    if not name:
        return None
    norm = normalize(name)
    if norm in table:
        return norm
    stripped = _MAGIC_PREFIX.sub("", norm)
    if stripped != norm and stripped in table:
        return stripped
    if norm.endswith("s") and norm[:-1] in table:
        return norm[:-1]
    return None


def _armor_catalog() -> tuple[dict, set]:
    """({normalized name/alias -> armour entry}, {normalized shield keys}) cached."""
    global _ARMOR, _SHIELDS
    if _ARMOR is None:
        armor: dict[str, dict] = {}
        shields: set[str] = set()
        for raw in _config("armor.yml"):
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            entry = {
                "name": str(raw["name"]),
                "ac": int(raw.get("ac", 10)),
                "dex_cap": raw.get("dex_cap"),
                "type": str(raw.get("type") or "light"),
                "strength_req": raw.get("strength_req"),
                "stealth_disadvantage": bool(raw.get("stealth_disadvantage")),
                "shield": str(raw.get("type")) == "shield",
            }
            for label in [entry["name"], *(raw.get("aliases") or [])]:
                key = normalize(label)
                if not key:
                    continue
                armor[key] = entry
                if entry["shield"]:
                    shields.add(key)
        _ARMOR, _SHIELDS = armor, shields
    return _ARMOR, _SHIELDS


def _weapon_catalog() -> dict:
    """{normalized name -> weapon entry from config/weapons.yml} cached."""
    global _WEAPONS
    if _WEAPONS is None:
        weapons: dict[str, dict] = {}
        for raw in _config("weapons.yml"):
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            entry = dict(raw)
            entry["name"] = str(raw["name"])
            entry["properties"] = [str(p) for p in (raw.get("properties") or [])]
            key = normalize(entry["name"])
            if key:
                weapons[key] = entry
        _WEAPONS = weapons
    return _WEAPONS


def armor_entry(name, base=None) -> dict | None:
    """The armour archetype for an item name (or its declared `base`), else None."""
    table, _ = _armor_catalog()
    return (table.get(_key(base, table) or "") if base else None) \
        or table.get(_key(name, table) or "") or None


def weapon_entry(name, base=None) -> dict | None:
    """The weapon archetype for an item name (or its declared `base`), else None."""
    table = _weapon_catalog()
    return (table.get(_key(base, table) or "") if base else None) \
        or table.get(_key(name, table) or "") or None


def is_shield(name, base=None) -> bool:
    entry = armor_entry(name, base)
    return bool(entry and entry.get("shield"))


def is_armor(name, base=None) -> bool:
    entry = armor_entry(name, base)
    return bool(entry and not entry.get("shield"))


def is_weapon(name, base=None) -> bool:
    return weapon_entry(name, base) is not None


_CLOTHING_KEYWORDS = ("clothes", "clothing", "vestments", "robe")


def is_clothing(name, base=None) -> bool:
    """True for worn clothing (the base layer): Common Clothes, Vestments, Robes, ...

    The Forge's `classify_item` files clothing under generic `gear` together with packs and
    pouches, so clothing is recognised by name here and worn automatically.
    """
    norm = normalize(base or name)
    return any(keyword in norm for keyword in _CLOTHING_KEYWORDS)


# Spellcasting focuses and component pouches satisfy a spell's material components when held.
_FOCUS_NAMES = {"arcane focus", "druidic focus", "holy symbol", "component pouch",
                "spellcasting focus", "sprig of mistletoe", "druidic totem"}
_FOCUS_KEYWORDS = ("focus", "holy symbol", "component pouch", "sprig", "totem")


def is_focus(name, base=None) -> bool:
    """True for a held spellcasting focus / component pouch (covers material components)."""
    norm = normalize(base or name)
    if not norm:
        return False
    if norm in _FOCUS_NAMES:
        return True
    return any(keyword in norm for keyword in _FOCUS_KEYWORDS)


def _declared(inventory, name) -> dict:
    """The inventory entry (dict) an equipped item name points at, else {}."""
    for entry in inventory or []:
        if isinstance(entry, dict) and str(entry.get("name") or "") == str(name):
            return entry
    return {}


def _base_of(inventory, name):
    return _declared(inventory, name).get("base")


def properties_for(name, base=None, declared=None) -> list:
    """An item's weapon properties: a GM declaration wins, else the archetype."""
    if isinstance(declared, dict) and declared.get("properties"):
        props = declared["properties"]
        if isinstance(props, str):
            props = [p.strip() for p in props.split(",")]
        return [str(p) for p in props]
    entry = weapon_entry(name, base)
    return list(entry.get("properties") or []) if entry else []


def has_property(name, prop, base=None, declared=None) -> bool:
    needle = str(prop).strip().lower()
    return any(needle in str(p).lower() for p in properties_for(name, base, declared))


# ── hands ───────────────────────────────────────────────────────────────────

def _two_handed(name, inventory) -> bool:
    declared = _declared(inventory, name)
    return has_property(name, "two-handed", declared.get("base"), declared)


def hands_used(hands, inventory=None, casting=False) -> int:
    """Hands occupied by the items held. A two-handed weapon takes BOTH hands to
    attack with, but only one while casting (SRD: it needs two hands when you
    attack with it, not to hold it)."""
    used = 0
    for name in (hands or []):
        if not name:
            continue
        used += 1 if casting else (2 if _two_handed(name, inventory) else 1)
    return min(used, 2)


# ── armour class ─────────────────────────────────────────────────────────────

def _mod(score) -> int:
    try:
        return (int(score) - 10) // 2
    except (TypeError, ValueError):
        return 0


# ── attunement, paired items, armour proficiency, don/doff times ─────────────

def _attuned_set(attuned) -> set:
    if isinstance(attuned, str):
        return {attuned}
    return {str(a) for a in (attuned or [])}


def _pair_count(name, declared, equipped_names, inventory) -> int:
    """How many equipped items share this item's pair group (`base`, else name)."""
    key = normalize(declared.get("base") or name)
    return sum(1 for other in (equipped_names or [])
               if normalize(_declared(inventory, other).get("base") or other) == key)


def _bonuses_apply(name, declared, attuned, equipped_names, inventory) -> bool:
    """SRD 5.1: an item needing attunement grants its magic only once attuned, and a
    paired item (boots, bracers, gauntlets, gloves) only while both halves are worn."""
    declared = declared if isinstance(declared, dict) else {}
    if declared.get("attunement") and str(name) not in attuned:
        return False
    if declared.get("pair") and _pair_count(name, declared, equipped_names, inventory) < 2:
        return False
    return True


# ── item effects: the general model (SRD 5.1) ─────────────────────────────────
#
# Every magical effect an item grants is a typed entry. Legacy flat fields
# (`ac_bonus`, `attack_bonus`, `damage_bonus`, ...) and the flat authoring sugar
# (`save_bonus`, `set_con`, `str_bonus`, ...) normalize into the same entries, so
# every consumer reads one shape. Explicit `effects` list entries are passed
# through verbatim.

_ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")

# flat field -> canonical single-value effect type
_FLAT_EFFECTS = {
    "ac_bonus": "ac_bonus",
    "attack_bonus": "attack_bonus",
    "damage_bonus": "damage_bonus",
    "save_bonus": "save_bonus",
    "check_bonus": "check_bonus",
    "proficiency_bonus": "proficiency_bonus",
    "spell_attack_bonus": "spell_attack_bonus",
    "spell_dc_bonus": "spell_dc_bonus",
    "hp_per_level": "hp_per_level",
    "hit_die_healing_multiplier": "hit_die_healing_multiplier",
}
_ABILITY_SET_FIELDS = {f"set_{k}": k for k in _ABILITY_KEYS}
_ABILITY_BONUS_FIELDS = {f"{k}_bonus": k for k in _ABILITY_KEYS}

# Effect types the engine understands at all; and the subset it APPLIES today.
# A known-but-not-applied type is reported as unmodelled rather than dropped.
KNOWN_EFFECT_TYPES = {
    "ability_set", "ability_bonus", "proficiency_bonus", "save_bonus", "save_advantage",
    "check_bonus", "skill_bonus", "initiative", "ac_bonus", "ac_set", "damage_resistance",
    "damage_immunity", "condition_immunity", "attack_bonus", "damage_bonus",
    "spell_attack_bonus", "spell_dc_bonus", "weapon_property", "hp_per_level",
    "hit_die_healing_multiplier", "regeneration", "speed", "speed_grant", "grant_proficiency",
}
APPLIED_EFFECT_TYPES = {
    "ability_set", "ability_bonus", "proficiency_bonus", "save_bonus", "check_bonus",
    "ac_bonus", "ac_set", "attack_bonus", "damage_bonus", "spell_attack_bonus",
    "spell_dc_bonus", "save_advantage", "skill_bonus", "initiative", "damage_resistance",
    "damage_immunity", "condition_immunity", "speed", "speed_grant", "grant_proficiency",
    "weapon_property", "hp_per_level", "hit_die_healing_multiplier",
}
# `regeneration` is deliberately NOT applied: it needs a clock the engine does not have; it stays
# declared and is narrated by the GM.


def _to_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def item_effects(declared) -> list[dict]:
    """Normalize an inventory entry into its typed effect entries.

    Legacy/sugar flat fields come first, then any explicit `effects` list. Item-level gating
    (attunement / pair) is applied later by the aggregation helpers, not here.
    """
    declared = declared if isinstance(declared, dict) else {}
    effects: list[dict] = []
    for field, etype in _FLAT_EFFECTS.items():
        value = declared.get(field)
        if value not in (None, 0, False):
            effects.append({"type": etype, "value": _to_int(value)})
    for field, ability in _ABILITY_SET_FIELDS.items():
        value = declared.get(field)
        if value:
            effects.append({"type": "ability_set", "ability": ability, "value": _to_int(value, 10)})
    for field, ability in _ABILITY_BONUS_FIELDS.items():
        value = declared.get(field)
        if value:
            entry = {"type": "ability_bonus", "ability": ability, "value": _to_int(value)}
            cap = declared.get(f"{ability}_bonus_max")
            if cap is not None:
                entry["max"] = _to_int(cap)
            effects.append(entry)
    raw = declared.get("effects")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("type"):
                effects.append(dict(item))
    return effects


def _predicate_known(when) -> bool:
    when = str(when or "always").strip().lower()
    if when in ("", "always", "no_armor", "no_shield", "no_armor_or_shield", "while_holding",
                "requires_items", "vs_spells"):
        return True
    return (when.startswith("vs_damage:") or when.startswith("vs_condition:")
            or when.startswith("context:"))


def effect_applies(effect, ctx) -> bool:
    """Evaluate an effect's `when` predicate against the current equipment state.

    ctx carries at least `armor`, `hands`, `worn`, `inventory`, `equipped_names`; save-scoped
    predicates additionally read `against`, `damage` and `condition`. An unknown predicate is
    treated as NOT satisfied (never silently active).
    """
    when = str((effect or {}).get("when") or "always").strip().lower()
    if when in ("", "always"):
        return True
    ctx = ctx or {}
    inventory = ctx.get("inventory") or []
    armor = ctx.get("armor")
    hands = [h for h in (ctx.get("hands") or []) if h]
    worn = [w for w in (ctx.get("worn") or []) if w]
    equipped_names = ctx.get("equipped_names")
    if equipped_names is None:
        equipped_names = ([armor] if armor else []) + hands + worn
    if when == "no_armor":
        return not armor
    if when == "no_shield":
        return not any(is_shield(h, _base_of(inventory, h)) for h in hands)
    if when == "no_armor_or_shield":
        return (not armor) and not any(is_shield(h, _base_of(inventory, h)) for h in hands)
    if when == "while_holding":
        name = ctx.get("name")
        return bool(name) and name in hands
    if when == "requires_items":
        need = effect.get("items") or effect.get("requires") or []
        if isinstance(need, str):
            need = [need]
        return all(n in equipped_names for n in need)
    if when == "vs_spells":
        return str(ctx.get("against") or "").lower() == "spells"
    if when.startswith("vs_damage:"):
        return str(ctx.get("damage") or "").lower() == when.split(":", 1)[1]
    if when.startswith("vs_condition:"):
        return str(ctx.get("condition") or "").lower() == when.split(":", 1)[1]
    if when.startswith("context:"):
        return str(ctx.get("context") or "").lower() == when.split(":", 1)[1]
    return False


def _active_effects(name, inventory, attuned, equipped_names, ctx) -> list[dict]:
    """This item's effects that are live: its magic applies (attuned/pair) and each `when` holds."""
    declared = _declared(inventory, name)
    effects = item_effects(declared)
    if not effects:
        return []
    if not _bonuses_apply(name, declared, attuned, equipped_names, inventory):
        return []
    local = dict(ctx or {})
    local["name"] = name
    local["inventory"] = inventory
    local["equipped_names"] = list(equipped_names or [])
    return [e for e in effects if effect_applies(e, local)]


def _equip_context(inventory, armor, hands, worn) -> dict:
    hands = [h for h in (hands or []) if h]
    worn = [str(w) for w in (worn or []) if w]
    return {
        "armor": armor,
        "hands": hands,
        "worn": worn,
        "inventory": inventory or [],
        "equipped_names": ([armor] if armor else []) + hands + worn,
    }


def effective_scores(stats, inventory, equipped, attuned=()) -> tuple:
    """(effective scores, {ability: [source item, ...]}) with worn/wielded score modifiers.

    SRD semantics: `ability_bonus` adds (optionally capped at `max`), then `ability_set` acts
    as a floor. Only items whose magic applies (attuned / pair complete) contribute.
    """
    stats = stats if isinstance(stats, dict) else {}
    scores = {k: _to_int(stats.get(k, 10), 10) for k in _ABILITY_KEYS}
    sources: dict[str, list[str]] = {}
    equipped = equipped if isinstance(equipped, dict) else {}
    armor = equipped.get("armor") or None
    hands = _hands_list(equipped.get("hands"))
    worn_raw = equipped.get("worn")
    worn = [str(w) for w in worn_raw if w] if isinstance(worn_raw, list) else []
    ctx = _equip_context(inventory, armor, hands, worn)
    equipped_names = ctx["equipped_names"]
    attuned = _attuned_set(attuned)

    for name in equipped_names:
        for eff in _active_effects(name, inventory, attuned, equipped_names, ctx):
            ability = str(eff.get("ability") or "").lower()[:3]
            if ability not in scores:
                continue
            if eff.get("type") == "ability_set":
                value = _to_int(eff.get("value"), scores[ability])
                if value > scores[ability]:
                    scores[ability] = value
                    sources.setdefault(ability, []).append(name)
            elif eff.get("type") == "ability_bonus":
                value = scores[ability] + _to_int(eff.get("value"))
                if eff.get("max") is not None:
                    value = min(value, _to_int(eff["max"]))
                if value != scores[ability]:
                    scores[ability] = value
                    sources.setdefault(ability, []).append(name)
    return scores, sources


def effect_state(get) -> dict:
    """Aggregate the scalar channels granted by equipped items (the general model).

    Returns effective scores + sources, the item proficiency bonus, save/check bonuses, worn
    attack/damage, spell attack/DC bonuses, and any declared-but-unmodelled effects.
    """
    equipped = get("equipped", None)
    present = isinstance(equipped, dict)
    eq = equipped if present else {}
    armor = eq.get("armor") or None
    hands = _hands_list(eq.get("hands"))
    worn_raw = eq.get("worn")
    worn = [str(w) for w in worn_raw if w] if isinstance(worn_raw, list) else []
    attuned = _attuned_set(get("attuned", []))
    inventory = get("inventory", []) or []
    if not isinstance(inventory, list):
        inventory = []
    stats = get("stats", {}) or {}
    if not isinstance(stats, dict):
        stats = {}
    ctx = _equip_context(inventory, armor, hands, worn)
    equipped_names = ctx["equipped_names"]

    scores, sources = effective_scores(stats, inventory, eq, attuned)
    state = {
        "derived_from_equipped": present,
        "base_scores": {k: _to_int(stats.get(k, 10), 10) for k in _ABILITY_KEYS},
        "scores": scores,
        "score_sources": sources,
        "proficiency_bonus_mod": 0,
        "hp_per_level": 0,
        "hit_die_healing_multiplier": 1.0,
        "initiative_bonus": 0,
        "initiative_advantage": False,
        "save_bonus": 0,
        "check_bonus": 0,
        "spell_attack_bonus": 0,
        "spell_dc_bonus": 0,
        "worn_attack_bonus": 0,
        "worn_damage_bonus": 0,
        "worn_attack_sources": [],
        "worn_damage_sources": [],
        "save_bonus_sources": [],
        "check_bonus_sources": [],
        "granted_proficiencies": [],
        "unmodelled": [],
    }
    for name in equipped_names:
        active = _active_effects(name, inventory, attuned, equipped_names, ctx)
        declared = _declared(inventory, name)
        for eff in item_effects(declared):
            etype = str(eff.get("type") or "")
            is_active = eff in active
            if etype not in KNOWN_EFFECT_TYPES or not _predicate_known(eff.get("when")):
                state["unmodelled"].append({
                    "item": name, "type": etype or "?",
                    "reason": "unknown_effect" if etype not in KNOWN_EFFECT_TYPES
                              else "unknown_predicate"})
                continue
            if etype not in APPLIED_EFFECT_TYPES:
                state["unmodelled"].append({"item": name, "type": etype, "reason": "not_yet_applied"})
                continue
            if not is_active:
                continue
            value = _to_int(eff.get("value"))
            if etype == "proficiency_bonus":
                state["proficiency_bonus_mod"] += value
            elif etype == "save_bonus":
                state["save_bonus"] += value
                state["save_bonus_sources"].append(name)
            elif etype == "check_bonus":
                state["check_bonus"] += value
                state["check_bonus_sources"].append(name)
            elif etype == "spell_attack_bonus":
                state["spell_attack_bonus"] += value
            elif etype == "spell_dc_bonus":
                state["spell_dc_bonus"] += value
            elif etype == "grant_proficiency":
                state["granted_proficiencies"].append(
                    {"category": str(eff.get("category") or "").lower(),
                     "value": str(eff.get("value") or "").lower(), "item": name})
            elif etype == "hp_per_level":
                state["hp_per_level"] += value
            elif etype == "hit_die_healing_multiplier":
                if value:
                    state["hit_die_healing_multiplier"] *= float(value)
            elif etype == "initiative":
                state["initiative_bonus"] += _to_int(eff.get("bonus"))
                if eff.get("advantage"):
                    state["initiative_advantage"] = True
    # Worn (non-hand) attack/damage bonuses (the weapon's own are read from its entry).
    for name in worn:
        for eff in _active_effects(name, inventory, attuned, equipped_names, ctx):
            if eff.get("type") == "attack_bonus":
                state["worn_attack_bonus"] += _to_int(eff.get("value"))
                state["worn_attack_sources"].append(name)
            elif eff.get("type") == "damage_bonus":
                state["worn_damage_bonus"] += _to_int(eff.get("value"))
                state["worn_damage_sources"].append(name)
    return state


# ── defense, speed, and context-aware check bonuses ──────────────────────────

def _active_effect_entries_from_buffs(get) -> list[dict]:
    """Normalize `_active_buff_data` (active spell effects) into typed effect entries.

    Accepts the legacy `{field, delta}` shape (-> kind 'delta') and the typed
    `{kind, field, value}` shape.
    """
    raw = get("_active_buff_data", {}) or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = {}
    out: list[dict] = []
    for spell, entries in (raw.items() if isinstance(raw, dict) else []):
        for entry in (entries or []):
            if not isinstance(entry, dict):
                continue
            if entry.get("kind"):
                out.append({**entry, "spell": spell})
            elif entry.get("delta") is not None:
                out.append({"kind": "delta", "field": entry.get("field"),
                            "value": entry.get("delta"), "spell": spell})
    return out


def defense_state(get) -> dict:
    """The player's damage / condition defenses from equipped items AND active spell effects.

    Returns normalized lists + the source names, for `_apply_player_damage_modifiers` and the
    condition-immunity check.
    """
    equipped = get("equipped", {}) or {}
    inventory = get("inventory", []) or []
    if not isinstance(inventory, list):
        inventory = []
    attuned = _attuned_set(get("attuned", []))
    armor = equipped.get("armor") or None
    hands = _hands_list(equipped.get("hands"))
    worn_raw = equipped.get("worn")
    worn = [str(w) for w in worn_raw if w] if isinstance(worn_raw, list) else []
    ctx = _equip_context(inventory, armor, hands, worn)
    equipped_names = ctx["equipped_names"]
    out = {"damage_resistances": [], "damage_immunities": [], "damage_vulnerabilities": [],
           "condition_immunities": [], "sources": []}

    def add(bucket, value, source):
        value = str(value or "").strip().lower()
        if value and value not in out[bucket]:
            out[bucket].append(value)
            out["sources"].append(f"{source}: {value}")

    for name in equipped_names:
        for eff in _active_effects(name, inventory, attuned, equipped_names, ctx):
            etype = eff.get("type")
            if etype == "damage_resistance":
                add("damage_resistances", eff.get("damage") or eff.get("value"), name)
            elif etype == "damage_immunity":
                add("damage_immunities", eff.get("damage") or eff.get("value"), name)
            elif etype == "condition_immunity":
                add("condition_immunities", eff.get("condition") or eff.get("value"), name)
    for entry in _active_effect_entries_from_buffs(get):
        kind = str(entry.get("kind") or "").lower()
        spell = entry.get("spell")
        if kind in ("resistance", "damage_resistance"):
            add("damage_resistances", entry.get("value"), spell)
        elif kind in ("immunity", "damage_immunity"):
            add("damage_immunities", entry.get("value"), spell)
        elif kind in ("vulnerability", "damage_vulnerability"):
            add("damage_vulnerabilities", entry.get("value"), spell)
        elif kind in ("condition_immunity",):
            add("condition_immunities", entry.get("value"), spell)
    return out


def speed_state(get) -> dict:
    """Derived walking speed and granted movement modes from equipped items (SRD 5.1).

    Passive `speed` effects only: a `minimum` floor (Boots of Striding and Springing), a `bonus`,
    a `multiplier`, `ignore_encumbrance`, and `speed_grant` modes (swim/fly/climb).
    """
    base = _to_int(get("speed", 30), 30)
    equipped = get("equipped", {}) or {}
    inventory = get("inventory", []) or []
    if not isinstance(inventory, list):
        inventory = []
    attuned = _attuned_set(get("attuned", []))
    armor = equipped.get("armor") or None
    hands = _hands_list(equipped.get("hands"))
    worn_raw = equipped.get("worn")
    worn = [str(w) for w in worn_raw if w] if isinstance(worn_raw, list) else []
    ctx = _equip_context(inventory, armor, hands, worn)
    equipped_names = ctx["equipped_names"]
    minimum = None
    bonus = 0
    multiplier = 1.0
    ignore_encumbrance = False
    modes: dict[str, int] = {}
    for name in equipped_names:
        for eff in _active_effects(name, inventory, attuned, equipped_names, ctx):
            etype = eff.get("type")
            if etype == "speed":
                if eff.get("minimum") is not None:
                    minimum = max(minimum or 0, _to_int(eff["minimum"]))
                if eff.get("bonus"):
                    bonus += _to_int(eff["bonus"])
                if eff.get("multiplier"):
                    multiplier *= float(eff["multiplier"])
                if eff.get("ignore_encumbrance"):
                    ignore_encumbrance = True
            elif etype == "speed_grant":
                mode = str(eff.get("mode") or "").strip().lower()
                if mode:
                    modes[mode] = max(modes.get(mode, 0), _to_int(eff.get("value")))
    walk = base
    if minimum is not None:
        walk = max(walk, minimum)
    walk += bonus
    walk = int(walk * multiplier)
    return {"base": base, "walk": walk, "modes": modes,
            "ignore_encumbrance": ignore_encumbrance, "minimum": minimum,
            "bonus": bonus, "multiplier": multiplier}


def check_bonus_for(get, check_name: str = "", context=None) -> tuple:
    """(flat bonus, sources) for an ability/skill check from equipped items.

    Applies `check_bonus` (all checks) and any `skill_bonus` whose `skill` appears in the check
    name; a `context:<tag>` predicate is matched against `context` (e.g. 'climbing').
    """
    equipped = get("equipped", {}) or {}
    inventory = get("inventory", []) or []
    if not isinstance(inventory, list):
        inventory = []
    attuned = _attuned_set(get("attuned", []))
    armor = equipped.get("armor") or None
    hands = _hands_list(equipped.get("hands"))
    worn_raw = equipped.get("worn")
    worn = [str(w) for w in worn_raw if w] if isinstance(worn_raw, list) else []
    ctx = _equip_context(inventory, armor, hands, worn)
    ctx["context"] = context
    equipped_names = ctx["equipped_names"]
    needle = normalize(check_name)
    total = 0
    sources = []
    for name in equipped_names:
        for eff in _active_effects(name, inventory, attuned, equipped_names, ctx):
            etype = eff.get("type")
            if etype == "check_bonus":
                total += _to_int(eff.get("value"))
                sources.append(name)
            elif etype == "skill_bonus":
                skill = normalize(eff.get("skill"))
                if skill and skill in needle:
                    total += _to_int(eff.get("value"))
                    sources.append(name)
    return total, sources


def active_dice(get, field: str) -> list[dict]:
    """Active spell dice bonuses for a field ('saving_throws', 'ability_checks', 'attack_rolls',
    'damage_rolls') as `[{value, spell}]` (Bless +1d4, Bane -1d4, ...)."""
    out = []
    for entry in _active_effect_entries_from_buffs(get):
        if (str(entry.get("kind") or "").lower() == "dice"
                and str(entry.get("field") or "") == field):
            out.append({"value": str(entry.get("value") or ""), "spell": entry.get("spell")})
    return out


def save_advantage_for(get, against=None, damage=None, condition=None) -> tuple:
    """(advantage?, sources) for a saving throw from equipped items, scoped by context.

    `against` (e.g. 'spells'), `damage` (a damage type) and `condition` match the effect's
    `vs_spells` / `vs_damage:<t>` / `vs_condition:<c>` predicate.
    """
    equipped = get("equipped", {}) or {}
    inventory = get("inventory", []) or []
    if not isinstance(inventory, list):
        inventory = []
    attuned = _attuned_set(get("attuned", []))
    armor = equipped.get("armor") or None
    hands = _hands_list(equipped.get("hands"))
    worn_raw = equipped.get("worn")
    worn = [str(w) for w in worn_raw if w] if isinstance(worn_raw, list) else []
    ctx = _equip_context(inventory, armor, hands, worn)
    ctx.update({"against": against, "damage": damage, "condition": condition})
    equipped_names = ctx["equipped_names"]
    sources = []
    for name in equipped_names:
        for eff in _active_effects(name, inventory, attuned, equipped_names, ctx):
            if eff.get("type") == "save_advantage":
                sources.append(name)
    return bool(sources), sources


def bonus_suppressed_reason(name, inventory, attuned=(), equipped_names=None) -> str | None:
    """Why an item's magical effects are not applying, or None when they apply."""
    declared = _declared(inventory, name)
    if not item_effects(declared):
        return None
    equipped_names = equipped_names if equipped_names is not None else [name]
    if declared.get("attunement") and str(name) not in _attuned_set(attuned):
        return "not_attuned"
    if declared.get("pair") and _pair_count(name, declared, equipped_names, inventory) < 2:
        return "pair_incomplete"
    return None


ARMOR_PROFICIENCY = {  # armour `type` -> the proficiency string the Forge stores
    "light": "light armor", "medium": "medium armor", "heavy": "heavy armor", "shield": "shields",
}


def is_proficient(name, base=None, proficiencies=()) -> bool:
    """True when the character is proficient with this armour/shield (unknown gear counts as fine)."""
    entry = armor_entry(name, base)
    if entry is None:
        return True
    needed = ARMOR_PROFICIENCY.get(entry["type"])
    if needed is None:
        return True
    profs = {str(p).strip().lower() for p in (proficiencies or [])}
    return (not profs) or needed in profs


DON_TIMES = {"light": "1 minute", "medium": "5 minutes", "heavy": "10 minutes", "shield": "1 action"}
DOFF_TIMES = {"light": "1 minute", "medium": "1 minute", "heavy": "5 minutes", "shield": "1 action"}


def don_time(name, base=None, doff=False) -> str | None:
    """SRD 5.1 "Getting Into and Out of Armor" time, or None for non-armour."""
    entry = armor_entry(name, base)
    if entry is None:
        return None
    return (DOFF_TIMES if doff else DON_TIMES).get(entry["type"])


def ac_from_parts(stats, character_class, armor=None, hands=None, features=(),
                  inventory=None, attuned=(), worn=()) -> tuple[int, dict]:
    """(armour class, breakdown) computed from the equipped set alone.

    `attuned` is the character's attunement list: an item flagged `attunement` contributes no
    magical bonus until attuned, and an item flagged `pair` needs both halves worn.
    """
    stats = stats if isinstance(stats, dict) else {}
    dex = _mod(stats.get("dex", 10))
    con = _mod(stats.get("con", 10))
    wis = _mod(stats.get("wis", 10))
    feats = {str(f).strip().lower() for f in (features or [])}
    defense = "fighting style: defense" in feats
    cls = str(character_class or "")
    attuned = _attuned_set(attuned)
    equipped_names = ([armor] if armor else []) + [h for h in (hands or []) if h] + [w for w in (worn or []) if w]

    worn_entry = armor_entry(armor, _base_of(inventory, armor)) if armor else None
    shield_name = next((h for h in (hands or []) if h and is_shield(h, _base_of(inventory, h))), None)

    ctx = _equip_context(inventory, armor, hands, worn)

    def ac_bonus_of(item_name):
        return sum(_to_int(e.get("value"))
                   for e in _active_effects(item_name, inventory, attuned, equipped_names, ctx)
                   if e.get("type") == "ac_bonus")

    parts = []
    armor_bonus = ac_bonus_of(armor) if armor else 0
    if worn_entry is not None:
        cap = worn_entry.get("dex_cap")
        if cap is None:
            eff_dex = dex
        elif cap == 0:
            # Heavy armour: no Dexterity at all — and no penalty for a negative modifier either.
            eff_dex = 0
        else:
            eff_dex = min(dex, cap)
        base = int(worn_entry["ac"]) + eff_dex + armor_bonus
        parts.append(f"{worn_entry['name']} {worn_entry['ac']}")
        if armor_bonus:
            parts.append(f"magic +{armor_bonus}")
        if eff_dex:
            parts.append(f"DEX {eff_dex:+d}")
        unarmored = False
    else:
        # Unarmoured candidates: the standard formula plus any `ac_set` item (Robe of the
        # Archmagi, Mage Armor) — SRD: use the formula that gives the highest AC.
        candidates = []
        u_parts = ["unarmoured 10"]
        u_base = 10 + dex
        if dex:
            u_parts.append(f"DEX {dex:+d}")
        if cls == "Barbarian":
            u_base += con
            if con:
                u_parts.append(f"CON {con:+d}")
        elif cls == "Monk":
            u_base += wis
            if wis:
                u_parts.append(f"WIS {wis:+d}")
        candidates.append((u_base, u_parts))
        for name in equipped_names:
            for eff in _active_effects(name, inventory, attuned, equipped_names, ctx):
                if eff.get("type") != "ac_set":
                    continue
                set_base = _to_int(eff.get("base"), 10)
                set_parts = [f"{name} {set_base}"]
                if eff.get("plus_dex"):
                    set_base += dex
                    if dex:
                        set_parts.append(f"DEX {dex:+d}")
                candidates.append((set_base, set_parts))
        base, parts = max(candidates, key=lambda candidate: candidate[0])
        unarmored = True

    shield_bonus = 0
    if shield_name is not None and cls != "Monk":
        shield_declared = _declared(inventory, shield_name)
        shield_entry = armor_entry(shield_name, shield_declared.get("base"))
        shield_bonus = int((shield_entry or {}).get("ac", 2)) + ac_bonus_of(shield_name)
        parts.append(f"shield +{shield_bonus}")

    if defense and worn_entry is not None:
        base += 1
        parts.append("Defense +1")

    # Worn items that are neither armour nor shields (cloaks, rings, ...) add their magic bonus.
    worn_bonus = sum(ac_bonus_of(name) for name in (worn or []) if name)
    if worn_bonus:
        parts.append(f"worn +{worn_bonus}")

    total = base + shield_bonus + worn_bonus
    return total, {
        "armor_class": total,
        "armor": worn_entry["name"] if worn_entry else None,
        "armor_type": worn_entry["type"] if worn_entry else None,
        "unarmored": unarmored,
        "shield": shield_name,
        "shield_bonus": shield_bonus,
        "defense": bool(defense and worn_entry is not None),
        "dex_modifier": dex,
        "breakdown": " = ".join([str(total), " + ".join(parts)]) if parts else str(total),
    }


def armor_class(stats, character_class, equipped, features=(), inventory=None,
                attuned=()) -> int:
    """Convenience AC for a forge-style `equipped` dict."""
    equipped = equipped if isinstance(equipped, dict) else {}
    hands = _hands_list(equipped.get("hands"))
    worn_raw = equipped.get("worn")
    worn = [str(w) for w in worn_raw if w] if isinstance(worn_raw, list) else []
    value, _ = ac_from_parts(stats, character_class, equipped.get("armor"), hands,
                             features, inventory, attuned, worn)
    return value


def _hands_list(hands) -> list:
    out = [None, None]
    if isinstance(hands, (list, tuple)):
        for i, name in enumerate(list(hands)[:2]):
            out[i] = name or None
    return out


# ── the derived equipment block ──────────────────────────────────────────────

def equipment_state(get) -> dict:
    """The `_equipment` block for a character.

    ``get(key, default)`` reads one player-DB value (already JSON-decoded), which
    lets both the engine's ``_db_val`` and the sheet's reader share this function.
    `derived_from_equipped` is False for payloads with no `equipped` key: nothing is
    equipped and no AC is claimed from it.
    """
    equipped = get("equipped", None)
    present = isinstance(equipped, dict)
    eq = equipped if present else {}

    armor = eq.get("armor") or None
    hands = _hands_list(eq.get("hands"))
    worn_raw = eq.get("worn")
    worn = [str(w) for w in worn_raw if w] if isinstance(worn_raw, list) else []
    attuned = _attuned_set(get("attuned", []))

    inventory = get("inventory", []) or []
    if not isinstance(inventory, list):
        inventory = []
    stats = get("stats", {}) or {}
    if not isinstance(stats, dict):
        stats = {}
    character_class = get("character_class", "") or ""
    features = get("features", []) or []
    if not isinstance(features, list):
        features = []

    effect = effect_state(get)
    scores = effect["scores"]
    defense = defense_state(get)
    speed = speed_state(get)
    ac, ac_info = ac_from_parts(scores, character_class, armor, hands, features, inventory,
                                attuned, worn)
    equipped_names = ([armor] if armor else []) + [h for h in hands if h] + worn
    proficiencies = list(get("armor_proficiencies", []) or [])
    for grant in effect["granted_proficiencies"]:
        if grant["category"] in ("armor", "armour"):
            proficiencies.append(grant["value"])

    hand_slots = []
    for index, name in enumerate(hands):
        if not name:
            continue
        base = _base_of(inventory, name)
        hand_slots.append({
            "name": name,
            "slot": "main_hand" if index == 0 else "off_hand",
            "two_handed": _two_handed(name, inventory),
            "shield": is_shield(name, base),
            "armor": is_armor(name, base),
            "base": base,
            "attuned": str(name) in attuned,
        })

    warnings = []
    if hands[0] and hands[1] and _two_handed(hands[0], inventory):
        warnings.append({
            "code": "two_handed_conflict",
            "message": f"{hands[0]} needs two hands to attack with, but {hands[1]} is also held.",
        })
    held_shields = [h for h in hands if h and is_shield(h, _base_of(inventory, h))]
    if len(held_shields) > 1:
        warnings.append({
            "code": "multiple_shields",
            "message": "Only one shield benefits a creature at a time.",
        })
    # Armour / shield proficiency (SRD: disadvantage on STR/DEX checks, saves and attacks, and
    # no spellcasting — one offending item is enough).
    unproficient = [n for n in equipped_names
                    if not is_proficient(n, _base_of(inventory, n), proficiencies)
                    and (is_armor(n, _base_of(inventory, n)) or is_shield(n, _base_of(inventory, n)))]
    for name in unproficient:
        entry = armor_entry(name, _base_of(inventory, name))
        warnings.append({
            "code": "armor_not_proficient",
            "message": f"Not proficient with {entry['type']} armour ({name}) — disadvantage on "
                       f"STR/DEX checks, saves and attacks, and no spellcasting.",
        })
    # Magic bonuses held back by attunement or an incomplete pair.
    for name in equipped_names:
        reason = bonus_suppressed_reason(name, inventory, attuned, equipped_names)
        if reason == "not_attuned":
            warnings.append({
                "code": "not_attuned",
                "message": f"{name} requires attunement — its magical bonuses do not apply yet.",
            })
        elif reason == "pair_incomplete":
            warnings.append({
                "code": "pair_incomplete",
                "message": f"{name} is one of a pair — it grants no benefit unless both halves "
                           f"are worn.",
            })
    if armor and not is_armor(armor, _base_of(inventory, armor)):
        warnings.append({
            "code": "unknown_archetype",
            "message": f"No armour archetype is known for '{armor}' — declare a base or its ac.",
        })
    for item in effect["unmodelled"]:
        warnings.append({
            "code": "unmodelled_effect",
            "message": f"{item['item']} declares a '{item['type']}' effect the engine does not "
                       f"apply yet ({item['reason']}) — narrate it by hand.",
        })

    return {
        "derived_from_equipped": present,
        "armor": armor,
        "armor_type": ac_info["armor_type"],
        "hands": hand_slots,
        "main_hand": hands[0],
        "off_hand": hands[1],
        "hands_free": 2 - hands_used(hands, inventory),
        "hands_free_casting": 2 - hands_used(hands, inventory, casting=True),
        "base_ac": ac,
        "ac_breakdown": ac_info["breakdown"],
        "unarmored": ac_info["unarmored"],
        "shield": ac_info["shield"],
        "shield_bonus": ac_info["shield_bonus"],
        "defense": ac_info["defense"],
        "armor_proficient": not unproficient,
        "proficiency_sources": [f"not proficient with {n}" for n in unproficient],
        "attuned": sorted(attuned),
        "attunement_slots_free": max(0, 3 - len(attuned)),
        "effective_stats": scores,
        "base_stats": effect["base_scores"],
        "score_sources": effect["score_sources"],
        "save_bonus": effect["save_bonus"],
        "save_bonus_sources": effect["save_bonus_sources"],
        "check_bonus": effect["check_bonus"],
        "check_bonus_sources": effect["check_bonus_sources"],
        "proficiency_bonus_mod": effect["proficiency_bonus_mod"],
        "granted_proficiencies": effect["granted_proficiencies"],
        "defenses": defense,
        "speed_state": speed,
        "unmodelled_effects": effect["unmodelled"],
        "worn": [{"name": name, "slot": "worn",
                  "kind": (str(_declared(inventory, name).get("kind") or "").lower() or None),
                  "attuned": name in attuned} for name in worn],
        "warnings": warnings,
    }


# ── starting equipment (the Forge) ───────────────────────────────────────────

def pick_starting_equipped(inventory) -> dict:
    """Choose what a freshly created character is wearing/wielding.

    `inventory` is a sequence of item names (or objects with a `.name`). The best armour is worn,
    clothing (Common Clothes, Vestments, Robes, ...) is worn as the base layer, a weapon goes to the
    main hand and a shield to the off hand. A two-handed weapon leaves the off hand empty (a shield
    stays in the pack). Always returns a well-formed `{armor, hands, worn}` (entries may be null).
    """
    names = []
    for item in inventory or []:
        name = getattr(item, "name", None) or (item if isinstance(item, str) else None)
        if name:
            names.append(str(name))

    best_armor, best_ac = None, -1
    for name in names:
        entry = armor_entry(name)
        if entry and not entry.get("shield") and int(entry["ac"]) > best_ac:
            best_armor, best_ac = name, int(entry["ac"])

    weapon_names = [n for n in names if is_weapon(n)]
    shield = next((n for n in names if is_shield(n)), None)
    worn = [n for n in names if is_clothing(n)]

    main, off = None, None
    if weapon_names:
        # Prefer a one-handed weapon when a shield is in the pack, so both hands work.
        chosen = weapon_names[0]
        if shield is not None:
            chosen = next((n for n in weapon_names if not _two_handed(n, names)), weapon_names[0])
        main = chosen
        if not _two_handed(chosen, names):
            off = shield
    elif shield is not None:
        main = shield

    return {"armor": best_armor, "hands": [main, off], "worn": worn}


# ── back-compatibility exports (the Forge's classify_item / old callers) ─────

def _armor_data() -> dict:
    """`{name or alias as written: {ac, dex_cap, type}}` — the old ARMOR_DATA shape."""
    out: dict[str, dict] = {}
    for raw in _config("armor.yml"):
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        entry = {"ac": int(raw.get("ac", 10)), "dex_cap": raw.get("dex_cap"),
                 "type": str(raw.get("type") or "light")}
        for label in [str(raw["name"]), *(raw.get("aliases") or [])]:
            out[label] = entry
    return out


def _shield_names() -> set:
    out = set()
    for raw in _config("armor.yml"):
        if isinstance(raw, dict) and str(raw.get("type")) == "shield":
            for label in [str(raw.get("name")), *(raw.get("aliases") or [])]:
                out.add(label)
    return out


ARMOR_DATA = _armor_data()
SHIELD_NAMES = _shield_names()
