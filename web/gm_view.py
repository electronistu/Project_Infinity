import json
import re

from .protocol import MAX_LEGEND_CHARS, MAX_LEGEND_EXTRACT, REMEMBERS_RE


# ── What the GM is shown of a tool result ─────────────────────────────────
#
# Tool results carry only what the GM does not already hold: the event/delta, the exact
# mechanics, and errors that explain what happened. The base sheet is already in context
# (dump_player_db), so state snapshots (`equipment`, the carrying breakdown, a full inventory
# list) are never re-sent. The FULL result still goes to the client (the tool_result event) for
# debugging; only the copy appended to the GM conversation is trimmed.
#
# Every GM-offered tool has an EXPLICIT decision -- KEEP, DROP or FULL below. There is no
# fail-open default: a tool in none of the three is a bug, and a coverage test fails the build
# for it (web/tests/test_tool_projection.py).
_GM_VIEW_KEEP: dict[str, tuple[str, ...]] = {
    "modify_player_numeric": (
        "success", "key", "old_value", "new_value", "delta", "clamped",
        "item_depleted", "depleted_item", "level_up", "old_level", "new_level",
        "level_up_changes", "level_up_summary", "hp_status", "message",
        "remaining_slots"),
    "update_player_list": (
        "success", "key", "item", "action", "unequipped", "note", "warning",
        "unweighed_item", "reverted", "spells_prepared_info"),
    "equip_item": (
        "success", "action", "item", "armor_class_before", "armor_class_after",
        "warnings", "time_cost", "already_equipped", "equipped", "held",
        "occupied", "worn", "slot"),
    "attune_item": (
        "success", "action", "item", "attuned", "attunement_slots_free",
        "warnings", "already_attuned"),
    "rest": ("success", "rest_type", "changes", "hints"),
    "register_combatants": ("success", "initiative_order", "registry_summary", "companions"),
    "end_combat": ("success",),
    "update_combatant": ("success", "name", "is_player", "hp", "ac", "conditions",
                         "exhaustion", "blocked_conditions", "already_present", "note"),
    "modify_exhaustion": ("success", "exhaustion", "old", "delta", "effects", "max_hp"),
    "make_death_save": ("success", "roll", "successes", "failures", "outcome", "hp"),
    # Acknowledgements: the GM just made the call, so only the outcome is new. Every field an
    # ack carries must be something the GM does NOT already have -- the declaration echoed
    # back is not it, and neither is a `note` that restates the tool's own description.
    "request_scene_image": ("status",),
    "register_npcs": ("status", "count"),
    # The one ack `note` that is load-bearing: it is the only GM-facing carrier of "register
    # the companion by name alone from now on" (the docstring does not say it). Guarded in
    # web/tests/test_scenes.py -- do not drop it with the others.
    "register_companion": ("status", "name", "count", "limit", "note"),
    "note_place": ("status",),
}
# Added on failure so the GM can explain and act on a refused/wrong call.
_GM_VIEW_ERROR_EXTRA = (
    "error", "reason", "gm_instruction", "turn_lost", "action_consumed",
    "current_items", "held", "occupied", "worn", "slot", "available_keys",
    "attuned", "attunement_slots_free", "conflicting_item", "prerequisite")

# Deny-lists: keep everything EXCEPT these. For a broad mechanics result, naming what to drop
# is safer than naming what to keep.
_GM_VIEW_DROP: dict[str, tuple[str, ...]] = {
    # A mechanics tool embeds the FULL result of the calls it made -- `slot_result` and
    # `xp_result` (whole `modify_player_numeric` results) and `hp_change` (a whole HP-change
    # result). The scalars the GM needs already ride the top level (`slot_consumed`,
    # `slot_level_used`, `xp_awarded`, `damage_total`, `targets[].remaining_hp`).
    "resolve_magic": ("slot_result", "xp_result", "hp_change"),
    "resolve_attack": ("slot_result", "xp_result", "hp_change"),
}

# Intentional FULL pass-throughs, each with its reason. Naming them is what makes coverage
# testable; none of these is a default.
_GM_VIEW_FULL: dict[str, str] = {
    "lookup": "plain text (era lore / a tool reference); there is no JSON to project",
    "perform_check": "one small mechanics result; nothing stateful to strip",
    "roll_dice": "one small magnitude roll",
    "read_player_field": "the requested fields only, already sliced server-side; the "
                         "always-drop strip is the final guard",
}

# Always dropped, at ANY depth: the engine's own mechanics disclosure, and the state the GM
# already holds. Recursive because a tool can embed another tool's result.
_GM_VIEW_ALWAYS_DROP = ("narrative_format", "equipment", "current_list", "registry_summary",
                        "sheets", "combat_turn", "difficulty", "era", "arrival")
# `carrying` is reduced to the one-line state that drives disadvantage.
_GM_VIEW_CARRYING = ("status", "speed_penalty", "carried", "capacity")


def _gm_strip(value):
    """Recursively drop the always-drop keys, at any depth.

    A mechanics tool embeds other tools' results (`slot_result`, `hp_change`), so a
    top-level-only pop used to leak a nested `narrative_format` -- the engine's own mechanics
    disclosure -- straight to the GM."""
    if isinstance(value, dict):
        return {k: _gm_strip(v) for k, v in value.items() if k not in _GM_VIEW_ALWAYS_DROP}
    if isinstance(value, list):
        return [_gm_strip(v) for v in value]
    return value


def _gm_tool_view(name: str, text: str) -> str:
    """The trimmed result the GM sees, as JSON (falls back to `text` when not a dict).

    Every GM-offered tool is classified: KEEP (only these keys), DROP (everything but these) or
    FULL (whole, with a stated reason). A name in none of them is a bug, not a free pass -- the
    payload is still stripped of the always-drop keys rather than dumped raw, and the coverage
    test (web/tests/test_tool_projection.py) fails the build for a new tool."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if not isinstance(payload, dict):
        return text
    if name in _GM_VIEW_KEEP:
        keys = list(_GM_VIEW_KEEP[name])
        if payload.get("success", True) is False:
            keys += list(_GM_VIEW_ERROR_EXTRA)
        view = {k: payload[k] for k in dict.fromkeys(keys) if k in payload}
    elif name in _GM_VIEW_DROP:
        drop = set(_GM_VIEW_DROP[name])
        if str(payload.get("attack_type") or "").strip().lower() == "automatic":
            # An automatic multi-projectile spell needs no per-dart list: `targets[].darts`
            # already carries every dart's damage. An attack-roll one keeps it -- the rolls and
            # AC are the only record of those attacks.
            drop.add("per_projectile")
        view = {k: v for k, v in payload.items() if k not in drop}
    else:  # _GM_VIEW_FULL, or (a bug) unclassified: fail closed, never dump the raw payload
        view = dict(payload)
    view = _gm_strip(view)
    carry = payload.get("carrying")
    if isinstance(carry, dict):
        slim = {k: carry[k] for k in _GM_VIEW_CARRYING if k in carry}
        if slim:
            view["carrying"] = slim
        else:
            view.pop("carrying", None)
    return json.dumps(view, ensure_ascii=True)


# The GM's era memory, written on the closing turn: `{{remembers: <the age's memory>}}` --
# the age's whole memory, not one sentence (see `_MEMORY_SCOPE`). The client hides it while
# streaming; the engine keeps only the memory block. The marker is matched tolerantly
# (`protocol.REMEMBERS_RE`); the payload is judged here.


def _extract_legend(text: str) -> tuple[str, str]:
    """Pull the `{{remembers: ...}}` marker out of an assistant message.

    Returns ``(legend, cleaned_text)``. A legend that merely echoes the placeholder, or is
    absurdly short or long, is rejected -- the template fallback is better than a broken
    memory line. A message without the marker is returned untouched.
    """
    raw = text or ""
    match = REMEMBERS_RE.search(raw)
    if not match:
        return "", raw
    legend = " ".join(match.group(1).split())
    cleaned = REMEMBERS_RE.sub("", raw).strip()
    if "<" in legend:
        return "", cleaned
    if not (15 <= len(legend) <= MAX_LEGEND_EXTRACT):
        return "", cleaned
    return legend, cleaned


def _trim_legend(text: str, limit: int = MAX_LEGEND_CHARS) -> str:
    """Bound an era's memory to `limit` characters, dropping the OLDEST sentences.

    The GM rewrites the whole memory; on overflow the earliest events are dropped, at a
    sentence boundary, so the most recent memory survives intact."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    kept = ""
    for sentence in reversed(re.split(r"(?<=[.!?])\s+", text)):
        candidate = f"{sentence} {kept}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    return kept or text[-limit:].lstrip()


def _clamp_block(text: str, limit: int) -> str:
    """Hard-bound a multi-line block to `limit` characters, at a line boundary.

    Unlike `_trim_legend` this keeps line structure (the STATE notebook is a list of
    fields, not one paragraph) and, on overflow, keeps the EARLIEST lines -- the prompt
    orders the fields by importance (Goal and Hooks first)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    out: list[str] = []
    total = 0
    for line in text.splitlines():
        if total + len(line) + 1 > limit:
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out) or text[:limit]
