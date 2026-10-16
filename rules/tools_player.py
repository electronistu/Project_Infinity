"""Player-state MCP tools: the sheet's numeric and list edits, equipment and
attunement, the reference `lookup`, and the debug/field readers."""

import json
import carrying
import device
import equipment
from .app import mcp
from . import state
from .combat import _blocked_action, _companions_store, _in_active_combat
from .consts import SRD_CLASSES
from .db import (
    _db_set, _db_val, _equipment_block, _recompute_armor_class, get_nested_value,
    set_nested_value
)
from .items import (
    _clear_equipped, _device_add_entry, _inventory_entry, _is_worn_slot, _rename_equipped,
    _validate_item_add, _worn_group
)
from .reputation import _reputation_error, _resolve_reputation_bucket
from .sheet import _ac_note, _carry_block, _player_gm_view
from .spell_knowledge import build_prepared_spells_info
from .vitals import _get_concentration, _remove_active_effect, apply_numeric_change

@mcp.tool()
def modify_player_numeric(key: str, delta: int) -> dict:
    """Add to (or subtract from) a numeric player field, by dotted path.

    WHEN: gold, HP, XP, slots, consumables or capacity change.
    FIELDS:
    - key: dotted path, e.g. 'gold', 'spellcasting.slots.1', 'consumables.Bolts'.
    - delta: integer, negative to decrement.

    RULES:
    - current_hit_points: clamped to [0, total_hit_points]; at 0 returns death_saves and Unconscious.
    - spellcasting.slots.N: availability validated; available slots returned on error.
    - consumables.ITEM: auto-created at 0; auto-removed with DEPLETION at 0 or below (never negative).
    - xp: crossing a threshold auto-applies level, proficiency, hit dice, HP, slots, DC and attack modifier. Class features, cantrips/spells known, ASIs and subclass features stay manual.
    - capacity_multiplier: scales carrying capacity (2 for Bull's Strength, back to 1 when it ends; a long rest resets it).

    EX:
    modify_player_numeric(key='gold', delta=-10)
    modify_player_numeric(key='xp', delta=50)
    """
    return apply_numeric_change(key, delta)


@mcp.tool()
def update_player_list(key: str, item: str, action: str, weight: float | None = None,
                       base: str | None = None,
                       damage_dice: str | None = None,
                       damage_type: str | None = None,
                       properties: list[str] | None = None,
                       ac: int | None = None,
                       dex_cap: int | None = None,
                       strength_req: int | None = None,
                       ac_bonus: int | None = None,
                       attack_bonus: int | None = None,
                       damage_bonus: int | None = None,
                       attunement: bool | None = None,
                       attunement_by: str | None = None,
                       kind: str | None = None,
                       pair: bool | None = None,
                       save_bonus: int | None = None,
                       check_bonus: int | None = None,
                       proficiency_bonus: int | None = None,
                       spell_attack_bonus: int | None = None,
                       spell_dc_bonus: int | None = None,
                       set_str: int | None = None, set_dex: int | None = None,
                       set_con: int | None = None, set_int: int | None = None,
                       set_wis: int | None = None, set_cha: int | None = None,
                       str_bonus: int | None = None, dex_bonus: int | None = None,
                       con_bonus: int | None = None, int_bonus: int | None = None,
                       wis_bonus: int | None = None, cha_bonus: int | None = None,
                       str_bonus_max: int | None = None, dex_bonus_max: int | None = None,
                       con_bonus_max: int | None = None, int_bonus_max: int | None = None,
                       wis_bonus_max: int | None = None, cha_bonus_max: int | None = None,
                       effects: list[dict] | None = None,
                       appearance: str | None = None,
                       description: str | None = None,
                       new_name: str | None = None) -> dict:
    """Add, remove or edit one player-list entry (inventory, spells, reputation, active effects).

    WHEN: gear/items/spells/reputation/effects change.
    FIELDS (only the non-obvious; declare a weapon's/armour's stats WITH the add):
    - key: the dotted list path to edit; its list fields are the player fields `read_player_field` lists (e.g. 'inventory', 'spellcasting.spells_known', 'reputation.KINGDOM.FACTION').
    - item: add -> 'Name' or 'Name: Description'; remove -> name only.
    - action: 'add' | 'remove' | 'update'.
    - description: the item's flavour/state text -- on add AND update; preferred over the inline 'Name: Description' form. The NAME is identity; transient state (sealed/opened, lit, half-full, emptied, broken) belongs here.
    - base: the SRD archetype the item is built on (e.g. base='Dagger'). Set it on EVERY weapon and suit of armour so properties/hands/weight/AC resolve even when the name is reflavoured.
    - damage_dice/damage_type/properties: a weapon's stats. ac/dex_cap/strength_req: an armour's stats.
    - weight: pounds, only for items the SRD weight catalog does not know (magic items, loot, homebrew).
    - ac_bonus/attack_bonus/damage_bonus: magic bonuses, declared explicitly (never parsed from a '+1' name); they apply only while attuned (and, for a pair, only while both halves are worn).
    - save_bonus/check_bonus: a flat bonus to ALL saves / ALL checks -- the engine applies it; never add it yourself.
    - effects: typed list for anything the flat fields cannot express; the full type + `when` vocabulary: lookup('ref:update_player_list'). Ability scores: ability_set {ability, value} (a floor) or ability_bonus {ability, value, max}.
    - attunement: True when the item's magic needs attunement; attunement_by names a prerequisite (class, spellcaster, creature type, alignment). Bonuses declared WITHOUT attunement apply immediately.
    - kind: the worn slot -- armor, cloak, boots, gloves, gauntlets, bracers, headwear, ring, amulet, belt, other. Required for a worn magic item so it is donned, not held.
    - pair: True when this entry is ONE HALF of a paired item. Add both halves sharing the same `base`; a single half grants no bonus.
    - appearance: (active_effects adds) the look the character takes on; the image uses it INSTEAD of the portrait, and a known covering disguise also replaces the depicted clothing/armour/weapons.
    - new_name: (update only) rename in place; declared stats are kept.

    RULES:
    - Declare `base` + the damage/ac fields + `properties` + `kind` + any bonuses in the SAME add: the engine derives attacks, damage, AC, saves, ability scores and attunement from them, and a declared value wins over the catalog. A weapon with no damage and no known base cannot attack.
    - Prepared casters: capacity enforced (max = spellcasting mod + level).
    - Removing an active_effect auto-reverts its stat deltas.
    - Consumables: NEVER here -- use modify_player_numeric(key='consumables.ITEM', delta=N).
    - Reputation: lowercase, no apostrophes; a missing faction under a known polity is created; a bare 'reputation.POLITY' writes to 'others'; unknown polities are rejected.
    - Removing a worn/wielded inventory item auto-unequips it; a replacement is NOT auto-equipped.

    EX:
    update_player_list(key='inventory', item='Dagger: a rusty blade', action='add', base='Dagger', damage_dice='1d4', damage_type='piercing', properties=['Finesse','Light','Thrown (range 20/60)'])
    update_player_list(key='inventory', item='Ring of Warding: a band of cold silver', action='add', base='Ring of Protection', kind='ring', ac_bonus=1, save_bonus=1, attunement=True)
    update_player_list(key='inventory', item="the stranger's letter", action='update', description="a folded scrap, the grey seal broken")
    """
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized.", "key": key}
    try:
        cursor = state.DB_CONNECTION.cursor()

        if '.' in key:
            root_key = key.split('.')[0]
            cursor.execute("SELECT value FROM player WHERE key = ?", (root_key,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("SELECT key FROM player")
                available = [r[0] for r in cursor.fetchall()]
                return {"success": False, "error": f"Root key '{root_key}' not found.", "available_keys": available, "key": key}

            data = json.loads(row[0])
            path_in_obj = key[len(root_key)+1:]

            era_now = str(_db_val(cursor, "era", "") or "").strip()
            if root_key == "reputation" and era_now:
                # Reputation is era-scoped *in the era game*: resolve through the era FIRST
                # there, so a flat map cannot be written to by accident -- the generic nested
                # path would happily find it. A save with no era is the classic game and keeps
                # the flat map it has always had. Every action resolves here, not just `add`:
                # `update` and `remove` used to fall through to the raw short path and fail.
                current_list, path_in_obj = _resolve_reputation_bucket(
                    data, path_in_obj, era_now, create=(action == "add"))
                if current_list is None:
                    return _reputation_error(key, data, era_now)
            else:
                current_list = get_nested_value(data, path_in_obj)
                if current_list is None or not isinstance(current_list, list):
                    return {"success": False, "error": f"Key '{key}' not found or is not a list.", "available_nested_keys": list(data.keys()), "key": key}
        else:
            cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("SELECT key FROM player")
                available = [r[0] for r in cursor.fetchall()]
                return {"success": False, "error": f"Key '{key}' not found in database.", "available_keys": available, "key": key}
            try:
                current_list = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                current_list = [row[0]]

        is_prepared_spells = (key == "spellcasting.spells_prepared")
        added_name = None
        removed_equipped = False
        device_owned = False
        # Encumbrance is reported only when this change TIPS the character into it (SRD 5.1
        # variant) — never the default "unencumbered" line.
        before_carry_status = None
        if key == "inventory":
            try:
                before_carry_status = (_carry_block(cursor) or {}).get("status")
            except Exception:  # noqa: BLE001 - never fail a list write over reporting
                before_carry_status = None

        if action == "add":
            name = item
            desc = ""
            if ":" in item:
                name, desc = [p.strip() for p in item.split(":", 1)]
            # An explicit `description=` wins over the inline 'Name: Description' form
            # (the GM usually passes the field rather than embedding it in `item`).
            if isinstance(description, str) and description.strip():
                desc = description.strip()
            added_name = name

            new_entry = {"name": name, "description": desc} if (desc or ":" in item) else name
            declared = {}
            if key == "inventory":
                # The Device and its four parts: a closed, engine-owned vocabulary. A part is
                # accepted only by its exact name and gets the engine description + the age it
                # was recovered in; the Device itself may only be restored, never invented.
                device_add = _device_add_entry(cursor, name, desc, weight)
                if device_add is not None:
                    if device_add.get("reject"):
                        return device_add["reject"]
                    new_entry = device_add["entry"]
                    device_owned = True
                    name = str(new_entry.get("name") or name)
                    added_name = name
                else:
                    if weight is not None:
                        declared["weight"] = float(weight)
                    for field, value in (("base", base), ("damage_dice", damage_dice),
                                         ("damage_type", damage_type), ("properties", properties),
                                         ("ac", ac), ("dex_cap", dex_cap),
                                         ("strength_req", strength_req), ("ac_bonus", ac_bonus),
                                         ("attack_bonus", attack_bonus), ("damage_bonus", damage_bonus),
                                         ("attunement", attunement), ("attunement_by", attunement_by),
                                         ("kind", kind), ("pair", pair),
                                         ("save_bonus", save_bonus), ("check_bonus", check_bonus),
                                         ("proficiency_bonus", proficiency_bonus),
                                         ("spell_attack_bonus", spell_attack_bonus),
                                         ("spell_dc_bonus", spell_dc_bonus),
                                         ("set_str", set_str), ("set_dex", set_dex),
                                         ("set_con", set_con), ("set_int", set_int),
                                         ("set_wis", set_wis), ("set_cha", set_cha),
                                         ("str_bonus", str_bonus), ("dex_bonus", dex_bonus),
                                         ("con_bonus", con_bonus), ("int_bonus", int_bonus),
                                         ("wis_bonus", wis_bonus), ("cha_bonus", cha_bonus),
                                         ("str_bonus_max", str_bonus_max),
                                         ("dex_bonus_max", dex_bonus_max),
                                         ("con_bonus_max", con_bonus_max),
                                         ("int_bonus_max", int_bonus_max),
                                         ("wis_bonus_max", wis_bonus_max),
                                         ("cha_bonus_max", cha_bonus_max)):
                        if value is not None:
                            declared[field] = value
                    if effects is not None:
                        declared["effects"] = effects
            if declared:
                new_entry = {"name": name, "description": desc, **declared}

            if appearance and key == "active_effects":
                entry = new_entry if isinstance(new_entry, dict) else {"name": name}
                entry["appearance"] = str(appearance).strip()
                new_entry = entry

            if key == "inventory" and not device_owned:
                rejection = _validate_item_add(name, declared, weight)
                if rejection is not None:
                    return rejection

            exists = any((isinstance(e, dict) and e.get("name") == name) or e == name for e in current_list)
            if exists:
                available = []
                for e in current_list:
                    if isinstance(e, dict):
                        available.append(e.get("name", str(e)))
                    else:
                        available.append(str(e))
                result = {"success": False, "error": "already_exists", "key": key, "item": name,
                          "action": action, "current_items": available}
                if is_prepared_spells:
                    info = build_prepared_spells_info(cursor)
                    if info is not None:
                        result["spells_prepared_info"] = info
                return result

            if is_prepared_spells:
                info = build_prepared_spells_info(cursor)
                if info is not None and info.get("at_capacity"):
                    info["reason"] = f"Cannot add '{name}'. Maximum prepared spells reached ({info['max_count']}). Remove a spell first before adding a new one."
                    return {"success": False, "error": "spells_prepared_at_capacity", "key": key, "item": name, "action": action, "spells_prepared_info": info}

            current_list.append(new_entry)

        elif action == "remove":
            found = False
            for i, e in enumerate(current_list):
                if (isinstance(e, dict) and e.get("name") == item) or e == item:
                    current_list.pop(i)
                    found = True
                    break
            if not found:
                available = []
                for e in current_list:
                    if isinstance(e, dict):
                        available.append(e.get("name", str(e)))
                    else:
                        available.append(str(e))
                return {"success": False, "error": "not_found", "key": key, "item": item,
                        "action": action, "current_items": available}

            if key == "inventory":
                # The item left the pack, so it cannot stay worn or wielded.
                removed_equipped = _clear_equipped(cursor, item)

            if key == "active_effects":
                reverted = _remove_active_effect(cursor, item)
                conc = _get_concentration(cursor)
                if conc and conc.get("spell") == item:
                    _db_set(cursor, "concentration", {})
                # Persist the removal of the effect from the list (the early
                # return below would otherwise skip the normal list write).
                cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                               (key, json.dumps(current_list)))
                state.DB_CONNECTION.commit()
                result_early = {
                    "success": True,
                    "key": key,
                    "item": item,
                    "action": action,
                    "current_list": [e.get("name", str(e)) if isinstance(e, dict) else str(e) for e in current_list],
                    "reverted": reverted,
                    "narrative_format": f"Removed active effect '{item}' and reverted its bonuses.",
                }
                return result_early
        elif action == "update":
            index = None
            for i, e in enumerate(current_list):
                if (isinstance(e, dict) and e.get("name") == item) or e == item:
                    index = i
                    break
            if index is None:
                available = [e.get("name", str(e)) if isinstance(e, dict) else str(e)
                             for e in current_list]
                return {"success": False, "error": "not_found", "key": key, "item": item,
                        "action": action, "current_items": available}
            entry = current_list[index]
            if key == "inventory" and device.is_device_item(
                    str(entry.get("name")) if isinstance(entry, dict) else str(entry)):
                return {"success": False, "error": "device_engine_owned", "key": key,
                        "item": item, "action": action,
                        "reason": "The Device and its parts are engine-owned; their description and state are written by the engine.",
                        "gm_instruction": ("Do not edit the Device or a part. Remove one with "
                                           "action='remove' if the fiction takes it away.")}
            new_desc = description if isinstance(description, str) else None
            rename = new_name.strip() if isinstance(new_name, str) and new_name.strip() else None
            if new_desc is None and rename is None:
                return {"success": False, "error": "nothing_to_update", "key": key, "item": item,
                        "action": action,
                        "reason": "Pass description= and/or new_name= to change the entry."}
            if rename and any(
                    (isinstance(e, dict) and e.get("name") == rename) or e == rename
                    for i, e in enumerate(current_list) if i != index):
                available = [e.get("name", str(e)) if isinstance(e, dict) else str(e)
                             for e in current_list]
                return {"success": False, "error": "already_exists", "key": key, "item": rename,
                        "action": action, "current_items": available}
            if isinstance(entry, dict):
                updated = dict(entry)
                if rename:
                    updated["name"] = rename
                if new_desc is not None:
                    updated["description"] = new_desc
            else:
                entry_name = rename or str(entry)
                updated = ({"name": entry_name, "description": new_desc}
                           if new_desc is not None else entry_name)
            current_list[index] = updated
            if key == "inventory" and rename and rename != item:
                _rename_equipped(cursor, item, rename)
            updated_label = rename or (entry.get("name") if isinstance(entry, dict) else str(entry))
        else:
            return {"success": False,
                    "error": "Invalid action. Use 'add', 'remove' or 'update'.",
                    "key": key, "action": action}

        if '.' in key:
            set_nested_value(data, path_in_obj, current_list)
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
        else:
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, json.dumps(current_list)))

        state.DB_CONNECTION.commit()

        display_list = []
        for e in current_list:
            if isinstance(e, dict):
                display_list.append(e.get("name", str(e)))
            else:
                display_list.append(str(e))

        result = {
            "success": True,
            "key": key,
            "item": item,
            "action": action,
            "current_list": display_list,
        }
        if device_owned and added_name:
            # The Device vocabulary is canonical: tell the GM the exact name it now holds.
            result["item"] = added_name

        if is_prepared_spells:
            info = build_prepared_spells_info(cursor)
            if info is not None:
                result["spells_prepared_info"] = info

        if key == "inventory":
            result["carrying"] = _carry_block(cursor)
            result["equipment"] = _equipment_block(cursor)
            if action == "remove" and removed_equipped:
                result["unequipped"] = item
                result["note"] = (f"{item} was worn or wielded, so it was unequipped when it left the "
                                  f"inventory. If you replaced it with a new item, call equip_item "
                                  f"on the replacement — it is not auto-equipped.")
            added_base = None
            added_entry = next((e for e in current_list
                                 if isinstance(e, dict) and e.get("name") == (added_name or item)), None)
            if isinstance(added_entry, dict):
                added_base = added_entry.get("base")
            if (action == "add" and weight is None and added_name and not device_owned
                    and carrying.weight_for(added_name, base=added_base) is None):
                result["unweighed_item"] = added_name
                result["warning"] = (
                    f"'{added_name}' is not in the SRD weight catalog and no weight was given, so it "
                    f"counts as 0 lb. Re-add it with weight=<pounds> so carrying capacity stays accurate."
                )

        if action == "update":
            narrative = f"Updated {key}: {updated_label}"
            if new_desc is not None:
                narrative += f": {new_desc}"
            narrative += "."
        else:
            verb = "Removed from" if action == "remove" else "Added to"
            narrative = f"{verb} {key}: {item}."
        if result.get("carrying"):
            status = result["carrying"].get("status")
            if status and status != "unencumbered" and status != before_carry_status:
                narrative += f" Encumbrance: {status}."
        if result.get("unequipped"):
            narrative += f" {item} was worn or wielded, so it was unequipped."
        result["narrative_format"] = narrative

        return result
    except Exception as e:
        return {"success": False, "error": f"Error updating list: {str(e)}", "key": key}


@mcp.tool()
def equip_item(item: str, action: str = "equip", slot: str | None = None,
               replace: bool = False, instant: bool = False) -> dict:
    """Equip or unequip something the character is carrying.

    WHEN: worn/wielded gear changes. The item must already be in the inventory (use update_player_list first) -- this tool only moves it.
    FIELDS:
    - item: the inventory name exactly.
    - action: 'equip' (default) | 'unequip'.
    - slot: 'main_hand' | 'off_hand' | 'armor' | 'worn'. Omit to let the engine choose (armour -> armour slot; anything else -> a free hand). Cloaks, boots, gloves, gauntlets, bracers, headwear, rings and amulets go to 'worn'; clothing is worn automatically.
    - replace: True stows whatever occupied the slot/hand instead of refusing (the stowed item stays in the inventory).
    - instant: allow an armour change in combat (otherwise refused; use only for magic or a GM call). Shields take one action and are always allowed.

    RULES:
    - 5e has NO equipment slots -- only two hands and one suit of armour. A shield is held in a hand; a two-handed weapon needs BOTH hands (equip it only with the other hand free, or replace=True).
    - Equipping recomputes AC; only one suit of armour and one shield benefit a creature. Unequipping never drops the item.
    - The in-combat refusal checks the registry: hostiles all dead = no longer in combat.

    EX:
    equip_item(item='Chain Mail')
    equip_item(item='Greatsword', replace=True)
    """
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        cursor = state.DB_CONNECTION.cursor()
        action = str(action or "equip").strip().lower()
        if action not in ("equip", "unequip"):
            return {"success": False, "error": "invalid_action", "action": action,
                    "reason": "Use action='equip' or action='unequip'."}

        inventory = _db_val(cursor, "inventory", []) or []
        names = [(e.get("name") if isinstance(e, dict) else str(e)) for e in inventory]
        if item not in names:
            return {"success": False, "error": "not_in_inventory", "item": item,
                    "reason": f"'{item}' is not in the inventory — add it with "
                              f"update_player_list first.",
                    "current_items": names}
        declared = next((e for e in inventory if isinstance(e, dict) and e.get("name") == item), {})
        base = declared.get("base")
        if device.is_device_item(item) or (isinstance(declared, dict)
                                           and (declared.get("device") or declared.get("device_part"))):
            return {"success": False, "error": "device_not_equippable", "item": item,
                    "reason": "The Device is not gear — it cannot be equipped, wielded or worn.",
                    "gm_instruction": "The Device and its parts are carried, never equipped. Leave them in the inventory."}

        equipped = _db_val(cursor, "equipped", None)
        if not isinstance(equipped, dict):
            equipped = {"armor": None, "hands": [None, None]}
        hands = equipped.get("hands")
        hands = (list(hands) + [None, None])[:2] if isinstance(hands, list) else [None, None]
        worn_raw = equipped.get("worn")
        worn = [str(w) for w in worn_raw if w] if isinstance(worn_raw, list) else []
        before = int(_db_val(cursor, "armor_class", 10) or 10)
        is_armor_item = equipment.is_armor(item, base)
        is_shield_item = equipment.is_shield(item, base)

        # SRD 5.1 "Getting Into and Out of Armor": minutes for armour, one action for a shield.
        time_cost = equipment.don_time(item, base, doff=(action == "unequip"))
        if is_armor_item and not instant and _in_active_combat():
            verb = "doff" if action == "unequip" else "don"
            return _blocked_action(
                cursor, "cannot_change_armor_in_combat",
                f"{verb}ning {item} takes {time_cost} — impossible in the middle of a fight.",
                item,
                f"{item} was not {verb}ned — it takes {time_cost} and the party is in combat. "
                f"The action is spent.")

        if action == "unequip":
            where = None
            if equipped.get("armor") == item:
                equipped["armor"] = None
                where = "worn"
            if item in worn:
                worn = [w for w in worn if w != item]
                where = where or "worn"
            for index, held in enumerate(hands):
                if held == item:
                    hands[index] = None
                    where = where or ("main hand" if index == 0 else "off hand")
            if where is None:
                return {"success": False, "error": "not_equipped", "item": item,
                        "reason": f"'{item}' is not currently worn or wielded.",
                        "equipment": _equipment_block(cursor)}
            equipped["hands"] = hands
            equipped["worn"] = worn
            _db_set(cursor, "equipped", equipped)
            after = _recompute_armor_class(cursor)
            block = _equipment_block(cursor)
            label = "stowed" if where == "worn" else f"hand freed ({where})"
            return {
                "success": True, "action": "unequip", "item": item,
                "equipped": equipped, "armor_class_before": before, "armor_class_after": after,
                "equipment": block, "warnings": block.get("warnings", []),
                "time_cost": time_cost,
                "narrative_format": f"{item} {label}{_ac_note(before, after)}"
                                    + (f" ({time_cost})" if time_cost else ""),
            }

        target = (slot or "").strip().lower() or None
        if target not in (None, "main_hand", "off_hand", "armor", "worn"):
            return {"success": False, "error": "invalid_slot", "slot": slot,
                    "reason": "Use slot='main_hand', 'off_hand', 'armor' or 'worn' (or omit it)."}

        is_armor = is_armor_item
        if is_armor:
            if target not in (None, "armor"):
                return {"success": False, "error": "invalid_slot", "slot": slot,
                        "reason": f"'{item}' is armour and can only go in the armour slot."}
            if equipped.get("armor") == item:
                return {"success": True, "action": "equip", "item": item, "already_equipped": True,
                        "equipped": equipped, "armor_class_before": before, "armor_class_after": before,
                        "equipment": _equipment_block(cursor), "narrative_format":
                        f"{item} is already worn"}
            if equipped.get("armor") and not replace:
                return {"success": False, "error": "armor_slot_occupied", "item": item,
                        "worn": equipped.get("armor"),
                        "reason": f"{equipped.get('armor')} is already worn — unequip it first, or "
                                  f"repeat with replace=True.",
                        "equipment": _equipment_block(cursor)}
            equipped["armor"] = item
            equipped["hands"] = [None if h == item else h for h in hands]
        else:
            if target == "armor":
                return {"success": False, "error": "invalid_slot", "slot": slot,
                        "reason": f"'{item}' is not armour — use slot='main_hand', 'off_hand' or 'worn'."}
            if item in hands:
                return {"success": True, "action": "equip", "item": item, "already_equipped": True,
                        "equipped": equipped, "armor_class_before": before, "armor_class_after": before,
                        "equipment": _equipment_block(cursor), "narrative_format":
                        f"{item} is already held"}
            if target == "worn" or (target is None and _is_worn_slot(item, declared)):
                if item in worn:
                    return {"success": True, "action": "equip", "item": item,
                            "already_equipped": True, "equipped": equipped,
                            "armor_class_before": before, "armor_class_after": before,
                            "equipment": _equipment_block(cursor),
                            "narrative_format": f"{item} is already worn"}
                # SRD 5.1 "Multiple Items of the Same Kind": one pair of footwear, gloves/gauntlets,
                # bracers, headwear, cloak. Rings and amulets are unrestricted.
                group = _worn_group(declared)
                if group:
                    conflict = next((w for w in worn
                                     if _worn_group(_inventory_entry(inventory, w)) == group), None)
                    if conflict and not replace:
                        return {"success": False, "error": "worn_slot_taken", "item": item,
                                "worn": conflict,
                                "reason": f"{conflict} is already worn in the {group} slot — unequip "
                                          f"it first, or repeat with replace=True.",
                                "gm_instruction": f"Re-call equip_item(item='{item}', "
                                                  f"replace=True) to stow {conflict}, or unequip "
                                                  f"{conflict} first.",
                                "equipment": _equipment_block(cursor)}
                    if conflict:
                        worn = [w for w in worn if w != conflict]
                worn.append(item)
                equipped["worn"] = worn
                equipped["hands"] = hands
            else:
                # A shield prefers the off hand, a weapon the main hand; otherwise the first free one.
                order = (1, 0) if equipment.is_shield(item, base) else (0, 1)
                if target == "main_hand":
                    index = 0
                elif target == "off_hand":
                    index = 1
                else:
                    index = next((i for i in order if hands[i] is None), None)
                    if index is None and not replace:
                        return {"success": False, "error": "no_free_hand", "item": item,
                                "held": [h for h in hands if h],
                                "reason": "Both hands are busy — stow or drop something first, or "
                                          "repeat with replace=True.",
                                "equipment": _equipment_block(cursor)}
                    if index is None:
                        index = order[0]
                other = 1 - index
                if equipment.hands_used([item, None], inventory) == 2:
                    if hands[other] and not replace:
                        return {"success": False, "error": "two_handed_needs_both_hands", "item": item,
                                "occupied": hands[other],
                                "reason": f"{item} needs both hands to attack with, but {hands[other]} "
                                          f"is in the other hand — free it first, or repeat with "
                                          f"replace=True.",
                                "equipment": _equipment_block(cursor)}
                    hands[other] = None
                hands[index] = item
                equipped["hands"] = hands

        _db_set(cursor, "equipped", equipped)
        after = _recompute_armor_class(cursor)
        block = _equipment_block(cursor)
        where = "worn" if (is_armor or item in worn) else ("main hand" if hands[0] == item else "off hand")
        return {
            "success": True, "action": "equip", "item": item,
            "equipped": equipped, "armor_class_before": before, "armor_class_after": after,
            "equipment": block, "warnings": block.get("warnings", []),
            "time_cost": time_cost,
            "narrative_format": f"{item} equipped ({where}){_ac_note(before, after)}"
                                + (f" ({time_cost})" if time_cost else ""),
        }
    except Exception as e:
        return {"success": False, "error": f"Error equipping item: {str(e)}", "item": item}


@mcp.tool()
def attune_item(item: str, action: str = "attune", instant: bool = False) -> dict:
    """Attune to (or break attunement with) a magic item.

    WHEN: after a rest, for an item whose magic needs attunement.
    FIELDS:
    - item: the inventory name exactly.
    - action: 'attune' (default) | 'unattune'.
    - instant: allow it in combat (otherwise refused -- attuning normally takes a short rest).

    EX:
    attune_item(item='Voidmail')
    """
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        cursor = state.DB_CONNECTION.cursor()
        action = str(action or "attune").strip().lower()
        if action not in ("attune", "unattune"):
            return {"success": False, "error": "invalid_action", "action": action,
                    "reason": "Use action='attune' or action='unattune'."}

        inventory = _db_val(cursor, "inventory", []) or []
        names = [(e.get("name") if isinstance(e, dict) else str(e)) for e in inventory]
        if item not in names:
            return {"success": False, "error": "not_in_inventory", "item": item,
                    "reason": f"'{item}' is not in the inventory.", "current_items": names}
        declared = next((e for e in inventory if isinstance(e, dict) and e.get("name") == item), {})
        if device.is_device_item(item) or (isinstance(declared, dict)
                                           and (declared.get("device") or declared.get("device_part"))):
            return {"success": False, "error": "device_not_attunable", "item": item,
                    "reason": "The Device is not a magic item — it cannot be attuned.",
                    "gm_instruction": "The Device and its parts are carried, never attuned."}
        attuned = _db_val(cursor, "attuned", []) or []
        attuned = [str(a) for a in attuned] if isinstance(attuned, list) else []

        def _identity(name):
            entry = next((e for e in inventory if isinstance(e, dict) and e.get("name") == name), {})
            return str(entry.get("base") or name).strip().lower()

        def _prereq_met(prereq):
            """SRD 5.1 prerequisites the sheet can answer; anything else is a GM ruling."""
            text = str(prereq or "").strip().lower()
            if not text:
                return True
            klass = str(_db_val(cursor, "character_class", "") or "").strip().lower()
            race = str(_db_val(cursor, "race", "") or "").strip().lower()
            alignment = str(_db_val(cursor, "alignment", "") or "").strip().lower()
            if klass and klass in text:
                return True
            if race and race in text:
                return True
            if "spellcaster" in text or "spellcasting" in text:
                return bool(_db_val(cursor, "spellcasting", None))
            named_class = next((c for c in SRD_CLASSES if c.lower() in text), None)
            if named_class:
                return named_class.lower() == klass
            for term in ("lawful", "chaotic", "neutral", "good", "evil"):
                if term in text:
                    return term in alignment
            return True

        def _fail(error, reason, **extra):
            result = {"success": False, "error": error, "item": item, "reason": reason,
                      "attuned": attuned, "attunement_slots_free": max(0, 3 - len(attuned))}
            result.update(extra)
            return result

        if action == "unattune":
            if item not in attuned:
                return _fail("not_attuned", f"'{item}' is not attuned.")
            attuned.remove(item)
            _db_set(cursor, "attuned", attuned)
            _recompute_armor_class(cursor)
            block = _equipment_block(cursor)
            return {"success": True, "action": "unattune", "item": item, "attuned": attuned,
                    "attunement_slots_free": max(0, 3 - len(attuned)), "equipment": block,
                    "warnings": block.get("warnings", []),
                    "narrative_format": f"Attunement with {item} ends — "
                                        f"{max(0, 3 - len(attuned))} slot(s) free"}

        if not declared.get("attunement"):
            return _fail("attunement_not_required",
                         f"'{item}' does not require attunement (declare attunement=True to change that).")
        if item in attuned:
            return {"success": True, "action": "attune", "item": item, "already_attuned": True,
                    "attuned": attuned, "attunement_slots_free": max(0, 3 - len(attuned)),
                    "equipment": _equipment_block(cursor),
                    "narrative_format": f"Already attuned to {item}"}
        if not instant and _in_active_combat():
            return _fail("requires_short_rest",
                         f"Attuning to {item} requires a short rest — not possible during a fight.")
        if len(attuned) >= 3:
            return _fail("attunement_full",
                         "Already attuned to three magic items — break one attunement first.")
        prereq = declared.get("attunement_by")
        if prereq and not _prereq_met(prereq):
            return _fail("attunement_prerequisite_unmet",
                         f"'{item}' requires attunement {prereq} — this character does not qualify.",
                         prerequisite=prereq)
        # SRD 5.1: "a creature can't attune to more than one copy of an item."
        ident = _identity(item)
        clash = next((a for a in attuned if _identity(a) == ident), None)
        if clash is not None:
            return _fail("attunement_duplicate_item",
                         f"Already attuned to another copy of {declared.get('base') or item} "
                         f"({clash}).", conflicting_item=clash)
        attuned.append(item)
        _db_set(cursor, "attuned", attuned)
        _recompute_armor_class(cursor)
        block = _equipment_block(cursor)
        return {"success": True, "action": "attune", "item": item, "attuned": attuned,
                "attunement_slots_free": max(0, 3 - len(attuned)), "equipment": block,
                "warnings": block.get("warnings", []),
                "narrative_format": f"Attuned to {item} — "
                                    f"{max(0, 3 - len(attuned))} slot(s) free"}
    except Exception as e:
        return {"success": False, "error": f"Error attuning item: {str(e)}", "item": item}


@mcp.tool()
def lookup(query: str) -> str:
    """Reference on demand (read-only).

    WHEN: you need era lore again, or a tool's full reference (its description points here).
    - query: 'here' = this era; an era id = that era; a polity name = its factions; 'ref:TOOL' = that tool's reference.
    """
    try:
        from web import refs as web_refs
        if web_refs.is_tool_ref(query):
            return web_refs.render_tool_ref(str(query).split(":", 1)[1])
        from web.eras import lookup as _lookup

        current = ""
        if state.DB_CONNECTION is not None:
            current = str(_db_val(state.DB_CONNECTION.cursor(), "era", "") or "")
            if not current:
                # No era on the save = the classic game. It has no eras to fetch, and
                # `web.eras.lookup` would otherwise fall back to the first age and leak the
                # wrong world into a classic prompt. `lookup` here serves references only.
                return ("This game has no eras. `lookup` here serves a tool's field "
                        "reference: 'ref:TOOL'.")
        return _lookup(query, current)
    except Exception as e:  # noqa: BLE001
        return f"lookup failed: {type(e).__name__}: {e}"


@mcp.tool()
def set_player_field(key: str, value: str) -> dict:
    """Set one stored player field (session bookkeeping)."""
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        cursor = state.DB_CONNECTION.cursor()
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                       (str(key), json.dumps(str(value))))
        state.DB_CONNECTION.commit()
        return {"success": True, "key": str(key), "value": str(value)}
    except Exception as e:
        return {"success": False, "error": f"Error setting '{key}': {str(e)}"}


@mcp.tool()
def debug_set_field(key: str, value: str) -> dict:
    """DEBUG ONLY -- set one stored player field to an arbitrary JSON value.

    Unlike `set_player_field`, which stringifies everything, this writes the value as real
    JSON, so dicts, lists, numbers, booleans and null all survive. The derived blocks are
    recomputed afterwards (armour class, spell save DC / attack, max HP). Enabled only when
    the engine started this server with `--debug`.
    """
    if not state.DEBUG_MODE:
        return {"success": False, "error": "developer mode is not enabled."}
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = value
    try:
        cursor = state.DB_CONNECTION.cursor()
        key = str(key)
        if '.' in key:
            root_key, _, path_in_obj = key.partition('.')
            cursor.execute("SELECT value FROM player WHERE key = ?", (root_key,))
            row = cursor.fetchone()
            data = json.loads(row[0]) if row else {}
            set_nested_value(data, path_in_obj, parsed)
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                           (root_key, json.dumps(data)))
        else:
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                           (key, json.dumps(parsed)))
        state.DB_CONNECTION.commit()
        ac = _recompute_armor_class(cursor)
        state.DB_CONNECTION.commit()
        return {"success": True, "key": key, "value": parsed,
                "armor_class": ac if ac is not None else _db_val(cursor, "armor_class", None)}
    except Exception as e:
        return {"success": False, "error": f"Error setting '{key}': {str(e)}"}


@mcp.tool()
def debug_device(parts: list[str] | None = None, present: bool = True) -> dict:
    """DEBUG ONLY -- set the Device's recovered parts directly.

    Replaces every Device / part inventory entry: keeps the Device when `present`, then adds
    each named part (canonical names, in config order) stamped with the current era. This
    bypasses the GM's add guards on purpose -- it is the developer's lever. Enabled only
    when the engine started this server with `--debug`.
    """
    if not state.DEBUG_MODE:
        return {"success": False, "error": "developer mode is not enabled."}
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        cursor = state.DB_CONNECTION.cursor()
        era = str(_db_val(cursor, "era", "") or "").strip().lower()
        inventory = _db_val(cursor, "inventory", []) or []
        if not isinstance(inventory, list):
            inventory = []
        kept = [e for e in inventory
                if not (isinstance(e, dict) and (e.get("device") or e.get("device_part")))]
        if present:
            kept.append(device.device_entry())
        wanted = {device.canonical_part(p) for p in (parts or []) if device.canonical_part(p)}
        recovered = []
        for name in device.part_names():
            if name in wanted:
                kept.append(device.part_entry(name, era or "egypt"))
                recovered.append(name)
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                       ("inventory", json.dumps(kept)))
        state.DB_CONNECTION.commit()
        _recompute_armor_class(cursor)
        state.DB_CONNECTION.commit()
        return {"success": True, "present": bool(present), "parts": recovered,
                "total": device.part_total()}
    except Exception as e:
        return {"success": False, "error": f"Error setting the Device: {str(e)}"}


@mcp.tool()
def dump_player_save_state() -> dict:
    """The whole player row map, unreduced. For the engine's save, never for the GM.

    `dump_player_db` is the GM's view: it reduces `reputation` to the era the save is in. A
    save built from that view writes a **flat** map -- the `{era: ...}` wrapper and every
    other era's standing are lost, and the reloaded save can never record reputation again.
    This returns the rows verbatim, derived blocks omitted (they are recomputed).
    """
    if state.DB_CONNECTION is None:
        return {"error": "Database not initialized."}
    cursor = state.DB_CONNECTION.cursor()
    cursor.execute("SELECT * FROM player")
    result: dict = {}
    for key, value in cursor.fetchall():
        try:
            result[key] = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            result[key] = value
    return result


@mcp.tool()
def dump_player_db() -> dict:
    """Full dump of the in-memory player database.

    WHEN: the engine's own state reads (the session CHARACTER SHEET, /stats, the Device, scene hints). The Game Master reads the fields it needs with `read_player_field`.
    NOTE: the derived `_carrying` (capacity/encumbrance/speed), `_equipment` (worn armour, hands, AC breakdown) and `_passive` (passive Perception/Investigation/Insight = 10 + modifier) blocks are computed, never saved.
    """
    if state.DB_CONNECTION is None:
        return {"error": "Database not initialized."}
    try:
        return _player_gm_view(state.DB_CONNECTION.cursor())
    except Exception as e:
        return {"error": f"Error dumping database: {str(e)}"}


@mcp.tool()
def read_player_field(fields: list[str]) -> dict:
    """Read specific fields of the player database.

    WHEN: you need a value the CHARACTER SHEET does not carry -- above all the ability scores, or a companion's whole sheet. Never use it to refresh the sheet.
    FIELDS (the sheet already carries identity, HP, gold, conditions, features, inventory, equipped, consumables, languages, speed and the era's reputation): stats, saves, skills, expertise, proficiency_bonus, armor_class, _passive, xp, armor_proficiencies, weapon_proficiencies, tool_proficiencies, hit_dice_count, hit_dice_size, spellcasting, _carrying, _equipment, companions. `companions` is your party -- one store, they travel with you -- keyed by name. Every other stored field (name, gold, ...) is readable too.
    RETURNS {"fields": {<field>: <value>}, "missing": [<names not found>]}.
    """
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    wanted = [str(f).strip() for f in (fields or []) if str(f or "").strip()]
    if not wanted:
        return {"success": False, "error": "fields_required",
                "reason": "Name at least one field to read.",
                "gm_instruction": "Pass the fields you need, e.g. fields=['stats']."}
    try:
        cursor = state.DB_CONNECTION.cursor()
        view = _player_gm_view(cursor)
        if "companions" in wanted:
            # One party, keyed by display name -- they travel with the Traveller, so the store
            # is not scoped to the age (see `register_companion`).
            view["companions"] = {str(sheet.get("name") or key): sheet
                                  for key, sheet in _companions_store(cursor).items()}
        return {"fields": {k: view[k] for k in wanted if k in view},
                "missing": [k for k in wanted if k not in view]}
    except Exception as e:
        return {"success": False, "error": f"Error reading the player database: {str(e)}"}
