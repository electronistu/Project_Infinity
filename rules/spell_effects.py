"""What a resolved spell leaves behind: buffs, temporary HP and spell attribution.

The post-hit half of spell resolution: active buffs and their charges, temporary hit points,
concentration attribution, the XP a kill awards, and the projectile-spell damage loop.
"""

import json
import random
import re

from . import state
from .combat import (
    _attach_roster, _finish_combat_if_over, _flush_recurring_into, _register_recurring,
    _registry_ac, _registry_cr, _registry_hp, _registry_kill, _registry_max_hp, _registry_update_hp
)
from .conditions import _apply_player_damage_modifiers, _mark_condition_ends_on_damage
from .consts import CR_XP_TABLE
from .db import _db_set, _db_val, _effective_stats
from .dice import _combine_dice, _parse_and_roll_dice, _parse_higher_levels, _roll_d20
from .sheet import _target_suffix
from .vitals import (
    _apply_combat_condition, _apply_hp_change, _set_concentration, apply_numeric_change
)


def _ordinal(n: int) -> str:
    if 11 <= n <= 13:
        return f"{n}th"
    if n % 10 == 1:
        return f"{n}st"
    if n % 10 == 2:
        return f"{n}nd"
    if n % 10 == 3:
        return f"{n}rd"
    return f"{n}th"


def _resolve_hp_temporary(value, cursor):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        dice_match = re.match(r'^(\d+)d(\d+)(?:\+(\d+))?$', value)
        if dice_match:
            num = int(dice_match.group(1))
            size = int(dice_match.group(2))
            flat = int(dice_match.group(3)) if dice_match.group(3) else 0
            rolls = [random.randint(1, size) for _ in range(num)]
            return sum(rolls) + flat
    return None


def _apply_thp(cursor, spell_name, amount):
    old_thp = int(_db_val(cursor, "temporary_hit_points", 0))
    _db_set(cursor, "temporary_hit_points", str(amount))
    state.DB_CONNECTION.commit()

    buff_data_raw = _db_val(cursor, "_active_buff_data", {})
    if isinstance(buff_data_raw, str):
        buff_data_raw = json.loads(buff_data_raw)
    if spell_name not in buff_data_raw:
        buff_data_raw[spell_name] = []
    buff_data_raw[spell_name].append({"field": "temporary_hit_points", "delta": amount})
    _db_set(cursor, "_active_buff_data", buff_data_raw)

    effects_list = _db_val(cursor, "active_effects", [])
    if isinstance(effects_list, str):
        effects_list = json.loads(effects_list)
    if spell_name not in effects_list:
        effects_list.append(spell_name)
    _db_set(cursor, "active_effects", effects_list)

    state.DB_CONNECTION.commit()
    return {"field": "temporary_hit_points", "new": amount, "old": old_thp}


def _apply_active_buff(cursor, spell_name, field, delta):
    buff_data_raw = _db_val(cursor, "_active_buff_data", {})
    if isinstance(buff_data_raw, str):
        buff_data_raw = json.loads(buff_data_raw)

    # Reject a re-application of the SAME field (the recast guard is at the `resolve_magic`
    # level); a multi-field buff like Haste (speed + armour_class) must be able to add each.
    existing = buff_data_raw.get(spell_name) or []
    if any(isinstance(e, dict) and e.get("field") == field for e in existing):
        return {
            "error": "already_active",
            "spell_name": spell_name,
            "hint": (
                f"{spell_name}'s {field} is already active. Remove it first via "
                f"update_player_list(key='active_effects', item='{spell_name}', action='remove') "
                "before recasting."
            )
        }

    old_val = int(_db_val(cursor, field, 0))
    num_result = apply_numeric_change(key=field, delta=delta)

    if field not in buff_data_raw.get(spell_name, []):
        if spell_name not in buff_data_raw:
            buff_data_raw[spell_name] = []
        buff_data_raw[spell_name].append({"field": field, "delta": delta})

    _db_set(cursor, "_active_buff_data", buff_data_raw)

    effects_list = _db_val(cursor, "active_effects", [])
    if isinstance(effects_list, str):
        effects_list = json.loads(effects_list)
    if spell_name not in effects_list:
        effects_list.append(spell_name)
    _db_set(cursor, "active_effects", effects_list)

    state.DB_CONNECTION.commit()

    return {
        "field": field,
        "type": "delta",
        "delta": delta,
        "old": old_val,
        "new": num_result.get("new_value", old_val + delta),
    }


def _store_active_effect(cursor, spell_name, kind, field, value):
    """Record a NON-numeric active spell effect (resistance, dice, condition immunity).

    Typed entries live alongside the numeric deltas in `_active_buff_data` and are consumed by
    `equipment.defense_state` and the dice helpers. Duplicate (kind, field, value) entries for the
    same spell are skipped.
    """
    buff_data_raw = _db_val(cursor, "_active_buff_data", {}) or {}
    if isinstance(buff_data_raw, str):
        buff_data_raw = json.loads(buff_data_raw)
    entry = {"kind": kind, "field": field, "value": value}
    entries = buff_data_raw.get(spell_name)
    if not isinstance(entries, list):
        entries = []
    if entry not in entries:
        entries = [e for e in entries if isinstance(e, dict)] + [entry]
    buff_data_raw[spell_name] = entries
    _db_set(cursor, "_active_buff_data", buff_data_raw)
    effects_list = _db_val(cursor, "active_effects", []) or []
    if isinstance(effects_list, str):
        effects_list = json.loads(effects_list)
    if spell_name not in effects_list:
        effects_list.append(spell_name)
    _db_set(cursor, "active_effects", effects_list)
    state.DB_CONNECTION.commit()
    return {"field": field, "type": kind, "value": value}


def _consume_buff_charge(cursor, spell, field):
    """Spend one charge of a one-shot active dice buff (Branding Smite's next hit)."""
    if cursor is None or not spell:
        return
    raw = _db_val(cursor, "_active_buff_data", {}) or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
    entries = raw.get(spell)
    if not isinstance(entries, list):
        return
    keep, changed = [], False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("field") != field:
            keep.append(entry)
            continue
        if entry.get("charges") is not None:
            entry["charges"] = int(entry["charges"]) - 1
            if entry["charges"] <= 0:
                changed = True
                continue
        elif isinstance(entry.get("value"), dict) and entry["value"].get("charges") is not None:
            entry["value"]["charges"] = int(entry["value"]["charges"]) - 1
            if entry["value"]["charges"] <= 0:
                changed = True
                continue
        keep.append(entry)
    if changed:
        if keep:
            raw[spell] = keep
        else:
            raw.pop(spell, None)
        _db_set(cursor, "_active_buff_data", raw)
        state.DB_CONNECTION.commit()


def _scale_buff_dice(buffs, higher_levels, levels_above):
    """Grow a `damage_rolls` buff by the upcast dice (Branding Smite +1d6 per level above 2nd)."""
    parsed = _parse_higher_levels(higher_levels or "")
    if not isinstance(buffs, dict) or not parsed or levels_above <= 0 or parsed[0] != "dice":
        return
    _, num, size, _flat = parsed
    extra = f"{num * levels_above}d{size}"
    for field, value in list(buffs.items()):
        if field != "damage_rolls":
            continue
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            value["value"] = _combine_dice(value["value"].lstrip("+"), extra)
        elif isinstance(value, str) and value.startswith("+"):
            buffs[field] = _combine_dice(value.lstrip("+"), extra)


def _apply_special_buff(cursor, spell_name, field, value):
    """Turn a non-numeric `buffs` field into a typed active effect the consumers read.

    Covers the fields the generic delta/dice/defense paths do not: scoped save advantage
    (Haste, Beacon of Hope), one-shot save dice (Resistance), a flat skill bonus (Pass Without
    Trace), psychic immunity (Mind Blank), Foresight's four advantages, and the non-`+` speed
    forms (`doubled`, `fly 60`). Returns the `applied` dict, or None when the field is a plain
    note for the GM.
    """
    def store(kind, fieldname, val):
        return _store_active_effect(cursor, spell_name, kind, fieldname, val)

    if field == "psychic_immunity" and value:
        return store("immunity", "damage_immunity", "psychic")
    if field in ("dexterity_saves", "wisdom_saves", "death_saves") \
            and str(value).strip().lower() == "advantage":
        scope = {"dexterity_saves": "dex", "wisdom_saves": "wis", "death_saves": "death"}[field]
        return store("save_advantage", field, scope)
    if field == "saving_throws_once":
        return store("dice", "saving_throws", {"value": str(value), "charges": 1})
    if field == "stealth_checks":
        return store("skill_bonus", "stealth", str(value))
    if field == "foresight":
        for kind in ("save_advantage", "check_advantage", "attack_advantage",
                     "attackers_disadvantage"):
            store(kind, "foresight", "all")
        return {"field": field, "type": "foresight", "value": value}
    if field == "speed" and isinstance(value, str) and not value.startswith("+"):
        low = value.strip().lower()
        if low == "doubled":
            current = int(_db_val(cursor, "speed", 30) or 30)
            return _apply_active_buff(cursor, spell_name, "speed", current)
        grant = re.match(r"^(fly|swim|climb)\s+(\d+)", low)
        if grant:
            return store("speed_grant", grant.group(1), int(grant.group(2)))
    return None


def _ensure_spell_attribution(result, narrative_parts) -> None:
    """Guarantee the Mechanics block says who cast what: `{actor} casts {spell} -> target`.

    Most paths already lead with `{actor} {spell_name}` (Attack / Damage / Healing / HP Pool /
    projectiles); a condition-only or utility spell, an NPC heal, a post-hit save, or a
    recurring effect does not. Insert the line only when no line already attributes the spell,
    so nothing is duplicated."""
    actor = str(result.get("actor") or "").strip()
    spell = str(result.get("spell_name") or "").strip()
    if not actor or not spell:
        return
    heads = (f"{actor} {spell}", f"{actor} casts {spell}")
    if any(str(line).startswith(heads) for line in narrative_parts):
        return
    names = [str(n).strip() for n in (result.get("condition_targets") or [])
             if str(n or "").strip()]
    if not names:
        names = [str(n).strip() for n in (result.get("targets_named_only") or [])
                 if str(n or "").strip()]
    if not names and isinstance(result.get("targets"), list):
        names = [str(t.get("name")).strip() for t in result["targets"]
                 if isinstance(t, dict) and str(t.get("name") or "").strip()]
    target = str(result.get("target_name") or "").strip()
    if not target or target == "{player_name}":
        shown = ", ".join(names[:3])
        target = shown + (f" +{len(names) - 3}" if len(names) > 3 else "")
    arrow = f" \u2192 {target}" if target else ""
    narrative_parts.insert(0, f"{actor} casts {spell}{arrow}")


def _finalize_spell_result(result, narrative_parts, sp_duration, sp_buffs, sp_requires_concentration, is_npc, is_npc_vs_npc=False):
    buffs_applied = {}
    active_names = None

    if sp_buffs and state.DB_CONNECTION is not None and not is_npc:
        cursor = state.DB_CONNECTION.cursor()
        stats_dict = _effective_stats(cursor)

        for field, value in sp_buffs.items():
            special = _apply_special_buff(cursor, result.get("spell_name", ""), field, value)
            if special is not None:
                buffs_applied[field] = special
                continue
            applied = None

            if isinstance(value, str) and value.startswith("+"):
                try:
                    delta = int(value[1:].strip())
                    if field == "armor_class":
                        applied = _apply_active_buff(cursor, result.get("spell_name", ""), field, delta)
                    elif field == "speed":
                        applied = _apply_active_buff(cursor, result.get("spell_name", ""), field, delta)
                except ValueError:
                    pass

            elif field == "hp_temporary":
                thp_amount = _resolve_hp_temporary(value, cursor)
                if thp_amount is not None:
                    applied = _apply_thp(cursor, result.get("spell_name", ""), thp_amount)
                    narrative_parts.append(f"{result.get('actor', '?')} {result.get('spell_name', '?')} Temporary HP: {thp_amount}")

            elif isinstance(value, str) and "+" in value and any(mod in value.upper() for mod in ["DEX", "STR", "CON", "INT", "WIS", "CHA"]):
                if field == "armor_class":
                    for stat_name, key in [("DEX", "dex"), ("STR", "str"), ("CON", "con"),
                                            ("INT", "int"), ("WIS", "wis"), ("CHA", "cha")]:
                        if stat_name in value.upper():
                            parts = value.upper().split("+")
                            base = int(parts[0].strip())
                            stat_mod = (int(stats_dict.get(key, 10)) - 10) // 2
                            target_ac = base + stat_mod
                            current_ac = int(_db_val(cursor, "armor_class", 10))
                            delta = target_ac - current_ac
                            if delta != 0:
                                applied = _apply_active_buff(
                                    cursor, result.get("spell_name", ""), field, delta)

            if applied is None and field not in ("armor_class", "hp_temporary") \
                    and "error" not in buffs_applied:
                kind = "note"
                if field in ("damage_resistance", "resistance"):
                    kind = "resistance"
                elif field in ("damage_immunity", "immunity"):
                    kind = "immunity"
                elif field in ("damage_vulnerability", "vulnerability"):
                    kind = "vulnerability"
                elif field == "condition_immunity":
                    kind = "condition_immunity"
                elif field in ("saving_throws", "ability_checks", "attack_rolls", "damage_rolls"):
                    kind = "dice"
                applied = _store_active_effect(
                    cursor, result.get("spell_name", ""), kind, field, value)

            if applied:
                if "error" in applied:
                    buffs_applied["error"] = applied
                else:
                    buffs_applied[field] = applied
            elif field not in buffs_applied and "error" not in buffs_applied:
                pass

        buff_data_raw = _db_val(cursor, "_active_buff_data", {})
        if isinstance(buff_data_raw, str):
            buff_data_raw = json.loads(buff_data_raw)
        active_names = list(buff_data_raw.keys())

        if active_names:
            result["active_effects"] = active_names

    if sp_duration and sp_duration != "Instantaneous":
        result["duration"] = sp_duration
        if sp_buffs:
            result["buffs"] = sp_buffs
        # SRD 5.1: track concentration for a player so damage can force a CON save.
        if sp_requires_concentration and not is_npc and state.DB_CONNECTION is not None:
            conc_spell = result.get("spell_name", "")
            replaced = _set_concentration(state.DB_CONNECTION.cursor(), conc_spell)
            if replaced:
                result["concentration_replaced"] = replaced
            if conc_spell:
                result["concentration"] = conc_spell
        conc_tag = " (Requires Concentration)" if sp_requires_concentration else ""

        if buffs_applied and "error" not in buffs_applied:
            applied_parts = []
            for field, info in buffs_applied.items():
                if isinstance(info, dict) and "old" in info:
                    applied_parts.append(f"{field} ({info['old']} -> {info['new']})")
            applied_str = ", ".join(applied_parts) if applied_parts else ""
            revert_str = ""
            if active_names:
                revert_str = (
                    "Remove via: update_player_list(key='active_effects', item='"
                    + "', item='".join(active_names)
                    + "', action='remove') when the duration expires. "
                )
            result["duration_reminder"] = (
                f"[GM REMINDER: Duration = {sp_duration}{conc_tag}. "
                f"Applied: {applied_str}. "
                f"{revert_str}"
                f"The GM must inform the player of both the buff application "
                f"and its expiration in narration.]"
            )
        elif buffs_applied and "error" in buffs_applied:
            err = buffs_applied["error"]
            result["duration_reminder"] = (
                f"[GM REMINDER: {err.get('hint', err.get('error', ''))}]"
            )
        else:
            if is_npc and not is_npc_vs_npc:
                result["duration_reminder"] = (
                    f"[GM REMINDER: Duration = {sp_duration}{conc_tag}. "
                    "The GM must manually remove this effect from the player sheet via "
                    "tool calls and inform the player when the duration expires in narration.]"
                )

    if buffs_applied:
        result["buffs_applied"] = buffs_applied

    # A condition a spell applied is written onto the combatant here, so the GM never has to
    # re-declare it with update_combatant (update_combatant stays available to remove it later).
    cond = result.get("condition")
    cond_targets = result.get("condition_targets")
    if cond and isinstance(cond_targets, list) and cond_targets:
        ccur = state.DB_CONNECTION.cursor() if state.DB_CONNECTION is not None else None
        applied, refused = [], []
        for tname in cond_targets:
            status, _reason = _apply_combat_condition(ccur, tname, cond)
            if status in ("added", "present") and result.get("_condition_ends_on_damage"):
                _mark_condition_ends_on_damage(tname, cond)
            if status == "added":
                applied.append(tname)
            elif status in ("immune", "not_registered"):
                refused.append(tname)
        if applied:
            result["conditions_applied"] = applied
            dur = result.get("condition_duration") or "until ended"
            who = ", ".join(applied)
            result["condition_reminder"] = (
                f"[GM: {who} — {cond} ({dur}). Remove with "
                f"update_combatant(name=..., conditions_remove=['{cond}']) when it ends.]")
        if refused:
            result["conditions_refused"] = refused

    # A repeating-save effect is registered for every target that failed the initial save.
    rep_block = result.get("_repeat_save")
    if rep_block and isinstance(cond_targets, list) and cond_targets:
        registered = []
        for tname in cond_targets:
            if _register_recurring(tname, result.get("spell_name", ""), rep_block,
                                   result.get("_repeat_dc"), result.get("_repeat_caster")):
                registered.append(tname)
        if registered:
            result["recurring_registered"] = registered

    # The acting combatant's turn starts here: resolve any pending repeating saves.
    _flush_recurring_into(result, narrative_parts)

    _ensure_spell_attribution(result, narrative_parts)
    result["narrative_format"] = "\n".join(narrative_parts)
    _attach_roster(result)
    _finish_combat_if_over()
    return result


def _resolve_projectile_spell(
    *, actor, spell_name, spell, count, attack_type, damage_dice, damage_modifier,
    damage_type, targets, target_name, target_current_hp, target_ac, challenge_rating,
    is_npc_attack, is_npc_vs_npc, cursor, spell_attack_modifier, advantage, force_crit,
    result, narrative_parts, disadvantage=False,
):
    """Resolve a multi-projectile spell (Magic Missile / Scorching Ray / Eldritch Blast).

    Each projectile is rolled separately. `attack_type == "automatic"` projectiles
    never roll to hit and all strike simultaneously; attack-roll projectiles each
    make their own attack roll. `targets=[{name, darts}]` splits the projectiles
    (darts must total `count`); without `darts` the spell hits a single target.

    Returns the finished result dict, or an error dict (`success: False`).
    """
    # ── 1. Build the (target, darts) distribution ──
    entries = []
    if targets:
        for t in targets:
            name = t.get("name", "Unknown")
            chp = t.get("current_hp")
            if chp is None:
                chp = _registry_hp(name)
            ac = t.get("ac")
            if ac is None:
                ac = _registry_ac(name)
            if ac is None:
                ac = target_ac
            cr = t.get("challenge_rating")
            if cr is None:
                cr = _registry_cr(name)
            entries.append({
                "name": name, "darts": t.get("darts"),
                "current_hp": chp if chp is not None else 0,
                "ac": ac, "cr": cr, "is_player": bool(t.get("is_player", False)),
            })
        if all(e["darts"] is None for e in entries):
            if len(entries) == 1:
                entries[0]["darts"] = count
            else:
                return {"success": False, "spell_name": spell_name,
                        "error": f"{spell_name} has {count} projectiles — pass 'darts' per target to split them.",
                        "expected_projectiles": count}
        elif any(e["darts"] is None for e in entries):
            return {"success": False, "spell_name": spell_name,
                    "error": "'darts' must be provided on every target, or on none.",
                    "expected_projectiles": count}
        else:
            assigned = sum(int(e["darts"]) for e in entries)
            if assigned != count:
                return {"success": False, "spell_name": spell_name,
                        "error": f"Projectiles assigned ({assigned}) must equal the spell's count ({count}).",
                        "expected_projectiles": count}
    else:
        is_player_target = bool(is_npc_attack)
        name = target_name or ("Player" if is_player_target else "")
        if is_player_target:
            chp = target_current_hp
            if chp is None and cursor:
                chp = int(_db_val(cursor, "current_hit_points", 0))
        else:
            chp = target_current_hp if target_current_hp is not None else _registry_hp(name)
        cr = challenge_rating if challenge_rating is not None else _registry_cr(name)
        entries = [{
            "name": name, "darts": count,
            "current_hp": chp if chp is not None else 0,
            "ac": target_ac if target_ac is not None else _registry_ac(name),
            "cr": cr, "is_player": is_player_target,
        }]

    # ── 2. Roll every projectile before applying anything (simultaneous) ──
    total_damage = 0
    per_dart = []
    per_target = {}
    for e in entries:
        bucket = per_target.setdefault(e["name"], {"damage": 0, "darts": []})
        for _ in range(int(e["darts"])):
            detail = {"target": e["name"], "hit": True, "crit": False}
            if attack_type == "attack_roll":
                d20, dart_rolls, dart_mode, _dart_cancelled = _roll_d20(
                    advantage=advantage, disadvantage=disadvantage)
                if dart_rolls:
                    detail[f"{dart_mode}_rolls"] = dart_rolls
                total_attack = d20 + spell_attack_modifier
                ac = e["ac"] if e["ac"] is not None else 0
                nat20 = d20 == 20
                nat1 = d20 == 1
                detail.update({"attack_roll": d20, "total_attack": total_attack, "ac": ac})
                detail["crit"] = nat20 or (force_crit and not nat1)
                detail["hit"] = nat20 or (not nat1 and total_attack >= ac)
                if not detail["hit"]:
                    per_dart.append(detail)
                    continue
            die, rolls, raw = _parse_and_roll_dice(damage_dice)
            if die is None:
                return {"success": False, "spell_name": spell_name,
                        "error": f"Invalid damage_dice notation: '{damage_dice}'."}
            dmg = raw + damage_modifier
            detail["rolls"] = rolls
            if detail.get("crit"):
                crit_rolls = [random.randint(1, die) for _ in range(len(rolls))]
                dmg += sum(crit_rolls)
                detail["crit_rolls"] = crit_rolls
            detail["damage"] = dmg
            total_damage += dmg
            bucket["damage"] += dmg
            bucket["darts"].append(dmg)
            per_dart.append(detail)

    # ── 3. Apply damage simultaneously, then resolve kills/XP ──
    total_xp = 0
    killed_count = 0
    target_results = []
    hp_lines = []
    for e in entries:
        name = e["name"]
        dealt = per_target[name]["damage"]
        if e["is_player"]:
            tr = {"name": name, "damage": dealt, "darts": per_target[name]["darts"]}
            if dealt > 0 and cursor:
                dealt, dmg_note = _apply_player_damage_modifiers(cursor, dealt, damage_type)
                if dmg_note:
                    tr["damage_modified"] = dmg_note
                    tr["damage"] = dealt
                hp_result = _apply_hp_change(cursor, -dealt)
                tr["hp_change"] = hp_result
            target_results.append(tr)
            continue
        chp = e["current_hp"]
        remaining = chp - dealt
        killed = chp > 0 and remaining <= 0
        if name:
            _registry_update_hp(name, max(remaining, 0))
        tr = {"name": name, "damage": dealt, "darts": per_target[name]["darts"],
              "remaining_hp": max(remaining, 0), "killed": killed}
        max_hp = _registry_max_hp(name) if name else 0
        display_max = f"/{max_hp}" if max_hp else ""
        if killed:
            killed_count += 1
            if name:
                _registry_kill(name)
            hp_lines.append(f"{name} HP: 0{display_max} (KILLED)")
            if e["cr"] is not None:
                xp = CR_XP_TABLE.get(e["cr"], 0)
                if xp > 0:
                    total_xp += xp
                    tr["xp_awarded"] = xp
        target_results.append(tr)

    # ── 4. Breakdown narrative ──
    label = "rays" if attack_type == "attack_roll" else "darts"
    if attack_type == "attack_roll":
        bits = []
        for i, d in enumerate(per_dart, start=1):
            if d["hit"]:
                tag = " [CRIT]" if d.get("crit") else ""
                bits.append(f"ray {i}: {d['total_attack']} vs AC {d['ac']} (hit){tag} {d['damage']} {damage_type}".strip())
            else:
                bits.append(f"ray {i}: {d['total_attack']} vs AC {d['ac']} (miss)")
        narrative_parts.append(f"{actor} {spell_name}{_target_suffix(cursor, target_name, bool(is_npc_attack))}: {total_damage} — {count} {label} — " + " | ".join(bits))
    elif len(entries) == 1:
        vals = per_target[entries[0]["name"]]["darts"]
        narrative_parts.append(f"{actor} {spell_name}{_target_suffix(cursor, target_name, bool(is_npc_attack))}: {total_damage} — {count} darts: " + ", ".join(str(v) for v in vals))
    else:
        parts = []
        for e in entries:
            vals = per_target[e["name"]]["darts"]
            plural = "dart" if len(vals) == 1 else "darts"
            parts.append(f"{e['name']}: {sum(vals)} ({len(vals)} {plural}: " + ", ".join(str(v) for v in vals) + ")")
        narrative_parts.append(f"{actor} {spell_name}: {total_damage} — " + ", ".join(parts))

    narrative_parts.extend(hp_lines)

    if total_xp > 0 and not is_npc_attack and not is_npc_vs_npc and cursor:
        xp_result = apply_numeric_change(key="xp", delta=total_xp)
        result["xp_awarded"] = total_xp
        result["xp_result"] = xp_result
        narrative_parts.append(f"Total XP Awarded: {total_xp}")
        if xp_result.get("level_up"):
            narrative_parts.append(xp_result["level_up_summary"])

    result.update({
        "projectiles": count,
        "per_projectile": per_dart,
        "damage_total": total_damage,
        "damage_type": damage_type,
        "targets": target_results,
        "killed_count": killed_count,
    })
    return result
