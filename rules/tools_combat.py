"""Combat-registry MCP tools: companions, combatant registration and updates, and
ending a fight."""

import random
from .app import mcp
from . import state
from .combat import (
    _combat_turn_state, _combatant_sheet_lines, _companion_line, _companion_sheet,
    _companions_store, _expand_companions, _expand_groups, _finish_combat_if_over,
    _normalize_combatant, _player_registry_entry, _registry_entry, _registry_summary_list,
    _reset_combat_cursor, _resync_combat_cursor, _save_companions
)
from .conditions import _drain_woke, _player_conditions, _wake_on_damage
from .consts import COMPANION_LIMIT, EXHAUSTION_MAX
from .db import _db_set, _exhaustion_level
from .dice import _roll_d20
from .vitals import _apply_exhaustion_change

@mcp.tool()
def register_companion(name: str, sheet: dict | None = None, action: str = "declare") -> dict:
    """Declare a companion's character sheet once, or forget it.

    WHEN: a companion joins (declare in FULL, once), changes lastingly (re-declare the same name), or leaves or dies (forget).
    - name: the exact name you keep using.
    - sheet: the companion's static COMBAT sheet. REQUIRED: `stats` (all six ability scores), `ac`, `max_hp`. Optional: `attacks`, `advantages` (combat-only special rules, e.g. 'Sneak Attack 2d6'), `skills` (the skills it is proficient in), `expertise`, `proficiency_bonus` (+2 default), `speed`, `saves`, `spellcasting`, damage/condition resistances and immunities. `race`, `class` and `description` (the stable physical look the illustrator keeps) are identity, not combat. There is NO current-HP field.
    - action: 'declare' (add or update) | 'forget' (remove).
    """
    if state.DB_CONNECTION is None:
        return {"success": False, "error": "Database not initialized."}
    action = str(action or "declare").strip().lower()
    key = " ".join(str(name or "").split())
    if action not in ("declare", "forget"):
        return {"success": False, "error": "invalid_action",
                "reason": "action must be 'declare' or 'forget'."}
    if not key:
        return {"success": False, "error": "name_required", "reason": "A companion needs a name."}
    try:
        cursor = state.DB_CONNECTION.cursor()
        store = _companions_store(cursor)
        if action == "forget":
            if key.lower() not in store:
                return {"success": True, "status": "absent", "name": key,
                        "note": f"No companion named {key} is declared here."}
            store.pop(key.lower(), None)
            _save_companions(cursor, store)
            return {"success": True, "status": "forgotten", "name": key,
                    "count": len(store), "note": f"{key} is forgotten."}
        entry = _companion_sheet(key, sheet or {})
        if entry is None:
            return {"success": False, "error": "incomplete_sheet",
                    "reason": "A companion sheet needs `ac` and `max_hp` (and a name).",
                    "gm_instruction": "Give the full sheet: description, race, class, ac, "
                                      "max_hp, attacks, saves."}
        if key.lower() not in store and len(store) >= COMPANION_LIMIT:
            return {"success": False, "error": "companion_limit",
                    "reason": f"You already have {COMPANION_LIMIT} companions. "
                              "The player needs to decide which companion to remove if he "
                              "wants a new one.",
                    "companions": sorted(str(v.get("name") or k) for k, v in store.items()),
                    "gm_instruction": "Ask the player which companion leaves, then call "
                                      "register_companion(name=..., action='forget')."}
        store[key.lower()] = entry
        _save_companions(cursor, store)
        return {"success": True, "status": "declared", "name": entry["name"],
                "count": len(store), "limit": COMPANION_LIMIT,
                "note": "Register this companion by name alone from now on; the engine fills "
                        "the sheet."}
    except Exception as e:  # noqa: BLE001 - a declaration must never crash the server
        return {"success": False, "error": f"Error declaring the companion: {str(e)}"}


@mcp.tool()
def register_combatants(combatants: list[dict], add_to_existing: bool = False) -> dict:
    """Register every combatant for a battle and roll initiative; the player is auto-registered.

    WHEN: a fight starts -- call this FIRST, before ANY resolve_attack/resolve_magic.
    FIELDS (combatants: list of dicts; required name (or names), hp, ac):
    - name; hp; max_hp (defaults to hp); ac; initiative_modifier (DEX mod; default 0); initiative_advantage; challenge_rating (XP); role 'hostile' | 'ally' | 'neutral' (default hostile); speed.
    - names: a list of names sharing this whole block (identical mooks) -- declare the sheet once instead of repeating it. `name` and `names` are exclusive; the group shares one initiative roll (identical creatures act together), while each name keeps its own HP and conditions.
    - saves {str, dex, con, int, wis, cha}; damage_resistances/damage_immunities/damage_vulnerabilities (list; 'all' allowed); condition_immunities.
    - attacks: list of {name, base?, attack_bonus, damage_dice, damage_modifier?, damage_type?, properties?, reach?, range?} -- resolve_attack(actor, attack) uses these.
    - multiattack (int or str; reported, not enforced); spellcasting {ability, save_dc, attack_modifier}; conditions; exhaustion (0-6); traits (a list of strings, e.g. ["Sneak Attack 2d6."]).
    - add_to_existing (default False): add arrivals without wiping the registry; each arrival rolls its own initiative and joins the order.

    RULES:
    - Declare the FULL stat block -- the engine derives attacks/saves/AC/damage from it and you never repeat those values; estimate only when a creature truly has no block.
    - resolve_attack/resolve_magic auto-look up HP/AC/CR/saves, so you need not pass them each call; HP carries forward.
    - Re-calling without add_to_existing overwrites the registry and starts a new fight; to end a fight with NO new one, call `end_combat`.
    - Declared conditions drive advantage/disadvantage, auto failures and crits automatically.

    EX:
    register_combatants(combatants=[
        {"name":"Bahram","hp":9,"ac":12,"initiative_modifier":1,"challenge_rating":0.125,"role":"hostile"},
        {"names":["Market Warden 1","Market Warden 2"],"hp":11,"ac":13,"initiative_modifier":1,"challenge_rating":0.125,"role":"hostile","attacks":[{"name":"Cudgel","attack_bonus":3,"damage_dice":"1d6","damage_modifier":1,"damage_type":"bludgeoning"}]}])
    """

    # A `names` group is one shared stat block for many identical creatures: expand it into one
    # plain entry per name first, so everything downstream sees single combatants.
    combatants, group_of, problem = _expand_groups(combatants)
    if problem is not None:
        return problem

    # A companion is registered by NAME ALONE: the engine fills its declared sheet here, so the
    # Game Master never restates the stat block. An explicit block is left as it is.
    cursor = state.DB_CONNECTION.cursor() if state.DB_CONNECTION is not None else None
    combatants, companions_filled, problem = _expand_companions(cursor, combatants)
    if problem is not None:
        return problem

    if not add_to_existing:
        state.registry_clear()
        _reset_combat_cursor()

    if not add_to_existing and state.DB_CONNECTION is not None:
        cursor = state.DB_CONNECTION.cursor()
        player = _player_registry_entry(cursor)
        player_name = player["name"]
        player_init_mod = player["initiative_modifier"]
        state.registry_set(player_name, player)

        player_d20, player_rolls, _, _ = _roll_d20(advantage=player.get("initiative_advantage", False))
        state._COMBAT_REGISTRY[player_name]["initiative_roll"] = player_d20
        state._COMBAT_REGISTRY[player_name]["initiative_total"] = player_d20 + player_init_mod

    # One roll per `names` group (SRD: identical creatures act together); a lone entry rolls its
    # own. Arrivals roll too, so a latecomer joins the order instead of always acting last.
    group_rolls: dict[int, tuple[int, int, int]] = {}
    for c in combatants:
        name = c["name"]
        entry = _normalize_combatant(c)
        init_mod = entry["initiative_modifier"]
        state.registry_set(name, entry)
        gid = group_of.get(name)
        if gid is not None and gid in group_rolls:
            d20, init_mod, init_total = group_rolls[gid]
        else:
            d20 = (max(random.randint(1, 20), random.randint(1, 20))
                   if entry.get("initiative_advantage") else random.randint(1, 20))
            init_total = d20 + init_mod
            if gid is not None:
                group_rolls[gid] = (d20, init_mod, init_total)
        entry["initiative_roll"] = d20
        entry["initiative_total"] = init_total

    # The whole order is rebuilt from the registry, so an arrival re-sorts it for the client.
    initiative_results = [
        {"name": n, "roll": e.get("initiative_roll", 0),
         "modifier": e.get("initiative_modifier", 0), "total": e.get("initiative_total", 0),
         "is_player": bool(e.get("is_player"))}
        for n, e in state._COMBAT_REGISTRY.items()
    ]
    initiative_results.sort(key=lambda r: (-r["total"], r["name"]))
    order = [r["name"] for r in initiative_results]
    _resync_combat_cursor()

    registry_summary = _registry_summary_list()
    total = len(state._COMBAT_REGISTRY)

    new_npc_names = [c["name"] for c in combatants
                     if not (_registry_entry(c["name"]) or {}).get("is_player")]
    sheets = []
    for nname in new_npc_names:
        entry = _registry_entry(nname)
        if entry is not None:
            sheets.append({"name": nname, "lines": _combatant_sheet_lines(nname, entry)})

    # The companions the engine filled in, so the Game Master plays them with their real
    # numbers instead of remembering the sheet (the roster and the sheets never reach it).
    companion_lines = []
    for cname in new_npc_names:
        sheet = companions_filled.get(cname.lower())
        entry = _registry_entry(cname)
        if sheet is not None and entry is not None:
            companion_lines.append({
                "name": cname,
                "hp": f"{entry['current_hp']}/{entry['max_hp']}",
                "ac": entry["ac"],
                "line": _companion_line(sheet, entry),
            })

    if add_to_existing:
        added = ", ".join(c["name"] for c in combatants)
        narrative = (f"Combatants registered ({total} total). "
                     f"Added to existing registry: {added}")
    else:
        narrative = f"Combatants registered ({total} total). Initiative Order"

    return {
        "success": True,
        "initiative": initiative_results,
        "initiative_order": order,
        "registry_summary": registry_summary,
        "combat_turn": _combat_turn_state(),
        "sheets": sheets,
        "companions": companion_lines,
        "narrative_format": narrative,
    }


@mcp.tool()
def update_combatant(name: str, conditions_add: list[str] | None = None,
                     conditions_remove: list[str] | None = None,
                     hp_delta: int | None = None, max_hp: int | None = None,
                     ac: int | None = None, exhaustion_delta: int | None = None,
                     status: str | None = None) -> dict:
    """Change a registered combatant mid-fight: conditions, exhaustion, and (NPC) HP/defence.

    WHEN: a condition the fiction causes (a shove -> prone), a condition ends, a correction is needed, or an NPC's HP/AC changes.
    FIELDS:
    - name: the combatant exactly as registered.
    - conditions_add / conditions_remove: adding an immune condition is refused (in 'blocked_conditions'); a condition a TOOL already applied returns 'already_present' and changes nothing.
    - exhaustion_delta: add/remove exhaustion levels 0-6 (player: updates the sheet and re-derives max HP; NPC: updates the registry entry).
    - hp_delta: (NPCs only) signed, clamped to [0, max_hp], sets 'killed' at 0. max_hp / ac: (NPCs only) correct the declared values.
    - status: (NPCs only) 'active' | 'fled' | 'surrendered' -- a hostile that leaves the fight stops counting as in combat; 'active' returns it.

    RULES:
    - Conditions drive the engine automatically (advantage/disadvantage, auto-failed saves, melee crits vs helpless, petrified resists all damage).
    - The player's conditions live on their sheet; use modify_player_numeric for player HP.

    EX:
    update_combatant(name='Goblin', conditions_add=['prone'])
    update_combatant(name='{player_name}', exhaustion_delta=1)
    """
    entry = _registry_entry(name)
    if entry is None:
        return {"success": False, "error": "not_registered", "name": name,
                "reason": f"'{name}' is not in the combat registry.",
                "combatants": list(state._COMBAT_REGISTRY)}
    is_player = bool(entry.get("is_player"))
    new_status = None if status is None else str(status).strip().lower()
    if new_status is not None and new_status not in ("active", "fled", "surrendered"):
        # GM-correctable shape error: no `narrative_format` (see `_action_failure`) -- a bad
        # parameter must never compose a player-facing Mechanics line.
        return {"success": False, "error": "invalid_status", "name": name,
                "reason": f"status must be 'active', 'fled' or 'surrendered', not '{status}'."}
    notes = []
    blocked = []
    already_present = []

    if is_player:
        if state.DB_CONNECTION is None:
            return {"success": False, "error": "Database not initialized."}
        cursor = state.DB_CONNECTION.cursor()
        current = _player_conditions(cursor)
        immunities = set()
    else:
        current = {str(c).strip().lower() for c in (entry.get("conditions") or [])}
        immunities = {str(c).strip().lower() for c in (entry.get("condition_immunities") or [])}

    # Exhaustion is a level, not a boolean condition.
    cond_add = [str(c).strip().lower() for c in (conditions_add or []) if str(c).strip()]
    cond_remove = [str(c).strip().lower() for c in (conditions_remove or [])]
    add_levels = int(exhaustion_delta or 0)
    if is_player:
        if add_levels:
            _apply_exhaustion_change(cursor, add_levels, reason="update_combatant")
        entry["exhaustion"] = _exhaustion_level(cursor)
        current = _player_conditions(cursor)
    else:
        entry["exhaustion"] = max(0, min(EXHAUSTION_MAX,
            int(entry.get("exhaustion") or 0) + add_levels))
    # `exhaustion` is a level, not a condition: adjust it with exhaustion_delta.
    cond_add = [c for c in cond_add if c != "exhaustion"]
    cond_remove = [c for c in cond_remove if c != "exhaustion"]

    for key in cond_add:
        if key in immunities:
            blocked.append({"condition": key, "reason": "immune"})
            continue
        if key in current:
            already_present.append(key)
        current.add(key)
    for key in cond_remove:
        current.discard(key)
    current = sorted(current)

    if is_player:
        cursor = state.DB_CONNECTION.cursor()
        _db_set(cursor, "conditions", current)
        entry["conditions"] = current
    else:
        if entry["exhaustion"] > 0:
            current = sorted(set(current) | {"exhaustion"})
        else:
            current = sorted(set(current) - {"exhaustion"})
        entry["conditions"] = current

    if hp_delta is not None or max_hp is not None or ac is not None:
        if is_player:
            notes.append("Player HP/AC are on the sheet — use modify_player_numeric("
                         "key='current_hit_points'|'armor_class', delta=…).")
        else:
            if max_hp is not None:
                entry["max_hp"] = max(1, int(max_hp))
                entry["current_hp"] = min(entry["current_hp"], entry["max_hp"])
            if ac is not None:
                entry["ac"] = int(ac)
            if hp_delta is not None:
                new_hp = max(0, min(entry["max_hp"], entry["current_hp"] + int(hp_delta)))
                entry["current_hp"] = new_hp
                if new_hp == 0:
                    entry["killed"] = True
                elif int(hp_delta) > 0:
                    entry["killed"] = False
                if int(hp_delta) < 0:
                    _wake_on_damage(name)  # damage ends a marked condition (Eyebite: Asleep)

    # `status` is the explicit out-of-combat mark for a hostile that fled or surrendered.
    status_note = None
    if new_status is not None:
        if is_player:
            notes.append("Player status is not tracked — conditions and HP live on the sheet.")
        else:
            old_status = str(entry.get("status") or "active").strip().lower()
            entry["status"] = new_status
            if new_status != old_status:
                status_note = {"active": "back in the fight",
                               "fled": "fled the fight",
                               "surrendered": "surrendered"}[new_status]

    # Vitals appear only when this call actually changed them; otherwise the client tooltip
    # carries the live HP/AC. The player's AC is never shown here (ac= is ignored for the player).
    bits = []
    woke = _drain_woke()
    if hp_delta is not None and not is_player:
        bits.append(f"{entry['current_hp']}/{entry['max_hp']} HP")
    if ac is not None and not is_player:
        bits.append(f"AC {entry['ac']}")
    narrative = f"{name}: {', '.join(bits)}" if bits else name
    if woke:
        narrative += (" — wakes: "
                       + ", ".join(f"{w['name']} ({', '.join(w['conditions'])})" for w in woke))
    if status_note:
        narrative += f" — {status_note}"
    if current:
        narrative += f" — {', '.join(current)}"
    if blocked:
        narrative += f" (immune to {', '.join(b['condition'] for b in blocked)})"
    if already_present:
        narrative += f" ({', '.join(already_present)} already present — no change)"
    # This call may have ended the fight (the last hostile killed, fled or surrendered): the
    # roster below is the FINAL one, and the registry empties behind it.
    summary = _registry_summary_list()
    _finish_combat_if_over()
    return {
        "success": True, "name": name, "is_player": is_player,
        "hp": f"{entry['current_hp']}/{entry['max_hp']}", "ac": entry["ac"],
        "conditions": current, "exhaustion": int(entry.get("exhaustion") or 0),
        "status": str(entry.get("status") or "active"),
        "blocked_conditions": blocked,
        "already_present": already_present,
        "note": " ".join(notes) or None,
        "registry_summary": summary,
        "combat_turn": _combat_turn_state(),
        "conditions_woke": woke or None,
        "narrative_format": narrative,
    }


@mcp.tool()
def end_combat() -> dict:
    """End the combat: clear the combat registry.

    WHEN: the fiction ends a fight with no new fight beginning -- the player escaped or teleported away, the foes are dead, caged or gone, or a truce is struck. The engine already ends combat on its own when every hostile is killed, fled or surrendered; call this for the cases it cannot see. Never call it mid-fight.
    """
    state.registry_clear()
    _reset_combat_cursor()
    return {"success": True, "registry_summary": [],
            "combat_turn": _combat_turn_state(),
            "narrative_format": "The combat is over."}
