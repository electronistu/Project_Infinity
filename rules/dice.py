"""Pure dice maths and formatting primitives for the 5e engine.

No state, no I/O: dice-notation arithmetic, XP thresholds, the d20 and HP-status
primitives.  Safe to import from anywhere.
"""

import random
import re

from .consts import XP_THRESHOLDS


def _parse_higher_levels(hl_str: str) -> tuple | None:
    if not hl_str or not hl_str.startswith("+"):
        return None
    dice_match = re.match(r"\+(\d+)d(\d+)(?:\+(\d+))?", hl_str)
    if dice_match:
        num = int(dice_match.group(1))
        size = int(dice_match.group(2))
        flat = int(dice_match.group(3)) if dice_match.group(3) else 0
        return ("dice", num, size, flat)
    flat_match = re.match(r"\+(\d+)", hl_str)
    if flat_match:
        return ("flat", int(flat_match.group(1)), 0, 0)
    return None


def _compute_spell_damage(spell: dict, character_level: int, slot_level: int | None) -> tuple:
    base_dice_str = spell.get("damage_dice", "0d0")
    base_mod = spell.get("damage_modifier", 0)
    native_level = spell.get("level", 0)

    is_cantrip = native_level == 0
    cantrip_scales = spell.get("cantrip_scaling", False)
    scale_dice_str = spell.get("cantrip_scale_dice", None)

    dice_str = base_dice_str
    modifier = base_mod

    if is_cantrip and cantrip_scales and scale_dice_str:
        if character_level >= 17:
            extra = 3
        elif character_level >= 11:
            extra = 2
        elif character_level >= 5:
            extra = 1
        else:
            extra = 0
        if extra > 0:
            dice_str = _multiply_dice_notation(base_dice_str, extra + 1)
    elif slot_level is not None and slot_level > native_level and not is_cantrip:
        hl_str = spell.get("higher_levels", None)
        if hl_str:
            per = int(spell.get("higher_levels_per", 1) or 1)
            levels_above = (slot_level - native_level) // per
            parsed = _parse_higher_levels(hl_str)
            if parsed and levels_above > 0:
                kind, num, size, flat = parsed
                if kind == "dice":
                    extra_dice_str = f"{num * levels_above}d{size}"
                    dice_str = _combine_dice(base_dice_str, extra_dice_str)
                    modifier += flat * levels_above
                elif kind == "flat":
                    dice_str = base_dice_str
                    modifier += num * levels_above

    extra_dice_str = spell.get("extra_damage_dice", None)
    if extra_dice_str and slot_level is not None and slot_level > native_level and not is_cantrip:
        ehl_str = spell.get("extra_higher_levels", None)
        if ehl_str:
            eper = int(spell.get("extra_higher_levels_per", 1) or 1)
            levels_above = (slot_level - native_level) // eper
            eparsed = _parse_higher_levels(ehl_str)
            if eparsed and levels_above > 0:
                ekind, enum, esize, eflat = eparsed
                if ekind == "dice":
                    extra_dice_str = _combine_dice(extra_dice_str, f"{enum * levels_above}d{esize}")

    return dice_str, modifier, extra_dice_str


def _multiply_dice_notation(dice_str: str, multiplier: int) -> str:
    if multiplier <= 1:
        return dice_str
    parts = dice_str.lower().split("d")
    if len(parts) != 2:
        return dice_str
    try:
        num = int(parts[0]) if parts[0] else 1
        size = int(parts[1])
        return f"{num * multiplier}d{size}"
    except (ValueError, TypeError):
        return dice_str


def _combine_dice(d1: str, d2: str) -> str:
    if not d2:
        return d1
    p1 = d1.lower().split("d")
    p2 = d2.lower().split("d")
    if len(p1) == 2 and len(p2) == 2:
        try:
            n1 = int(p1[0]) if p1[0] else 1
            s1 = int(p1[1])
            n2 = int(p2[0]) if p2[0] else 1
            s2 = int(p2[1])
            if s1 == s2:
                return f"{n1 + n2}d{s1}"
        except (ValueError, TypeError):
            pass
    return f"{d1}+{d2}"


def get_level_for_xp(xp: int) -> int:
    level = 1
    for lvl, threshold in XP_THRESHOLDS:
        if xp >= threshold:
            level = lvl
        else:
            break
    return level


def _roll_d20(advantage: bool = False, disadvantage: bool = False):
    """5e d20 with advantage/disadvantage, which cancel each other out.

    Returns ``(d20, rolls, mode, cancelled)``: `rolls` is the pair for the
    narrative, `mode` is "advantage"/"disadvantage"/None.
    """
    if advantage and disadvantage:
        return random.randint(1, 20), None, None, True
    if advantage or disadvantage:
        r1, r2 = random.randint(1, 20), random.randint(1, 20)
        if advantage:
            return max(r1, r2), [r1, r2], "advantage", False
        return min(r1, r2), [r1, r2], "disadvantage", False
    return random.randint(1, 20), None, None, False


def _hp_status_tag(current, total):
    if total <= 0:
        return "Unknown"
    if current == 0:
        return "Unconscious"
    ratio = current / total
    if ratio < 0.25:
        return "Critical"
    elif ratio < 0.50:
        return "Bloodied"
    elif ratio < 0.75:
        return "Wounded"
    else:
        return "Healthy"


def _format_hp_status(current, total):
    tag = _hp_status_tag(current, total)
    pct = round((current / total) * 100) if total > 0 else 0
    return f"HP: {current}/{total} ({tag} {pct}%)"


def _parse_and_roll_dice(dice_notation):
    try:
        if isinstance(dice_notation, (int, float)):
            return 0, [], int(dice_notation)
        s = str(dice_notation).strip().lower()
        if s.isdigit():
            return 0, [], int(s)
        parts = s.split('d')
        if len(parts) != 2:
            return None, [], 0
        num_dice = int(parts[0]) if parts[0] else 1
        die_size = int(parts[1])
        if num_dice == 0 or die_size == 0:
            return 0, [], 0
        if num_dice < 0 or die_size < 0:
            return None, [], 0
        rolls = [random.randint(1, die_size) for _ in range(num_dice)]
        return die_size, rolls, sum(rolls)
    except (ValueError, TypeError):
        return None, [], 0
