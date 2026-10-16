"""The `resolve_attack` MCP tool: one attack resolution against the combat registry."""

import random
import equipment
from .app import mcp
from . import state
from .combat import (
    _apply_target_damage, _attach_roster, _blocked_action, _derive_npc_attack,
    _finish_combat_if_over, _flush_recurring_into, _npc_attack_is_ranged, _registry_ac,
    _registry_entry
)
from .conditions import (
    _apply_damage_modifiers, _apply_player_damage_modifiers, _condition_attack_effects,
    _conditions_for
)
from .db import _carry_get, _db_val, _equipment_block
from .dice import _parse_and_roll_dice, _roll_d20
from .items import _derive_weapon_attack, _is_light_melee
from .sheet import (
    _encumbrance_note, _exhaustion_effects, _exhaustion_level_for, _is_player_actor,
    _roll_disadvantage, _target_suffix
)
from .spell_effects import _consume_buff_charge
from .vitals import _apply_hp_change


@mcp.tool()
def resolve_attack(
    actor: str,
    attack_modifier: int | None = None,
    target_ac: int | None = None,
    damage_dice: str | None = None,
    damage_modifier: int | None = None,
    target_name: str = "",
    target_current_hp: int | None = None,
    challenge_rating: float | None = None,
    extra_damage_dice: str = "",
    extra_damage_modifier: int = 0,
    is_npc_attack: bool = False,
    is_npc_vs_npc: bool = False,
    advantage: bool = False,
    force_crit: bool = False,
    weapon: str | None = None,
    off_hand: bool = False,
    attack: str | None = None,
    damage_type: str | None = None,
) -> dict:
    """Resolve a weapon/unarmed attack: roll, damage, HP, kills, XP.

    WHEN: any weapon or unarmed attack. Spells -> resolve_magic; checks -> perform_check.
    FIELDS:
    - actor: the attacker's name.
    - weapon: (player) the item's exact inventory name; the engine derives modifier/dice/type from it, its SRD archetype (or `base`) and the character's stats. If it is not in hand the attack is REFUSED (no roll; action spent).
    - attack: (NPC) a declared attack name in its registry entry; the engine derives bonus/dice/type. Refused if the actor is unregistered or the attack undeclared.
    - damage_type: for resistances; auto-filled from weapon/attack when omitted.
    - off_hand: True for the bonus-action attack of two-weapon fighting (both hands must hold a different light melee weapon; no ability modifier unless negative).
    - target_ac: omit for a registry target (uses its AC). target_name/target_current_hp/challenge_rating: registry lookup (HP/CR auto if omitted). An unregistered/misspelled target_name, or none at all, with no target_ac is REFUSED ('target_not_registered'/'target_ac_required') — never attacked against a guessed AC.
    - attack_modifier/damage_dice/damage_modifier: explicit values (omit when passing weapon; an explicit value wins).
    - extra_damage_dice: bonus dice, NOT doubled on a crit. extra_damage_modifier: flat extra.
    - is_npc_attack: NPC->player, damage auto-applied, no slot. is_npc_vs_npc: NPC->NPC, no player HP, no auto XP.
    - advantage; force_crit.

    RULES:
    - Registry HP updates after each hit, so sequential hits use the reduced HP.
    - XP is auto-awarded on kill (unless is_npc_vs_npc).
    - A refused attack returns success=false with error 'item_not_equipped' | 'item_not_carried' | 'weapon_stats_unknown', turn_lost=true, gm_instruction.
    - A Versatile weapon uses its two-handed die while the other hand is free; a one-handed Ammunition weapon cannot fire while the other hand holds something (error 'cannot_reload').
    - Conditions drive the roll (advantage/disadvantage, melee crits vs helpless); damage to a registered NPC is adjusted by its resistances/immunities/vulnerabilities ('damage_modified').
    - Unproficient armour/shield -> attack at disadvantage ('disadvantage_sources').

    EX:
    resolve_attack(actor='{player_name}', weapon='Longsword', target_ac=13, target_name='Goblin', target_current_hp=12, challenge_rating=0.5)
    """
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}

    try:
        cursor = state.DB_CONNECTION.cursor()

        is_player_attacker = (not is_npc_attack and not is_npc_vs_npc) and _is_player_actor(cursor, actor)
        if is_player_attacker and state.DB_CONNECTION is not None:
            actor = _db_val(cursor, "name", "Player")

        # ── derived equipped weapon (SRD 5.1) ───────────────────────────────
        weapon_info = None
        equipment_warnings = []
        if weapon and is_player_attacker:
            inventory = _db_val(cursor, "inventory", []) or []
            carried = [(e.get("name") if isinstance(e, dict) else str(e)) for e in inventory]
            if weapon not in carried:
                return _blocked_action(
                    cursor, "item_not_carried", f"{weapon} is not in your inventory.", weapon,
                    f"Attack wasted — {weapon} is not carried. The action is spent.")
            worn_state = _equipment_block(cursor)
            in_hands = [h.get("name") for h in worn_state.get("hands", [])]
            if weapon not in in_hands:
                held = ", ".join(h for h in in_hands if h) or "nothing"
                return _blocked_action(
                    cursor, "item_not_equipped",
                    f"{weapon} is not equipped (holding: {held}).", weapon,
                    f"Attack wasted — {weapon} is not equipped (holding: {held}). The action is spent.")
            weapon_info = _derive_weapon_attack(cursor, weapon, inventory,
                                                worn_state.get("hands_free", 1), off_hand)
            equipment_warnings = worn_state.get("warnings", [])
            other_held = next((h.get("name") for h in worn_state.get("hands", [])
                               if h.get("name") and h.get("name") != weapon), None)

            # SRD 5.1 ammunition: a one-handed loading weapon needs a free hand to reload.
            if (any("ammunition" in str(p).lower() for p in weapon_info["properties"])
                    and not weapon_info["two_handed"] and worn_state.get("hands_free", 2) == 0):
                return _blocked_action(
                    cursor, "cannot_reload",
                    f"{weapon} needs a free hand to load, but {other_held} occupies it.", weapon,
                    f"Attack wasted — {weapon} cannot be reloaded while {other_held} is held. "
                    f"The action is spent.")

            # SRD 5.1 two-weapon fighting: a different light melee weapon in the other hand.
            if off_hand:
                if other_held is None:
                    return _blocked_action(
                        cursor, "off_hand_needs_another_weapon",
                        "Two-weapon fighting needs a light melee weapon in the other hand.", weapon,
                        "Off-hand attack wasted — the other hand is empty. The action is spent.")
                if not (_is_light_melee(weapon, inventory)
                        and _is_light_melee(other_held, inventory)):
                    return _blocked_action(
                        cursor, "two_weapon_requires_light_melee",
                        f"Two-weapon fighting needs a light melee weapon in each hand "
                        f"({weapon} / {other_held}).", weapon,
                        f"Off-hand attack wasted — {weapon} and {other_held} are not both light "
                        f"melee weapons. The action is spent.")

        attack_info = None
        multiattack = None
        if attack and not is_player_attacker:
            entry = _registry_entry(actor)
            if entry is None:
                return {"success": False, "error": "actor_not_registered",
                        "reason": f"'{actor}' is not in the combat registry — call "
                                  f"register_combatants first."}
            attack_info = _derive_npc_attack(entry, attack)
            if attack_info is None:
                return {"success": False, "error": "attack_not_declared", "actor": actor,
                        "reason": f"'{actor}' has no declared attack named '{attack}'.",
                        "declared_attacks": [a.get("name") for a in (entry.get("attacks") or [])]}
            if attack_modifier is None:
                attack_modifier = attack_info.get("attack_bonus")
            if damage_dice is None:
                damage_dice = attack_info.get("damage_dice")
            if damage_modifier is None:
                damage_modifier = int(attack_info.get("damage_modifier") or 0)
            if damage_type is None:
                damage_type = attack_info.get("damage_type")
            base = attack_info.get("base")
            if base and (not damage_dice or not damage_type):
                archetype = equipment.weapon_entry(base) or {}
                damage_dice = damage_dice or archetype.get("damage")
                damage_type = damage_type or archetype.get("damage_type")
            multiattack = entry.get("multiattack")

        if attack_modifier is None and weapon_info is not None:
            attack_modifier = weapon_info["attack_modifier"]
        if damage_dice is None and weapon_info is not None:
            damage_dice = weapon_info["damage_dice"]
        if damage_modifier is None:
            damage_modifier = weapon_info["damage_modifier"] if weapon_info else 0
        if damage_type is None and weapon_info is not None:
            damage_type = weapon_info.get("damage_type")
        if attack_modifier is None:
            return {"success": False, "error": "attack_modifier_required",
                    "reason": "Pass attack_modifier, or weapon=/attack= to let the engine derive it."}
        if not damage_dice:
            if weapon and is_player_attacker:
                return _blocked_action(
                    cursor, "weapon_stats_unknown",
                    f"No damage is known for {weapon} — declare damage_dice (or a base archetype) "
                    f"with update_player_list.", weapon,
                    f"Attack wasted — {weapon} has no known damage. The action is spent.")
            return {"success": False, "error": "damage_dice_required",
                    "reason": "Pass damage_dice, or weapon=/attack= for a known weapon."}

        # A registered target's AC comes from its stat block when the caller omits it. A name
        # the registry does not hold (or no target at all) has no AC to use: the engine refuses
        # rather than attacking against a fabricated AC 10, which would silently succeed.
        if target_ac is None:
            target_ac = _registry_ac(target_name) if target_name else None
        if target_ac is None:
            if target_name:
                return {
                    "success": False,
                    "error": "target_not_registered",
                    "target_name": target_name,
                    "reason": f"'{target_name}' is not in the combat registry, so its AC is "
                              f"unknown.",
                    "gm_instruction": "Register the combatants with register_combatants, or pass "
                                      "target_ac, then resolve the attack again.",
                }
            return {
                "success": False,
                "error": "target_ac_required",
                "reason": "Provide target_ac (or a registered target_name).",
                "gm_instruction": "Pass target_ac, or name a registered target via target_name, "
                                  "then resolve the attack again.",
            }

        # SRD 5.1 variant: heavy encumbrance (and armour proficiency) gives disadvantage on
        # attack rolls; conditions on either side drive it too.
        ranged = False
        if weapon_info is not None:
            ranged = not weapon_info.get("melee", True)
        elif attack_info is not None:
            ranged = _npc_attack_is_ranged(attack_info)
        target_is_player = bool(is_npc_attack)
        attacker_conditions = _conditions_for(cursor, actor, is_player=is_player_attacker)
        target_conditions = _conditions_for(cursor, target_name, is_player=target_is_player)
        cond_adv, cond_dis, cond_crit, cond_sources = _condition_attack_effects(
            attacker_conditions, target_conditions, ranged)
        if cond_crit:
            force_crit = True
        # Active spell effects: Foresight grants the player advantage on their attacks, and
        # disadvantage on attacks made against them.
        if is_player_attacker and cursor is not None:
            if equipment.active_advantage_for(_carry_get(cursor), "attack_advantage")[0]:
                advantage = True
        if target_is_player and cursor is not None:
            if equipment.active_advantage_for(_carry_get(cursor), "attackers_disadvantage")[0]:
                cond_dis = True
        enc_disadvantage, encumbrance_sources = _roll_disadvantage(cursor, is_player_attacker)
        encumbrance_sources = [*encumbrance_sources, *cond_sources]
        exh = _exhaustion_effects(_exhaustion_level_for(
            cursor, actor, is_player=is_player_attacker))
        if exh["attack_disadvantage"]:
            enc_disadvantage = True
            encumbrance_sources = [*encumbrance_sources, f"exhaustion {exh['level']}"]
        d20, advantage_rolls, roll_mode, advantage_cancelled = _roll_d20(
            advantage=advantage or cond_adv, disadvantage=enc_disadvantage or cond_dis)
        die_label = f"{min(advantage_rolls)} / {max(advantage_rolls)} → " if advantage_rolls else ""

        attack_dice_bonus = 0
        if is_player_attacker and cursor is not None:
            for dice in equipment.active_dice(_carry_get(cursor), "attack_rolls"):
                raw = str(dice.get("value") or "").strip()
                sign = -1 if raw.startswith("-") else 1
                _, _, rolled = _parse_and_roll_dice(raw.lstrip("+-"))
                if rolled:
                    attack_dice_bonus += sign * rolled
        total_attack = d20 + attack_modifier + attack_dice_bonus

        is_natural_1 = (d20 == 1)
        is_natural_20 = (d20 == 20)
        is_forced_crit = False

        if is_natural_1:
            outcome = "Critical Failure"
            is_crit = False
        elif is_natural_20:
            outcome = "Critical Success"
            is_crit = True
        elif total_attack >= target_ac:
            if force_crit:
                outcome = "Critical Success"
                is_crit = True
                is_forced_crit = True
            else:
                outcome = "Success"
                is_crit = False
        else:
            outcome = "Failure"
            is_crit = False

        adv_label = f" ({roll_mode.capitalize()})" if roll_mode else ""

        narrative_parts = []
        attack_label = ""
        if weapon and is_player_attacker:
            attack_label = f" with {weapon}"
        elif attack_info is not None:
            attack_label = f" with {attack}"
        narrative_parts.append(
            f"{actor} Attack{attack_label}{adv_label}{_target_suffix(cursor, target_name, bool(is_npc_attack))}: {die_label}{total_attack} vs AC {target_ac} ({outcome}) ({d20} + {attack_modifier})"
        )

        result = {
            "success": True,
            "actor": actor,
            "target_name": target_name,
            "attack_roll": d20,
            "attack_modifier": attack_modifier,
            "total_attack": total_attack,
            "target_ac": target_ac,
            "outcome": outcome,
            "is_crit": is_crit,
            "is_natural_20": is_natural_20,
            "advantage": advantage,
            "force_crit": force_crit,
        }
        if advantage_rolls:
            result[f"{roll_mode}_rolls"] = advantage_rolls
        result["disadvantage"] = bool(enc_disadvantage)
        if weapon_info is not None:
            result["used_item"] = weapon
            result["attack_ability"] = weapon_info["ability"]
            result["proficient"] = weapon_info["proficient"]
            result["damage_modifier"] = damage_modifier
            result["damage_dice"] = damage_dice
            if off_hand:
                result["two_weapon"] = True
                result["bonus_action_consumed"] = True
                result["off_hand_weapon"] = weapon
            if weapon_info.get("bonus_suppressed"):
                result["magic_bonus_suppressed"] = weapon_info["bonus_suppressed"]
            if weapon_info.get("damage_type"):
                result["damage_type"] = weapon_info["damage_type"]
            if weapon_info.get("item_bonus"):
                result["item_bonus"] = weapon_info["item_bonus"]
            if equipment_warnings:
                result["equipment_warnings"] = equipment_warnings
        _encumbrance_note(result, encumbrance_sources, advantage_cancelled)
        if is_forced_crit:
            result["forced_crit"] = True
        if attack_info is not None:
            result["used_attack"] = attack
            result["damage_dice"] = damage_dice
            result["damage_type"] = damage_type
            result["multiattack"] = multiattack
        if attacker_conditions or target_conditions:
            result["conditions"] = {"attacker": sorted(attacker_conditions),
                                    "target": sorted(target_conditions)}

        if outcome in ("Failure", "Critical Failure"):
            result["damage_total"] = 0
            result["target_killed"] = None
            if is_npc_vs_npc:
                result["npc_vs_npc"] = True
            _flush_recurring_into(result, narrative_parts)
            result["narrative_format"] = "\n".join(narrative_parts)
            _attach_roster(result)
            _finish_combat_if_over()
            return result

        primary_die_size, primary_rolls, primary_sum = _parse_and_roll_dice(damage_dice)
        if primary_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation: '{damage_dice}'. Use format 'XdY' (e.g., '2d6')."}

        if outcome == "Critical Success":
            crit_rolls = [random.randint(1, primary_die_size) for _ in range(len(primary_rolls))]
            primary_damage = sum(primary_rolls) + sum(crit_rolls) + damage_modifier
            crit_rolls_str = " + ".join(str(r) for r in crit_rolls)
            if damage_modifier != 0:
                narrative_parts.append(
                    f"{actor} Damage: {primary_damage} ({' + '.join(str(r) for r in primary_rolls)} + {crit_rolls_str} + {damage_modifier}) [CRIT]"
                )
            else:
                narrative_parts.append(
                    f"{actor} Damage: {primary_damage} ({' + '.join(str(r) for r in primary_rolls)} + {crit_rolls_str}) [CRIT]"
                )
            result["crit_damage_rolls"] = crit_rolls
        else:
            primary_damage = sum(primary_rolls) + damage_modifier
            if damage_modifier != 0:
                narrative_parts.append(
                    f"{actor} Damage: {primary_damage} ({' + '.join(str(r) for r in primary_rolls)} + {damage_modifier})"
                )
            else:
                narrative_parts.append(
                    f"{actor} Damage: {primary_damage} ({' + '.join(str(r) for r in primary_rolls)})"
                )

        extra_damage = 0
        extra_rolls = []
        if extra_damage_dice:
            extra_die_size, extra_base_rolls, extra_base_sum = _parse_and_roll_dice(extra_damage_dice)
            if extra_die_size is None:
                return {"success": False, "error": f"Invalid extra_damage_dice notation: '{extra_damage_dice}'. Use format 'XdY' (e.g., '1d6')."}
            extra_rolls = extra_base_rolls

            # Bug 2 fix: extra dice are NOT doubled on crit
            extra_damage = sum(extra_base_rolls) + extra_damage_modifier
            crit_tag = " [NO CRIT]" if outcome == "Critical Success" else ""
            if extra_damage_modifier != 0:
                narrative_parts.append(
                    f"{actor} Extra Damage: {extra_damage} ({' + '.join(str(r) for r in extra_base_rolls)} + {extra_damage_modifier}){crit_tag}"
                )
            else:
                narrative_parts.append(
                    f"{actor} Extra Damage: {extra_damage} ({' + '.join(str(r) for r in extra_base_rolls)}){crit_tag}"
                )

            result["extra_damage"] = extra_damage
            result["extra_damage_rolls"] = extra_rolls
            result["extra_damage_modifier"] = extra_damage_modifier

        damage_dice_bonus = 0
        if is_player_attacker and cursor is not None:
            for dice in equipment.active_dice(_carry_get(cursor), "damage_rolls"):
                raw = str(dice.get("value") or "").strip()
                sign = -1 if raw.startswith("-") else 1
                _, _, rolled = _parse_and_roll_dice(raw.lstrip("+-"))
                if not rolled:
                    continue
                value = sign * rolled
                # A typed buff (Branding Smite: radiant) resists on its own type.
                buff_type = dice.get("damage_type")
                buff_target = _registry_entry(target_name) if target_name else None
                if buff_type and buff_target is not None and value > 0:
                    value, _buff_note = _apply_damage_modifiers(buff_target, value, buff_type)
                damage_dice_bonus += value
                # A one-shot buff (the next hit) is spent now.
                if dice.get("charges") is not None:
                    _consume_buff_charge(cursor, dice.get("spell"), "damage_rolls")
        total_damage = primary_damage + extra_damage + damage_dice_bonus
        result["damage_dice_bonus"] = damage_dice_bonus

        # SRD 5.1 resistances/immunities/vulnerabilities of a registered NPC target.
        target_entry = _registry_entry(target_name) if target_name else None
        if not is_npc_attack and target_entry is not None:
            total_damage, dmg_note = _apply_damage_modifiers(target_entry, total_damage, damage_type)
            if dmg_note:
                result["damage_modified"] = dmg_note
                narrative_parts.append(f"Damage adjusted: {dmg_note} ({target_name})")

        result["damage_total"] = total_damage
        result["primary_damage"] = primary_damage
        result["primary_damage_rolls"] = primary_rolls
        result["damage_modifier"] = damage_modifier
        result["primary_die_size"] = primary_die_size
        if damage_type:
            result["damage_type"] = damage_type
        if multiattack is not None:
            result["multiattack"] = multiattack
        if attacker_conditions or target_conditions:
            result["conditions"] = {"attacker": sorted(attacker_conditions),
                                    "target": sorted(target_conditions)}

        if is_npc_attack and total_damage > 0:
            total_damage, dmg_note = _apply_player_damage_modifiers(cursor, total_damage, damage_type)
            if dmg_note:
                result["damage_modified"] = dmg_note
                narrative_parts.append(f"Damage adjusted: {dmg_note}")
            result["damage_total"] = total_damage
            hp_result = _apply_hp_change(cursor, -total_damage)
            result["hp_change"] = hp_result
            target_remaining = hp_result["new_value"]
        elif is_npc_vs_npc:
            result["npc_vs_npc"] = True
            _apply_target_damage(target_name, total_damage, result, narrative_parts,
                                 current_hp=target_current_hp, challenge_rating=challenge_rating,
                                 require_damage=True)
        elif not is_npc_attack and not is_npc_vs_npc:
            _apply_target_damage(target_name, total_damage, result, narrative_parts,
                                 current_hp=target_current_hp, challenge_rating=challenge_rating,
                                 award_xp=True, cursor=cursor)
        else:
            result["target_killed"] = None

        # The acting combatant's turn starts here: resolve any pending repeating saves.
        _flush_recurring_into(result, narrative_parts)

        result["narrative_format"] = "\n".join(narrative_parts)
        _attach_roster(result)
        _finish_combat_if_over()

        return result

    except Exception as e:
        return {"success": False, "error": f"Error resolving attack: {str(e)}"}
