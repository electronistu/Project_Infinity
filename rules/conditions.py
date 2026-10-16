"""Conditions and damage modifiers: the condition lists and what each condition does.

The registry/sheet condition readers, the SRD attack/save/check effect tables, applying a
condition to a combatant, the ends-on-damage marks a spell leaves, and the resistance,
immunity and vulnerability arithmetic damage flows through.
"""

import equipment

from . import state
from .consts import CONDITION_EFFECTS
from .db import _carry_get, _db_set, _db_val


def _registry_conditions(name) -> set:
    # `combat._registry_entry` is the resolver the combat layer exposes; this module is imported
    # by it, so it reaches the same index through `state.registry_get` directly (no cycle).
    entry = state.registry_get(name) or {}
    return {str(c).strip().lower() for c in (entry.get("conditions") or [])}


def _player_conditions(cursor) -> set:
    if cursor is None:
        return set()
    return {str(c).strip().lower() for c in (_db_val(cursor, "conditions", []) or [])}


def _conditions_for(cursor, name, is_player=False) -> set:
    if is_player:
        return _player_conditions(cursor)
    return _registry_conditions(name)


# A long rest and a Device jump both end the running spells. The lasting marks -- a curse, a
# geas, a disease -- are the GM's `active_effects` tags: they carry no buff data and no
# appearance, so their shape alone tells them apart from a running spell. `petrified` and a long
# `poisoned` are the two SRD conditions a rest does not simply wash away; everything else in
# `conditions` is a transient spell or combat state.

def _effect_name(entry) -> str:
    return str(entry.get("name") if isinstance(entry, dict) else entry or "").strip()


def _is_player_name(cursor, name) -> bool:
    """True when `name` is the player (registry is_player flag, else the save's name)."""
    entry = state.registry_get(name)
    if isinstance(entry, dict) and entry.get("is_player"):
        return True
    if cursor is None or not name:
        return False
    return (str(name).strip().lower()
            == str(_db_val(cursor, "name", "") or "").strip().lower())


def _mark_condition_ends_on_damage(name, condition):
    """Remember that `condition` on `name` ends when they take damage (registry or sheet)."""
    cond = str(condition or "").strip().lower()
    if not cond or not name:
        return
    entry = state.registry_get(name)
    if isinstance(entry, dict):
        entry["conditions_end_on_damage"] = sorted(
            {str(c).strip().lower() for c in (entry.get("conditions_end_on_damage") or [])}
            | {cond})
        return
    state._SHEET_WAKE_CONDITIONS.setdefault(name, set()).add(cond)


def _clear_combat_condition(cursor, name, condition):
    """Undo `_apply_combat_condition` for a registered combatant or the player's sheet."""
    cond = str(condition or "").strip().lower()
    marks = state._SHEET_WAKE_CONDITIONS.get(name)
    if marks is not None:
        marks.discard(cond)
        if not marks:
            state._SHEET_WAKE_CONDITIONS.pop(name, None)
    entry = state.registry_get(name)
    if entry is None:
        if cursor is not None and _is_player_name(cursor, name):
            current = _player_conditions(cursor)
            if cond in current:
                _db_set(cursor, "conditions", sorted(current - {cond}))
                state.DB_CONNECTION.commit()
                return True
        return False
    if entry.get("is_player"):
        if cursor is not None:
            _db_set(cursor, "conditions", sorted(_player_conditions(cursor) - {cond}))
            state.DB_CONNECTION.commit()
        entry["conditions"] = sorted(
            {str(c).strip().lower() for c in (entry.get("conditions") or [])} - {cond})
        return True
    current = {str(c).strip().lower() for c in (entry.get("conditions") or [])}
    if cond not in current:
        return False
    entry["conditions"] = sorted(current - {cond})
    return True


def _wake_on_damage(name):
    """End every condition on `name` that was marked as ending on damage."""
    entry = state.registry_get(name)
    if not isinstance(entry, dict):
        marked = state._SHEET_WAKE_CONDITIONS.get(name)
        if not marked:
            return
        cursor = state.DB_CONNECTION.cursor() if state.DB_CONNECTION is not None else None
        if cursor is None:
            return
        woke = sorted(marked & _player_conditions(cursor))
        if not woke:
            return
        for cond in woke:
            _clear_combat_condition(cursor, name, cond)
        state._WOKE.append({"name": name, "conditions": woke})
        return
    marked = {str(c).strip().lower() for c in (entry.get("conditions_end_on_damage") or [])}
    if not marked:
        return
    current = {str(c).strip().lower() for c in (entry.get("conditions") or [])}
    woke = sorted(marked & current)
    if not woke:
        return
    cursor = state.DB_CONNECTION.cursor() if state.DB_CONNECTION is not None else None
    for cond in woke:
        _clear_combat_condition(cursor, name, cond)
    entry["conditions_end_on_damage"] = sorted(marked - set(woke))
    state._WOKE.append({"name": name, "conditions": woke})


def _drain_woke() -> list[dict]:
    out, state._WOKE = state._WOKE, []
    return out


def _condition_attack_effects(attacker_conditions, target_conditions, ranged=False):
    """(advantage?, disadvantage?, force_crit?, sources) from conditions on both sides."""
    advantage = disadvantage = force_crit = False
    sources = []
    for cond in sorted(attacker_conditions or []):
        eff = CONDITION_EFFECTS.get(cond, {})
        if eff.get("self_attack_advantage"):
            advantage = True
            sources.append(f"{cond} (attacker)")
        if eff.get("self_attack_disadvantage"):
            disadvantage = True
            sources.append(f"{cond} (attacker)")
    for cond in sorted(target_conditions or []):
        eff = CONDITION_EFFECTS.get(cond, {})
        if eff.get("attackers_advantage"):
            advantage = True
            sources.append(f"{cond} (target)")
        if eff.get("attackers_advantage_melee") and not ranged:
            advantage = True
            sources.append(f"{cond} (target)")
        if eff.get("attackers_disadvantage"):
            disadvantage = True
            sources.append(f"{cond} (target)")
        if eff.get("attackers_disadvantage_ranged") and ranged:
            disadvantage = True
            sources.append(f"{cond} (target, ranged)")
        if eff.get("melee_crit") and not ranged:
            force_crit = True
            sources.append(f"{cond} (crit)")
    return advantage, disadvantage, force_crit, sources


def _condition_save_effects(conditions, ability):
    """(disadvantage?, auto_fail?, sources) for a saving throw."""
    disadvantage = auto_fail = False
    sources = []
    for cond in sorted(conditions or []):
        eff = CONDITION_EFFECTS.get(cond, {})
        key = str(ability or "").strip().lower()[:3]
        if eff.get("auto_fail_str_dex_saves") and key in ("str", "dex"):
            auto_fail = True
            sources.append(f"{cond} (auto-fail)")
        if eff.get("self_dex_save_disadvantage") and key == "dex":
            disadvantage = True
            sources.append(f"{cond}")
    return disadvantage, auto_fail, sources


def _condition_check_disadvantage(conditions, ability=None):
    """(disadvantage?, sources) for an ability check (poisoned/frightened, restrained DEX)."""
    disadvantage = False
    sources = []
    key = str(ability or "").strip().lower()[:3]
    for cond in sorted(conditions or []):
        eff = CONDITION_EFFECTS.get(cond, {})
        if eff.get("self_check_disadvantage"):
            disadvantage = True
            sources.append(cond)
        if eff.get("self_dex_save_disadvantage") and key == "dex":
            disadvantage = True
            sources.append(cond)
    return disadvantage, sources


def _apply_damage_modifiers(entry, damage, damage_type) -> tuple[int, str | None]:
    """SRD 5.1 resistances/immunities/vulnerabilities applied to incoming damage.

    immunity -> 0, resistance -> half (round down), vulnerability -> double; resistance and
    vulnerability together cancel. Petrified resists all damage. Returns (damage, note).
    """
    if entry is None or damage <= 0 or not damage_type:
        return damage, None
    dtype = str(damage_type).strip().lower()
    immunities = [str(x).strip().lower() for x in (entry.get("damage_immunities") or [])]
    resistances = [str(x).strip().lower() for x in (entry.get("damage_resistances") or [])]
    vulnerabilities = [str(x).strip().lower() for x in (entry.get("damage_vulnerabilities") or [])]
    if "all" in immunities or dtype in immunities:
        return 0, f"immune to {dtype}"
    if any(CONDITION_EFFECTS.get(c, {}).get("resist_all") for c in (entry.get("conditions") or [])):
        resistances = [*resistances, dtype]
    factor = 1.0
    if "all" in resistances or dtype in resistances:
        factor *= 0.5
    if "all" in vulnerabilities or dtype in vulnerabilities:
        factor *= 2.0
    if factor == 1.0:
        return damage, None
    return int(damage * factor), f"{damage} -> {int(damage * factor)} ({dtype}: x{factor:g})"


def _apply_component_damage(entry, total, primary, extra, primary_type, extra_type,
                             halved=False) -> tuple[int, str | None]:
    """Apply resistances to each damage component separately, then sum (SRD 5.1).

    A multi-component spell (Ice Storm, Flame Strike, Meteor Swarm) resists per type. When a
    successful save already halved the total, the components are split back out proportionally
    (the rounding difference is at most 1 point per component)."""
    if entry is None or total <= 0:
        return total, None
    if not extra_type or extra <= 0 or not primary_type:
        return _apply_damage_modifiers(entry, total, primary_type)
    if halved and (primary + extra) > 0:
        part_p = min(total, int(round(total * primary / (primary + extra))))
        part_e = total - part_p
    else:
        part_p = min(primary, total)
        part_e = max(0, total - part_p)
    part_p, note_p = _apply_damage_modifiers(entry, part_p, primary_type)
    part_e, note_e = _apply_damage_modifiers(entry, part_e, extra_type)
    return part_p + part_e, "; ".join(n for n in (note_p, note_e) if n) or None


def _player_defenses(cursor) -> dict:
    """The player's damage/condition defenses from equipped items AND active spell effects."""
    if cursor is None:
        return {"damage_resistances": [], "damage_immunities": [], "damage_vulnerabilities": [],
                "condition_immunities": [], "sources": []}
    return equipment.defense_state(_carry_get(cursor))


def _apply_player_damage_modifiers(cursor, damage, damage_type) -> tuple[int, str | None]:
    """SRD 5.1 resistances/immunities/vulnerabilities applied to damage the PLAYER takes.

    Mirrors `_apply_damage_modifiers` but sources the defenses from the player's sheet (items +
    active spells) and respects petrified's resistance to all damage.
    """
    if cursor is None or damage <= 0 or not damage_type:
        return damage, None
    defenses = _player_defenses(cursor)
    dtype = str(damage_type).strip().lower()
    immunities = list(defenses.get("damage_immunities") or [])
    resistances = list(defenses.get("damage_resistances") or [])
    vulnerabilities = list(defenses.get("damage_vulnerabilities") or [])
    if "all" in immunities or dtype in immunities:
        return 0, f"player immune to {dtype} ({defenses.get('sources')})"
    if any(CONDITION_EFFECTS.get(c, {}).get("resist_all") for c in _player_conditions(cursor)):
        resistances.append(dtype)
    factor = 1.0
    if "all" in resistances or dtype in resistances:
        factor *= 0.5
    if "all" in vulnerabilities or dtype in vulnerabilities:
        factor *= 2.0
    if factor == 1.0:
        return damage, None
    return int(damage * factor), f"player {damage} -> {int(damage * factor)} ({dtype}: x{factor:g})"
