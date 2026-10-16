"""The player database layer: the SQLite read/write primitives and the readers
that derive a character's numbers from the stored sheet.

All of these take a `cursor`; the connection itself is `rules.state.DB_CONNECTION`.
"""

import json
import sqlite3

import equipment

from level_up import ABILITY_TO_STAT

from . import state
from .consts import COMPANION_LIMIT, EXHAUSTION_MAX


def init_player_db(player_file_path: str) -> str:
    try:
        with open(player_file_path, 'r', encoding="utf-8") as f:
            data = json.load(f)

        state.DB_CONNECTION = sqlite3.connect(":memory:")
        cursor = state.DB_CONNECTION.cursor()

        cursor.execute("CREATE TABLE player (key TEXT PRIMARY KEY, value TEXT)")

        for key, value in data.items():
            if isinstance(value, (dict, list)):
                cursor.execute("INSERT INTO player (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            elif isinstance(value, str):
                cursor.execute("INSERT INTO player (key, value) VALUES (?, ?)", (key, value))
            else:
                cursor.execute("INSERT INTO player (key, value) VALUES (?, ?)", (key, json.dumps(value)))

        state.DB_CONNECTION.commit()

        # Companions are ONE party now (they travel with the Traveller): normalize a legacy
        # per-era `{era: {name: sheet}}` map to the flat `{name: sheet}` store at load, so the
        # next Save writes the flat shape and every reader sees it.
        if isinstance(data.get("companions"), dict):
            cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)",
                           ("companions", json.dumps(_flatten_companions(data.get("companions"),
                                                                        data.get("era")))))
            state.DB_CONNECTION.commit()

        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("active_effects", "[]"))
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("_active_buff_data", "{}"))
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("temporary_hit_points", "0"))
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", ("capacity_multiplier", "1"))
        state.DB_CONNECTION.commit()

        # A character file that records what is equipped gets its armour class from
        # that set (the Forge writes the first consistent pair). Payloads without an
        # `equipped` value keep whatever `armor_class` they carry.
        if isinstance(data.get("equipped"), dict):
            _recompute_armor_class(cursor)
            state.DB_CONNECTION.commit()

        return f"Database initialized with player data from {player_file_path}."
    except Exception as e:
        return f"Failed to initialize database: {str(e)}"


def _db_val(cursor, key, default=None):
    cursor.execute("SELECT value FROM player WHERE key = ?", (key,))
    row = cursor.fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return row[0]


# ── carrying capacity / encumbrance (SRD 5.1 variant) ──────────────────────

def _carry_get(cursor):
    """A `get(key, default)` reader over the player DB, as carrying.py expects."""
    return lambda key, default=None: _db_val(cursor, key, default)


def _item_effect_state(cursor) -> dict:
    """Aggregated item-effect channels for the player (scores, save/check/prof/spell bonuses).

    The single source of truth is `equipment.effect_state`, so the engine and the sheet agree.
    """
    if cursor is None:
        return {}
    return equipment.effect_state(_carry_get(cursor))


def _effective_stats(cursor) -> dict:
    """The player's six ability scores with worn/wielded score items applied (SRD 5.1)."""
    state = _item_effect_state(cursor)
    return dict(state.get("scores") or {})


def _effective_prof_bonus(cursor) -> int:
    """The player's proficiency bonus including item grants (Ioun Stone, Mastery)."""
    base = int(_db_val(cursor, "proficiency_bonus", 2) or 2)
    return base + int(_item_effect_state(cursor).get("proficiency_bonus_mod") or 0)


def _equipment_block(cursor) -> dict:
    """Derived equipped-items state (never persisted: stripped before saving)."""
    return equipment.equipment_state(_carry_get(cursor))


def _recompute_armor_class(cursor) -> int | None:
    """Recompute everything derived from the equipped set and effective scores (SRD 5.1).

    Called after every equip/unequip/attune/level change: sets `armor_class` from the equipped set,
    recomputes the spell save DC / spell attack from the effective spellcasting ability, and
    reconciles the CON-score contribution to max HP. No-op (for each part) without an `equipped`
    or `spellcasting` value.
    """
    ac = None
    if isinstance(_db_val(cursor, "equipped", None), dict):
        ac = _equipment_block(cursor)["base_ac"]
        _db_set(cursor, "armor_class", ac)
    _recompute_spellcasting(cursor)
    _recompute_hit_points(cursor)
    return ac


def _recompute_spellcasting(cursor) -> dict | None:
    """Recompute the player's spell save DC and spell attack from the effective sheet.

    SRD 5.1: DC = 8 + proficiency bonus + spellcasting ability modifier (+ item spell-DC bonus);
    attack = proficiency bonus + ability modifier (+ item spell-attack bonus).
    """
    sc = _db_val(cursor, "spellcasting", None)
    if not isinstance(sc, dict) or not sc:
        return None
    stat_key = ABILITY_TO_STAT.get(str(sc.get("ability") or "").lower())
    if not stat_key:
        return None
    scores = _effective_stats(cursor)
    mod = _ability_mod(scores.get(stat_key, 10))
    prof = _effective_prof_bonus(cursor)
    state = _item_effect_state(cursor)
    dc = 8 + prof + mod + int(state.get("spell_dc_bonus") or 0)
    attack = prof + mod + int(state.get("spell_attack_bonus") or 0)
    if sc.get("dc") != dc or sc.get("attack_modifier") != attack:
        sc["dc"] = dc
        sc["attack_modifier"] = attack
        _db_set(cursor, "spellcasting", sc)
    return {"dc": dc, "attack_modifier": attack}


def _recompute_hit_points(cursor) -> int | None:
    """Apply the effective-CON contribution to max HP (SRD 5.1: max changes retroactively).

    Reconciles the stored `item_hp_delta` (the HP added by worn/wielded CON score items) with the
    current effective CON, adjusting max and current HP by the difference (`Δcon_mod x level`).
    Idempotent; no-op without an `equipped` value.
    """
    if not isinstance(_db_val(cursor, "equipped", None), dict):
        _recompute_exhaustion_hp(cursor)
        return None
    base = _db_val(cursor, "stats", {}) or {}
    if isinstance(base, str):
        base = json.loads(base)
    effective = _effective_stats(cursor)
    level = int(_db_val(cursor, "level", 1) or 1)
    base_mod = _ability_mod(base.get("con", 10))
    eff_mod = _ability_mod(effective.get("con", base.get("con", 10)))
    hp_per_level = int(_item_effect_state(cursor).get("hp_per_level") or 0)
    new_delta = (eff_mod - base_mod) * level + hp_per_level * level
    old_delta = int(_db_val(cursor, "item_hp_delta", 0) or 0)
    diff = new_delta - old_delta
    total = int(_db_val(cursor, "total_hit_points", 1) or 1)
    if diff:
        total = max(1, total + diff)
        current = int(_db_val(cursor, "current_hit_points", 0) or 0)
        # Never revive: a larger CON changes the maximum; a downed character stays down.
        if current > 0:
            current = min(max(0, current + diff), total)
        _db_set(cursor, "total_hit_points", total)
        _db_set(cursor, "current_hit_points", current)
    _db_set(cursor, "item_hp_delta", new_delta)
    _recompute_exhaustion_hp(cursor)
    return int(_db_val(cursor, "total_hit_points", total) or total)


def _recompute_exhaustion_hp(cursor) -> int:
    """SRD 5.1 exhaustion: at level 4 the HP maximum is halved.

    Reconciled through a persisted `exhaustion_hp_delta` (the same shape as `item_hp_delta`), so
    recovering below level 4 restores the full maximum automatically. Never revives a downed
    character. Idempotent; safe with no equipped set.
    """
    level = _exhaustion_level(cursor)
    total = int(_db_val(cursor, "total_hit_points", 1) or 1)
    old_penalty = int(_db_val(cursor, "exhaustion_hp_delta", 0) or 0)
    base = total - old_penalty  # the maximum without the exhaustion penalty
    target = max(1, int(base * (0.5 if level >= 4 else 1.0)))
    new_penalty = target - base
    if new_penalty != old_penalty:
        current = int(_db_val(cursor, "current_hit_points", 0) or 0)
        _db_set(cursor, "total_hit_points", str(target))
        if current > 0:
            _db_set(cursor, "current_hit_points", str(min(current, target)))
        _db_set(cursor, "exhaustion_hp_delta", str(new_penalty))
        state.DB_CONNECTION.commit()
    return target


def _ability_mod(score) -> int:
    try:
        return (int(score) - 10) // 2
    except (TypeError, ValueError):
        return 0


def _db_set(cursor, key, value):
    if isinstance(value, (dict, list)):
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, json.dumps(value)))
    else:
        cursor.execute("INSERT OR REPLACE INTO player (key, value) VALUES (?, ?)", (key, str(value)))


def _exhaustion_level(cursor) -> int:
    """The player's exhaustion level (0-6), from the numeric sheet field."""
    if cursor is None:
        return 0
    try:
        return max(0, min(EXHAUSTION_MAX, int(_db_val(cursor, "exhaustion", 0) or 0)))
    except (TypeError, ValueError):
        return 0


def get_nested_value(data, path):
    parts = path.split('.')
    for part in parts:
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list):
            try:
                idx = int(part)
                if 0 <= idx < len(data):
                    data = data[idx]
                else:
                    return None
            except ValueError:
                return None
        else:
            return None
    return data


def set_nested_value(data, path, value):
    parts = path.split('.')
    for i in range(len(parts) - 1):
        part = parts[i]
        if isinstance(data, dict):
            data = data.setdefault(part, {})
        elif isinstance(data, list):
            try:
                idx = int(part)
                if 0 <= idx < len(data):
                    data = data[idx]
                else:
                    return data
            except ValueError:
                return data
        else:
            return data

    last_part = parts[-1]
    if isinstance(data, dict):
        data[last_part] = value
    elif isinstance(data, list):
        try:
            idx = int(last_part)
            if 0 <= idx < len(data):
                data[idx] = value
            else:
                if idx == len(data):
                    data.append(value)
        except ValueError:
            pass
    return data


def _looks_like_sheet(value) -> bool:
    """A companion sheet (vs a legacy era bucket): every sheet carries `ac`."""
    return isinstance(value, dict) and any(k in value for k in ("ac", "stats", "max_hp"))


def _flatten_companions(raw, era: str = "") -> dict:
    """One flat `{lowername: sheet}` companion store, from either shape.

    The store used to be `{era: {name: sheet}}`; companions now travel with the Traveller, so a
    load migrates a legacy map in place (the current era's companions first, then the rest, at
    most COMPANION_LIMIT). A store that is already flat is rebuilt unchanged."""
    if not isinstance(raw, dict):
        return {}
    current = str(era or "").strip().lower()
    items = sorted(raw.items(),
                   key=lambda kv: 0 if str(kv[0]).strip().lower() == current else 1)
    flat: dict = {}

    def add(name, sheet):
        key = str(name).strip().lower()
        if key and key not in flat and _looks_like_sheet(sheet) and len(flat) < COMPANION_LIMIT:
            flat[key] = sheet

    for key, value in items:
        if _looks_like_sheet(value):        # already flat
            add(key, value)
        elif isinstance(value, dict):        # a legacy era bucket
            for name, sheet in value.items():
                add(name, sheet)
    return flat
