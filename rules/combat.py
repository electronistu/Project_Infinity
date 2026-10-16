"""The combat registry, initiative cursor, roster, companions and recurring saves.

One battle's whole mutable picture: registry entries, who acts next, the roster lines the GM
reads, the companion store, and the repeat-save effects the cursor resolves as it advances.
"""

import json

import equipment
import skills

from . import state
from .conditions import (
    _apply_damage_modifiers, _apply_player_damage_modifiers, _condition_save_effects,
    _conditions_for, _drain_woke, _player_conditions, _player_defenses,
    _wake_on_damage
)
from .consts import (
    ABILITY_KEYS, COMPANION_ABILITY_KEYS, COMPANION_DEFAULT_PROF, CR_XP_TABLE, EXHAUSTION_MAX,
    _COMPANION_KEEP
)
from .db import (
    _ability_mod, _carry_get, _db_val, _effective_prof_bonus, _effective_stats, _equipment_block,
    _exhaustion_level, _flatten_companions, _item_effect_state
)
from .dice import _combine_dice, _parse_and_roll_dice, _parse_higher_levels, _roll_d20
from .sheet import _derived_save, _encumbrance, _exhaustion_effects, _save_proficiencies
from .vitals import _apply_combat_condition, _apply_hp_change, apply_numeric_change


def _in_active_combat() -> bool:
    """True while the combat registry holds a living, hostile, active non-player.

    Donning or doffing armour takes minutes, so it cannot happen mid-fight; shields take one
    action and are always allowed. Combat is over when the last hostile has dropped to 0 HP
    (``killed``), or fled or surrendered (an explicit ``status``). Allies and neutrals never
    keep a fight alive.
    """
    for entry in (state._COMBAT_REGISTRY or {}).values():
        if not isinstance(entry, dict) or entry.get("is_player"):
            continue
        if str(entry.get("role") or "hostile").strip().lower() != "hostile":
            continue
        if entry.get("killed"):
            continue
        if str(entry.get("status") or "active").strip().lower() != "active":
            continue
        return True
    return False


def _finish_combat_if_over() -> None:
    """The fight is over when no living, active hostile is left: empty the registry behind the
    result that ended it.

    The registry is fight state -- initiative, live HP, conditions, recurring saves. A companion's
    health between fights is the Game Master's to declare when the next fight is registered, not
    a number to carry, so nothing may leak from one fight into the next. The result that ends the
    fight still carries the FINAL roster (the deaths the engine tracks, the client's tooltips);
    only the calls that follow see an empty registry. `end_combat` is the explicit form of the
    same thing, for the endings the engine cannot see."""
    if not state._COMBAT_REGISTRY:
        return
    if not any(str(e.get("role") or "hostile").strip().lower() == "hostile"
               for e in state._COMBAT_REGISTRY.values() if isinstance(e, dict)):
        return  # an allies-only registration is not a fight
    if not _in_active_combat():
        state.registry_clear()
        _reset_combat_cursor()


def _blocked_action(cursor, error: str, reason: str, item: str | None = None,
                    narrative: str = "") -> dict:
    """A refused player action (SRD 5.1 equipped items): the action is spent.

    The engine does not track whose turn it is, so the turn loss is signalled here for the Game
    Master to apply: tell the player why the action failed and move on. No dice are rolled.
    """
    return {
        "success": False,
        "error": error,
        "reason": reason,
        "item": item,
        "turn_lost": True,
        "action_consumed": True,
        "gm_instruction": ("Tell the player the action is wasted and why — the turn is spent. "
                           "Move on to the next combatant."),
        "equipment": _equipment_block(cursor),
        "narrative_format": narrative or f"Action wasted — {reason}",
    }


def _action_failure(error: str, narrative_format: str, **fields) -> dict:
    """A refused player action that costs nothing (no slot, no turn spent).

    The GM gets the `error` (and any extra fields); the engine composes the player-facing
    `narrative_format` line into the turn's Mechanics block. Tool-shape / GM-correctable errors
    (a missing parameter, an invalid notation, an unknown registry name) must NOT use this —
    they stay GM-only.
    """
    return {"success": False, "error": error, "narrative_format": narrative_format, **fields}


def _registry_name(target_name: str) -> str | None:
    """The canonical registry key that matches `target_name`, or None when unregistered.

    Matching is forgiving (surrounding/inner whitespace, case) while the stored key -- and so
    the roster and tooltip display -- keeps the Game Master's exact spelling."""

    return state.registry_key(target_name)


def _registry_entry(target_name: str) -> dict | None:
    """The registry entry for `target_name`, THE resolver every registry lookup goes through.

    `None` means the name is not registered; callers must refuse rather than substitute a
    fabricated AC/HP/save/CR. See `state.registry_key` for the forgiving match."""

    return state.registry_get(target_name)


def _registry_hp(target_name: str) -> int | None:
    entry = _registry_entry(target_name)
    if entry:
        return entry["current_hp"]
    return None


def _registry_ac(target_name: str) -> int | None:
    entry = _registry_entry(target_name)
    if entry:
        return entry["ac"]
    return None


def _registry_update_hp(target_name: str, new_hp: int):
    entry = _registry_entry(target_name)
    if entry:
        if new_hp < int(entry.get("current_hp") or 0):
            _wake_on_damage(target_name)  # damage ends a marked condition (Eyebite: Asleep)
        entry["current_hp"] = new_hp


def _registry_kill(target_name: str):
    entry = _registry_entry(target_name)
    if entry:
        entry["killed"] = True


# ── RECURRING ("repeat") SAVES ───────────────────────────────────────────────
# A spell carrying a `repeat_save` block forces a save at the start (or end) of each of
# the target's turns, with damage and/or a ladder of successes/failures. The engine owns
# the rolls: the effect is registered when the initial save fails and resolved as the
# initiative cursor passes the target (see _advance_turn), so a turn the GM skips still
# resolves (a creature whose turn starts is passed over by whoever acts next).

def _record_saved_target(caster, spell, target):
    """Remember, for this fight, that `target` saved against `caster`'s casting of `spell`.

    SRD 5.1 (Eyebite): a creature that has already succeeded cannot be targeted by this casting
    again. The memory lives on the caster's registry entry, so it dies with the fight the way a
    concentration spell does."""
    entry = _registry_entry(caster)
    if not entry or not target or not spell:
        return
    saved = entry.setdefault("spell_saved_targets", {})
    names = saved.setdefault(spell, [])
    if target not in names:
        names.append(target)


def _reset_combat_cursor():
    state._COMBAT_POS = -1
    state._COMBAT_ROUND = 0
    state._COMBAT_ACTOR = ""
    state._COMBAT_SKIPPED = []


def _scale_repeat_dice(base: str, higher_levels: str, levels_above: int) -> str:
    """Apply a `+NdM` upcast to a repeating effect's damage dice (baked in at cast time)."""
    parsed = _parse_higher_levels(higher_levels)
    if not parsed or levels_above <= 0:
        return base
    if parsed[0] == "dice":
        _, num, size, flat = parsed
        extra = f"{num * levels_above}d{size}"
        if flat:
            extra += f"+{flat * levels_above}"
        return _combine_dice(base, extra)
    return base


def _register_recurring(name, spell_name, block, dc, caster):
    """Attach a repeating-save effect to a combatant. Re-registering the same spell replaces it."""
    entry = _registry_entry(name)
    if entry is None:
        return None
    eff = {
        "spell": spell_name,
        "save_type": str(block.get("save_type") or "").strip().lower()[:3],
        "dc": int(dc or 0),
        "timing": str(block.get("timing") or "start_of_turn").strip().lower(),
        "damage_dice": block.get("damage_dice"),
        "damage_type": block.get("damage_type") or "",
        "ends_on_success": bool(block.get("ends_on_success", False)),
        "successes_to_end": block.get("successes_to_end"),
        "failures_to_lock": block.get("failures_to_lock"),
        "failures_to_effect": block.get("failures_to_effect"),
        "condition_on_failure": block.get("condition_on_failure"),
        "caster": caster,
        "successes": 0,
        "failures": 0,
        "locked": False,
    }
    effects = entry.setdefault("recurring", [])
    effects[:] = [e for e in effects if e.get("spell") != spell_name]
    effects.append(eff)
    return eff


def _roll_recurring_effect(name, entry, eff, cursor):
    """Roll one repeating save, apply its damage/condition, and step its ladder."""
    is_player = bool(entry.get("is_player"))
    ability = eff.get("save_type") or "wis"
    save_detail = None
    if is_player and cursor is not None:
        save_detail = _derived_save(cursor, ability, 0, against="spells",
                                    damage=eff.get("damage_type"),
                                    condition=eff.get("condition_on_failure"))
        mod = save_detail["modifier"]
        advantage = bool(save_detail.get("advantage"))
    else:
        mod = _registry_save(name, ability)
        mod = 0 if mod is None else mod
        advantage = False
    disadvantage, dis_sources = _encumbrance(cursor, is_player, ability)
    cond_dis, auto_fail, cond_sources = _condition_save_effects(
        _conditions_for(cursor, name, is_player=is_player), ability)
    disadvantage = disadvantage or cond_dis
    d20, rolls, mode, _cancelled = _roll_d20(advantage=advantage, disadvantage=disadvantage)
    total = d20 + mod
    success = total >= eff["dc"] and not auto_fail
    out = {"name": name, "spell": eff["spell"], "caster": eff.get("caster") or "",
           "save_type": ability,
           "save_roll": d20, "save_modifier": mod, "save_total": total,
           "save_dc": eff["dc"], "save_success": success, "damage": 0,
           "damage_type": eff.get("damage_type") or "", "ended": False}
    if save_detail is not None:
        out["save"] = save_detail
    if rolls:
        out[f"save_{mode}_rolls"] = rolls
    if disadvantage and dis_sources:
        out["disadvantage_sources"] = list(dis_sources)
    if auto_fail:
        out["save_auto_failed"] = True

    if success:
        if eff.get("successes_to_end"):
            eff["successes"] = int(eff.get("successes") or 0) + 1
            out["successes"] = eff["successes"]
            if eff["successes"] >= int(eff["successes_to_end"]):
                out["ended"] = True
        elif eff.get("ends_on_success"):
            out["ended"] = True
        return out

    if eff.get("damage_dice"):
        die, dmg_rolls, raw = _parse_and_roll_dice(eff["damage_dice"])
        if die is not None:
            out["damage_rolls"] = dmg_rolls
            damage = raw
            if is_player and cursor is not None:
                damage, note = _apply_player_damage_modifiers(cursor, damage, eff.get("damage_type") or "")
                if note:
                    out["damage_modified"] = note
                hp = _apply_hp_change(cursor, -damage)
                out["hp_change"] = hp
                out["remaining_hp"] = hp.get("new_value")
                entry["current_hp"] = hp.get("new_value", entry.get("current_hp"))
            else:
                damage, note = _apply_damage_modifiers(entry, damage, eff.get("damage_type") or "")
                if note:
                    out["damage_modified"] = note
                before = entry.get("current_hp", 0)
                remaining = before - damage
                entry["current_hp"] = max(remaining, 0)
                if damage > 0:
                    _wake_on_damage(name)
                out["remaining_hp"] = max(remaining, 0)
                if before > 0 and remaining <= 0:
                    entry["killed"] = True
                    out["killed"] = True
                    out["ended"] = True  # a repeating effect dies with its target
                    cr = entry.get("challenge_rating")
                    if cr is not None and cursor is not None and not is_player:
                        xp = CR_XP_TABLE.get(cr, 0)
                        if xp > 0:
                            out["xp_change"] = apply_numeric_change(key="xp", delta=xp)
                            out["xp_awarded"] = xp
            out["damage"] = damage

    if eff.get("failures_to_effect"):
        eff["failures"] = int(eff.get("failures") or 0) + 1
        out["failures"] = eff["failures"]
        if eff["failures"] >= int(eff["failures_to_effect"]):
            eff["locked"] = True
            out["locked"] = True
            cond = eff.get("condition_on_failure")
            if cond:
                out["effect_applied"] = cond
                if cursor is not None:
                    _apply_combat_condition(cursor, name, cond)
    elif eff.get("failures_to_lock"):
        eff["failures"] = int(eff.get("failures") or 0) + 1
        out["failures"] = eff["failures"]
        if eff["failures"] >= int(eff["failures_to_lock"]):
            eff["locked"] = True
            out["locked"] = True
    return out


def _recurring_line(res: dict) -> str:
    verdict = "Success" if res["save_success"] else "Failure"
    spell = str(res.get("spell") or "")
    caster = str(res.get("caster") or "").strip()
    label = f"{spell}, cast by {caster}" if (spell and caster) else (spell or "the spell")
    line = (f"{res['name']} {str(res['save_type']).upper()} Save ({label}): "
            f"{res['save_total']} vs DC {res['save_dc']} ({verdict}) "
            f"({res['save_roll']} + {res['save_modifier']})")
    if res.get("damage"):
        line += f" — {res['damage']} {res.get('damage_type') or ''}".rstrip()
        if res.get("remaining_hp") is not None:
            line += f", {res['remaining_hp']} HP"
        if res.get("killed"):
            line += " (KILLED)"
        if res.get("xp_awarded"):
            line += f", +{res['xp_awarded']} XP"
    if res.get("effect_applied"):
        line += f" — {res['effect_applied']}"
    if res.get("ended"):
        line += " — the spell ends"
    return line


def _flush_recurring(names):
    """Resolve every pending repeating save for the named combatants whose turn starts now."""
    if state.DB_CONNECTION is None:
        return [], []
    cursor = state.DB_CONNECTION.cursor()
    results, lines = [], []
    for name in names:
        entry = _registry_entry(name)
        if not entry:
            continue
        effects = entry.get("recurring") or []
        if not effects:
            continue
        keep = []
        for eff in effects:
            if eff.get("locked"):
                keep.append(eff)
                continue
            res = _roll_recurring_effect(name, entry, eff, cursor)
            results.append(res)
            lines.append(_recurring_line(res))
            if not res.get("ended"):
                keep.append(eff)
        entry["recurring"] = keep
    return results, lines


def _combat_turn_order() -> list[str]:
    """The initiative order used to advance turns. Neutrals sit the fight out (auto-skip)."""
    return sorted(
        (name for name, entry in state._COMBAT_REGISTRY.items()
         if str((entry or {}).get("role") or "hostile").strip().lower() != "neutral"),
        key=lambda n: (-state._COMBAT_REGISTRY[n].get("initiative_total", 0), n))


def _resync_combat_cursor() -> None:
    """Re-point the cursor at the last actor after the order changed (an arrival inserted)."""
    order = _combat_turn_order()
    state._COMBAT_POS = order.index(state._COMBAT_ACTOR) if state._COMBAT_ACTOR in order else -1


def _combat_is_active(entry) -> bool:
    """A combatant still able to take a turn: not killed, not fled or surrendered."""
    if not isinstance(entry, dict):
        return False
    if entry.get("killed"):
        return False
    return str(entry.get("status") or "active").strip().lower() == "active"


def _combat_next_actor():
    """The next active combatant after the last actor, best-effort (never enforced).

    Walks forward from the last actor's position in the non-neutral turn order, skipping
    anyone killed, fled or surrendered. Returns ``(round, name)`` where the round is the
    UPCOMING turn's round, so it advances by one when the walk wraps past the last position.
    When nobody has acted yet the first active combatant is next and the round reads 1."""
    order = _combat_turn_order()
    if not order:
        return state._COMBAT_ROUND, ""
    n = len(order)
    base = (order.index(state._COMBAT_ACTOR) + 1) if state._COMBAT_ACTOR in order else 0
    for off in range(n):
        pos = base + off
        name = order[pos % n]
        if _combat_is_active(_registry_entry(name)):
            return (state._COMBAT_ROUND or 1) + (pos // n), name
    return state._COMBAT_ROUND, ""


def _combat_turn_state() -> dict:
    """The turn cursor the client shows: the last actor, the NEXT in line, the round of that
    upcoming turn, and the turns the cursor crossed without acting (skipped). Informational
    only -- the engine does not enforce the turn sequence; the Game Master adjudicates timing."""
    round_, nxt = _combat_next_actor()
    return {"round": round_, "actor": state._COMBAT_ACTOR, "next": nxt,
            "skipped": list(state._COMBAT_SKIPPED)}


def _attach_roster(result: dict) -> dict:
    """Attach the live roster + turn cursor to a combat tool result."""
    result["registry_summary"] = _registry_summary_list()
    result["combat_turn"] = _combat_turn_state()
    return result


def _advance_turn(actor):
    """Advance the initiative cursor to `actor` and resolve every turn that started on the
    way. Returns (results, narrative_lines). Turn-advancing calls name an actor; the cursor
    fires each position it passes, so a combatant whose turn the GM skipped still resolves.
    Neutrals never take a turn. The cursor is informational -- it does not enforce order."""
    if not state._COMBAT_REGISTRY or not actor:
        return [], []
    entry = _registry_entry(actor)
    if entry is not None and str(entry.get("role") or "hostile").strip().lower() == "neutral":
        return [], []
    order = _combat_turn_order()
    # A name the GM typed with different case/spacing still points at its combatant: resolve to
    # the canonical key before touching the order, or the cursor would treat it as a newcomer.
    actor = _registry_name(actor) or actor
    if actor not in order:
        order.append(actor)
    q = order.index(actor)
    if state._COMBAT_POS < 0:
        fired = order[:q + 1]
        state._COMBAT_ROUND = 1
    elif q > state._COMBAT_POS:
        fired = order[state._COMBAT_POS + 1:q + 1]
    elif q < state._COMBAT_POS:
        fired = order[state._COMBAT_POS + 1:] + order[:q + 1]
        state._COMBAT_ROUND += 1
    else:
        fired = []
    state._COMBAT_POS = q
    state._COMBAT_ACTOR = actor
    state._COMBAT_SKIPPED = [n for n in fired if n != actor]
    return _flush_recurring(fired)


def _flush_recurring_into(result, narrative_parts):
    """The end-of-tool hook: the acting combatant's turn may start here, and any condition
    that damage ended (Eyebite's Asleep) is reported."""
    rec_results, rec_lines = _advance_turn(result.get("actor"))
    if rec_lines:
        result["recurring"] = rec_results
        narrative_parts.extend(rec_lines)
    woke = _drain_woke()
    if woke:
        result["conditions_woke"] = woke
        for entry in woke:
            narrative_parts.append(
                f"{entry['name']} wakes — {', '.join(entry['conditions'])} ends")


def _registry_cr(target_name: str) -> float | None:
    entry = _registry_entry(target_name)
    if entry:
        return entry.get("challenge_rating")
    return None


def _registry_max_hp(target_name: str) -> int:
    entry = _registry_entry(target_name)
    if entry:
        return entry["max_hp"]
    return 0


def _resolve_target_hp(target_name, is_player, cursor, override_current=None, override_max=None):
    """(current_hp, max_hp, warning) for a healing target.

    Precedence: the GM's override, the combat registry (NPCs), the player's sheet, then any
    value the action carried. The ceiling is never silently taken from the current HP unless
    nothing else is known, and the caller is told when that happens.
    """
    current = override_current
    maximum = override_max

    if is_player:
        if cursor is not None:
            if current is None:
                current = int(_db_val(cursor, "current_hit_points", 0))
            if maximum is None:
                maximum = int(_db_val(cursor, "total_hit_points", 1))
    else:
        entry = _registry_entry(target_name) if target_name else None
        if entry is not None:
            if current is None:
                current = entry.get("current_hp")
            if maximum is None:
                maximum = entry.get("max_hp")

    if current is None and target_name:
        current = _registry_hp(target_name)
    if maximum is None and target_name:
        maximum = _registry_max_hp(target_name) or None

    warning = None
    if current is None:
        current = 0
        warning = f"Unknown current HP for {target_name or 'the target'}."
    if not maximum:
        maximum = int(current)
        if warning is None:
            warning = (f"Unknown max HP for {target_name or 'the target'}; pass target_max_hp "
                       f"if it is wounded.")
    current = int(current)
    maximum = max(int(maximum), 1)
    if current > maximum:
        current = maximum
        warning = warning or (f"{target_name or 'Target'} is above its declared max HP "
                              f"({maximum}); using the declared ceiling.")
    return current, maximum, warning


def _heal_target(target_name, amount, cursor, *, is_player=False,
                 override_current=None, override_max=None, full=False):
    """Apply healing to one target, capped at its maximum HP.

    Returns `{name, healing_total, healing_applied, remaining_hp, max_hp}` plus `hp_change`
    (player) and/or `warning`. `full=True` restores to the ceiling regardless of `amount`.
    """
    current, maximum, warning = _resolve_target_hp(
        target_name, is_player, cursor, override_current, override_max)
    rolled = (maximum - current) if full else int(amount)
    applied = max(0, min(int(rolled), maximum - current))

    entry = {
        "name": target_name,
        "healing_total": int(rolled),
        "healing_applied": applied,
        "remaining_hp": current + applied,
        "max_hp": maximum,
    }
    if warning:
        entry["warning"] = warning

    if is_player:
        if cursor is not None:
            if applied > 0:
                hp_result = _apply_hp_change(cursor, applied)
                entry["hp_change"] = hp_result
                entry["remaining_hp"] = hp_result["new_value"]
        else:
            entry["warning"] = entry.get("warning") or "No player database; healing was not applied."
    elif target_name:
        if _registry_hp(target_name) is None:
            entry["warning"] = entry.get("warning") or (
                f"{target_name} is not in the combat registry, so the healing was not written "
                f"to any combatant.")
        else:
            _registry_update_hp(target_name, entry["remaining_hp"])
    return entry


def _apply_target_damage(target_name, damage, result, narrative_parts, *,
                         current_hp, challenge_rating=None, award_xp=False,
                         require_damage=False, player_fallback=False,
                         cursor=None, damage_type=None):
    """Resolve one target's damage: the registry HP fallback, the registry write, the kill
    path and the optional XP award.

    `current_hp` is the HP the caller already knows (a registry lookup fills it in when None);
    `require_damage` keeps the npc-vs-npc gate (`damage > 0`); `player_fallback` lets a
    non-registry player target be resolved from the DB and take its damage there. Writes
    `target_remaining_hp`/`target_killed` (plus `xp_awarded`/`challenge_rating`/`xp_result` on
    an awarded kill) into `result`, and appends the `(KILLED)`/`XP Awarded:` lines to
    `narrative_parts`.
    """
    from_registry = False
    db_target = False
    if current_hp is None and target_name:
        current_hp = _registry_hp(target_name)
        if current_hp is not None:
            from_registry = True
        elif player_fallback and cursor:
            player_name = (_db_val(cursor, "name", "") or "").lower()
            if str(target_name or "").lower() == player_name:
                current_hp = int(_db_val(cursor, "current_hit_points", 0))
                db_target = True
    if challenge_rating is None and target_name:
        challenge_rating = _registry_cr(target_name)
    if current_hp is None or (require_damage and damage <= 0):
        result["target_killed"] = None
        return

    remaining = current_hp - damage
    result["target_remaining_hp"] = remaining

    if target_name and from_registry:
        _registry_update_hp(target_name, max(remaining, 0))
    elif db_target and cursor:
        damage, dmg_note = _apply_player_damage_modifiers(cursor, damage, damage_type)
        if dmg_note:
            result["damage_modified"] = dmg_note
        result["hp_change"] = _apply_hp_change(cursor, -damage)

    if remaining <= 0:
        result["target_killed"] = True
        if target_name and from_registry:
            _registry_kill(target_name)
        max_hp = _registry_max_hp(target_name)
        display_max = f"/{max_hp}" if max_hp else ""
        narrative_parts.append(f"{target_name} HP: 0{display_max} (KILLED)")
    else:
        result["target_killed"] = False

    if award_xp and result.get("target_killed") and challenge_rating is not None:
        xp_awarded = CR_XP_TABLE.get(challenge_rating, 0)
        if xp_awarded > 0 and cursor:
            xp_result = apply_numeric_change(key="xp", delta=xp_awarded)
            result["xp_awarded"] = xp_awarded
            result["challenge_rating"] = challenge_rating
            result["xp_result"] = xp_result
            narrative_parts.append(f"XP Awarded: {xp_awarded}")
            if xp_result.get("level_up"):
                narrative_parts.append(xp_result["level_up_summary"])


# ── stat blocks: saves, attacks, conditions, damage types ────────────────────


# SRD conditions -> the roll effects the engine applies. `attackers_*_melee/ranged` need to know
# whether the attack is made at range; `resist_all` is petrified's resistance to all damage.

# SRD 5.1: concentration ends if you are incapacitated or killed.

def _registry_save(target_name: str, ability) -> int | None:
    """Save modifier for a registry target from its per-ability `saves`."""
    entry = _registry_entry(target_name)
    if not entry:
        return None
    key = str(ability or "").strip().lower()[:3]
    saves = entry.get("saves")
    if isinstance(saves, dict) and key in saves:
        try:
            return int(saves[key])
        except (TypeError, ValueError):
            pass
    return None


def _derive_npc_attack(entry, attack_name) -> dict | None:
    """The declared attack entry whose name matches (case-insensitive)."""
    wanted = str(attack_name or "").strip().lower()
    for attack in (entry or {}).get("attacks") or []:
        if isinstance(attack, dict) and str(attack.get("name") or "").strip().lower() == wanted:
            return attack
    return None


def _npc_attack_is_ranged(attack) -> bool:
    if not isinstance(attack, dict):
        return False
    props = [p.lower() for p in _string_list(attack.get("properties"))]
    if any("two-handed" in p for p in props):
        pass  # a two-handed melee weapon is still melee — range/reach decides
    if attack.get("range"):
        return True
    return any(k in " ".join(props) for k in ("ammunition", "ranged"))


def _string_list(value) -> list[str]:
    """Coerce a declaration to a list of strings.

    A model may send a scalar where a list is expected (e.g.
    `traits: "Sneak Attack 2d6."` instead of `["Sneak Attack 2d6."]`).
    Iterating the bare string would split it into characters.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _normalize_attacks(value) -> list[dict]:
    """A declared `attacks` value as a list of dicts, with string-list fields coerced.

    Accepts a single dict as well as a list, and normalizes each attack's
    `properties` so a bare string is never iterated character by character.
    """
    if isinstance(value, dict):
        value = [value]
    elif not isinstance(value, (list, tuple)):
        return []
    attacks = []
    for a in value:
        if not isinstance(a, dict):
            continue
        a = dict(a)
        a["properties"] = _string_list(a.get("properties"))
        attacks.append(a)
    return attacks


def _normalize_combatant(c) -> dict:
    """Normalize a declared stat block into a registry entry."""
    max_hp = int(c.get("max_hp", c["hp"]))
    saves = c.get("saves") if isinstance(c.get("saves"), dict) else {}
    normalized = {
        "current_hp": int(c["hp"]),
        "max_hp": max_hp,
        "ac": int(c["ac"]),
        "saves": {k: int(v) for k, v in saves.items()
                  if k in ABILITY_KEYS and isinstance(v, (int, float))},
        "challenge_rating": c.get("challenge_rating"),
        "initiative_modifier": int(c.get("initiative_modifier", 0) or 0),
        "initiative_advantage": bool(c.get("initiative_advantage", False)),
        "role": str(c.get("role") or "hostile").strip().lower(),
        "speed": c.get("speed"),
        "damage_resistances": [s.lower() for s in _string_list(c.get("damage_resistances"))],
        "damage_immunities": [s.lower() for s in _string_list(c.get("damage_immunities"))],
        "damage_vulnerabilities": [s.lower() for s in _string_list(c.get("damage_vulnerabilities"))],
        "condition_immunities": [s.lower() for s in _string_list(c.get("condition_immunities"))],
        "attacks": _normalize_attacks(c.get("attacks")),
        "multiattack": c.get("multiattack", 1),
        "spellcasting": c.get("spellcasting") if isinstance(c.get("spellcasting"), dict) else None,
        "traits": _string_list(c.get("traits")),
        "conditions": [s.lower() for s in _string_list(c.get("conditions"))],
        "exhaustion": max(0, min(EXHAUSTION_MAX, int(c.get("exhaustion") or 0))),
        "initiative_roll": 0,
        "initiative_total": 0,
        "is_player": False,
        "killed": False,
        "status": "active",
    }
    return normalized


def _player_registry_entry(cursor) -> dict:
    """The player's registry entry, with saves/speed/spellcasting derived from their sheet."""
    name = _db_val(cursor, "name", "Player")
    scores = _effective_stats(cursor)
    mods = {k: _ability_mod(scores.get(k, 10)) for k in ABILITY_KEYS}
    proficient = _save_proficiencies(cursor)
    prof_bonus = _effective_prof_bonus(cursor)
    state = _item_effect_state(cursor)
    item_save = int(state.get("save_bonus") or 0)
    saves = {k: mods[k] + (prof_bonus if k in proficient else 0) + item_save
             for k in ABILITY_KEYS}
    defense = _player_defenses(cursor)
    speed_state = equipment.speed_state(_carry_get(cursor))
    exh = _exhaustion_effects(_exhaustion_level(cursor))
    speed_walk = int(speed_state["walk"] * exh["speed_multiplier"])
    speeds = {k: int(v * exh["speed_multiplier"])
              for k, v in (speed_state["modes"] or {}).items()}
    conditions = sorted(_player_conditions(cursor))
    if exh["level"] > 0 and "exhaustion" not in conditions:
        conditions = sorted([*conditions, "exhaustion"])
    spell_raw = _db_val(cursor, "spellcasting", {}) or {}
    spellcasting = None
    if isinstance(spell_raw, dict) and spell_raw.get("dc") is not None:
        spellcasting = {"ability": spell_raw.get("ability"), "save_dc": spell_raw.get("dc"),
                        "attack_modifier": spell_raw.get("attack_modifier")}
    return {
        "name": name,
        "current_hp": int(_db_val(cursor, "current_hit_points", 1)),
        "max_hp": int(_db_val(cursor, "total_hit_points", 1)),
        "ac": int(_db_val(cursor, "armor_class", 10)),
        "saves": saves,
        "challenge_rating": None,
        "initiative_modifier": mods["dex"] + int(state.get("initiative_bonus") or 0),
        "initiative_advantage": bool(state.get("initiative_advantage")),
        "role": "ally",
        "speed": speed_walk,
        "speeds": speeds,
        "damage_resistances": list(defense.get("damage_resistances") or []),
        "damage_immunities": list(defense.get("damage_immunities") or []),
        "damage_vulnerabilities": list(defense.get("damage_vulnerabilities") or []),
        "condition_immunities": list(defense.get("condition_immunities") or []),
        "attacks": [],
        "multiattack": 1,
        "spellcasting": spellcasting,
        "traits": [],
        "conditions": conditions,
        "exhaustion": exh["level"],
        "initiative_roll": 0,
        "initiative_total": 0,
        "is_player": True,
        "killed": False,
        "status": "active",
    }


def _fmt_attack(a) -> str:
    """One attack as a readable line: 'Dagger +4, 1d4+2 piercing (Finesse, reach 5)'."""
    name = str(a.get("name") or "attack")
    bonus = a.get("attack_bonus")
    hit = f"{int(bonus):+d}" if isinstance(bonus, (int, float)) else ""
    dmg = str(a.get("damage_dice") or a.get("damage") or "").strip()
    mod = a.get("damage_modifier")
    if isinstance(mod, (int, float)) and mod:
        dmg += f"{int(mod):+d}"
    dtype = str(a.get("damage_type") or "").strip()
    reach = a.get("reach")
    props = _string_list(a.get("properties"))
    if reach:
        props.append(f"reach {reach}")
    detail = ", ".join(p for p in (f"{dmg} {dtype}".strip(), *props) if p)
    head = " ".join(p for p in (name, hit) if p)
    return f"{head}, {detail}" if detail else head


def _combatant_sheet_lines(name, entry) -> list[str]:
    """The full declared sheet of a registry NPC, for the client's name tooltip (never the player)."""
    lines = [f"{name} — {entry.get('role') or 'hostile'}"]
    vitals = [f"HP {entry['current_hp']}/{entry['max_hp']}", f"AC {entry['ac']}"]
    if entry.get("speed") is not None:
        vitals.append(f"Speed {entry['speed']}")
    if entry.get("challenge_rating") is not None:
        vitals.append(f"CR {entry['challenge_rating']}")
    lines.append("  " + "  ".join(vitals))
    saves = entry.get("saves") or {}
    if saves:
        lines.append("  Saves: " + ", ".join(f"{k.upper()} {int(v):+d}" for k, v in saves.items()))
    attacks = entry.get("attacks") or []
    if attacks:
        lines.append("  Attacks: " + "; ".join(_fmt_attack(a) for a in attacks))
    if int(entry.get("multiattack") or 1) > 1:
        lines.append(f"  Multiattack: {entry['multiattack']}")
    sc = entry.get("spellcasting")
    if isinstance(sc, dict) and sc:
        lines.append("  Spellcasting: " + json.dumps(sc, ensure_ascii=True))
    traits = entry.get("traits") or []
    if traits:
        lines.append("  Traits: " + "; ".join(str(t) for t in traits))
    for label, key in (("Resistances", "damage_resistances"), ("Immunities", "damage_immunities"),
                       ("Vulnerabilities", "damage_vulnerabilities"),
                       ("Condition immunities", "condition_immunities")):
        vals = entry.get(key) or []
        if vals:
            lines.append(f"  {label}: " + ", ".join(str(v) for v in vals))
    if entry.get("conditions"):
        lines.append("  Conditions: " + ", ".join(str(c) for c in entry["conditions"]))
    if int(entry.get("exhaustion") or 0) > 0:
        lines.append(f"  Exhaustion: {int(entry['exhaustion'])}")
    status = str(entry.get("status") or "active").strip().lower()
    if status != "active":
        lines.append(f"  Status: {status}")
    return lines


def _player_registry_vitals() -> tuple[int, int, int] | None:
    """The player's live vitals (current HP, max HP, AC) straight from the DB, or None.

    The registry's player entry is a registration snapshot: damage from an NPC attack,
    ``modify_player_numeric``, healing, a rest and a changed AC all land in the DB only. The
    roster summary refreshes from here so the client never shows a stale player."""
    if state.DB_CONNECTION is None:
        return None
    cursor = state.DB_CONNECTION.cursor()
    current = int(_db_val(cursor, "current_hit_points", 0) or 0)
    total = int(_db_val(cursor, "total_hit_points", 0) or 0)
    ac = int(_db_val(cursor, "armor_class", 10) or 10)
    return current, total, ac


def _registry_summary_list() -> list[dict]:
    """The compact roster the engine forwards to the client for the combatant tooltips.

    The player's row is refreshed from the DB, since the registry entry only snapshots their
    vitals at registration."""
    vitals = _player_registry_vitals()
    out: list[dict] = []
    for rname, entry in state._COMBAT_REGISTRY.items():
        cur_hp, max_hp, ac = entry["current_hp"], entry["max_hp"], entry["ac"]
        if entry.get("is_player") and vitals is not None:
            v_cur, v_max, v_ac = vitals
            if v_max > 0:
                cur_hp, max_hp = v_cur, v_max
            if v_ac > 0:
                ac = v_ac
            entry["current_hp"], entry["max_hp"], entry["ac"] = cur_hp, max_hp, ac
        summary = {
            "name": rname,
            "hp": f"{cur_hp}/{max_hp}",
            "ac": ac,
            "initiative": entry["initiative_total"],
            "is_player": entry.get("is_player", False),
            "role": entry.get("role", "hostile"),
            "killed": bool(entry.get("killed", False)),
            "status": str(entry.get("status") or "active"),
        }
        if entry.get("speed") is not None:
            summary["speed"] = entry["speed"]
        if entry.get("challenge_rating") is not None:
            summary["cr"] = entry["challenge_rating"]
        if entry.get("conditions"):
            summary["conditions"] = entry["conditions"]
        out.append(summary)
    return out


def _companions_store(cursor) -> dict:
    """The declared companion sheets, `{lowername: sheet}` -- one party in every age."""
    raw = _db_val(cursor, "companions", {}) if cursor is not None else {}
    return _flatten_companions(raw)


def _save_companions(cursor, data: dict) -> None:
    cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                   ("companions", json.dumps(data)))
    state.DB_CONNECTION.commit()


def _companion_sheet(name: str, sheet: dict) -> dict | None:
    """One companion's durable sheet: static combat data, validated through the combatant
    normaliser, plus what an out-of-combat check needs.

    Requires a name, an `ac`, a `max_hp` (an `hp` in the declaration is read as the max) and the
    six `stats` -- a companion is a full character, and its ability scores are what let the
    engine derive its checks (`skills` may be empty, `proficiency_bonus` defaults to +2).
    `advantages` are its combat-only special rules (Sneak Attack 2d6, Pack Tactics), which the
    registry calls `traits`. `description`/`race`/`class` are identity for the illustrator, never
    combat data. Returns None when the declaration is too thin to fight with."""
    name = " ".join(str(name or "").split())
    if not isinstance(sheet, dict) or not name:
        return None
    max_hp = sheet.get("max_hp", sheet.get("hp"))
    raw_stats = sheet.get("stats") if isinstance(sheet.get("stats"), dict) else {}
    stats = {k: int(raw_stats[k]) for k in COMPANION_ABILITY_KEYS
             if isinstance(raw_stats.get(k), (int, float))}
    if max_hp is None or not sheet.get("ac") or len(stats) != len(COMPANION_ABILITY_KEYS):
        return None
    advantages = _string_list(sheet.get("advantages"))
    declared = dict(sheet)
    declared.update({"name": name, "hp": max_hp, "role": "ally", "traits": advantages})
    entry = _normalize_combatant(declared)
    out = {k: entry[k] for k in _COMPANION_KEEP if k in entry}
    out.update({
        "name": name,
        "role": "ally",
        "max_hp": int(max_hp),
        "stats": stats,
        "advantages": advantages,
        "skills": _string_list(sheet.get("skills")),
        "expertise": _string_list(sheet.get("expertise")),
        "proficiency_bonus": int(sheet.get("proficiency_bonus") or COMPANION_DEFAULT_PROF),
        "race": " ".join(str(sheet.get("race") or "").split()),
        "class": " ".join(str(sheet.get("class") or "").split()),
        "description": " ".join(str(sheet.get("description") or "").split()),
    })
    return out


def _expand_companions(cursor, combatants):
    """Fill a name-only combatant from a declared companion's sheet.

    Returns ``(combatants, filled, problem)``; `filled` is ``{lowername: sheet}`` for the
    entries the engine filled. An entry carrying `ac` is an explicit stat block and is never
    shadowed; a name-only entry that is no companion at all is refused with words instead of the
    normalizer's KeyError. Companions travel with the Traveller, so the store is one party."""
    if cursor is None or not isinstance(combatants, list):
        return combatants, {}, None
    store = _companions_store(cursor)
    filled: dict = {}
    out: list = []
    for c in combatants:
        if not isinstance(c, dict) or "ac" in c:
            out.append(c)
            continue
        name = " ".join(str(c.get("name") or "").split())
        if not name:
            out.append(c)
            continue
        sheet = store.get(name.lower())
        if sheet is None:
            return combatants, {}, {
                "success": False, "error": "not_declared",
                "reason": f"{name} has no stat block and is not a declared companion.",
                "gm_instruction": "Declare the companion with register_companion, or give hp and ac.",
            }
        entry = dict(sheet)
        entry["name"] = name
        entry["role"] = "ally"
        # The registry's field for a companion's combat-only special rules is `traits`; a sheet
        # written before the field was renamed stored them under `traits` directly.
        entry["traits"] = list(sheet.get("advantages") or sheet.get("traits") or [])
        entry["hp"] = (int(c["hp"]) if isinstance(c.get("hp"), (int, float))
                       else int(sheet.get("max_hp") or 1))
        filled[name.lower()] = sheet
        out.append(entry)
    return out, filled, None


def _companion_line(sheet: dict, entry: dict) -> str:
    """One compact line for the Game Master: who the companion is and how they fight."""
    ident = ", ".join(p for p in (sheet.get("race"), sheet.get("class")) if p)
    line = (f"{sheet.get('name')} ({ident})" if ident else str(sheet.get("name")))
    line += f" — {entry['current_hp']}/{entry['max_hp']} HP, AC {entry['ac']}"
    if entry.get("speed") is not None:
        line += f", speed {entry['speed']}"
    attacks = entry.get("attacks") or []
    if attacks:
        line += "; " + "; ".join(_fmt_attack(a) for a in attacks)
    traits = entry.get("traits") or []
    if traits:
        line += "; " + "; ".join(str(t) for t in traits)
    return line


def _companion_here(cursor, name: str) -> dict | None:
    """The declared companion with that name, or None (the party travels, one store)."""
    if cursor is None:
        return None
    return _companions_store(cursor).get(str(name or "").strip().lower())


def _companion_check_detail(sheet: dict, check_name: str, ability: str | None,
                            situational: int = 0) -> dict:
    """A companion's ability/skill check modifier, derived from its own sheet.

    The player path's shape and arithmetic, without the item/feature layers a player has: the
    ability modifier from its `stats`, plus its proficiency bonus when the check is a skill it
    is proficient in (doubled for Expertise). `proficiency_bonus` defaults to +2."""
    skill = skills.skill_name(check_name) if check_name else None
    ab = (str(ability or "").strip().lower()[:3]
          or (skills.SKILL_ABILITIES.get(skill, "") if skill else ""))
    stats = sheet.get("stats") or {}
    ability_mod = _ability_mod(int(stats.get(ab, 10))) if ab else 0
    prof_bonus = int(sheet.get("proficiency_bonus") or COMPANION_DEFAULT_PROF)
    key = skills.normalize(skill) if skill else ""
    known = {skills.normalize(s) for s in (sheet.get("skills") or [])}
    expert = {skills.normalize(s) for s in (sheet.get("expertise") or [])}
    proficient = bool(key) and key in known
    expertise = proficient and key in expert
    prof_part = 2 * prof_bonus if expertise else (prof_bonus if proficient else 0)
    return {
        "skill": skill, "ability": ab, "ability_modifier": ability_mod,
        "proficiency_bonus": prof_part, "proficient": proficient, "expertise": expertise,
        "jack_of_all_trades": False, "item_bonus": 0, "item_sources": [],
        "advantage": False, "advantage_sources": [],
        "situational": int(situational or 0),
        "modifier": ability_mod + prof_part + int(situational or 0),
    }


def _expand_groups(combatants):
    """Expand a `names` group into one entry per name, sharing the rest of the block.

    A combatant entry carries either `name` (one creature) or `names` (many identical ones) --
    never both. Grouping a stat block saves the Game Master from repeating it for every mook. The
    engine then rolls ONE initiative for the whole group (SRD: identical creatures act together),
    while each name keeps its own HP and conditions. Returns ``(combatants, group_of, problem)``;
    `group_of` maps each grouped name to a group id, and `problem` is a refusal or None."""
    if not isinstance(combatants, list):
        return combatants, {}, None
    out: list = []
    group_of: dict[str, int] = {}
    next_gid = 0
    for c in combatants:
        if not isinstance(c, dict) or "names" not in c:
            out.append(c)
            continue
        if "name" in c:
            return combatants, {}, {
                "success": False, "error": "name_and_names",
                "reason": "An entry has both `name` and `names`; use one or the other.",
                "narrative_format": "Refused: a combatant cannot carry both `name` and `names`.",
            }
        names = c.get("names")
        if not isinstance(names, list) or not names:
            return combatants, {}, {
                "success": False, "error": "empty_names",
                "reason": "`names` must be a non-empty list of combatant names.",
                "narrative_format": "Refused: `names` was empty.",
            }
        block = {k: v for k, v in c.items() if k != "names"}
        gid = next_gid
        next_gid += 1
        seen: set[str] = set()
        for raw in names:
            nm = " ".join(str(raw or "").split())
            if not nm:
                return combatants, {}, {
                    "success": False, "error": "blank_name",
                    "reason": "`names` contains a blank entry.",
                    "narrative_format": "Refused: `names` contains a blank entry.",
                }
            if nm.lower() in seen:
                return combatants, {}, {
                    "success": False, "error": "duplicate_name",
                    "reason": f"`names` lists '{nm}' more than once.",
                    "narrative_format": f"Refused: '{nm}' is listed twice in `names`.",
                }
            seen.add(nm.lower())
            group_of[nm] = gid
            out.append({**block, "name": nm})
    return out, group_of, None
