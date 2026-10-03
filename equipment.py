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


def bonus_suppressed_reason(name, inventory, attuned=(), equipped_names=None) -> str | None:
    """Why an item's magical bonus is not applying, or None when it applies."""
    declared = _declared(inventory, name)
    if not (declared.get("ac_bonus") or declared.get("attack_bonus") or declared.get("damage_bonus")):
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

    parts = []
    armor_declared = _declared(inventory, armor) if armor else {}
    armor_bonus = int(armor_declared.get("ac_bonus") or 0) \
        if _bonuses_apply(armor, armor_declared, attuned, equipped_names, inventory) else 0
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
        base = 10 + dex
        parts.append("unarmoured 10")
        if dex:
            parts.append(f"DEX {dex:+d}")
        unarmored = True
        if cls == "Barbarian":
            base += con
            if con:
                parts.append(f"CON {con:+d}")
        elif cls == "Monk":
            base += wis
            if wis:
                parts.append(f"WIS {wis:+d}")

    shield_bonus = 0
    if shield_name is not None and cls != "Monk":
        shield_declared = _declared(inventory, shield_name)
        shield_entry = armor_entry(shield_name, shield_declared.get("base"))
        shield_bonus = int((shield_entry or {}).get("ac", 2))
        if _bonuses_apply(shield_name, shield_declared, attuned, equipped_names, inventory):
            shield_bonus += int(shield_declared.get("ac_bonus") or 0)
        parts.append(f"shield +{shield_bonus}")

    if defense and worn_entry is not None:
        base += 1
        parts.append("Defense +1")

    # Worn items that are neither armour nor shields (cloaks, rings, ...) add their magic bonus.
    worn_bonus = 0
    for name in (worn or []):
        if not name:
            continue
        declared = _declared(inventory, name)
        if _bonuses_apply(name, declared, attuned, equipped_names, inventory):
            worn_bonus += int(declared.get("ac_bonus") or 0)
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

    ac, ac_info = ac_from_parts(stats, character_class, armor, hands, features, inventory,
                                attuned, worn)
    equipped_names = ([armor] if armor else []) + [h for h in hands if h] + worn
    proficiencies = get("armor_proficiencies", []) or []

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
