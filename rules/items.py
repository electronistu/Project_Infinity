"""Equipped items and worn gear: the worn set, its bonuses, and item validation.

The equipped-items model itself is `equipment.py`; this module holds the engine's readers and
writers over the `equipped` slot map plus the weapon attack the worn bonuses feed.
"""

import re

import device
import equipment

from .consts import WORN_EXCLUSIVE, WORN_KINDS
from .db import (
    _ability_mod, _db_set, _db_val, _effective_prof_bonus, _effective_stats, _item_effect_state,
    _recompute_armor_class
)


# ── equipped items / derived armour class (SRD 5.1) ───────────────────────────

def _clear_equipped(cursor, name) -> bool:
    """Drop an item from the equipped set (it left the inventory). True if changed.

    Keeps `equipped` from dangling on a name that is no longer carried, and recomputes
    the armour class from what remains.
    """
    equipped = _db_val(cursor, "equipped", None)
    if not isinstance(equipped, dict):
        return False
    changed = False
    if equipped.get("armor") == name:
        equipped["armor"] = None
        changed = True
    hands = equipped.get("hands")
    if isinstance(hands, list):
        hands = (list(hands) + [None, None])[:2]
        for index, held in enumerate(hands):
            if held == name:
                hands[index] = None
                changed = True
        equipped["hands"] = hands
    worn = equipped.get("worn")
    if isinstance(worn, list) and name in worn:
        equipped["worn"] = [w for w in worn if w != name]
        changed = True
    if changed:
        _db_set(cursor, "equipped", equipped)
        _recompute_armor_class(cursor)
    return changed


def _rename_equipped(cursor, old, new) -> bool:
    """Follow a renamed inventory item through the equipped set and attunement (True if changed).

    A rename must not orphan the item from how it is worn/wielded or from its attunement.
    """
    changed = False
    equipped = _db_val(cursor, "equipped", None)
    if isinstance(equipped, dict):
        if equipped.get("armor") == old:
            equipped["armor"] = new
            changed = True
        hands = equipped.get("hands")
        if isinstance(hands, list):
            hands = (list(hands) + [None, None])[:2]
            for index, held in enumerate(hands):
                if held == old:
                    hands[index] = new
                    changed = True
            equipped["hands"] = hands
        worn = equipped.get("worn")
        if isinstance(worn, list) and old in worn:
            equipped["worn"] = [new if w == old else w for w in worn]
            changed = True
        if changed:
            _db_set(cursor, "equipped", equipped)
            _recompute_armor_class(cursor)
    attuned = _db_val(cursor, "attuned", None)
    if isinstance(attuned, list) and old in attuned:
        _db_set(cursor, "attuned", [new if a == old else a for a in attuned])
        changed = True
    return changed


def _worn_group(declared) -> str | None:
    """The SRD "same kind" wearing group for an item (None when unconstrained)."""
    kind = str((declared or {}).get("kind") or "").strip().lower()
    return WORN_EXCLUSIVE.get(kind)


def _inventory_entry(inventory, name) -> dict:
    """The inventory entry (dict) an item name points at, else {}."""
    return next((e for e in (inventory or [])
                 if isinstance(e, dict) and e.get("name") == name), {})


def _is_worn_slot(item, declared) -> bool:
    """True when equipping should put the item in the `worn` container rather than a hand.

    5e has no slots, but clothing, cloaks, boots, gloves, bracers, headwear, rings and amulets are
    worn rather than held — they need somewhere to live so attunement and pairing rules have
    something to bind to.
    """
    if equipment.is_weapon(item) or equipment.is_shield(item) or equipment.is_armor(item):
        return False
    kind = str((declared or {}).get("kind") or "").strip().lower()
    return kind in WORN_KINDS or equipment.is_clothing(item)


def _reject_item(error: str, reason: str, item: str, nudge: str, **extra) -> dict:
    """A refused inventory add: the GM is told exactly how to call the tool correctly."""
    result = {"success": False, "error": error, "item": item, "reason": reason,
              "gm_instruction": nudge}
    result.update(extra)
    return result


def _device_add_entry(cursor, name: str, desc: str, weight):
    """The Device vocabulary, enforced: an add is either engine-owned or refused.

    - The Device itself is engine-owned. It is seeded with the character; a second one
      (however it is spelled) is refused. If it was removed in play, the GM may restore it
      and the engine writes the description.
    - A part is accepted only by its exact canonical name; the engine supplies the
      description and stamps the age it was recovered in. Never a numeric edit -- the
      inventory entry is the only record of where a part came from.

    Returns ``{"entry": {...}}`` for an accepted device add, ``{"reject": {...}}`` for a
    refusal, or None when the name is not a Device thing at all.
    """
    era = str(_db_val(cursor, "era", "") or "").strip().lower()
    inventory = _db_val(cursor, "inventory", []) or []
    if device.is_device(name):
        if device.has_device(inventory):
            return {"reject": _reject_item(
                "device_engine_owned",
                f"'{device.device_name()}' is created with the Traveller and owned by the engine.",
                name,
                "Do not add or rename the Device -- it is already in the inventory. To recover "
                "a part, add it by its exact name, e.g. update_player_list(key='inventory', "
                "item='Escapement', action='add').")}
        # Removed in play (the GM decides): restoring it is allowed, engine-described.
        return {"entry": device.device_entry()}
    part = device.canonical_part(name)
    if part:
        if not era:
            return {"reject": _reject_item(
                "device_not_in_this_game",
                f"'{part}' is a part of the Device, which exists only in the Time Traveler game.",
                part,
                "This is a classic world -- there is no Device and no parts. Do not add one.")}
        if device.entry_for_part(inventory, part) is not None:
            return {"reject": _reject_item(
                "already_exists", f"'{part}' is already in the inventory.", part,
                "A part can only be recovered once.")}
        return {"entry": device.part_entry(part, era)}
    return None


def _validate_item_add(name: str, declared: dict, weight) -> dict | None:
    """Reject a combat-relevant inventory add that is missing required stats (SRD 5.1).

    The engine derives attack/damage/AC from the declared fields, so a weapon or armour that is
    neither a known SRD archetype nor explicitly statted cannot be used. Returns None when the add
    is acceptable.
    """
    base = declared.get("base")
    known_archetype = bool(base) and (equipment.is_weapon(base) or equipment.is_armor(base)
                                      or equipment.is_shield(base))
    has_stats = bool(declared.get("damage_dice") or declared.get("ac"))
    has_bonus = bool(equipment.item_effects(declared))
    kind = str(declared.get("kind") or "").strip().lower()

    # 1. An unknown `base` with nothing else to derive from is unusable. A declared bonus or kind
    #    is enough for a homebrew worn item (e.g. base='Ring of Protection', kind='ring').
    if base and not known_archetype and not has_stats and not has_bonus:
        return _reject_item(
            "unknown_base_no_stats",
            f"Unknown base archetype '{base}' and no explicit stats — the engine cannot derive "
            f"hands, weight or combat stats from it.", name,
            f"Re-add with a real SRD base (e.g. base='Dagger'), or declare the stats: "
            f"update_player_list(key='inventory', item='{name}', action='add', "
            f"damage_dice='1d4', damage_type='piercing', properties=['Finesse']).",
            unknown_base=base)

    # 2. Weapon intent (dice/type/properties) with no damage dice and no resolvable archetype.
    if (declared.get("damage_type") or declared.get("properties")) and not declared.get("damage_dice"):
        wpn = equipment.weapon_entry(name, base) or {}
        if not wpn.get("damage"):
            return _reject_item(
                "weapon_stats_missing",
                f"No damage is known for '{name}' — declare damage_dice or a known weapon base.",
                name,
                f"Re-add as a weapon: update_player_list(key='inventory', item='{name}', "
                f"action='add', base='Dagger', damage_dice='1d4', damage_type='piercing', "
                f"properties=['Finesse','Light']).")

    # 3. A worn magic item (bonus or paired) with no `kind` would be held, not worn.
    worn_intent = has_bonus or bool(declared.get("pair"))
    if worn_intent and not kind:
        resolves_worn = (equipment.is_weapon(name, base) or equipment.is_armor(name, base)
                         or equipment.is_shield(name, base) or equipment.is_clothing(name, base))
        if not resolves_worn:
            return _reject_item(
                "worn_kind_missing",
                f"'{name}' grants a magic bonus but has no `kind`, so it would be held rather "
                f"than worn and its bonus would not apply.", name,
                f"Declare the slot: update_player_list(key='inventory', item='{name}', "
                f"action='add', kind='ring' (or 'amulet', 'cloak', 'boots', 'gloves', "
                f"'gauntlets', 'bracers', 'headwear', 'belt'), save_bonus=<n>).")
    return None


def _is_light_melee(name, inventory) -> bool:
    """True for a light melee weapon (SRD two-weapon fighting needs one in each hand)."""
    entry = next((e for e in (inventory or []) if isinstance(e, dict) and e.get("name") == name), {})
    base = entry.get("base")
    wpn = equipment.weapon_entry(name, base) or {}
    props = [str(p).lower() for p in equipment.properties_for(name, base, entry)]
    return bool(wpn.get("melee", True)) and any(p.startswith("light") for p in props)


def _worn_item_bonuses(cursor, inventory):
    """Sum worn items' attack/damage bonuses (SRD 5.1), by attunement/pair gating.

    A thin projection of the general effect model (`equipment.effect_state`); returns
    (attack, damage, sources).
    """
    state = _item_effect_state(cursor)
    attack = int(state.get("worn_attack_bonus") or 0)
    damage = int(state.get("worn_damage_bonus") or 0)
    sources = list(state.get("worn_attack_sources") or [])
    for src in state.get("worn_damage_sources") or []:
        if src not in sources:
            sources.append(src)
    return attack, damage, sources


def _derive_weapon_attack(cursor, weapon: str, inventory: list, hands_free: int = 1,
                          off_hand: bool = False) -> dict:
    """Derived attack/damage for a player's equipped weapon (SRD 5.1).

    The item's declared stats (damage_dice/damage_type/attack_bonus/damage_bonus) win over its SRD
    archetype (`base` or its own name). Ability: Finesse -> the better of STR/DEX, otherwise a melee
    weapon uses STR and a ranged one DEX. Proficiency adds the proficiency bonus (and a warning
    when the character is not proficient, SRD: no bonus). A Versatile weapon uses its two-handed die
    while the other hand is free, and an off-hand (two-weapon fighting) attack takes no ability
    modifier to damage unless that modifier is negative. Magic bonuses apply only while the item is
    attuned / its pair is complete.
    """
    entry = next((e for e in inventory if isinstance(e, dict) and e.get("name") == weapon), {})
    base = entry.get("base")
    wpn = equipment.weapon_entry(weapon, base) or {}
    props = [str(p) for p in equipment.properties_for(weapon, base, entry)]
    for eff in equipment.item_effects(entry):
        if eff.get("type") == "weapon_property" and eff.get("property"):
            props.append(str(eff["property"]))
    low = [p.lower() for p in props]
    melee = bool(wpn.get("melee", True))
    stats = _effective_stats(cursor)
    str_mod = _ability_mod(stats.get("str", 10))
    dex_mod = _ability_mod(stats.get("dex", 10))
    if any("finesse" in p for p in low):
        ability, ability_mod = (("dexterity", dex_mod) if dex_mod >= str_mod else ("strength", str_mod))
    elif melee:
        ability, ability_mod = "strength", str_mod
    else:
        ability, ability_mod = "dexterity", dex_mod

    proficiencies = {str(p).strip().lower() for p in (_db_val(cursor, "weapon_proficiencies", []) or [])}
    for grant in (_item_effect_state(cursor).get("granted_proficiencies") or []):
        if str(grant.get("category") or "") in ("weapon", "weapons"):
            proficiencies.add(str(grant.get("value") or "").lower())
    category = str(wpn.get("category") or "").lower()
    # Proficiency is a fact about the weapon's KIND, not its display name: a magic or
    # reflavoured weapon ("Dagger +1", "Storm's Edge" base Longsword) still matches its
    # base (or category) proficiency.
    base_name = str(base or weapon).strip().lower()
    proficient = (not proficiencies
                  or f"{category} weapons" in proficiencies
                  or base_name in proficiencies
                  or weapon.strip().lower() in proficiencies)
    prof_bonus = _effective_prof_bonus(cursor)

    # Magic bonuses: attunement / paired-item gating, from the weapon itself and any worn item.
    equipped = _db_val(cursor, "equipped", {}) or {}
    worn = [str(w) for w in (equipped.get("worn") or []) if w]
    equipped_names = ([equipped.get("armor")] if equipped.get("armor") else []) \
        + [h for h in (equipped.get("hands") or []) if h] + worn
    suppressed = equipment.bonus_suppressed_reason(weapon, inventory,
                                                   _db_val(cursor, "attuned", []) or [],
                                                   equipped_names)
    weapon_effects = equipment.item_effects(entry)
    attack_bonus = sum(int(e.get("value") or 0) for e in weapon_effects
                       if e.get("type") == "attack_bonus") if suppressed is None else 0
    damage_bonus = sum(int(e.get("value") or 0) for e in weapon_effects
                       if e.get("type") == "damage_bonus") if suppressed is None else 0
    worn_attack, worn_damage, worn_sources = _worn_item_bonuses(cursor, inventory)

    # Damage dice: a declared value wins; a Versatile weapon uses its two-handed die with a free hand.
    declared_dice = entry.get("damage_dice")
    damage_dice = declared_dice or wpn.get("damage")
    versatile_die = None
    for prop in props:
        if prop.lower().startswith("versatile"):
            match = re.search(r"(\d+d\d+)", prop)
            if match:
                versatile_die = match.group(1)
    if not declared_dice and versatile_die and hands_free >= 1:
        damage_dice = versatile_die

    damage_modifier = ability_mod + damage_bonus + worn_damage
    if off_hand:
        # Two-weapon fighting: no ability modifier on the bonus attack's damage unless negative.
        damage_modifier = min(ability_mod, 0) + damage_bonus + worn_damage

    return {
        "attack_modifier": ability_mod + (prof_bonus if proficient else 0) + attack_bonus + worn_attack,
        "damage_dice": str(damage_dice) if damage_dice else None,
        "damage_modifier": damage_modifier,
        "damage_type": entry.get("damage_type") or wpn.get("damage_type") or "",
        "ability": ability,
        "ability_modifier": ability_mod,
        "proficient": proficient,
        "melee": melee,
        "properties": props,
        "two_handed": any("two-handed" in p for p in low),
        "light_melee": melee and any(p.startswith("light") for p in low),
        "versatile_die": versatile_die,
        "bonus_suppressed": suppressed,
        "item_bonus": {
            "attack": attack_bonus + worn_attack,
            "damage": damage_bonus + worn_damage,
            "sources": ([weapon] if (attack_bonus or damage_bonus) else []) + worn_sources,
        },
    }


def _covers_material(cursor, held_names) -> bool:
    """True when a held item covers a spell's material components (focus or component pouch).

    A Cleric's or Paladin's holy symbol can be borne on a shield, so their shield counts as a focus.
    """
    if any(equipment.is_focus(name) for name in held_names or []):
        return True
    character_class = str(_db_val(cursor, "character_class", "") or "")
    if character_class in ("Cleric", "Paladin"):
        return any(equipment.is_shield(name) for name in held_names or [])
    return False
