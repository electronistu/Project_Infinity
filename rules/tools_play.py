"""Play-loop MCP tools: rests, exhaustion and death saves, dice and checks, and
the Device jump reset."""

import json
import math
import random
from level_up import CASTER_TYPE_MAP, FULL_CASTER_SPELL_SLOTS, SLOT_TABLES
import equipment
import skills
from .app import mcp
from . import state
from .combat import (
    _action_failure, _advance_turn, _blocked_action, _companion_check_detail,
    _companion_here
)
from .conditions import _condition_check_disadvantage, _conditions_for, _drain_woke
from .consts import PREPARED_CASTER_CLASSES
from .db import (
    _ability_mod, _carry_get, _db_set, _db_val, _effective_stats, _equipment_block,
    _exhaustion_level, _item_effect_state
)
from .dice import _format_hp_status, _parse_and_roll_dice, _roll_d20
from .sheet import (
    _derived_check, _derived_save, _encumbrance_note, _exhaustion_effects,
    _exhaustion_level_for, _is_player_actor, _roll_disadvantage
)
from .spell_effects import _consume_buff_charge
from .spell_knowledge import _has_spellbook_item, _needs_spellbook, get_max_prepared_spells
from .vitals import (
    _apply_exhaustion_change, _apply_hp_change, _clear_death_saves, _end_concentration,
    _end_transient_effects, _mark_player_dead, _player_dead, _player_stable,
    _stabilize_death_saves
)

@mcp.tool()
def rest(rest_type: str, prepared_spells: list[str] | None = None) -> dict:
    """Apply a short or long rest; all numeric changes are auto-applied.

    WHEN: the player rests.
    FIELDS:
    - rest_type: 'short' | 'long'.
    - prepared_spells: (long rest only) full replacement list of prepared spell names; validated against capacity (Wizards against the spellbook).

    RULES:
    - Short rest: auto-spends hit dice until HP is full or none remain. Warlocks restore Pact Magic; Wizards auto-apply Arcane Recovery (ceil(level/2) combined slot levels, lowest first, not 6th+).
    - Long rest: full HP, regain max(level//2, 1) hit dice (capped at level), all slots restored, active effects cleared with deltas reverted, one exhaustion level removed.
    - Long rest is rejected at 0 HP.
    - Returns hints for class features needing manual recharge.

    EX:
    rest(rest_type='short')
    rest(rest_type='long', prepared_spells=['Magic Missile', 'Shield', 'Mage Armor', 'Burning Hands'])
    """
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}

    if rest_type not in ("short", "long"):
        return {"success": False, "error": "rest_type must be 'short' or 'long'."}

    try:
        cursor = state.DB_CONNECTION.cursor()

        char_class = _db_val(cursor, "character_class", "")
        level = int(_db_val(cursor, "level", 1))
        hd_count = int(_db_val(cursor, "hit_dice_count", 0))
        hd_size = int(_db_val(cursor, "hit_dice_size", 8))
        total_hp = int(_db_val(cursor, "total_hit_points", 1))
        current_hp = int(_db_val(cursor, "current_hit_points", 0))

        # A dead character cannot rest, and a short rest is not a revive at 0 HP.
        if _player_dead(cursor):
            return _action_failure(
                "The character is dead.", "Rest \u2014 impossible; the character is dead.")
        if current_hp <= 0:
            return _action_failure(
                "Cannot rest at 0 HP. The character is dying, stable or unconscious \u2014 "
                "heal them first.",
                "Rest \u2014 impossible while at 0 HP")

        con_mod = _ability_mod(_effective_stats(cursor).get("con", 10))

        sc_raw = _db_val(cursor, "spellcasting", {})
        if isinstance(sc_raw, str):
            sc_raw = json.loads(sc_raw)
        sc = dict(sc_raw) if sc_raw else {}

        result = {"success": True, "rest_type": rest_type}
        changes = {}
        hints = []

        caster_type = CASTER_TYPE_MAP.get(char_class)
        slot_table = None
        if caster_type:
            slot_table = SLOT_TABLES[caster_type].get(level, {})

        if rest_type == "short":
            dice_spent = 0
            total_healing = 0
            missing_hp = total_hp - current_hp
            # Periapt of Wound Closure (and similar): double the Hit-Die healing (SRD 5.1).
            hd_multiplier = float(_item_effect_state(cursor).get("hit_die_healing_multiplier") or 1)

            while missing_hp > 0 and dice_spent < hd_count:
                roll = random.randint(1, hd_size)
                healing = int(max(roll + con_mod, 0) * hd_multiplier)
                total_healing += healing
                missing_hp -= healing
                dice_spent += 1

            new_hd = hd_count - dice_spent

            if dice_spent > 0:
                hp_result = _apply_hp_change(cursor, total_healing)
                _db_set(cursor, "hit_dice_count", str(new_hd))
                state.DB_CONNECTION.commit()
                changes["hp"] = {"old": current_hp, "new": hp_result["new_value"],
                                 "healed": hp_result["new_value"] - current_hp,
                                 "healing_applied": hp_result["new_value"] - current_hp,
                                 "max_hp": total_hp,
                                 "dice_spent": dice_spent,
                                 "status": hp_result["hp_status"]}
                changes["hit_dice"] = {"old": hd_count, "new": new_hd, "spent": dice_spent}
            else:
                changes["hp"] = {"old": current_hp, "new": current_hp,
                                 "healed": 0, "healing_applied": 0, "max_hp": total_hp,
                                 "dice_spent": 0,
                                 "status": _format_hp_status(current_hp, total_hp)}
                changes["hit_dice"] = {"old": hd_count, "new": hd_count, "spent": 0}

            recovered = {}

            if caster_type == "warlock" and slot_table:
                sc["slots"] = {str(k): v for k, v in slot_table.items()}
                _db_set(cursor, "spellcasting", sc)
                state.DB_CONNECTION.commit()
                changes["slots_restored"] = {str(k): v for k, v in slot_table.items()}

            if char_class == "Wizard" and caster_type == "full":
                max_slots = FULL_CASTER_SPELL_SLOTS.get(level, {})
                current_slots = sc.get("slots", {})
                budget = math.ceil(level / 2)
                recovered = {}
                budget_used = 0

                for slot_lvl in range(1, 6):
                    slot_key = str(slot_lvl)
                    slot_cost = slot_lvl
                    curr = int(current_slots.get(slot_key, 0))
                    max_s = max_slots.get(slot_lvl, 0)
                    while curr < max_s and budget_used + slot_cost <= budget:
                        curr += 1
                        budget_used += slot_cost
                    if slot_key in current_slots:
                        recovered_amount = curr - int(current_slots.get(slot_key, 0))
                    else:
                        recovered_amount = curr
                    if recovered_amount > 0:
                        recovered[slot_key] = recovered_amount
                        sc["slots"] = sc.get("slots", {})
                        sc["slots"][slot_key] = curr

                if recovered:
                    _db_set(cursor, "spellcasting", sc)
                    state.DB_CONNECTION.commit()
                    changes["arcane_recovery"] = {
                        "recovered": recovered,
                        "budget_used": budget_used,
                        "budget_total": budget,
                    }

            narrative_parts = [f"Short Rest complete."]
            if dice_spent > 0:
                narrative_parts.append(f"Spent {dice_spent} hit die(s) → healed {total_healing} HP ({new_hd}/{level} remaining).")
            else:
                narrative_parts.append("HP already at maximum — no hit dice spent.")
            if caster_type == "warlock" and slot_table:
                narrative_parts.append("Pact Magic slots restored.")
            if recovered:
                arc_parts = ", ".join(f"Lv{k}: +{v}" for k, v in recovered.items())
                narrative_parts.append(f"Arcane Recovery: {arc_parts} (budget {budget_used}/{budget}).")

            hints.append("Wizard: Arcane Recovery has been used for this rest period (once per long rest)." if char_class == "Wizard" else None)
            hints.append("Fighter: Second Wind and Action Surge recharge on short/long rest." if char_class == "Fighter" else None)
            hints.append("Cleric: Channel Divinity recharges on short/long rest." if char_class == "Cleric" else None)
            hints.append("Warlock: Pact Magic slots recharge on short rest." if char_class == "Warlock" else None)
            hints.append("Bard: Bardic Inspiration recharges on short rest (level 5+)." if char_class == "Bard" and level >= 5 else None)
            hints.append("Monk: Ki points recharge on short rest (level 2+)." if char_class == "Monk" and level >= 2 else None)
            hints.append("Druid: Wild Shape uses recharge on short rest." if char_class == "Druid" and level >= 2 else None)
            hints.append("Paladin: Channel Divinity recharges on short/long rest." if char_class == "Paladin" and level >= 3 else None)
            hints = [h for h in hints if h is not None]

        elif rest_type == "long":
            _db_set(cursor, "current_hit_points", str(total_hp))
            changes["hp"] = {"old": current_hp, "new": total_hp,
                             "healing_applied": total_hp - current_hp, "max_hp": total_hp,
                             "status": _format_hp_status(total_hp, total_hp)}

            hd_regained = max(level // 2, 1)
            new_hd = min(hd_count + hd_regained, level)
            _db_set(cursor, "hit_dice_count", str(new_hd))
            state.DB_CONNECTION.commit()
            changes["hit_dice"] = {"old": hd_count, "new": new_hd, "regained": hd_regained}

            if slot_table:
                sc["slots"] = {str(k): v for k, v in slot_table.items()}
                _db_set(cursor, "spellcasting", sc)
                state.DB_CONNECTION.commit()
                changes["slots_restored"] = {str(k): v for k, v in slot_table.items()}
                if caster_type == "warlock":
                    changes["slots_restored"] = {"pact_magic": {str(k): v for k, v in slot_table.items()}}

            # End the running spells but keep the lasting marks (a curse, a geas, a disease).
            ended = _end_transient_effects(cursor)
            _clear_death_saves(cursor)
            state.DB_CONNECTION.commit()
            effects_cleared = ended["effects_cleared"]
            if effects_cleared:
                changes["effects_cleared"] = effects_cleared
            if ended["conditions_cleared"]:
                changes["conditions_cleared"] = ended["conditions_cleared"]

            # SRD 5.1: a long rest removes one level of exhaustion.
            if _exhaustion_level(cursor) > 0:
                exh_change = _apply_exhaustion_change(cursor, -1, reason="long rest")
                changes["exhaustion"] = {"old": exh_change["old"], "new": exh_change["new"]}

            # A long rest ends capacity-changing effects (e.g. enhance ability: Bull's Strength).
            previous_multiplier = _db_val(cursor, "capacity_multiplier", 1)
            if str(previous_multiplier) not in ("1", "1.0"):
                _db_set(cursor, "capacity_multiplier", 1)
                state.DB_CONNECTION.commit()
                changes["capacity_multiplier"] = {"old": previous_multiplier, "new": 1}

            if prepared_spells is not None:
                if char_class in PREPARED_CASTER_CLASSES or char_class == "Wizard":
                    max_spells = get_max_prepared_spells(cursor)
                    if _needs_spellbook(cursor) and not _has_spellbook_item(cursor):
                        changes["prepared_spells_error"] = {
                            "error": "Spellbook missing.",
                            "hint": "The character's spellbook is not in their inventory; "
                                    "no spells can be prepared until it is recovered.",
                        }
                    elif max_spells is not None and len(prepared_spells) > max_spells:
                        changes["prepared_spells_error"] = {
                            "error": "Too many prepared spells.",
                            "provided": len(prepared_spells),
                            "max": max_spells,
                            "formula": "spellcasting_ability_modifier + level",
                        }
                    elif char_class == "Wizard":
                        spellbook = sc.get("spellbook", [])
                        spellbook_names = set()
                        for s in spellbook:
                            if isinstance(s, dict):
                                spellbook_names.add(s.get("name", ""))
                            else:
                                spellbook_names.add(str(s))
                        not_in_book = [s for s in prepared_spells if s not in spellbook_names]
                        if not_in_book:
                            changes["prepared_spells_error"] = {
                                "error": "Spells not in spellbook.",
                                "not_in_spellbook": not_in_book,
                                "hint": "Wizards can only prepare spells from their spellbook.",
                            }
                        else:
                            old_prepared = [s.get("name", str(s)) if isinstance(s, dict) else str(s)
                                            for s in sc.get("spells_prepared", [])]
                            sc["spells_prepared"] = [{"name": s} for s in prepared_spells]
                            _db_set(cursor, "spellcasting", sc)
                            state.DB_CONNECTION.commit()
                            changes["prepared_spells"] = {
                                "old": old_prepared,
                                "new": list(prepared_spells),
                                "count": len(prepared_spells),
                                "max": max_spells if max_spells is not None else 0,
                            }
                    else:
                        old_prepared = [s.get("name", str(s)) if isinstance(s, dict) else str(s)
                                        for s in sc.get("spells_prepared", [])]
                        sc["spells_prepared"] = [{"name": s} for s in prepared_spells]
                        _db_set(cursor, "spellcasting", sc)
                        state.DB_CONNECTION.commit()
                        changes["prepared_spells"] = {
                            "old": old_prepared,
                            "new": list(prepared_spells),
                            "count": len(prepared_spells),
                            "max": max_spells if max_spells is not None else 0,
                        }
                else:
                    changes["prepared_spells_error"] = {
                        "error": f"{char_class} is not a prepared caster.",
                        "hint": f"{char_class} uses spells_known (cannot change on long rest).",
                    }

            narrative_parts = ["Long Rest complete. HP fully restored."]
            narrative_parts.append(f"Hit Dice: {new_hd}/{level} (+{hd_regained} regained).")
            if slot_table:
                narrative_parts.append("All spell slots restored.")
            if effects_cleared:
                narrative_parts.append(f"Active effects cleared: {', '.join(effects_cleared)}.")
            if prepared_spells is not None and "prepared_spells" in changes:
                narrative_parts.append(f"Prepared spells updated ({len(prepared_spells)}/{changes['prepared_spells']['max']}).")

            hints.append("Wizard: Arcane Recovery available (once per long rest) on next short rest." if char_class == "Wizard" else None)
            hints.append("Fighter: Second Wind and Action Surge recharge on short/long rest." if char_class == "Fighter" else None)
            hints.append("Cleric: Channel Divinity recharges on short/long rest." if char_class == "Cleric" else None)
            hints.append("Warlock: Pact Magic slots recharge on short rest." if char_class == "Warlock" else None)
            hints.append("Bard: Bardic Inspiration recharges on short rest (level 5+)." if char_class == "Bard" and level >= 5 else None)
            hints.append("Monk: Ki points recharge on short rest (level 2+)." if char_class == "Monk" and level >= 2 else None)
            hints.append("Druid: Wild Shape uses recharge on short/long rest." if char_class == "Druid" and level >= 2 else None)
            hints.append("Paladin: Channel Divinity recharges on short/long rest." if char_class == "Paladin" and level >= 3 else None)
            hints.append("Carrying capacity is back to normal (a capacity-changing effect ended)." if "capacity_multiplier" in changes else None)
            hints.append("No more than one long rest per 24 hours." if True else None)
            hints = [h for h in hints if h is not None]

        result["changes"] = changes
        result["hints"] = hints
        result["narrative_format"] = " ".join(narrative_parts)
        return result

    except Exception as e:
        return {"success": False, "error": f"Error applying rest: {str(e)}"}


@mcp.tool()
def modify_exhaustion(delta: int, reason: str | None = None) -> dict:
    """Add or remove exhaustion levels on the player (0-6).

    WHEN: exertion, a hazard or magic causes or clears exhaustion.
    FIELDS:
    - delta: signed number of levels. reason: an optional short note.

    RULES:
    - The engine applies the full level table and re-derives max HP, speed and roll disadvantage; never hand-apply the penalties.
    - A long rest removes one level automatically; use this tool for every other source.
    - Level 6 is death.

    EX:
    modify_exhaustion(delta=1, reason='forced march')
    """
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    cursor = state.DB_CONNECTION.cursor()
    change = _apply_exhaustion_change(cursor, delta, reason=reason)
    effects = change["effects"]
    parts = [f"Exhaustion {change['old']} \u2192 {change['new']}"]
    if reason:
        parts.append(f"({reason})")
    if effects["dead"]:
        parts.append("\u2014 level 6: the character is dead.")
    elif effects["level"]:
        bits = []
        if effects["check_disadvantage"]:
            bits.append("disadvantage on ability checks")
        if effects["speed_multiplier"] == 0.5:
            bits.append("speed halved")
        elif effects["speed_multiplier"] == 0.0:
            bits.append("speed 0")
        if effects["attack_disadvantage"]:
            bits.append("disadvantage on attack rolls and saves")
        if effects["hp_multiplier"] == 0.5:
            bits.append(f"HP maximum halved ({change['max_hp']})")
        if bits:
            parts.append("\u2014 " + ", ".join(bits))
    result = {
        "success": True,
        "exhaustion": change["new"],
        "old": change["old"],
        "delta": change["delta"],
        "effects": effects,
        "max_hp": change["max_hp"],
        "narrative_format": " ".join(parts),
    }
    if change.get("dead"):
        result["dead"] = True
        result["death_cause"] = change.get("death_cause") or "exhaustion level 6"
    return result


@mcp.tool()
def make_death_save() -> dict:
    """Roll a death saving throw for a player at 0 HP.

    WHEN: the start of the player's turn while at 0 HP.
    RULES:
    - Three successes stabilise (counters clear, still 0 HP); three failures kill. A natural 20 regains 1 HP and clears the counters; a natural 1 counts as two failures.
    - Healing any real HP clears the counters, and damage at 0 HP adds a failure automatically -- so call this only at the start of the turn, at 0 HP.

    EX:
    make_death_save()
    """
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    cursor = state.DB_CONNECTION.cursor()
    current_hp = int(_db_val(cursor, "current_hit_points", 0) or 0)
    if _player_dead(cursor):
        return {"success": False, "error": "already_dead", "dead": True,
                "death_cause": str(_db_val(cursor, "death_cause", "") or ""),
                "reason": "The character is dead."}
    if _player_stable(cursor):
        return {"success": False, "error": "stable", "stable": True, "current_hp": 0,
                "reason": "The character is stable and unconscious; no death save is needed."}
    if current_hp > 0:
        return {"success": False, "error": "not_dying",
                "reason": "The character is not at 0 HP \u2014 no death save is needed."}
    if int(_db_val(cursor, "death_save_failures", 0) or 0) >= 3:
        # A legacy/inconsistent row: three failures is death even without the old latch.
        _mark_player_dead(cursor, "third death save failure")
        return {"success": False, "error": "already_dead", "dead": True,
                "death_cause": "third death save failure",
                "reason": "The character is dead."}
    successes = int(_db_val(cursor, "death_save_successes", 0) or 0)
    failures = int(_db_val(cursor, "death_save_failures", 0) or 0)
    roll = random.randint(1, 20)
    # Active spell effects (Beacon of Hope, Foresight) can grant advantage on death saves.
    if equipment.active_advantage_for(_carry_get(cursor), "save_advantage", "death")[0]:
        second = random.randint(1, 20)
        roll = max(roll, second)
    if roll == 20:
        hp = _apply_hp_change(cursor, 1)
        _clear_death_saves(cursor)
        return {"success": True, "roll": 20, "outcome": "revived", "hp": hp["new_value"],
                "narrative_format":
                "Death save: natural 20 \u2014 the character regains 1 HP and is conscious."}
    if roll == 1:
        failures = min(3, failures + 2)
    elif roll >= 10:
        successes = min(3, successes + 1)
    else:
        failures = min(3, failures + 1)
    _db_set(cursor, "death_save_successes", str(successes))
    _db_set(cursor, "death_save_failures", str(failures))
    state.DB_CONNECTION.commit()
    result = {"success": True, "roll": roll, "successes": successes,
              "failures": failures}
    if failures >= 3:
        _end_concentration(cursor, reason="death")
        _mark_player_dead(cursor, "third death save failure")
        result.update({"outcome": "dead", "dead": True,
                       "death_cause": "third death save failure"})
        narrative = f"Death save: {roll} \u2014 third failure. The character is dead."
    elif successes >= 3:
        _stabilize_death_saves(cursor)
        result.update({"outcome": "stable", "stable": True})
        narrative = f"Death save: {roll} \u2014 third success. The character is stable."
    else:
        result["outcome"] = "success" if roll >= 10 else "failure"
        narrative = f"Death save: {roll} \u2014 {successes} successes, {failures} failures."
    result["narrative_format"] = narrative
    return result


@mcp.tool()
def roll_dice(dice_notation: str, modifier: int = 0, actor: str = "{player_name}") -> dict:
    """Roll dice for damage, healing, loot quantity, or any random magnitude.

    WHEN: a pure magnitude roll. For success/failure use perform_check.
    FIELDS:
    - dice_notation: dice only (e.g. '3d4') -- no modifiers.
    - modifier: flat bonus/penalty on the total. actor: who rolls.

    EX:
    roll_dice(actor='Senna', dice_notation='3d4', modifier=3)
    """
    try:
        if actor == "{player_name}" and state.DB_CONNECTION is not None:
            actor = _db_val(state.DB_CONNECTION.cursor(), "name", "Player")

        parts = dice_notation.lower().split('d')
        if len(parts) != 2:
            return {"error": "Invalid dice notation. Use format 'XdY' (e.g., '2d6')."}

        num_dice = int(parts[0]) if parts[0] else 1
        die_size = int(parts[1])

        if num_dice <= 0 or die_size <= 0:
            return {"error": "Number of dice and die size must be positive integers."}

        rolls = [random.randint(1, die_size) for _ in range(num_dice)]
        total = sum(rolls) + modifier

        rolls_str = " + ".join(str(r) for r in rolls)
        if modifier != 0:
            narrative = f"{actor} {dice_notation}: {total} ({rolls_str} + {modifier})"
        else:
            narrative = f"{actor} {dice_notation}: {total} ({rolls_str})"

        return {
            "actor": actor,
            "notation": dice_notation,
            "rolls": rolls,
            "modifier": modifier,
            "total": total,
            "narrative_format": narrative,
        }
    except ValueError:
        return {"error": "Invalid dice notation. Please provide integers (e.g., '2d6')."}


@mcp.tool()
def perform_check(modifier: int | None = None, dc: int = 0, check_name: str = "Check",
                  actor: str = "{player_name}", ability: str | None = None, grapple: bool = False,
                  save: bool = False, situational_modifier: int | None = None,
                  context: str | None = None, against: str | None = None,
                  damage: str | None = None, condition: str | None = None) -> dict:
    """Perform a skill check or saving throw (d20 + modifier vs DC).

    WHEN: the success/failure of a save or ability/skill check. Weapon attacks -> resolve_attack; spells -> resolve_magic.
    FIELDS:
    - modifier: only for an NPC with no declared companion sheet, or a custom player check with no ability and no known skill (e.g. 'Luck'). IGNORED for a player check/save, and for a declared companion -- both are engine-derived.
    - dc: the DC to beat. check_name: a label (e.g. 'Athletics', 'Dexterity save'). actor: who acts.
    - ability: 'str' | 'dex' | 'con' | 'int' | 'wis' | 'cha'; REQUIRED for a player save (save=True).
    - save: True for a SAVING THROW -- the engine computes the player's modifier (effective ability + proficiency + item bonuses) and adds situational_modifier; do NOT pass modifier.
    - situational_modifier: a situational bonus/penalty on top of the derived modifier (cover, a flat Bless total).
    - context: for item skill_bonus effects with a context:<tag> predicate.
    - against / damage / condition: scope a player save for item save_advantage effects.
    - grapple: True for a GRAPPLE attempt; refused, action spent, when both hands are full. Do NOT set it to escape a grapple.

    RULES:
    - Heavily encumbered or unproficient armour: STR/DEX/CON checks roll with disadvantage; the result carries 'disadvantage_sources' and both dice.
    - Grapple with no free hand is refused with turn_lost=true (no roll).

    EX:
    perform_check(actor='Thorin', dc=15, check_name='Athletics', ability='str')
    perform_check(actor='Thorin', dc=15, check_name='Dexterity save', ability='dex', save=True)
    """
    cursor = state.DB_CONNECTION.cursor() if state.DB_CONNECTION is not None else None
    is_player = _is_player_actor(cursor, actor)
    if is_player and cursor is not None:
        actor = _db_val(cursor, "name", "Player")

    # SRD 5.1: a grapple needs a free hand — refuse before rolling anything.
    if grapple and is_player and cursor is not None:
        worn_state = _equipment_block(cursor)
        if worn_state.get("derived_from_equipped") and worn_state.get("hands_free", 2) == 0:
            return _blocked_action(
                cursor, "no_free_hand",
                "Both hands are occupied — a grapple needs at least one free hand.",
                None, "Grapple failed — both hands are busy. The action is spent.")

    disadvantage, encumbrance_sources = _roll_disadvantage(cursor, is_player, ability)
    cond_disadvantage, cond_sources = _condition_check_disadvantage(
        _conditions_for(cursor, actor, is_player=is_player), ability)
    encumbrance_sources = [*encumbrance_sources, *cond_sources]
    disadvantage = disadvantage or cond_disadvantage

    # SRD 5.1 exhaustion: level 1 → disadvantage on ability checks, level 3 → on saving throws.
    exh = _exhaustion_effects(_exhaustion_level_for(cursor, actor, is_player=is_player))
    if exh["save_disadvantage"] if save else exh["check_disadvantage"]:
        disadvantage = True
        encumbrance_sources = [*encumbrance_sources, f"exhaustion {exh['level']}"]

    save_detail = None
    check_detail = None
    advantage = False
    check_bonus_sources = []
    if is_player and save and cursor is not None:
        save_detail = _derived_save(cursor, ability, situational_modifier or 0,
                                    against=against, damage=damage, condition=condition)
        modifier = save_detail["modifier"]
        advantage = bool(save_detail.get("advantage"))
    elif is_player and cursor is not None:
        # Full Option B: the engine derives ability/skill checks (ability + proficiency /
        # Expertise / Jack of All Trades + item bonuses). The passed `modifier` is only the
        # fallback for a custom check with no ability and no known skill (e.g. 'Luck').
        if skills.skill_name(check_name) or ability:
            check_detail = _derived_check(cursor, check_name, ability, context,
                                          situational_modifier or 0)
            modifier = check_detail["modifier"]
            advantage = bool(check_detail.get("advantage"))
        else:
            modifier = int(modifier or 0) + int(situational_modifier or 0)
            check_bonus, check_bonus_sources = equipment.check_bonus_for(
                _carry_get(cursor), check_name, context)
            modifier += check_bonus
    else:
        # A declared companion's check and save are derived from its own sheet, exactly as the
        # player's are: the engine rolls its numbers, never a number the GM computed.
        sheet = _companion_here(cursor, actor)
        saves = (sheet or {}).get("saves") if sheet is not None else None
        ab = str(ability or "").strip().lower()[:3]
        if sheet is not None and save and ab and isinstance(saves, dict) and ab in saves:
            modifier = int(saves[ab]) + int(situational_modifier or 0)
        elif sheet is not None and sheet.get("stats") and (skills.skill_name(check_name) or ability):
            check_detail = _companion_check_detail(sheet, check_name, ability,
                                                   situational_modifier or 0)
            modifier = check_detail["modifier"]
        else:
            # A sheet written before the ability scores were required, or an NPC that is no
            # companion at all: the caller's number, as before.
            modifier = int(modifier or 0)

    dice_bonus = 0
    dice_sources = []
    if is_player and cursor is not None:
        field = "saving_throws" if save else "ability_checks"
        for dice in equipment.active_dice(_carry_get(cursor), field):
            raw = str(dice.get("value") or "").strip()
            sign = -1 if raw.startswith("-") else 1
            _, _, rolled = _parse_and_roll_dice(raw.lstrip("+-"))
            if rolled:
                dice_bonus += sign * rolled
                dice_sources.append(f"{raw} ({dice.get('spell')})")
                if dice.get("charges") is not None:
                    _consume_buff_charge(cursor, dice.get("spell"), field)

    roll, rolls, roll_mode, _cancelled = _roll_d20(advantage=advantage, disadvantage=disadvantage)
    total = roll + modifier + dice_bonus

    if roll == 20:
        result = "Critical Success"
    elif roll == 1:
        result = "Critical Failure"
    elif total >= dc:
        result = "Success"
    else:
        result = "Failure"

    narrative = f"{actor} {check_name}: {total} vs DC {dc} ({result}) ({roll} + {modifier})"
    if dice_bonus:
        narrative += f" {'+' if dice_bonus >= 0 else '-'}{abs(dice_bonus)} dice"
    if roll_mode:
        narrative += f" [{roll_mode}]"
    if save_detail is not None:
        bits = []
        if save_detail["ability_modifier"]:
            bits.append(f"{save_detail['ability'].upper()} {save_detail['ability_modifier']:+d}")
        if save_detail["proficiency_bonus"]:
            bits.append(f"prof {save_detail['proficiency_bonus']:+d}")
        if save_detail["item_bonus"]:
            bits.append(f"item {save_detail['item_bonus']:+d}")
        if save_detail["situational"]:
            bits.append(f"situational {save_detail['situational']:+d}")
        if bits:
            narrative += " [" + ", ".join(bits) + "]"
    if check_detail is not None:
        bits = []
        if check_detail["ability"] and check_detail["ability_modifier"]:
            bits.append(f"{check_detail['ability'].upper()} {check_detail['ability_modifier']:+d}")
        if check_detail["proficiency_bonus"]:
            tag = ("expertise" if check_detail["expertise"]
                   else "JoAT" if check_detail["jack_of_all_trades"] else "prof")
            bits.append(f"{tag} {check_detail['proficiency_bonus']:+d}")
        if check_detail["item_bonus"]:
            bits.append(f"item {check_detail['item_bonus']:+d}")
        if check_detail["situational"]:
            bits.append(f"situational {check_detail['situational']:+d}")
        if bits:
            narrative += " [" + ", ".join(bits) + "]"

    # The acting combatant's turn starts here: resolve any pending repeating saves.
    rec_results, rec_lines = _advance_turn(actor)
    if rec_lines:
        narrative += "\n" + "\n".join(rec_lines)
    woke = _drain_woke()
    for entry in woke:
        narrative += f"\n{entry['name']} wakes — {', '.join(entry['conditions'])} ends"

    response = {
        "actor": actor,
        "check_name": check_name,
        "base_roll": roll,
        "modifier": modifier,
        "total": total,
        "dc_to_beat": dc,
        "outcome": result,
        "narrative_format": narrative,
    }
    if rec_results:
        response["recurring"] = rec_results
    if woke:
        response["conditions_woke"] = woke
    if save_detail is not None:
        response["save"] = save_detail
        if save_detail.get("advantage"):
            response["advantage_sources"] = save_detail.get("advantage_sources")
    elif check_detail is not None:
        response["check"] = check_detail
        if check_detail["item_sources"]:
            response["item_bonus_sources"] = check_detail["item_sources"]
    elif check_bonus_sources:
        response["item_bonus_sources"] = check_bonus_sources
    if dice_bonus:
        response["dice_bonus"] = dice_bonus
        response["dice_sources"] = dice_sources
    if rolls:
        response[f"{roll_mode}_rolls"] = rolls
    _encumbrance_note(response, encumbrance_sources)

    return response


@mcp.tool()
def device_jump_reset() -> dict:
    """ENGINE-ONLY (never offered to the Game Master): the Device jump's effect reset.

    The Traveller crosses an age and the crossing ends the running spells: every buff is reverted,
    concentration drops, and the transient conditions clear -- while a curse, a geas or a disease
    stays. Called by the engine when the Device fires."""
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    try:
        cursor = state.DB_CONNECTION.cursor()
        return {"success": True, **_end_transient_effects(cursor)}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"Error resetting for the jump: {str(e)}"}
