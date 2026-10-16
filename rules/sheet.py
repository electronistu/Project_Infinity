"""Derived sheet numbers: saves, checks, passives, carrying, exhaustion and labels.

Every number the engine derives from the player's stored sheet -- the equipped set, the effective
ability scores, the exhaustion level -- is recomputed here, so the sheet and the engine agree.
"""

import json

import carrying
import equipment
import skills

from level_up import ABILITY_TO_STAT

from . import state
from .consts import EXHAUSTION_MAX
from .db import (
    _ability_mod, _carry_get, _db_val, _effective_prof_bonus, _effective_stats, _equipment_block,
    _exhaustion_level, _item_effect_state
)
from .reputation import _era_reputation_scope


def _save_proficiencies(cursor) -> set:
    """Lowercased 3-letter ability keys the player is proficient at saving with."""
    return {str(s).strip().lower()[:3] for s in (_db_val(cursor, "saves", []) or [])}


def _derived_save(cursor, ability, situational: int = 0, against=None, damage=None,
                  condition=None) -> dict:
    """The player's full saving-throw modifier (SRD 5.1), engine-computed.

    ability modifier (effective) + proficiency bonus (if proficient with the save) + worn /
    attuned item save bonuses + a caller-supplied situational bonus or penalty. Scoped item
    advantage (`save_advantage` with `vs_spells`/`vs_damage:`/`vs_condition:`) is returned too.
    """
    key = str(ability or "").strip().lower()[:3]
    state = _item_effect_state(cursor)
    scores = state.get("scores") or {}
    ability_mod = _ability_mod(scores.get(key, scores.get("str", 10)))
    prof_bonus = int(_db_val(cursor, "proficiency_bonus", 2) or 2) \
        + int(state.get("proficiency_bonus_mod") or 0)
    proficient = key in _save_proficiencies(cursor)
    item_bonus = int(state.get("save_bonus") or 0)
    advantage, advantage_sources = equipment.save_advantage_for(
        _carry_get(cursor), against=against, damage=damage, condition=condition)
    active_adv, active_src = equipment.active_advantage_for(
        _carry_get(cursor), "save_advantage", key)
    if active_adv:
        advantage = True
        advantage_sources = [*advantage_sources, *active_src]
    return {
        "ability": key,
        "ability_modifier": ability_mod,
        "proficiency_bonus": prof_bonus if proficient else 0,
        "proficient": proficient,
        "item_bonus": item_bonus,
        "item_sources": list(state.get("save_bonus_sources") or []),
        "situational": int(situational or 0),
        "advantage": advantage,
        "advantage_sources": advantage_sources,
        "modifier": ability_mod + (prof_bonus if proficient else 0) + item_bonus
                    + int(situational or 0),
    }


def _list_names(value) -> list[str]:
    """Names from a player list whose entries may be strings or {name, ...} dicts."""
    out = []
    for entry in (value or []):
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("skill") or ""
        else:
            name = entry
        name = str(name or "").strip()
        if name:
            out.append(name)
    return out


def _player_skill_names(cursor) -> set:
    """Normalized names of the player's proficient skills (SRD 5.1)."""
    return {skills.normalize(n) for n in _list_names(_db_val(cursor, "skills", []))}


def _player_expertise(cursor) -> set:
    """Normalized names of the player's Expertise skills (double proficiency)."""
    return {skills.normalize(n) for n in _list_names(_db_val(cursor, "expertise", []))}


def _has_feature(cursor, needle) -> bool:
    """True when the player's feature list contains `needle` (case-insensitive substring)."""
    target = skills.normalize(needle)
    return any(target in skills.normalize(n) for n in _list_names(_db_val(cursor, "features", [])))


def _derived_check(cursor, check_name, ability=None, context=None, situational: int = 0) -> dict:
    """The player's full ability/skill check modifier (SRD 5.1), engine-computed.

    Ability modifier (effective) + proficiency bonus, doubled for Expertise or halved (rounded
    down) for Jack of All Trades on a check that is not already proficient + worn/attuned item
    `check_bonus`/`skill_bonus` + a caller-supplied situational bonus. `skill` is None for a plain
    ability check (or an unrecognised check name).
    """
    skill = skills.skill_name(check_name)
    ab = str(ability or "").strip().lower()[:3] or (skills.SKILL_ABILITIES.get(skill, "") if skill else "")
    scores = _effective_stats(cursor)
    ability_mod = _ability_mod(scores.get(ab, 10)) if ab else 0
    prof_bonus = _effective_prof_bonus(cursor)
    proficient = bool(skill) and skills.normalize(skill) in _player_skill_names(cursor)
    expertise = bool(skill) and skills.normalize(skill) in _player_expertise(cursor)
    joat = (not proficient) and _has_feature(cursor, "jack of all trades")
    if expertise:
        prof_part = 2 * prof_bonus
    elif proficient:
        prof_part = prof_bonus
    elif joat:
        prof_part = prof_bonus // 2
    else:
        prof_part = 0
    item_bonus, item_sources = equipment.check_bonus_for(_carry_get(cursor), check_name, context)
    active_bonus, active_sources = equipment.active_check_bonus_for(
        _carry_get(cursor), check_name, ab)
    check_adv, check_adv_sources = equipment.active_advantage_for(
        _carry_get(cursor), "check_advantage", skill or ab)
    item_bonus += active_bonus
    item_sources = [*item_sources, *active_sources]
    return {
        "skill": skill,
        "ability": ab,
        "ability_modifier": ability_mod,
        "proficiency_bonus": prof_part,
        "proficient": proficient,
        "expertise": expertise,
        "jack_of_all_trades": joat,
        "item_bonus": item_bonus,
        "item_sources": list(item_sources or []),
        "advantage": check_adv,
        "advantage_sources": list(check_adv_sources or []),
        "situational": int(situational or 0),
        "modifier": ability_mod + prof_part + item_bonus + int(situational or 0),
    }


def _passive_scores(cursor) -> dict:
    """SRD 5.1 passive scores (10 + the derived modifier) for the three passive skills."""
    return {skill.lower(): 10 + _derived_check(cursor, skill)["modifier"]
            for skill in ("Perception", "Investigation", "Insight")}


def _carry_block(cursor) -> dict:
    """Derived carrying state (never persisted: stripped before saving). Uses effective STR."""
    scores = _effective_stats(cursor)

    def get(key, default=None):
        if key == "stats":
            return scores
        return _db_val(cursor, key, default)

    state = carrying.carry_state(get)
    # Passive item speed (Boots of Striding and Springing set a 30 ft floor and ignore
    # encumbrance/heavy-armour reduction). Active speed items are spell-style buffs, not here.
    speed = equipment.speed_state(get)
    if speed["ignore_encumbrance"]:
        state["speed_penalty"] = 0
        state["speed"] = speed["walk"]
    else:
        state["speed"] = max(0, speed["walk"] - int(state.get("speed_penalty") or 0))
    state["speed_base"] = speed["base"]
    state["speeds"] = speed["modes"]
    # SRD 5.1 exhaustion: speed halved at level 2, 0 at level 5.
    exh = _exhaustion_effects(_exhaustion_level(cursor))
    if exh["speed_multiplier"] != 1.0:
        state["speed"] = int(state["speed"] * exh["speed_multiplier"])
        state["speeds"] = {k: int(v * exh["speed_multiplier"])
                           for k, v in (state.get("speeds") or {}).items()}
        state["exhaustion_speed"] = exh["level"]
    return state


def _encumbrance(cursor, is_player: bool, ability=None):
    """(disadvantage?, sources) from heavy encumbrance for a player actor.

    SRD 5.1 variant: heavily encumbered imposes disadvantage on attack rolls, on
    ability checks and on saving throws that use Strength, Dexterity or
    Constitution. Non-player actors are never affected.
    """
    if not is_player or state.DB_CONNECTION is None:
        return False, []
    return carrying.heavy_encumbrance(_carry_get(cursor), ability)


def _encumbrance_note(result: dict, sources: list, cancelled: bool = False) -> None:
    """Disclose where the disadvantage came from (and an eaten advantage)."""
    if sources:
        result["disadvantage_sources"] = list(sources)
    if cancelled:
        result["advantage_cancelled"] = True


def _armor_disadvantage(cursor, is_player: bool, ability=None):
    """(disadvantage?, sources) from wearing armour/shields the player is not proficient with.

    SRD 5.1: disadvantage on any STR or DEX ability check, saving throw or attack roll
    (spellcasting is refused separately). Non-player actors are never affected.
    """
    if not is_player or state.DB_CONNECTION is None:
        return False, []
    worn_state = _equipment_block(cursor)
    if worn_state.get("armor_proficient", True):
        return False, []
    if ability is not None:
        key = str(ability).strip().lower()[:3]
        if key not in ("str", "dex"):
            return False, []
    sources = worn_state.get("proficiency_sources") or ["not proficient with worn armour"]
    return True, sources


def _roll_disadvantage(cursor, is_player: bool, ability=None):
    """(disadvantage?, sources) merging every source: heavy encumbrance and non-proficient armour."""
    heavy, heavy_sources = _encumbrance(cursor, is_player, ability)
    armor, armor_sources = _armor_disadvantage(cursor, is_player, ability)
    sources = []
    for source in [*heavy_sources, *armor_sources]:
        if source not in sources:
            sources.append(source)
    return bool(heavy or armor), sources


def _is_player_actor(cursor, actor) -> bool:
    """True when `actor` refers to the player (the '{player_name}' placeholder or their name)."""
    if actor == "{player_name}":
        return True
    if state.DB_CONNECTION is None or cursor is None:
        return False
    name = _db_val(cursor, "name", None)
    return bool(name) and actor == name


def _player_name(cursor):
    return _db_val(cursor, "name", "Player")


def _target_label(cursor, target_name, is_player_target=False) -> str:
    """Display name of an action's target (SRD mechanics lines).

    A real name is used as-is; `{player_name}` or an empty name for a player target resolves to the
    sheet name; an empty non-player target yields "" so callers can omit the suffix.
    """
    name = str(target_name or "").strip()
    if name and name != "{player_name}":
        return name
    if is_player_target or name == "{player_name}":
        if cursor is not None:
            return str(_db_val(cursor, "name", "") or "").strip() or "the player"
        return "the player"
    return ""


def _target_suffix(cursor, target_name, is_player_target=False) -> str:
    """` → Name` for a mechanics line, or '' when the target is unknown."""
    label = _target_label(cursor, target_name, is_player_target)
    return f" \u2192 {label}" if label else ""


def _caster_spellcasting_mod(cursor) -> int:
    """The caster's spellcasting ability modifier from the player DB (0 if unknown)."""
    if cursor is None:
        return 0
    sc = _db_val(cursor, "spellcasting", {}) or {}
    if isinstance(sc, str):
        try:
            sc = json.loads(sc)
        except (json.JSONDecodeError, TypeError):
            sc = {}
    stat_key = ABILITY_TO_STAT.get(str(sc.get("ability", "")).lower())
    if not stat_key:
        return 0
    stats = _effective_stats(cursor)
    return _ability_mod(stats.get(stat_key, 10))


def _ac_note(before, after) -> str:
    """The ' — AC x → y' suffix for a tool narrative, only when the AC actually changed."""
    return f" — AC {before} → {after}" if before != after else ""


def _player_gm_view(cursor) -> dict:
    """The Game Master's view of the player row map: every stored row, the derived
    `_carrying`/`_equipment`/`_passive` blocks, and `reputation` reduced to the era the save is
    in. `dump_player_db` returns it whole; `read_player_field` slices it per field. The SAVE is
    never built from this view (see `dump_player_save_state`)."""
    cursor.execute("SELECT * FROM player")
    rows = cursor.fetchall()
    if not rows:
        return {}
    result = {}
    for key, value in rows:
        try:
            result[key] = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            result[key] = value
    result["_carrying"] = _carry_block(cursor)
    result["_equipment"] = _equipment_block(cursor)
    result["_passive"] = _passive_scores(cursor)
    # Reputation is stored era-scoped; the GM is only ever offered the era it is in.
    # The saved map keeps every era, so nothing is lost by leaving -- which is why the
    # SAVE must never be built from this view (see `dump_player_save_state`).
    rep = result.get("reputation")
    era = str(_db_val(cursor, "era", "") or "")
    if isinstance(rep, dict) and era:
        scoped = rep.get(era)
        # Never another age's standing -- but never nothing at all either: an era whose
        # scope has not been written to yet still shows its real polities, so the GM has
        # a shape to write against instead of inventing one.
        result["reputation"] = (scoped if isinstance(scoped, dict)
                                else _era_reputation_scope(era))
    return result


# ── exhaustion levels (SRD 5.1) ───────────────────────────────────────────

def _exhaustion_effects(level) -> dict:
    """The SRD 5.1 exhaustion table for a level 0-6."""
    try:
        level = max(0, min(EXHAUSTION_MAX, int(level or 0)))
    except (TypeError, ValueError):
        level = 0
    return {
        "level": level,
        "check_disadvantage": level >= 1,
        "speed_multiplier": 0.0 if level >= 5 else (0.5 if level >= 2 else 1.0),
        "attack_disadvantage": level >= 3,
        "save_disadvantage": level >= 3,
        "hp_multiplier": 0.5 if level >= 4 else 1.0,
        "dead": level >= 6,
    }


def _exhaustion_level_for(cursor, name, is_player=False) -> int:
    """Exhaustion level for a player or a registered NPC."""
    if is_player:
        return _exhaustion_level(cursor)
    entry = state.registry_get(name) or {}
    try:
        return max(0, min(EXHAUSTION_MAX, int(entry.get("exhaustion") or 0)))
    except (TypeError, ValueError):
        return 0
