"""P3 of `resolve_magic`: a cast that got this far, actually resolved.

`resolve_spell_effects` walks the delivery order -- projectiles, the attack roll, the
no-damage / 0d0 / temporary-HP / automatic-healing cascade, the HP pool, the damage roll,
the multi-target save loop, the single-target save, the HP application and the automatic
condition -- and returns the finished tool result.
"""

import random

from .combat import (
    _apply_target_damage, _heal_target, _record_saved_target, _registry_cr, _registry_entry,
    _registry_hp, _registry_kill, _registry_max_hp, _registry_save, _registry_update_hp,
    _scale_repeat_dice
)
from .conditions import (
    _apply_component_damage, _apply_player_damage_modifiers, _condition_save_effects,
    _conditions_for
)
from .consts import CONDITION_EFFECTS, CR_XP_TABLE
from .db import _db_val, _exhaustion_level
from .dice import _parse_and_roll_dice, _roll_d20
from .magic_cast import CastState
from .sheet import (
    _caster_spellcasting_mod, _derived_save, _encumbrance, _encumbrance_note, _exhaustion_effects,
    _is_player_actor, _target_label, _target_suffix
)
from .spell_effects import _finalize_spell_result, _resolve_projectile_spell
from .vitals import _apply_hp_change
from .tools_player import modify_player_numeric


def _resolve_save(cast: CastState, saver_name, is_player, save_mod, save_detail) -> dict:
    """One saving throw against the cast's DC: every source of advantage or disadvantage,
    the d20, the DC check, and the maintain bookkeeping when the saver shrugs it off.

    Shared by the multi-target loop and the single-target path; the caller narrates from
    the dict this returns (`d20`, `rolls`, `mode`, `total`, `modifier`, `success`,
    `auto_fail`, `disadvantage`, `sources`, `detail`).
    """
    save_disadvantage, save_sources = _encumbrance(cast.cursor, is_player, cast.sp_save_type)
    cond_save_dis, auto_fail, cond_save_sources = _condition_save_effects(
        _conditions_for(cast.cursor, saver_name, is_player=is_player), cast.sp_save_type)
    save_disadvantage = save_disadvantage or cond_save_dis
    save_sources = [*save_sources, *cond_save_sources]
    save_advantage = bool(save_detail.get("advantage")) if save_detail else False
    if save_detail is not None:
        exh = _exhaustion_effects(_exhaustion_level(cast.cursor))
        if exh["save_disadvantage"]:
            save_disadvantage = True
            save_sources = [*save_sources, f"exhaustion {exh['level']}"]
    save_d20, save_rolls, save_mode, _save_cancelled = _roll_d20(
        advantage=save_advantage, disadvantage=save_disadvantage)
    save_total = save_d20 + save_mod
    save_success = save_total >= cast.spell_save_dc and not auto_fail
    if save_success and cast.maintain_spell:
        _record_saved_target(cast.actor, cast.spell_name, saver_name)
    return {"d20": save_d20, "rolls": save_rolls, "mode": save_mode, "total": save_total,
            "modifier": save_mod, "success": save_success, "auto_fail": auto_fail,
            "disadvantage": save_disadvantage, "sources": save_sources,
            "detail": save_detail}


def _apply_target_hp(cast: CastState, tname, is_player, t_damage, tchp, save, t) -> tuple[dict, bool]:
    """Damage or heal ONE target of a multi-target save and build its `targets[]` entry.

    `save` is what `_resolve_save` returned, `t` the target dict (its HP overrides).
    Returns the entry and whether the target died.
    """
    heal_entry = None
    if cast.sp_healing:
        heal_entry = _heal_target(
            tname, t_damage, cast.cursor, is_player=is_player,
            override_current=t.get("current_hp"), override_max=t.get("max_hp"))
        remaining = heal_entry["remaining_hp"]
        killed = False
        if heal_entry["healing_applied"] > 0:
            cast.narrative_parts.append(f"{tname} healed for {heal_entry['healing_applied']} HP")
        elif not cast.sp_no_damage:
            cast.narrative_parts.append(f"{tname} already at full HP")
    else:
        remaining = tchp - t_damage
        killed = (remaining <= 0 if tchp > 0 else False)
        if not is_player and tname:
            _registry_update_hp(tname, max(remaining, 0))
        if killed:
            if not is_player:
                _registry_kill(tname)
            max_hp = _registry_max_hp(tname) if tname else 0
            display_max = f"/{max_hp}" if max_hp else ""
            cast.narrative_parts.append(f"{tname} HP: 0{display_max} (KILLED)")

    if heal_entry is not None:
        tr = {"name": tname, "save_roll": save["d20"], "save_modifier": save["modifier"],
              "save_total": save["total"], "save_success": save["success"],
              "damage": t_damage,
              "healing_total": heal_entry["healing_total"],
              "healing_applied": heal_entry["healing_applied"],
              "remaining_hp": remaining, "max_hp": heal_entry["max_hp"],
              "killed": False}
        if heal_entry.get("hp_change"):
            tr["hp_change"] = heal_entry["hp_change"]
    else:
        tr = {"name": tname, "save_roll": save["d20"], "save_modifier": save["modifier"],
               "save_total": save["total"], "save_success": save["success"],
               "damage": t_damage, "remaining_hp": max(0, remaining), "killed": killed}
        if is_player and t_damage > 0 and cast.cursor:
            t_damage, dmg_note = _apply_player_damage_modifiers(cast.cursor, t_damage, cast.sp_damage_type)
            if dmg_note:
                tr["damage_modified"] = dmg_note
                tr["damage"] = t_damage
            hp_result = _apply_hp_change(cast.cursor, -t_damage)
            tr["hp_change"] = hp_result
    if save["auto_fail"]:
        tr["save_auto_failed"] = True
    if save["rolls"]:
        tr[f"save_{save['mode']}_rolls"] = save["rolls"]
    if save["sources"]:
        tr["disadvantage_sources"] = list(save["sources"])
    return tr, killed


def _resolve_instant_kill(cast: CastState, *, target_current_hp, challenge_rating) -> dict:
    """Power Word Kill: a target at or below the spell's HP threshold dies; above it, nothing.

    SRD 5.1: "If the creature has 100 hit points or fewer, it dies. Otherwise, the spell has no
    effect." The threshold is the row's own `instant_kill_hp_threshold`; the target's HP is the
    caller's `target_current_hp`, else a registry lookup -- and is never guessed. An unknown HP
    is refused so the Game Master supplies it.
    """
    target = cast.target_name
    label = _target_label(cast.cursor, target, bool(cast.is_npc_attack)) or target or cast.actor
    current_hp = target_current_hp
    if current_hp is None and target:
        current_hp = _registry_hp(target)
    if current_hp is None:
        return {
            "success": False,
            "error": "instant_kill_hp_unknown",
            "reason": f"{cast.spell_name} needs {label}'s current hit points, and none are known "
                      f"(no `target_current_hp`, and {label} is not a registered combatant).",
            "gm_instruction": f"Give the target's current hit points via `target_current_hp`, or "
                              f"register the target as a combatant, then cast {cast.spell_name} "
                              f"again. The {int(cast.sp_instant_kill_threshold or 0)}-HP threshold "
                              f"decides whether the target dies -- do not guess it.",
        }
    threshold = int(cast.sp_instant_kill_threshold or 0)
    cast.result["damage_type"] = cast.sp_damage_type
    if current_hp <= threshold:
        # A death is not damage: run it through the same kill/XP machinery a normal hit uses.
        # Pass the caller's HP (not the registry value we just read) so a registry target is
        # detected inside and its HP written down.
        _apply_target_damage(
            target, current_hp, cast.result, cast.narrative_parts,
            current_hp=target_current_hp, challenge_rating=challenge_rating,
            award_xp=not (cast.is_npc_attack or cast.is_npc_vs_npc),
            require_damage=cast.is_npc_vs_npc, player_fallback=True,
            cursor=cast.cursor, damage_type=cast.sp_damage_type,
        )
        cast.result["instant_kill"] = True
        cast.result["damage_total"] = 0
        cast.narrative_parts.append(
            f"{label} dies — {current_hp} HP is at or below {cast.spell_name}'s {threshold}-HP threshold")
    else:
        cast.result["instant_kill"] = False
        cast.result["damage_total"] = 0
        cast.result["target_killed"] = None
        cast.narrative_parts.append(
            f"{label}: no effect — {label} has more than {threshold} hit points ({current_hp} current)")
    return _finalize_spell_result(
        cast.result, cast.narrative_parts, cast.sp_duration, cast.sp_buffs,
        cast.sp_requires_concentration, cast.is_npc_attack or cast.is_npc_vs_npc,
        cast.is_npc_vs_npc)


def _unregistered_target_refusal(cast: CastState, name: str, missing: list[str]) -> dict:
    """A save target the engine cannot resolve: the caller passed none of `missing`, and the
    name is not a registered combatant.

    Refused rather than reporting a fabricated 0 HP / +0 save. The explicit-value path -- pass
    `current_hp`/`save_modifier` in the target dict, or the single-target parameters -- needs no
    registration and is untouched.
    """
    fields = " and ".join(missing)
    return {
        "success": False,
        "error": "save_target_not_registered",
        "spell_name": cast.spell_name,
        "target_name": name,
        "missing": list(missing),
        "reason": f"{cast.spell_name} needs {name}'s {fields}, and '{name}' is not a registered "
                  f"combatant.",
        "gm_instruction": f"Register {name} as a combatant, or pass its {fields} with the target, "
                          f"then cast the spell again -- do not guess.",
    }


def resolve_spell_effects(
    cast: CastState,
    *,
    advantage: bool,
    force_crit: bool,
    target_current_hp: int | None,
    target_max_hp: int | None,
    challenge_rating: float | None,
    target_save_modifier: int | None,
    player_situational_modifier: int | None,
) -> dict:
    """Deliver one cast: attack or save, damage or healing, HP, kills and XP."""
    # ── PROJECTILE SPELLS (Magic Missile / Scorching Ray / Eldritch Blast) ──
    if cast.spell and (cast.spell.get("projectiles") or cast.spell.get("cantrip_projectile_scaling")) and not cast.sp_healing:
        if cast.spell.get("cantrip_projectile_scaling"):
            projectile_count = 1 + sum(1 for lvl in (5, 11, 17) if cast.character_level >= lvl)
        else:
            projectile_count = (int(cast.spell.get("projectiles", 0))
                                + int(cast.spell.get("projectiles_per_level", 0)) * max(0, (cast.computed_slot or 0) - cast.sp_level))
        proj = _resolve_projectile_spell(
            actor=cast.actor, spell_name=cast.spell_name, spell=cast.spell, count=projectile_count,
            attack_type=cast.sp_attack_type, damage_dice=cast.sp_damage_dice, damage_modifier=cast.sp_damage_modifier,
            damage_type=cast.sp_damage_type, targets=cast.targets, target_name=cast.target_name,
            target_current_hp=target_current_hp, target_ac=cast.target_ac, challenge_rating=challenge_rating,
            is_npc_attack=cast.is_npc_attack, is_npc_vs_npc=cast.is_npc_vs_npc, cursor=cast.cursor,
            spell_attack_modifier=cast.spell_attack_modifier, advantage=advantage, force_crit=force_crit,
            result=cast.result, narrative_parts=cast.narrative_parts, disadvantage=cast.enc_disadvantage,
        )
        if proj.get("success") is False:
            return proj
        if cast.enc_disadvantage:
            cast.result["disadvantage_sources"] = list(cast.encumbrance_sources)
            if advantage:
                cast.result["advantage_cancelled"] = True
        return _finalize_spell_result(proj, cast.narrative_parts, cast.sp_duration, cast.sp_buffs,
                                      cast.sp_requires_concentration,
                                      cast.is_npc_attack or cast.is_npc_vs_npc, cast.is_npc_vs_npc)

    # ── MULTI-TARGET ATTACK ROLLS (one attack per target; targets[] is never dropped) ──
    if cast.has_targets and cast.sp_attack_type == "attack_roll" and not cast.is_multi_projectile and not cast.sp_healing:
        entries = [{**t, "darts": 1} for t in cast.targets if isinstance(t, dict)]
        proj = _resolve_projectile_spell(
            actor=cast.actor, spell_name=cast.spell_name, spell=cast.spell, count=len(entries),
            attack_type=cast.sp_attack_type, damage_dice=cast.sp_damage_dice,
            damage_modifier=cast.sp_damage_modifier, damage_type=cast.sp_damage_type,
            targets=entries, target_name=cast.target_name, target_current_hp=target_current_hp,
            target_ac=cast.target_ac, challenge_rating=challenge_rating,
            is_npc_attack=cast.is_npc_attack, is_npc_vs_npc=cast.is_npc_vs_npc, cursor=cast.cursor,
            spell_attack_modifier=cast.spell_attack_modifier, advantage=advantage,
            force_crit=force_crit, result=cast.result, narrative_parts=cast.narrative_parts,
            disadvantage=cast.enc_disadvantage,
        )
        if proj.get("success") is False:
            return proj
        return _finalize_spell_result(proj, cast.narrative_parts, cast.sp_duration, cast.sp_buffs,
                                      cast.sp_requires_concentration,
                                      cast.is_npc_attack or cast.is_npc_vs_npc, cast.is_npc_vs_npc)

    # ── ATTACK ROLL ──
    if cast.sp_attack_type == "attack_roll":
        d20, advantage_rolls, roll_mode, advantage_cancelled = _roll_d20(
            advantage=advantage, disadvantage=cast.enc_disadvantage)
        die_label = f"{min(advantage_rolls)} / {max(advantage_rolls)} → " if advantage_rolls else ""
        total_attack = d20 + cast.spell_attack_modifier
        is_natural_1 = d20 == 1
        is_natural_20 = d20 == 20
        is_forced_crit = False

        if is_natural_1:
            outcome = "Critical Failure"
            cast.is_crit = False
        elif is_natural_20:
            outcome = "Critical Success"
            cast.is_crit = True
        elif total_attack >= cast.target_ac:
            if force_crit:
                outcome = "Critical Success"
                cast.is_crit = True
                is_forced_crit = True
            else:
                outcome = "Success"
                cast.is_crit = False
        else:
            outcome = "Failure"
            cast.is_crit = False

        adv_label = f" ({roll_mode.capitalize()})" if roll_mode else ""
        cast.narrative_parts.append(
            f"{cast.actor} {cast.spell_name} Attack{adv_label}{_target_suffix(cast.cursor, cast.target_name, bool(cast.is_npc_attack))}: {die_label}{total_attack} vs AC {cast.target_ac} ({outcome}) ({d20} + {cast.spell_attack_modifier})"
        )

        cast.result["attack_roll"] = d20
        cast.result["attack_modifier"] = cast.spell_attack_modifier
        cast.result["total_attack"] = total_attack
        cast.result["target_ac"] = cast.target_ac
        cast.result["outcome"] = outcome
        cast.result["is_crit"] = cast.is_crit
        cast.result["is_natural_20"] = is_natural_20
        if advantage_rolls:
            cast.result[f"{roll_mode}_rolls"] = advantage_rolls
        cast.result["disadvantage"] = bool(cast.enc_disadvantage)
        _encumbrance_note(cast.result, cast.encumbrance_sources, advantage_cancelled)
        if is_forced_crit:
            cast.result["forced_crit"] = True

        if outcome in ("Failure", "Critical Failure"):
            if cast.sp_damage_on_miss:
                # SRD 5.1: Acid Arrow's damage "both initial and later" scales with the slot.
                miss_dice = _scale_repeat_dice(
                    cast.sp_damage_on_miss, cast.sp_higher_levels,
                    max(0, (cast.computed_slot or cast.sp_level) - cast.sp_level))
                miss_die_size, miss_rolls, miss_sum = _parse_and_roll_dice(miss_dice)
                if miss_die_size is not None:
                    miss_damage = miss_sum + cast.final_mod
                    cast.narrative_parts.append(
                        f"{cast.actor} {cast.spell_name} Miss Damage{_target_suffix(cast.cursor, cast.target_name, bool(cast.is_npc_attack))}: {miss_damage} ({' + '.join(str(r) for r in miss_rolls)})"
                    )
                    cast.result["miss_damage"] = miss_damage
                    cast.result["miss_damage_rolls"] = miss_rolls
            cast.result["damage_total"] = cast.result.get("miss_damage", 0)
            cast.result["target_killed"] = None
            return _finalize_spell_result(cast.result, cast.narrative_parts, cast.sp_duration, cast.sp_buffs, cast.sp_requires_concentration, cast.is_npc_attack or cast.is_npc_vs_npc, cast.is_npc_vs_npc)

        # An attack-roll spell whose effect lands on a hit (Contagion): apply its condition now.
        if cast.sp_condition_on_hit and cast.sp_condition:
            affected = _target_label(cast.cursor, cast.target_name, bool(cast.is_npc_attack)) or cast.target_name or cast.actor
            cast.result["condition"] = cast.sp_condition
            if cast.sp_condition_duration:
                cast.result["condition_duration"] = cast.sp_condition_duration
            if cast.sp_requires_concentration:
                cast.result["requires_concentration"] = True
            cast.result["condition_targets"] = [affected]
            cast.narrative_parts.append(
                f"{affected}: Affected — {cast.sp_condition} ({cast.sp_condition_duration})")

        # A post-hit save (Ray of Enfeeblement): a hit forces a save, and the condition
        # applies only on a failure.
        if cast.sp_post_hit_save and cast.sp_condition:
            affected = _target_label(cast.cursor, cast.target_name, bool(cast.is_npc_attack)) or cast.target_name or cast.actor
            ability = str(cast.sp_post_hit_save.get("save_type") or cast.sp_save_type or "con").lower()[:3]
            if cast.is_npc_attack and cast.cursor is not None:
                smod = _derived_save(cast.cursor, ability, player_situational_modifier or 0,
                                     against="spells")["modifier"]
            else:
                smod = _registry_save(cast.target_name or affected, ability)
                if smod is None:
                    smod = target_save_modifier if target_save_modifier is not None else 0
            sd20, _srolls, _smode, _scancel = _roll_d20()
            stotal = sd20 + smod
            saved = stotal >= cast.spell_save_dc
            cast.narrative_parts.append(
                f"{affected} {ability.upper()} Save ({cast.spell_name}): {stotal} vs DC {cast.spell_save_dc} "
                f"({'Success' if saved else 'Failure'}) ({sd20} + {smod})")
            if not saved:
                cast.result["condition"] = cast.sp_condition
                if cast.sp_condition_duration:
                    cast.result["condition_duration"] = cast.sp_condition_duration
                if cast.sp_requires_concentration:
                    cast.result["requires_concentration"] = True
                cast.result["condition_targets"] = [affected]
                cast.narrative_parts.append(
                    f"{affected}: Affected — {cast.sp_condition} ({cast.sp_condition_duration})")

    # ── NO DAMAGE SPELLS ──
    if cast.sp_no_damage:
        cast.result["damage_total"] = 0
        cast.result["target_killed"] = None
        # Power Word Kill: an HP-threshold instant death, resolved and returned before the
        # save / condition fall-through (which it has neither of).
        if cast.sp_instant_kill:
            return _resolve_instant_kill(cast, target_current_hp=target_current_hp,
                                        challenge_rating=challenge_rating)
        # Fall through to saving throw / condition logic — do NOT return

    # ── DAMAGE / HEALING CALCULATION ──
    elif cast.final_dice in ("0d0", "0", "") and not cast.sp_healing:
        total_damage = cast.sp_damage_modifier + cast.final_mod
        cast.result["damage_total"] = total_damage
        cast.result["target_killed"] = None
        return _finalize_spell_result(cast.result, cast.narrative_parts, cast.sp_duration, cast.sp_buffs, cast.sp_requires_concentration, cast.is_npc_attack or cast.is_npc_vs_npc, cast.is_npc_vs_npc)

    # ── TEMPORARY-HP-ONLY SPELLS (False Life, ...) ──
    # These grant a temporary pool that is allowed to EXCEED the maximum HP. They restore no
    # real hit points; the hp_temporary buff below applies the pool.
    if cast.sp_healing and cast.sp_temp_hp:
        cast.narrative_parts.append(
            f"{cast.actor} {cast.spell_name} — temporary hit points only, no hit points restored")
        cast.result["healing_total"] = 0
        cast.result["healing_applied"] = 0
        cast.result["temporary_hp_only"] = True
        cast.result["damage_type"] = cast.sp_damage_type
        return _finalize_spell_result(cast.result, cast.narrative_parts, cast.sp_duration, cast.sp_buffs,
                                      cast.sp_requires_concentration,
                                      cast.is_npc_attack or cast.is_npc_vs_npc, cast.is_npc_vs_npc)

    if cast.sp_healing and not cast.is_npc_attack and not cast.is_npc_vs_npc and cast.sp_attack_type == "automatic":
        heal_die_size, heal_rolls, heal_raw = _parse_and_roll_dice(cast.final_dice)
        if heal_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation: '{cast.final_dice}'. Use format 'XdY' (e.g., '2d6')."}
        ability_bonus = 0
        if cast.spell and cast.spell.get("add_spellcasting_mod"):
            ability_bonus = _caster_spellcasting_mod(cast.cursor)
        heal_mod = cast.final_mod + ability_bonus
        total_healing = heal_raw + heal_mod
        full_restore = bool(cast.sp_flat_healing and total_healing == 0)

        if full_restore:
            cast.narrative_parts.append(f"{cast.actor} {cast.spell_name} Healing{_target_suffix(cast.cursor, cast.target_name or cast.actor, bool(cast.is_npc_attack))}: full HP restore")
            cast.result["healing_total"] = "full"
        else:
            if heal_rolls:
                heal_rolls_str = " + ".join(str(r) for r in heal_rolls)
                if heal_mod != 0:
                    heal_narrative = f"{cast.actor} {cast.spell_name} Healing{_target_suffix(cast.cursor, cast.target_name or cast.actor, bool(cast.is_npc_attack))}: {total_healing} ({heal_rolls_str} + {heal_mod})"
                else:
                    heal_narrative = f"{cast.actor} {cast.spell_name} Healing{_target_suffix(cast.cursor, cast.target_name or cast.actor, bool(cast.is_npc_attack))}: {total_healing} ({heal_rolls_str})"
            else:
                heal_narrative = f"{cast.actor} {cast.spell_name} Healing{_target_suffix(cast.cursor, cast.target_name or cast.actor, bool(cast.is_npc_attack))}: {total_healing}"
            cast.narrative_parts.append(heal_narrative)
            cast.result["healing_total"] = total_healing
            cast.result["healing_rolls"] = heal_rolls
        cast.result["damage_type"] = cast.sp_damage_type

        def _heal_one(tname, is_player, override_current, override_max):
            entry = _heal_target(
                tname, total_healing, cast.cursor, is_player=is_player,
                override_current=override_current, override_max=override_max,
                full=full_restore)
            applied = entry["healing_applied"]
            if applied > 0:
                cast.narrative_parts.append(
                    f"{tname} fully restored ({applied} HP)" if full_restore
                    else f"{tname} healed for {applied} HP")
            else:
                cast.narrative_parts.append(f"{tname} already at full HP")
            return entry

        applied_total = 0
        if cast.targets and len(cast.targets) > 0:
            healed_list = []
            for t in cast.targets:
                tname = t.get("name", "Unknown")
                is_player = bool(t.get("is_player", False)) or _is_player_actor(cast.cursor, tname)
                entry = _heal_one(tname, is_player, t.get("current_hp"), t.get("max_hp"))
                applied_total += entry["healing_applied"]
                healed_list.append(entry)
            cast.result["targets_healed"] = healed_list
        elif cast.target_name:
            is_player = _is_player_actor(cast.cursor, cast.target_name)
            entry = _heal_one(cast.target_name, is_player, target_current_hp, target_max_hp)
            applied_total = entry["healing_applied"]
            cast.result["target_healed"] = entry
            if entry.get("warning"):
                cast.result["healing_warning"] = entry["warning"]
        else:
            self_name = (_db_val(cast.cursor, "name", cast.actor) if cast.cursor else cast.actor) or cast.actor
            entry = _heal_one(self_name, True, None, target_max_hp)
            applied_total = entry["healing_applied"]
            cast.result["target_healed"] = entry

        cast.result["healing_applied"] = applied_total
        if full_restore:
            cast.result["healing_rolls"] = []
        return _finalize_spell_result(cast.result, cast.narrative_parts, cast.sp_duration, cast.sp_buffs, cast.sp_requires_concentration, cast.is_npc_attack or cast.is_npc_vs_npc, cast.is_npc_vs_npc)

    # ── HP POOL (Sleep, Color Spray) ──
    if cast.sp_hp_pool and not cast.sp_healing:
        pool_die_size, pool_rolls, pool_raw = _parse_and_roll_dice(cast.final_dice)
        if pool_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation for HP pool: '{cast.final_dice}'."}
        hp_pool_total = pool_raw + cast.final_mod

        pool_rolls_str = " + ".join(str(r) for r in pool_rolls)
        pool_narrative = f"{cast.actor} {cast.spell_name} HP Pool{_target_suffix(cast.cursor, cast.target_name, bool(cast.is_npc_attack))}: {hp_pool_total}"
        if cast.final_mod != 0:
            pool_narrative += f" ({pool_rolls_str} + {cast.final_mod})"
        else:
            pool_narrative += f" ({pool_rolls_str})"
        cast.narrative_parts.append(pool_narrative)

        cast.result["hp_pool"] = True
        cast.result["hp_pool_total"] = hp_pool_total
        cast.result["hp_pool_rolls"] = pool_rolls
        cast.result["damage_total"] = 0
        cast.result["target_killed"] = None
        if cast.sp_condition:
            cast.result["condition"] = cast.sp_condition
            if cast.sp_condition_duration:
                cast.result["condition_duration"] = cast.sp_condition_duration
            if cast.sp_requires_concentration:
                cast.result["requires_concentration"] = True

        if cast.targets and len(cast.targets) > 0:
            for t in cast.targets:
                if "current_hp" not in t:
                    name = t.get("name", "")
                    registry_hp = _registry_hp(name)
                    if registry_hp is not None:
                        t["current_hp"] = registry_hp
            sorted_targets = sorted(cast.targets, key=lambda t: t.get("current_hp", 0))
            affected = []
            unaffected = []
            remaining_pool = hp_pool_total
            for t in sorted_targets:
                name = t.get("name", "Unknown")
                chp = t.get("current_hp", 0)
                if chp <= remaining_pool:
                    affected.append({"name": name, "hp": chp})
                    remaining_pool -= chp
                else:
                    unaffected.append({"name": name, "hp": chp})
            cast.result["targets_affected"] = affected
            cast.result["targets_unaffected"] = unaffected
            cast.result["hp_pool_remaining"] = remaining_pool
            if cast.sp_condition and affected:
                cast.result["condition_targets"] = [t["name"] for t in affected]

            for t in affected:
                cast.narrative_parts.append(
                    f"{t['name']}: Affected — {cast.sp_condition} ({cast.sp_condition_duration})")
            for t in unaffected:
                cast.narrative_parts.append(
                    f"{t['name']}: Unaffected — HP exceeds remaining pool ({remaining_pool})")
        elif cast.sp_condition:
            who = _target_label(cast.cursor, cast.target_name, bool(cast.is_npc_attack)) or cast.target_name or cast.actor
            tchp = target_current_hp
            if tchp is None and cast.target_name:
                tchp = _registry_hp(cast.target_name)
            if tchp is None or tchp <= hp_pool_total:
                cast.result["condition_targets"] = [who]
                cast.narrative_parts.append(
                    f"{who}: Affected — {cast.sp_condition} ({cast.sp_condition_duration})")
            else:
                cast.narrative_parts.append(
                    f"{who}: Unaffected — HP exceeds remaining pool ({hp_pool_total})")

        return _finalize_spell_result(cast.result, cast.narrative_parts, cast.sp_duration, cast.sp_buffs, cast.sp_requires_concentration, cast.is_npc_attack or cast.is_npc_vs_npc, cast.is_npc_vs_npc)

    # ── ROLL DAMAGE ──
    if not cast.sp_no_damage:
        primary_die_size, primary_rolls, primary_sum = _parse_and_roll_dice(cast.final_dice)
        if primary_die_size is None:
            return {"success": False, "error": f"Invalid damage_dice notation: '{cast.final_dice}'. Use format 'XdY' (e.g., '2d6')."}

        label = "Healing" if cast.sp_healing else "Damage"
        dmg_suffix = _target_suffix(cast.cursor, (cast.target_name or cast.actor) if cast.sp_healing else cast.target_name,
                                    bool(cast.is_npc_attack))

        if cast.is_crit:
            crit_rolls = [random.randint(1, primary_die_size) for _ in range(len(primary_rolls))]
            primary_damage = primary_sum + sum(crit_rolls) + cast.final_mod
            crit_rolls_str = " + ".join(str(r) for r in crit_rolls)
            if primary_rolls:
                base_str = " + ".join(str(r) for r in primary_rolls)
                if cast.final_mod != 0:
                    cast.narrative_parts.append(
                        f"{cast.actor} {cast.spell_name} {label}{dmg_suffix}: {primary_damage} ({cast.final_dice}: {base_str} + {crit_rolls_str}, {cast.final_mod:+d}) [CRIT]"
                    )
                else:
                    cast.narrative_parts.append(
                        f"{cast.actor} {cast.spell_name} {label}{dmg_suffix}: {primary_damage} ({cast.final_dice}: {base_str} + {crit_rolls_str}) [CRIT]"
                    )
            else:
                cast.narrative_parts.append(
                    f"{cast.actor} {cast.spell_name} {label}{dmg_suffix}: {primary_damage} (flat + {crit_rolls_str} + {cast.final_mod}) [CRIT]"
                )
            cast.result["crit_damage_rolls"] = crit_rolls
        else:
            primary_damage = primary_sum + cast.final_mod
            if primary_rolls:
                base_str = " + ".join(str(r) for r in primary_rolls)
                if cast.final_mod != 0:
                    cast.narrative_parts.append(f"{cast.actor} {cast.spell_name} {label}{dmg_suffix}: {primary_damage} ({cast.final_dice}: {base_str}, {cast.final_mod:+d})")
                else:
                    cast.narrative_parts.append(f"{cast.actor} {cast.spell_name} {label}{dmg_suffix}: {primary_damage} ({cast.final_dice}: {base_str})")
            else:
                cast.narrative_parts.append(f"{cast.actor} {cast.spell_name} {label}{dmg_suffix}: {primary_damage}")

        cast.result["primary_damage"] = primary_damage
        cast.result["primary_damage_rolls"] = primary_rolls
        cast.result["damage_modifier"] = cast.final_mod
        cast.result["primary_die_size"] = primary_die_size

        extra_damage = 0
        extra_rolls = []
        extra_crit_rolls = []
        if cast.final_extra_dice:
            extra_die_size, extra_base_rolls, extra_base_sum = _parse_and_roll_dice(cast.final_extra_dice)
            if extra_die_size is None:
                return {"success": False, "error": f"Invalid extra_damage_dice notation: '{cast.final_extra_dice}'."}
            extra_rolls = extra_base_rolls
            extra_damage = sum(extra_base_rolls)
            extra_base_str = " + ".join(str(r) for r in extra_base_rolls)
            ext_type_label = cast.sp_extra_damage_type.title() if cast.sp_extra_damage_type else "Extra"
            cast.narrative_parts.append(
                f"{cast.actor} {cast.spell_name} {ext_type_label} Damage{dmg_suffix}: {extra_damage} ({extra_base_str})"
            )
            cast.result["extra_damage"] = extra_damage
            cast.result["extra_damage_rolls"] = extra_rolls
            cast.result["extra_damage_type"] = cast.sp_extra_damage_type

        total_damage = primary_damage + extra_damage
    else:
        primary_damage = 0
        extra_damage = 0
        total_damage = 0

    # ── MULTI-TARGET SAVING THROW ──
    target_results = None
    if cast.targets and len(cast.targets) > 0 and cast.sp_attack_type == "saving_throw":
        total_xp = 0
        target_results = []
        killed_count = 0
        save_name = cast.sp_save_type.upper() if cast.sp_save_type else "SAVE"
        condition_targets = []

        # Every saving-throw target needs current HP (to carry damage) and a save bonus. A
        # target that supplies neither and is not a registered combatant would otherwise be
        # resolved against a fabricated 0 HP / +0 save: refuse it up front, before any roll. A
        # registered target keeps the old defaults (its own declared save, or +0 when the stat
        # block declares none for that ability); a player target is engine-derived from the sheet.
        for t in cast.targets:
            if not isinstance(t, dict) or t.get("is_player"):
                continue
            tname = t.get("name", "Unknown")
            if _registry_entry(tname) is not None:
                continue
            missing = []
            if t.get("current_hp") is None:
                missing.append("current_hp")
            if t.get("save_modifier") is None:
                missing.append("save_modifier")
            if missing:
                return _unregistered_target_refusal(cast, tname, missing)

        for t in cast.targets:
            tname = t.get("name", "Unknown")
            tchp = t.get("current_hp")
            if tchp is None:
                tchp = _registry_hp(tname) or 0
            is_player = t.get("is_player", False)
            save_detail = None
            if is_player and cast.cursor is not None:
                # The player's save is engine-derived; any target save_modifier is situational.
                save_detail = _derived_save(cast.cursor, cast.sp_save_type, t.get("save_modifier") or 0,
                                            against="spells", damage=cast.sp_damage_type,
                                            condition=cast.sp_condition)
                tsave = save_detail["modifier"]
            else:
                tsave = t.get("save_modifier")
                if tsave is None:
                    tsave = _registry_save(tname, cast.sp_save_type)
                if tsave is None:
                    tsave = 0
            tcr = t.get("challenge_rating")
            if tcr is None:
                tcr = _registry_cr(tname)

            save = _resolve_save(cast, tname, is_player, tsave, save_detail)
            save_d20, save_total, save_success = save["d20"], save["total"], save["success"]
            save_outcome = "Success" if save_success else "Failure"

            t_damage = total_damage
            if save_success:
                if cast.sp_save_half:
                    t_damage = max(1, total_damage // 2)
                    cast.narrative_parts.append(
                        f"{tname} {save_name} Save: {save_total} vs DC {cast.spell_save_dc} ({save_outcome}) ({save_d20} + {tsave})"
                    )
                    if cast.sp_no_damage:
                        cast.narrative_parts.append(f"{tname} saved — no effect.")
                    elif cast.sp_healing:
                        cast.narrative_parts.append(f"{tname} saved — half healing: {t_damage}")
                    else:
                        cast.narrative_parts.append(f"{tname} saved — half damage: {t_damage}")
                else:
                    t_damage = 0
                    cast.narrative_parts.append(
                        f"{tname} {save_name} Save: {save_total} vs DC {cast.spell_save_dc} ({save_outcome}) ({save_d20} + {tsave})"
                    )
                    if cast.sp_no_damage:
                        cast.narrative_parts.append(f"{tname} saved — no effect.")
                    elif cast.sp_healing:
                        cast.narrative_parts.append(f"{tname} saved — no healing.")
                    else:
                        cast.narrative_parts.append(f"{tname} saved — no damage.")
            else:
                cast.narrative_parts.append(
                    f"{tname} {save_name} Save: {save_total} vs DC {cast.spell_save_dc} ({save_outcome}) ({save_d20} + {tsave})"
                )
                if cast.sp_condition:
                    cast.narrative_parts.append(f"{tname}: Affected — {cast.sp_condition} ({cast.sp_condition_duration})")
                    condition_targets.append(tname)

            # SRD resistances/immunities/vulnerabilities, after any save halving.
            if not cast.sp_healing and not is_player and t_damage > 0:
                t_damage, dmg_note = _apply_component_damage(
                    _registry_entry(tname), t_damage, primary_damage, extra_damage,
                    cast.sp_damage_type, cast.sp_extra_damage_type,
                    halved=bool(save_success and cast.sp_save_half))
                if dmg_note:
                    cast.narrative_parts.append(f"{tname} damage adjusted: {dmg_note}")

            tr, killed = _apply_target_hp(cast, tname, is_player, t_damage, tchp, save, t)
            if killed:
                killed_count += 1

            if killed and tcr is not None:
                xp = CR_XP_TABLE.get(tcr, 0)
                if xp > 0:
                    total_xp += xp
                    tr["xp_awarded"] = xp

            if save_detail is not None:
                tr["save"] = save_detail
            target_results.append(tr)

        cast.result["targets"] = target_results
        if not cast.sp_healing:
            cast.result["killed_count"] = killed_count
        else:
            cast.result["healing_total"] = total_damage
            cast.result["healing_applied"] = sum(t.get("healing_applied", 0) for t in target_results)
        if cast.sp_condition and condition_targets:
            cast.result["condition"] = cast.sp_condition
            if cast.sp_condition_duration:
                cast.result["condition_duration"] = cast.sp_condition_duration
            if cast.sp_requires_concentration:
                cast.result["requires_concentration"] = True
            cast.result["condition_targets"] = condition_targets

        if total_xp > 0 and not cast.is_npc_attack and not cast.is_npc_vs_npc and cast.cursor:
            xp_result = modify_player_numeric(key="xp", delta=total_xp)
            cast.result["xp_awarded"] = total_xp
            cast.result["xp_result"] = xp_result
            cast.narrative_parts.append(f"Total XP Awarded: {total_xp}")
            if xp_result.get("level_up"):
                cast.narrative_parts.append(xp_result["level_up_summary"])

        cast.result["damage_total"] = total_damage
        cast.result["damage_type"] = cast.sp_damage_type
        return _finalize_spell_result(cast.result, cast.narrative_parts, cast.sp_duration, cast.sp_buffs, cast.sp_requires_concentration, cast.is_npc_attack or cast.is_npc_vs_npc, cast.is_npc_vs_npc)

    # ── SINGLE-TARGET SAVING THROW ──
    if cast.sp_attack_type == "saving_throw":
        saver_name = _target_label(cast.cursor, cast.target_name, bool(cast.is_npc_attack)) or cast.target_name or cast.actor
        # A damage-dealing save against an unregistered target with no HP given would apply its
        # damage to nothing: refuse it up front, exactly as the multi-target path does. (The
        # player target's HP lives on the sheet; healing has its own unknown-ceiling warning.)
        if (not cast.is_npc_attack and not cast.sp_no_damage and not cast.sp_healing
                and cast.target_name and target_current_hp is None
                and _registry_entry(cast.target_name) is None
                and not _is_player_actor(cast.cursor, cast.target_name)):
            return _unregistered_target_refusal(cast, cast.target_name, ["current_hp"])
        save_detail = None
        if cast.is_npc_attack and cast.cursor is not None:
            # The player is the saver: the engine derives the whole save (SRD 5.1).
            save_detail = _derived_save(cast.cursor, cast.sp_save_type, player_situational_modifier or 0,
                                        against="spells", damage=cast.sp_damage_type,
                                        condition=cast.sp_condition)
            save_mod = save_detail["modifier"]
        elif target_save_modifier is not None:
            save_mod = target_save_modifier
        else:
            saver = cast.target_name or cast.actor
            if _registry_entry(saver) is None:
                # No explicit bonus and not a registered combatant: a fabricated +0 would flip
                # the save. Refuse; the GM registers the target or passes target_save_modifier.
                return _unregistered_target_refusal(cast, saver, ["save_modifier"])
            save_mod = _registry_save(saver, cast.sp_save_type)
            if save_mod is None:
                save_mod = 0   # registered, but that ability is not declared in its stat block
        # The player is the one saving when an NPC casts on them.
        save = _resolve_save(cast, saver_name, cast.is_npc_attack, save_mod, save_detail)
        save_d20, save_total, save_success = save["d20"], save["total"], save["success"]
        auto_fail = save["auto_fail"]
        save_disadvantage, save_sources = save["disadvantage"], save["sources"]
        if auto_fail:
            save_outcome = "Failure"

        cast.result["save_roll"] = save_d20
        cast.result["save_modifier"] = save_mod
        cast.result["save_total"] = save_total
        cast.result["save_dc"] = cast.spell_save_dc
        cast.result["save_success"] = save_success
        if save_detail is not None:
            cast.result["save"] = save_detail
        cast.result["save_disadvantage"] = bool(save_disadvantage)
        if save_disadvantage:
            cast.result["save_disadvantage_sources"] = save_sources
        save_outcome = "Success" if save_success else "Failure"

        cast.narrative_parts.append(
            f"{saver_name} {cast.sp_save_type.upper()} Save: {save_total} vs DC {cast.spell_save_dc}"
            f" ({save_outcome}) ({save_d20} + {save_mod})"
        )

        if save_success:
            if cast.sp_save_half:
                total_damage = max(1, total_damage // 2)
                if cast.sp_no_damage:
                    cast.narrative_parts.append(f"{saver_name} saved — no effect.")
                elif cast.sp_healing:
                    cast.narrative_parts.append(f"{saver_name} saved — half healing: {total_damage}")
                else:
                    cast.narrative_parts.append(f"{saver_name} saved — half damage: {total_damage}")
            else:
                total_damage = 0
                if cast.sp_no_damage:
                    cast.narrative_parts.append(f"{saver_name} saved — no effect.")
                elif cast.sp_healing:
                    cast.narrative_parts.append(f"{saver_name} saved — no healing.")
                else:
                    cast.narrative_parts.append(f"{saver_name} saved — no damage.")
        elif cast.sp_condition:
            cast.narrative_parts.append(f"{saver_name}: Affected — {cast.sp_condition} ({cast.sp_condition_duration})")
            cast.result["condition"] = cast.sp_condition
            if cast.sp_condition_duration:
                cast.result["condition_duration"] = cast.sp_condition_duration
            if cast.sp_requires_concentration:
                cast.result["requires_concentration"] = True
            cast.result["condition_targets"] = [saver_name]
        if auto_fail:
            cast.result["save_auto_failed"] = True

        # SRD resistances/immunities/vulnerabilities of a registered NPC target (not the player).
        if not cast.is_npc_attack and not cast.sp_healing and total_damage > 0:
            total_damage, dmg_note = _apply_component_damage(
                _registry_entry(cast.target_name), total_damage, primary_damage, extra_damage,
                cast.sp_damage_type, cast.sp_extra_damage_type,
                halved=bool(save_success and cast.sp_save_half))
            if dmg_note:
                cast.result["damage_modified"] = dmg_note
                cast.narrative_parts.append(f"Damage adjusted: {dmg_note} ({cast.target_name})")

    cast.result["damage_total"] = total_damage
    cast.result["damage_type"] = cast.sp_damage_type

    # ── APPLY HP CHANGES (single target) ──
    if cast.is_npc_attack and total_damage > 0 and not cast.sp_healing:
        total_damage, dmg_note = _apply_player_damage_modifiers(cast.cursor, total_damage, cast.sp_damage_type)
        if dmg_note:
            cast.result["damage_modified"] = dmg_note
            cast.result["damage_total"] = total_damage
            cast.narrative_parts.append(f"Damage adjusted: {dmg_note}")
        hp_result = _apply_hp_change(cast.cursor, -total_damage)
        cast.result["hp_change"] = hp_result
    elif cast.is_npc_attack and cast.sp_healing:
        player_name = (cast.target_name or (_db_val(cast.cursor, "name", "Player") if cast.cursor else "Player"))
        entry = _heal_target(player_name, total_damage, cast.cursor, is_player=True,
                             override_max=target_max_hp)
        cast.result["healing_total"] = entry["healing_total"]
        cast.result["healing_applied"] = entry["healing_applied"]
        cast.result["target_healed"] = entry
        if entry.get("hp_change"):
            cast.result["hp_change"] = entry["hp_change"]
        if entry.get("warning"):
            cast.result["healing_warning"] = entry["warning"]
        cast.narrative_parts.append(
            f"Healed {player_name} for {entry['healing_applied']} HP "
            f"({entry['remaining_hp']}/{entry['max_hp']})")
    elif cast.is_npc_vs_npc and total_damage > 0 and not cast.sp_healing:
        _apply_target_damage(cast.target_name, total_damage, cast.result, cast.narrative_parts,
                             current_hp=target_current_hp, challenge_rating=challenge_rating,
                             require_damage=True)
        cast.result["npc_vs_npc"] = True
    elif cast.is_npc_vs_npc and cast.sp_healing:
        cast.result["npc_vs_npc"] = True
        cast.result["target_killed"] = None
        entry = _heal_target(cast.target_name, total_damage, cast.cursor, is_player=False,
                             override_current=target_current_hp, override_max=target_max_hp)
        cast.result["healing_total"] = entry["healing_total"]
        cast.result["healing_applied"] = entry["healing_applied"]
        cast.result["target_healed"] = entry
        if entry.get("warning"):
            cast.result["healing_warning"] = entry["warning"]
        if entry["healing_applied"] > 0:
            cast.narrative_parts.append(
                f"{cast.actor} heals {cast.target_name} for {entry['healing_applied']} HP "
                f"({entry['remaining_hp']}/{entry['max_hp']})")
        else:
            cast.narrative_parts.append(
                f"{cast.actor}'s heal has no effect on {cast.target_name or 'target'} (already at full HP).")
    elif not cast.is_npc_attack and not cast.is_npc_vs_npc and not cast.sp_healing:
        _apply_target_damage(cast.target_name, total_damage, cast.result, cast.narrative_parts,
                             current_hp=target_current_hp, challenge_rating=challenge_rating,
                             award_xp=True, player_fallback=True, damage_type=cast.sp_damage_type,
                             cursor=cast.cursor)
    elif not cast.is_npc_attack and not cast.is_npc_vs_npc and cast.sp_healing:
        tname = cast.target_name or (_db_val(cast.cursor, "name", cast.actor) if cast.cursor else cast.actor)
        is_player = _is_player_actor(cast.cursor, tname)
        entry = _heal_target(tname, total_damage, cast.cursor, is_player=is_player,
                             override_current=target_current_hp, override_max=target_max_hp)
        cast.result["healing_total"] = entry["healing_total"]
        cast.result["healing_applied"] = entry["healing_applied"]
        cast.result["target_healed"] = entry
        if entry.get("hp_change"):
            cast.result["hp_change"] = entry["hp_change"]
        if entry.get("warning"):
            cast.result["healing_warning"] = entry["warning"]
        if entry["healing_applied"] > 0:
            cast.narrative_parts.append(
                f"{tname} healed for {entry['healing_applied']} HP "
                f"({entry['remaining_hp']}/{entry['max_hp']})")
        else:
            cast.narrative_parts.append(f"{tname} already at full HP")
    else:
        cast.result["target_killed"] = None
        if cast.is_npc_vs_npc:
            cast.result["npc_vs_npc"] = True

    # An automatic spell takes no branch above; if it carries an ENGINE condition, apply it to
    # the declared targets (or the caster for a self-buff). A GM-adjudicated tag (Trapped,
    # Teleported, ...) is left to the GM -- only CONDITION_EFFECTS are tracked here.
    if (cast.sp_condition and not cast.result.get("condition")
            and cast.sp_attack_type not in ("saving_throw", "attack_roll")
            and str(cast.sp_condition).strip().lower() in CONDITION_EFFECTS):
        who = [str(n).strip() for n in (cast.result.get("targets_named_only") or [])
               if str(n or "").strip()]
        if not who:
            label = _target_label(cast.cursor, cast.target_name, bool(cast.is_npc_attack)) or cast.target_name
            if label:
                who = [label]
        if not who:
            caster = str(cast.actor or "").strip()
            if not caster or caster == "{player_name}":
                caster = (_db_val(cast.cursor, "name", cast.actor) if cast.cursor is not None else "") or cast.actor
            who = [caster]
        cast.result["condition"] = cast.sp_condition
        if cast.sp_condition_duration:
            cast.result["condition_duration"] = cast.sp_condition_duration
        if cast.sp_requires_concentration:
            cast.result["requires_concentration"] = True
        cast.result["condition_targets"] = who
        for t in who:
            cast.narrative_parts.append(
                f"{t}: Affected — {cast.sp_condition} ({cast.sp_condition_duration})")

    return _finalize_spell_result(cast.result, cast.narrative_parts, cast.sp_duration, cast.sp_buffs, cast.sp_requires_concentration, cast.is_npc_attack or cast.is_npc_vs_npc, cast.is_npc_vs_npc)
