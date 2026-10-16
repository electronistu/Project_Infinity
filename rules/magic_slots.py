"""P2 of `resolve_magic`: the spell slot, the upcast, and the result scaffold.

`prepare_slot` validates and consumes the slot (or the scroll's ability check), works out
the character level, the slot level actually used and the scaled dice (`final_dice` /
`final_mod`), picks the Flame Strike component the upcast empowers, adds the delayed-blast
dice and the caster's spellcasting modifier, and lays down `result` + `narrative_parts`.
Returns a finished tool result (`dict`) when the slot is not there, else `None`.
"""

import json
import random
import re

from . import state
from .combat import _action_failure, _registry_ac, _scale_repeat_dice
from .db import _ability_mod, _db_val, _effective_stats
from .dice import _combine_dice, _compute_spell_damage
from .magic_cast import CastState
from .sheet import _caster_spellcasting_mod
from .spell_effects import _ordinal, _scale_buff_dice
from .spell_knowledge import _validate_spell_slot
from .tools_player import modify_player_numeric


def prepare_slot(
    cast: CastState,
    *,
    boost_damage_type: str | None,
    delayed_rounds: int | None,
    caster_level: int | None,
) -> dict | None:
    """Take the slot, scale the dice, and open the result. `dict` = refuse, `None` = go."""
    # ── SPELL SLOT MANAGEMENT ──
    is_cantrip = (cast.sp_level == 0)
    slot_consumed = None
    slot_level_used = None
    slot_narrative = None
    scroll_check_info = None

    if not cast.is_npc_attack and not cast.is_npc_vs_npc and not is_cantrip and not cast.ritual and not cast.is_scroll and not cast.maintain_spell and state.DB_CONNECTION is not None:
        if cast.slot_level is not None:
            effective_slot = cast.slot_level
        elif cast.spell:
            effective_slot = cast.sp_level
        else:
            return {
                "success": False,
                "error": f"Cannot determine spell slot level for custom spell '{cast.spell_name}'. Provide slot_level parameter.",
                "spell_name": cast.spell_name,
                "hint": f"For custom spells, specify which slot level to use (e.g. slot_level=3 for a 3rd-level slot).",
            }

        if cast.slot_level is not None and cast.slot_level < cast.sp_level:
            return {
                "success": False,
                "error": f"Cannot cast {cast.spell_name} (level {cast.sp_level}) in a {cast.slot_level}{_ordinal(cast.slot_level).replace(str(cast.slot_level), '')}-level slot. Slot level must be >= spell level ({cast.sp_level}).",
                "spell_name": cast.spell_name,
                "spell_level": cast.sp_level,
                "slot_level_provided": cast.slot_level,
                "hint": f"Upcasting requires a slot of level {cast.sp_level} or higher. Use slot_level={cast.sp_level} or above.",
            }

        slot_key = f"spellcasting.slots.{effective_slot}"
        validation = _validate_spell_slot(cast.cursor, slot_key, -1)

        if validation:
            cast.cursor.execute("SELECT value FROM player WHERE key = ?", ("spellcasting",))
            sc_row = cast.cursor.fetchone()
            available_slots = {}
            if sc_row:
                sc_data = json.loads(sc_row[0])
                for k, v in sc_data.get("slots", {}).items():
                    if int(v) > 0:
                        available_slots[f"lv{k}"] = v
            return _action_failure(
                f"No level {effective_slot} spell slots remaining to cast {cast.spell_name}.",
                f"{cast.actor} {cast.spell_name} — not cast: no {_ordinal(effective_slot)}-level slot remaining",
                spell_name=cast.spell_name,
                slot_level_needed=effective_slot,
                available_slots=available_slots if available_slots else "No spell slots available.",
                hint="Take a long rest to recover spell slots, or cast using a higher-level slot by providing slot_level.",
            )

        cast.cursor.execute("SELECT value FROM player WHERE key = ?", ("spellcasting",))
        sc_check = cast.cursor.fetchone()
        if sc_check:
            sc_data_check = json.loads(sc_check[0])
            str_effective_slot = str(effective_slot)
            if str_effective_slot not in sc_data_check.get("slots", {}):
                cast.cursor.execute("SELECT key FROM player")
                return _action_failure(
                    f"Player has no level {effective_slot} spell slots. Maximum available slot level may be insufficient for this spell.",
                    f"{cast.actor} {cast.spell_name} — not cast: no {_ordinal(effective_slot)}-level slot available",
                    spell_name=cast.spell_name,
                    slot_level_needed=effective_slot,
                    available_slots={f"lv{k}": v for k, v in sc_data_check.get("slots", {}).items() if int(v) > 0} if sc_data_check.get("slots") else "No spell slots.",
                    hint=f"This character does not have level {effective_slot} spell slots available.",
                )

    # ── PARAMETER VALIDATION (before consuming the slot) ──
    cast.is_multi_projectile = bool(cast.spell and (cast.spell.get("projectiles") or cast.spell.get("cantrip_projectile_scaling")))
    cast.has_targets = bool(cast.targets and len(cast.targets) > 0)
    if cast.sp_attack_type == "attack_roll" and cast.target_ac is None and cast.target_name:
        cast.target_ac = _registry_ac(cast.target_name)
    if cast.sp_attack_type == "attack_roll" and cast.target_ac is None and not cast.has_targets:
        return {"success": False, "error": f"Spell '{cast.spell_name}' requires an attack roll. Provide target_ac (or a registered target_name)."}
    if cast.sp_attack_type == "saving_throw" and cast.spell_save_dc <= 0:
        return {"success": False, "error": f"Spell '{cast.spell_name}' requires a saving throw. You must provide spell_save_dc."}
    # An automatic spell has no per-target shape, but a targets[] list is still the caster's
    # word for whom the spell touched: keep the names (naming-only) so the Mechanics line can
    # say who, and flag that no per-target mechanics ran -- a target list is never dropped
    # silently.
    cast.named_only: list[str] = []
    if cast.has_targets and not cast.is_multi_projectile and not cast.sp_hp_pool and not cast.sp_healing \
            and cast.sp_attack_type not in ("saving_throw", "attack_roll"):
        cast.named_only = [str(t.get("name")).strip() for t in cast.targets
                      if isinstance(t, dict) and str(t.get("name") or "").strip()]

    # ── CONSUME SPELL SLOT ──
    slot_result_data = None
    if not cast.is_npc_attack and not cast.is_npc_vs_npc and not is_cantrip and not cast.ritual and not cast.is_scroll and not cast.maintain_spell and state.DB_CONNECTION is not None:
        slot_result_data = modify_player_numeric(key=slot_key, delta=-1)
        slot_level_used = effective_slot
        slot_consumed = True
        if slot_result_data.get("success"):
            new_remaining = slot_result_data.get("new_value", "?")
            slot_narrative = f"Slot Used: {_ordinal(effective_slot)}-level ({new_remaining} remaining)"
        else:
            slot_narrative = f"Slot Used: {_ordinal(effective_slot)}-level"

    elif not cast.is_npc_attack and not cast.is_npc_vs_npc and cast.maintain_spell:
        slot_consumed = "maintain"
        slot_narrative = f"{cast.spell_name} maintained — no slot"

    elif not cast.is_npc_attack and not cast.is_npc_vs_npc and is_cantrip and not cast.is_scroll:
        slot_consumed = "cantrip"
        slot_narrative = "Cantrip — no slot used"
    elif not cast.is_npc_attack and not cast.is_npc_vs_npc and cast.ritual:
        slot_consumed = "ritual"
        slot_narrative = "Ritual Cast — no slot consumed"
    elif not cast.is_npc_attack and not cast.is_npc_vs_npc and cast.is_scroll:
        slot_consumed = "scroll"
        slot_narrative = "Scroll Cast — no slot consumed"
        effective_slot = cast.slot_level if cast.slot_level is not None else cast.sp_level
        if not is_cantrip and effective_slot > 0 and cast.cursor:
            sc_row = cast.cursor.execute(
                "SELECT value FROM player WHERE key = ?", ("spellcasting",)
            ).fetchone()
            if sc_row:
                sc_data = json.loads(sc_row[0])
                if str(effective_slot) not in sc_data.get("slots", {}):
                    ability_name = sc_data.get("ability", "intelligence").lower()
                    stat_key = {"intelligence": "int", "wisdom": "wis", "charisma": "cha"}.get(ability_name, "int")
                    ability_mod = _ability_mod(_effective_stats(cast.cursor).get(stat_key, 10))
                    dc = 10 + effective_slot
                    scroll_d20 = random.randint(1, 20)
                    scroll_total = scroll_d20 + ability_mod
                    if scroll_total < dc:
                        return _action_failure(
                            f"Scroll ability check failed for {cast.spell_name}. "
                            f"Check: {scroll_total} vs DC {dc} ({scroll_d20} + {ability_mod}). "
                            "The scroll's magic fizzles and is wasted.",
                            f"{cast.actor} {cast.spell_name} (scroll) — check {scroll_total} vs DC {dc}: the scroll fizzles",
                            spell_name=cast.spell_name,
                            scroll_level=effective_slot,
                            scroll_check={"d20": scroll_d20, "ability_modifier": ability_mod,
                                          "total": scroll_total, "dc": dc, "passed": False},
                            hint="The scroll is consumed but the spell does not take effect. Remove it from inventory."
                        )
                    else:
                        slot_narrative += f" (Ability Check: {scroll_total} vs DC {dc} — passed)"
                        scroll_check_info = {"d20": scroll_d20, "ability_modifier": ability_mod,
                                             "total": scroll_total, "dc": dc, "passed": True}
    elif cast.is_npc_attack or cast.is_npc_vs_npc:
        slot_consumed = "npc"

    if cast.is_npc_attack:
        cast.character_level = 1
    elif cast.is_npc_vs_npc:
        cast.character_level = caster_level if caster_level is not None else 1
    elif cast.cursor:
        cast.character_level = int(_db_val(cast.cursor, "level", 1))
    else:
        cast.character_level = 1

    cast.computed_slot = cast.slot_level if cast.slot_level is not None else (cast.sp_level if not cast.sp_cantrip_scaling else 0)
    if cast.sp_buffs and cast.spell and cast.sp_level and (cast.computed_slot or 0) > cast.sp_level:
        _scale_buff_dice(cast.sp_buffs, cast.sp_higher_levels, (cast.computed_slot or 0) - cast.sp_level)
    # F5: Flame Strike's upcast empowers ONE component (the caster's choice). With no choice,
    # the primary scales; naming the extra damage type moves the dice to that component.
    damage_spell = cast.spell
    if cast.spell and cast.spell.get("extra_damage_dice") \
            and (cast.spell.get("higher_levels") or cast.spell.get("extra_higher_levels")):
        damage_spell = dict(cast.spell)
        boost = str(boost_damage_type or "").strip().lower()
        extra_type = str(cast.spell.get("extra_damage_type") or "").strip().lower()
        if boost and boost == extra_type:
            damage_spell["higher_levels"] = None
            damage_spell["extra_higher_levels"] = (cast.spell.get("higher_levels")
                                                   or cast.spell.get("extra_higher_levels"))
        else:
            damage_spell["extra_higher_levels"] = None
    cast.final_dice, cast.final_mod, cast.final_extra_dice = _compute_spell_damage(
        damage_spell if damage_spell else {
            "damage_dice": cast.sp_damage_dice,
            "damage_modifier": cast.sp_damage_modifier,
            "level": cast.sp_level,
            "cantrip_scaling": cast.sp_cantrip_scaling,
            "cantrip_scale_dice": None,
            "higher_levels": cast.sp_higher_levels,
            "extra_damage_dice": cast.sp_extra_damage_dice,
            "extra_damage_type": cast.sp_extra_damage_type,
            "extra_higher_levels": None,
        },
        cast.character_level,
        cast.computed_slot if not cast.sp_cantrip_scaling else None,
    )
    # F5: Delayed Blast Fireball accumulates one bonus die per round the bead waits (cap 10).
    if cast.spell and delayed_rounds and cast.spell.get("delayed_bonus_dice"):
        _m = re.match(r"(\d+)d(\d+)", str(cast.spell["delayed_bonus_dice"]))
        if _m:
            _n = int(_m.group(1)) * min(int(delayed_rounds), 10)
            cast.final_dice = _combine_dice(cast.final_dice, f"{_n}d{_m.group(2)}")
    # F6: a damage spell can add the caster's spellcasting modifier (Spiritual Weapon).
    if (cast.spell and cast.spell.get("add_spellcasting_mod") and not cast.sp_healing and cast.cursor is not None
            and not cast.is_npc_attack and not cast.is_npc_vs_npc):
        cast.final_mod += _caster_spellcasting_mod(cast.cursor)


    cast.narrative_parts = []
    if slot_narrative:
        cast.narrative_parts.append(slot_narrative)
    cast.result = {
        "success": True,
        "actor": cast.actor,
        "spell_name": cast.spell_name,
        "target_name": cast.target_name,
        "attack_type": cast.sp_attack_type,
        "slot_consumed": slot_consumed,
    }
    if cast.named_only:
        cast.result["targets_named_only"] = cast.named_only
    if slot_level_used is not None:
        cast.result["slot_level_used"] = slot_level_used
    if cast.sp_repeat:
        rep = dict(cast.sp_repeat)
        if rep.get("damage_dice") and rep.get("higher_levels"):
            rep["damage_dice"] = _scale_repeat_dice(
                rep["damage_dice"], rep["higher_levels"],
                max(0, (cast.computed_slot or cast.sp_level) - cast.sp_level))
        cast.result["_repeat_save"] = rep
        cast.result["_repeat_dc"] = cast.spell_save_dc
        cast.result["_repeat_caster"] = cast.actor
    if cast.sp_condition_ends_on_damage:
        cast.result["_condition_ends_on_damage"] = True
    if scroll_check_info:
        cast.result["scroll_check"] = scroll_check_info
    if slot_result_data is not None:
        cast.result["slot_result"] = slot_result_data
    if cast.config_conflicts:
        cast.result["config_warning"] = {
            "spell": cast.spell_name,
            "note": "config/spells.yml was used; the provided fields differ",
            "fields": cast.config_conflicts,
        }

    cast.is_crit = False
    return None
