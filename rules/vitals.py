"""Hit points, exhaustion, concentration, death saves, and the numeric sheet writer.

`apply_numeric_change` -- the engine behind the `modify_player_numeric` tool -- lives here because
it is mutually recursive with the HP/concentration cluster below it: a numeric change can end
concentration (dropping to 0 HP), and ending an effect (dropping concentration) reverts that
effect's own numeric deltas. Splitting the two would need a function-level import back into
`dice_server`; the tool delegates here instead.
"""

import json

from level_up import apply_level_up

from . import state
from .conditions import (
    _condition_save_effects, _effect_name, _is_player_name, _player_conditions, _player_defenses,
    _wake_on_damage
)
from .consts import EXHAUSTION_MAX, _INCAPACITATING_CONDITIONS, _LASTING_CONDITIONS
from .db import (
    _db_set, _db_val, _exhaustion_level, _recompute_armor_class, _recompute_exhaustion_hp,
    get_nested_value, set_nested_value
)
from .dice import _format_hp_status, _roll_d20, get_level_for_xp
from .sheet import _derived_save, _exhaustion_effects
from .spell_knowledge import _validate_spell_slot


def _apply_exhaustion_change(cursor, delta, reason=None) -> dict:
    """Change the player's exhaustion level and reconcile derived state (HP max, speed)."""
    old = _exhaustion_level(cursor)
    new = max(0, min(EXHAUSTION_MAX, old + int(delta or 0)))
    _db_set(cursor, "exhaustion", str(new))
    state.DB_CONNECTION.commit()
    if new >= EXHAUSTION_MAX:
        _db_set(cursor, "current_hit_points", "0")
        _end_concentration(cursor, reason="exhaustion 6")
        state.DB_CONNECTION.commit()
    max_hp = _recompute_exhaustion_hp(cursor)
    return {"old": old, "new": new, "delta": new - old, "max_hp": max_hp,
            "effects": _exhaustion_effects(new), "reason": reason}


# ── concentration (SRD 5.1) ───────────────────────────────────────────────

def _get_concentration(cursor) -> dict | None:
    if cursor is None:
        return None
    conc = _db_val(cursor, "concentration", None)
    if isinstance(conc, str):
        try:
            conc = json.loads(conc)
        except (json.JSONDecodeError, TypeError):
            conc = None
    return conc if isinstance(conc, dict) and conc.get("spell") else None


def _remove_active_effect(cursor, name) -> dict:
    """Revert and delete one active spell effect; shared by update_player_list and concentration."""
    buff_data_raw = _db_val(cursor, "_active_buff_data", {})
    if isinstance(buff_data_raw, str):
        buff_data_raw = json.loads(buff_data_raw)
    if not isinstance(buff_data_raw, dict) or name not in buff_data_raw:
        return {}
    reverted = {}
    for entry in buff_data_raw[name]:
        if not isinstance(entry, dict):
            continue
        if entry.get("delta") is not None:
            apply_numeric_change(key=entry["field"], delta=-entry["delta"])
            reverted[entry["field"]] = {"delta": -entry["delta"]}
        else:
            reverted.setdefault(entry.get("field") or entry.get("kind"), []).append(
                entry.get("value"))
    del buff_data_raw[name]
    _db_set(cursor, "_active_buff_data", buff_data_raw)
    effects_list = _db_val(cursor, "active_effects", []) or []
    if isinstance(effects_list, str):
        effects_list = json.loads(effects_list)
    _db_set(cursor, "active_effects", [e for e in effects_list if e != name])
    state.DB_CONNECTION.commit()
    return reverted


def _end_concentration(cursor, reason="") -> dict | None:
    """End the player's concentration, reverting its active effect. Returns what ended."""
    conc = _get_concentration(cursor)
    if not conc:
        return None
    spell = conc.get("spell")
    reverted = _remove_active_effect(cursor, spell)
    _db_set(cursor, "concentration", {})
    state.DB_CONNECTION.commit()
    return {"spell": spell, "reverted": reverted, "reason": reason}


def _set_concentration(cursor, spell) -> dict | None:
    """Begin concentrating on `spell`, ending any previous concentration first."""
    ended = _end_concentration(cursor, reason=f"replaced by {spell}")
    _db_set(cursor, "concentration", {"spell": spell})
    state.DB_CONNECTION.commit()
    return ended


def _concentration_save(cursor, damage, source="damage") -> dict | None:
    """SRD 5.1: CON save (DC 10 or half the damage) when a concentrating player takes damage."""
    conc = _get_concentration(cursor)
    if not conc or damage <= 0:
        return None
    spell = conc.get("spell")
    dc = max(10, int(damage) // 2)
    save_detail = _derived_save(cursor, "con")
    cond_dis, _auto_fail, _ = _condition_save_effects(_player_conditions(cursor), "con")
    exh_dis = _exhaustion_effects(_exhaustion_level(cursor))["save_disadvantage"]
    roll, rolls, _mode, _ = _roll_d20(
        advantage=bool(save_detail.get("advantage")), disadvantage=(cond_dis or exh_dis))
    total = roll + save_detail["modifier"]
    success = total >= dc
    result = {
        "spell": spell,
        "dc": dc,
        "roll": roll,
        "modifier": save_detail["modifier"],
        "total": total,
        "success": success,
        "source": source,
    }
    if rolls:
        result["rolls"] = rolls
    if not success:
        result["broken"] = _end_concentration(cursor, reason=f"failed CON save (DC {dc})")
    return result


# ── death saving throws (SRD 5.1) ─────────────────────────────────────────

def _reset_death_saves(cursor):
    _db_set(cursor, "death_save_successes", "0")
    _db_set(cursor, "death_save_failures", "0")
    state.DB_CONNECTION.commit()


def _clear_death_saves(cursor):
    if int(_db_val(cursor, "death_save_successes", 0) or 0) or \
            int(_db_val(cursor, "death_save_failures", 0) or 0):
        _reset_death_saves(cursor)


def _add_death_save_failure(cursor, count=1) -> int:
    failures = min(3, int(_db_val(cursor, "death_save_failures", 0) or 0) + count)
    _db_set(cursor, "death_save_failures", str(failures))
    state.DB_CONNECTION.commit()
    return failures


def _apply_hp_change(cursor, delta):
    current_hp = int(_db_val(cursor, "current_hit_points", 0))
    total_hp = int(_db_val(cursor, "total_hit_points", 1))
    thp = int(_db_val(cursor, "temporary_hit_points", 0))

    thp_absorbed = 0
    effect_expired = ""
    if delta < 0 and thp > 0:
        damage = abs(delta)
        if thp >= damage:
            thp_absorbed = damage
            new_thp = thp - damage
            _db_set(cursor, "temporary_hit_points", str(new_thp))
            state.DB_CONNECTION.commit()
            if new_thp == 0:
                effect_expired = _cleanup_thp_effects(cursor)
            result = {
                "success": True,
                "key": "current_hit_points",
                "old_value": current_hp,
                "new_value": current_hp,
                "delta": 0,
                "hp_status": _format_hp_status(current_hp, total_hp),
                "max_hp": total_hp,
                "temporary_hit_points": {"old": thp, "new": new_thp, "absorbed": damage},
                "message": f"{damage} damage absorbed by temporary HP ({new_thp} THP remaining). {effect_expired}{_format_hp_status(current_hp, total_hp)}",
            }
            return result
        else:
            thp_absorbed = thp
            _db_set(cursor, "temporary_hit_points", "0")
            state.DB_CONNECTION.commit()
            effect_expired = _cleanup_thp_effects(cursor)
            delta = -(damage - thp)

    new_val = current_hp + delta

    clamped = False
    original_delta = delta
    if new_val > total_hp:
        new_val = total_hp
        clamped = True
    if new_val < 0:
        new_val = 0
        clamped = True

    _db_set(cursor, "current_hit_points", str(new_val))
    state.DB_CONNECTION.commit()
    if new_val < current_hp:
        player_name = _db_val(cursor, "name", "") or ""
        if player_name:
            _wake_on_damage(player_name)  # damage ends a marked condition (Eyebite: Asleep)

    result = {
        "success": True,
        "key": "current_hit_points",
        "old_value": current_hp,
        "new_value": new_val,
        "delta": original_delta,
        "hp_status": _format_hp_status(new_val, total_hp),
        "max_hp": total_hp,
    }
    if new_val > current_hp:
        result["healing_applied"] = new_val - current_hp
    if thp_absorbed > 0:
        result["temporary_hit_points"] = {"old": thp, "new": 0, "absorbed": thp_absorbed}

    # SRD 5.1 hooks on the player damage path: healing any real HP ends death saves; damage while
    # concentrating forces a CON save; massive leftover damage is instant death; damage at 0 HP
    # is a death-save failure.
    real_damage = -original_delta if original_delta < 0 else 0
    if new_val > current_hp and current_hp == 0:
        _clear_death_saves(cursor)

    if real_damage > 0:
        conc = _concentration_save(cursor, real_damage)
        if conc is not None:
            result["concentration_check"] = conc
            if not conc["success"]:
                result["concentration_broken"] = conc.get("broken")
        if new_val == 0 and current_hp > 0:
            overshoot = max(0, -(current_hp + original_delta))
            if overshoot >= total_hp:
                _end_concentration(cursor, reason="instant death")
                result["status"] = "Dead"
                result["dead"] = True
                result["instant_death"] = True
                result["message"] = (
                    f"Massive damage! {result['hp_status']}. Instant death — "
                    f"the leftover damage meets or exceeds the HP maximum ({total_hp}).")
            else:
                _reset_death_saves(cursor)
                _end_concentration(cursor, reason="unconscious")
                result["clamped"] = True
                result["status"] = "Unconscious"
                result["death_saves"] = True
                result["death_save_failures"] = 0
                result["message"] = f"HP has reached 0. {result['hp_status']}. Begin death saves."
        elif new_val == 0 and current_hp == 0:
            failures = _add_death_save_failure(cursor, count=1)
            result["status"] = "Unconscious"
            result["death_saves"] = True
            result["death_save_failures"] = failures
            if failures >= 3:
                _end_concentration(cursor, reason="death")
                result["status"] = "Dead"
                result["dead"] = True
                result["message"] = (f"Damage at 0 HP — third death-save failure. "
                                     f"{result['hp_status']}. The character is dead.")
            else:
                result["message"] = (f"Damage while at 0 HP — one death-save failure "
                                     f"({failures}/3). {result['hp_status']}.")
        elif clamped:
            result["clamped"] = True
            result["message"] = f"Value was clamped. {result['hp_status']}."
        else:
            result["message"] = result["hp_status"]
    elif clamped:
        result["clamped"] = True
        if new_val == 0 and current_hp > 0:
            result["status"] = "Unconscious"
            result["death_saves"] = True
            result["message"] = f"HP has reached 0. {result['hp_status']}. Begin death saves."
        elif new_val == total_hp and delta > 0:
            result["message"] = f"HP restored to maximum. {result['hp_status']}."
        else:
            result["message"] = f"Value was clamped. {result['hp_status']}."
    elif total_hp > 0 and new_val == total_hp:
        result["message"] = f"Fully healed. {result['hp_status']}."
    else:
        result["message"] = result["hp_status"]

    if thp_absorbed > 0:
        result["message"] = f"{thp_absorbed} damage absorbed by temporary HP. {effect_expired}" + result["message"]

    return result


def _cleanup_thp_effects(cursor):
    buff_data_raw = _db_val(cursor, "_active_buff_data", {})
    if isinstance(buff_data_raw, str):
        buff_data_raw = json.loads(buff_data_raw)

    effects_list = _db_val(cursor, "active_effects", [])
    if isinstance(effects_list, str):
        effects_list = json.loads(effects_list)

    removed = []
    for spell_name in list(buff_data_raw.keys()):
        for entry in buff_data_raw[spell_name]:
            if entry.get("field") == "temporary_hit_points":
                del buff_data_raw[spell_name]
                if spell_name in effects_list:
                    effects_list.remove(spell_name)
                removed.append(spell_name)
                break

    if removed:
        _db_set(cursor, "_active_buff_data", buff_data_raw)
        _db_set(cursor, "active_effects", effects_list)
        state.DB_CONNECTION.commit()
        names = ", ".join(removed)
        return f"{names} has expired — temporary HP depleted. "
    return ""


def apply_numeric_change(key: str, delta: int) -> dict:
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
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized.", "key": key}
    try:
        cursor = state.DB_CONNECTION.cursor()

        if key == "current_hit_points":
            return _apply_hp_change(cursor, delta)

        if key == "temporary_hit_points":
            current_val = int(_db_val(cursor, "temporary_hit_points", 0))
            new_val = max(current_val + delta, 0)
            _db_set(cursor, "temporary_hit_points", str(new_val))
            state.DB_CONNECTION.commit()
            result = {
                "success": True,
                "key": key,
                "old_value": current_val,
                "new_value": new_val,
                "delta": delta,
            }
            if new_val == 0 and current_val > 0:
                result["message"] = "Temporary HP depleted."
            else:
                result["message"] = f"Temporary HP: {new_val}"
            if current_val + delta < 0:
                result["clamped"] = True
            return result

        if key.startswith("spellcasting.slots."):
            slot_validation = _validate_spell_slot(cursor, key, delta)
            if slot_validation:
                return {
                    "success": False,
                    "error": slot_validation["error"],
                    "key": key,
                    "delta": delta,
                    "available_slots": slot_validation["available_slots"],
                    "hint": slot_validation["hint"],
                }

        if '.' in key:
            root_key = key.split('.')[0]
            cursor.execute("SELECT value FROM player WHERE key = ?", (root_key,))
            row = cursor.fetchone()

            auto_init_root = False
            if not row:
                if root_key == "consumables":
                    data = {}
                    auto_init_root = True
                else:
                    cursor.execute("SELECT key FROM player")
                    available = [r[0] for r in cursor.fetchall()]
                    return {"success": False, "error": f"Root key '{root_key}' not found.", "available_keys": available, "key": key}
            else:
                data = json.loads(row[0])

            path_in_obj = key[len(root_key)+1:]
            current_val = get_nested_value(data, path_in_obj)

            if current_val is None:
                if key.startswith("spellcasting.slots."):
                    current_val = 0
                elif key.startswith("consumables."):
                    current_val = 0
                else:
                    return {"success": False, "error": f"Key '{key}' not found in database.", "available_nested_keys": list(data.keys()), "key": key}

            current_val = int(current_val)
            new_val = current_val + delta
            set_nested_value(data, path_in_obj, new_val)

            if key.startswith("consumables.") and new_val <= 0:
                consumable_name = path_in_obj
                if new_val < 0:
                    data[consumable_name] = 0
                    cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
                    state.DB_CONNECTION.commit()
                    return {
                        "success": True,
                        "key": key,
                        "old_value": current_val,
                        "new_value": 0,
                        "delta": delta,
                        "clamped": True,
                        "message": f"Consumable '{consumable_name}' cannot go below 0. Set to 0.",
                        "narrative_format": f"Consumable '{consumable_name}' set to 0 (cannot go below 0).",
                    }
                del data[consumable_name]
                cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
                state.DB_CONNECTION.commit()
                return {
                    "success": True,
                    "key": key,
                    "old_value": current_val,
                    "new_value": 0,
                    "delta": delta,
                    "item_depleted": True,
                    "depleted_item": consumable_name,
                    "message": f"DEPLETED — {consumable_name} has been used up and removed from consumables.",
                    "narrative_format": f"DEPLETED — {consumable_name} has been used up and removed from consumables.",
                }

            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (root_key, json.dumps(data)))
        else:
            cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("SELECT key FROM player")
                available = [r[0] for r in cursor.fetchall()]
                return {"success": False, "error": f"Key '{key}' not found in database.", "available_keys": available, "key": key}

            current_val = int(row[0])
            new_val = current_val + delta
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, str(new_val)))

        state.DB_CONNECTION.commit()
        result = {
            "success": True,
            "key": key,
            "old_value": current_val,
            "new_value": new_val,
            "delta": delta,
        }
        if key.startswith("spellcasting.slots."):
            cursor.execute("SELECT value FROM player WHERE key = ?", ("spellcasting",))
            sc_row = cursor.fetchone()
            if sc_row:
                sc_data = json.loads(sc_row[0])
                result["remaining_slots"] = {f"lv{k}": v for k, v in sc_data.get("slots", {}).items()}
        if key == "xp":
            cursor.execute("SELECT value FROM player WHERE key = ?", ("level",))
            level_row = cursor.fetchone()
            if level_row:
                current_level = int(level_row[0])
                new_level = get_level_for_xp(new_val)
                if new_level > current_level:
                    cursor.execute("SELECT key, value FROM player")
                    all_rows = cursor.fetchall()
                    player_data = {row[0]: row[1] for row in all_rows}
                    character_class = player_data.get('character_class', '')
                    changes, summary = apply_level_up(character_class, current_level, new_level, player_data)
                    for db_key, db_value in changes.items():
                        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (db_key, db_value))
                    state.DB_CONNECTION.commit()
                    _recompute_armor_class(cursor)
                    state.DB_CONNECTION.commit()

                    total_hp = int(changes.get('total_hit_points', _db_val(cursor, 'total_hit_points', 0)))
                    current_hp = int(changes.get('current_hit_points', _db_val(cursor, 'current_hit_points', 0)))

                    result["level_up"] = True
                    result["old_level"] = current_level
                    result["new_level"] = new_level
                    result["level_up_changes"] = summary
                    result["level_up_summary"] = f"LEVEL UP! Level {current_level} → {new_level}. " + "; ".join(summary)
                    result["hp_status"] = _format_hp_status(current_hp, total_hp)
                    result["message"] = (
                        f"LEVEL UP! Level {current_level} → {new_level}. "
                        f"Changes: {', '.join(summary)}. "
                        f"You MUST still apply manually: class features, cantrips/spells, "
                        f"ability score improvements (at levels 4/8/12/16/19), and subclass features."
                    )
        result["narrative_format"] = (result.get("level_up_summary")
                                      or f"{key}: {current_val} → {new_val} ({delta:+d})")
        return result
    except Exception as e:
        return {"success": False, "error": f"Error modifying numeric value: {str(e)}", "key": key}


def _end_transient_effects(cursor) -> dict:
    """End the running-spell state on the sheet, keeping the GM's lasting marks.

    Reverts every buff delta and clears `_active_buff_data` and `concentration`; drops the active
    effects that are running-spell state (they have buff data) or a disguise (they carry an
    `appearance`); clears the transient conditions, keeping `_LASTING_CONDITIONS`. Returns what
    changed so the caller can tell the Game Master."""
    buff_data = _db_val(cursor, "_active_buff_data", {}) or {}
    if isinstance(buff_data, str):
        buff_data = json.loads(buff_data)
    if not isinstance(buff_data, dict):
        buff_data = {}
    effects = _db_val(cursor, "active_effects", []) or []
    if isinstance(effects, str):
        effects = json.loads(effects)

    running = {str(k).strip().lower() for k in buff_data}
    cleared_effects: list[str] = []
    kept_effects: list = []
    for entry in effects:
        name = _effect_name(entry)
        appearance = bool(isinstance(entry, dict) and entry.get("appearance"))
        if name.lower() in running or appearance:
            if name:
                cleared_effects.append(name)
            continue
        kept_effects.append(entry)

    # Revert the numeric deltas before dropping the buff data.
    for _spell, entries in buff_data.items():
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("delta") is not None:
                apply_numeric_change(key=entry["field"], delta=-entry["delta"])

    conditions = _db_val(cursor, "conditions", []) or []
    if isinstance(conditions, str):
        conditions = json.loads(conditions)
    kept_conditions = [c for c in conditions if str(c).strip().lower() in _LASTING_CONDITIONS]
    cleared_conditions = [c for c in conditions
                          if str(c).strip().lower() not in _LASTING_CONDITIONS]

    _db_set(cursor, "active_effects", kept_effects)
    _db_set(cursor, "_active_buff_data", {})
    _db_set(cursor, "concentration", {})
    _db_set(cursor, "conditions", sorted(kept_conditions))
    state.DB_CONNECTION.commit()
    return {
        "effects_cleared": cleared_effects,
        "effects_kept": [n for n in (_effect_name(e) for e in kept_effects) if n],
        "conditions_cleared": cleared_conditions,
        "conditions_kept": kept_conditions,
    }


def _apply_combat_condition(cursor, name, condition):
    """Add an SRD condition to a registered combatant (or the player's sheet).

    Respects a registry NPC's `condition_immunities`. Returns `(status, reason)` with
    status one of 'added' / 'present' / 'immune' / 'not_registered'.
    """
    cond = str(condition or "").strip().lower()
    if not cond or not name:
        return "not_registered", None
    entry = state.registry_get(name)
    if entry is None:
        if cursor is not None and _is_player_name(cursor, name):
            current = _player_conditions(cursor)
            if cond in current:
                return "present", None
            if cond in (_player_defenses(cursor).get("condition_immunities") or []):
                return "immune", f"immune to {cond}"
            _db_set(cursor, "conditions", sorted(current | {cond}))
            state.DB_CONNECTION.commit()
            if cond in _INCAPACITATING_CONDITIONS:
                _end_concentration(cursor, reason=cond)
            return "added", None
        return "not_registered", None
    if entry.get("is_player"):
        if cursor is None:
            return "not_registered", None
        current = _player_conditions(cursor)
        if cond in current:
            return "present", None
        if cond in (_player_defenses(cursor).get("condition_immunities") or []):
            return "immune", f"immune to {cond}"
        current = current | {cond}
        _db_set(cursor, "conditions", sorted(current))
        entry["conditions"] = sorted(current)
        state.DB_CONNECTION.commit()
        if cond in _INCAPACITATING_CONDITIONS:
            _end_concentration(cursor, reason=cond)
        return "added", None
    immunities = {str(c).strip().lower() for c in (entry.get("condition_immunities") or [])}
    if cond in immunities:
        return "immune", None
    current = {str(c).strip().lower() for c in (entry.get("conditions") or [])}
    if cond in current:
        return "present", None
    entry["conditions"] = sorted(current | {cond})
    return "added", None
