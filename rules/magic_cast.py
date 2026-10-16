"""P0/P1 of `resolve_magic`: who casts, whether they may, and what the spell is.

`prepare_cast` resolves the actor and the cursor, the DC / spell-attack bonus, the armour
and free-hand gate, the spell row (or the custom-spell parameters), the `sp_*` facts both
branches extract, and the knowledge / spellbook / maintain / duplicate-buff gates. A gate
that refuses returns a finished tool result (`dict`); otherwise the `CastState` the later
phases read and write.
"""

import json
from dataclasses import dataclass, field

import carrying

from . import state
from .combat import _action_failure, _blocked_action, _registry_entry
from .db import _db_val, _equipment_block, _exhaustion_level
from .items import _covers_material
from .sheet import _exhaustion_effects, _is_player_actor, _roll_disadvantage
from .spell_knowledge import (
    _castable_spells, _has_spellbook_item, _needs_spellbook, _player_knows_spell
)
from .spells import _load_spells, _spell_components
from .vitals import _get_concentration

@dataclass
class CastState:
    """One cast's frame: what `resolve_magic` was called with, and what each phase
    made of it. A plain carrier -- no methods -- so each phase reads and writes the
    field the single-function version kept in a local.
    """

    # ── the tool's inputs a later phase reads back ──
    spell_name: str
    actor: str
    spell_save_dc: int
    spell_attack_modifier: int
    target_ac: int | None
    target_name: str
    slot_level: int | None
    is_scroll: bool
    maintain_spell: bool
    ritual: bool
    is_npc_attack: bool
    is_npc_vs_npc: bool
    targets: list[dict] | None

    # ── resolved here ──
    cursor: object
    spell: dict | None
    enc_disadvantage: bool
    encumbrance_sources: list[str]
    config_conflicts: dict
    sp_attack_type: str
    sp_damage_dice: str
    sp_damage_modifier: int
    sp_damage_type: str
    sp_save_type: str | None
    sp_save_half: bool
    sp_healing: bool
    sp_cantrip_scaling: bool
    sp_higher_levels: str | None
    sp_level: int
    sp_no_damage: bool
    sp_instant_kill: bool
    sp_instant_kill_threshold: int
    sp_damage_on_miss: str | None
    sp_extra_damage_dice: str | None
    sp_extra_damage_type: str | None
    sp_hp_pool: bool
    sp_condition: str | None
    sp_condition_duration: str | None
    sp_requires_concentration: bool
    sp_duration: str
    sp_buffs: dict | None
    sp_temp_hp: bool
    sp_repeat: dict | None
    sp_condition_on_hit: bool
    sp_post_hit_save: dict | None
    sp_condition_ends_on_damage: bool
    # Bound only for a configured spell: a custom spell leaves it unset, and the
    # auto-healing path fails there exactly as it did before the split.
    sp_flat_healing: bool = field(init=False, repr=False)

    # ── filled by `prepare_slot`, read by `resolve_spell_effects` ──
    result: dict = field(init=False, repr=False)
    narrative_parts: list[str] = field(init=False, repr=False)
    final_dice: str = field(init=False, repr=False)
    final_mod: int = field(init=False, repr=False)
    final_extra_dice: str | None = field(init=False, repr=False)
    character_level: int = field(init=False, repr=False)
    computed_slot: int = field(init=False, repr=False)
    is_multi_projectile: bool = field(init=False, repr=False)
    has_targets: bool = field(init=False, repr=False)
    named_only: list[str] = field(init=False, repr=False)
    is_crit: bool = field(init=False, repr=False)


def prepare_cast(
    spell_name: str,
    actor: str,
    spell_attack_modifier: int | None,
    spell_save_dc: int | None,
    target_ac: int | None,
    target_name: str,
    slot_level: int | None,
    is_npc_attack: bool,
    is_scroll: bool,
    maintain_spell: bool,
    attack_type: str | None,
    save_type: str | None,
    save_half: bool,
    damage_dice: str | None,
    damage_modifier: int,
    damage_type: str | None,
    cantrip_scaling: bool,
    higher_levels: str | None,
    eyebite_effect: str | None,
    healing: bool,
    aoe: bool,
    ritual: bool,
    is_npc_vs_npc: bool,
    targets: list[dict] | None,
    components: str | None,
) -> dict | CastState:
    """Resolve the caster, the spell and every pre-cast gate. Returns a terminal tool
    result, or the state the slot and resolution phases continue from."""
    spells_db = _load_spells()

    if actor == "{player_name}" and state.DB_CONNECTION is not None:
        cursor = state.DB_CONNECTION.cursor()
        actor = _db_val(cursor, "name", "Player")
    else:
        cursor = state.DB_CONNECTION.cursor() if state.DB_CONNECTION is not None else None

    # SRD 5.1 variant: a heavily encumbered player rolls spell attacks with
    # disadvantage (cancelling a GM-granted advantage).
    is_player_caster = not is_npc_attack and not is_npc_vs_npc
    # The caller's own attack bonus, read before the registry/sheet below may fill it in: a
    # caster that supplied neither one nor a registration can then be refused (see below).
    attack_modifier_supplied = spell_attack_modifier is not None
    # DC / attack bonus: a registered NPC caster supplies its own; the player's comes from the sheet.
    if is_player_caster and cursor is not None:
        spell_raw = _db_val(cursor, "spellcasting", {}) or {}
        if isinstance(spell_raw, dict):
            if spell_save_dc is None and spell_raw.get("dc") is not None:
                spell_save_dc = int(spell_raw["dc"])
            if spell_attack_modifier is None and spell_raw.get("attack_modifier") is not None:
                spell_attack_modifier = int(spell_raw["attack_modifier"])
    else:
        npc_spell = (_registry_entry(actor) or {}).get("spellcasting") or {}
        if spell_save_dc is None and npc_spell.get("save_dc") is not None:
            spell_save_dc = int(npc_spell["save_dc"])
        if spell_attack_modifier is None and npc_spell.get("attack_modifier") is not None:
            spell_attack_modifier = int(npc_spell["attack_modifier"])
    spell_save_dc = int(spell_save_dc or 0)
    spell_attack_modifier = int(spell_attack_modifier or 0)
    enc_disadvantage, encumbrance_sources = _roll_disadvantage(cursor, is_player_caster)
    if is_player_caster:
        exh_att = _exhaustion_effects(_exhaustion_level(cursor))
        if exh_att["attack_disadvantage"]:
            enc_disadvantage = True
            encumbrance_sources = [*encumbrance_sources, f"exhaustion {exh_att['level']}"]

    # ── armour proficiency, then free-hand check (SRD 5.1) ───────────────────
    if (cursor is not None and is_player_caster and not is_scroll
            and _is_player_actor(cursor, actor)):
        comps = (components or _spell_components().get(carrying.normalize(spell_name)) or "VSM").upper()
        worn_state = _equipment_block(cursor)
        # No spellcasting at all while wearing armour you are not proficient with.
        if not worn_state.get("armor_proficient", True):
            blocked = _blocked_action(
                cursor, "armor_not_proficient",
                "You can't cast spells while wearing armour you lack proficiency with "
                f"({', '.join(worn_state.get('proficiency_sources') or [])}).", spell_name,
                f"{spell_name} failed — {actor} wears armour they are not proficient with, so no "
                f"spell can be cast. The action is spent.")
            blocked["spell"] = spell_name
            blocked["note"] = "No spell slot is consumed."
            return blocked
        if (("S" in comps or "M" in comps) and worn_state.get("derived_from_equipped")
                and worn_state.get("hands_free_casting", 2) == 0):
            # A held focus or component pouch covers M but not S (a hand is still needed).
            held = [h.get("name") for h in worn_state.get("hands", []) if h.get("name")]
            focus_ok = "M" in comps and "S" not in comps and _covers_material(cursor, held)
            if not focus_ok:
                held_label = ", ".join(held) or "both hands"
                blocked = _blocked_action(
                    cursor, "both_hands_occupied",
                    f"Both hands are occupied ({held_label}) — {spell_name} needs a free hand for its "
                    f"{'somatic' if 'S' in comps else 'material'} components.",
                    spell_name,
                    f"{spell_name} failed — both hands are busy ({held_label}), so the spell cannot "
                    f"be cast. The action is spent.")
                blocked["spell"] = spell_name
                blocked["components"] = comps
                blocked["note"] = "No spell slot is consumed."
                return blocked

    spell_key = spell_name.lower().strip()
    spell = spells_db.get(spell_key)

    if spell:
        sp_attack_type = spell.get("attack_type", "attack_roll")
        sp_damage_dice = spell.get("damage_dice", "0d0")
        sp_damage_modifier = spell.get("damage_modifier", 0)
        sp_damage_type = spell.get("damage_type", "")
        sp_save_type = spell.get("save_type", None)
        sp_save_half = spell.get("save_half", True)
        sp_healing = spell.get("healing", False)
        sp_flat_healing = spell.get("flat_healing", False)
        sp_aoe = spell.get("aoe", False)
        sp_cantrip_scaling = spell.get("cantrip_scaling", False)
        sp_higher_levels = spell.get("higher_levels", None)
        sp_level = spell.get("level", 0)
        sp_no_damage = spell.get("no_damage", False)
        sp_instant_kill = spell.get("instant_kill", False)
        sp_instant_kill_threshold = spell.get("instant_kill_hp_threshold", 0)
        sp_damage_on_miss = spell.get("damage_on_miss", None)
        sp_extra_damage_dice = spell.get("extra_damage_dice", None)
        sp_extra_damage_type = spell.get("extra_damage_type", None)
        sp_hp_pool = spell.get("hp_pool", False)
        sp_condition = spell.get("condition", None)
        sp_condition_duration = spell.get("condition_duration", None)
        sp_requires_concentration = spell.get("requires_concentration", False)
        sp_duration = spell.get("duration", "Instantaneous")
        sp_buffs = json.loads(json.dumps(spell["buffs"])) if spell.get("buffs") else None
        sp_temp_hp = spell.get("temporary_hp", False)
        sp_repeat = spell.get("repeat_save", None)
        sp_condition_on_hit = bool(spell.get("apply_condition_on_hit", False))
        sp_post_hit_save = spell.get("post_hit_save", None)
        sp_variants = spell.get("eyebite_variants", None)
        # A condition that ends when its holder takes damage (Sleep's Unconscious, Eyebite's
        # Asleep). The per-cast variant below can also set this.
        sp_condition_ends_on_damage = bool(spell.get("ends_on_damage", False))
    else:
        if attack_type is None or damage_dice is None:
            return {
                "success": False,
                "error": f"Spell '{spell_name}' not found in spells database.",
                "hint": "Provide attack_type and damage_dice to cast a custom spell, or use one of the known spells.",
                "custom_params_required": ["attack_type", "damage_dice"],
                "custom_params_optional": [
                    "save_type", "save_half", "damage_modifier", "damage_type",
                    "cantrip_scaling", "higher_levels", "healing", "aoe",
                ],
            }
        sp_attack_type = attack_type.lower().strip()
        sp_damage_dice = damage_dice
        sp_damage_modifier = damage_modifier
        sp_damage_type = damage_type or ""
        sp_save_type = save_type
        sp_save_half = save_half
        sp_healing = healing
        sp_aoe = aoe
        sp_cantrip_scaling = cantrip_scaling
        sp_higher_levels = higher_levels
        sp_level = slot_level if slot_level is not None else 0
        sp_no_damage = False
        sp_instant_kill = False
        sp_instant_kill_threshold = 0
        sp_damage_on_miss = None
        sp_extra_damage_dice = None
        sp_extra_damage_type = None
        sp_hp_pool = False
        sp_condition = None
        sp_condition_duration = None
        sp_requires_concentration = False
        sp_duration = "Instantaneous"
        sp_buffs = None
        sp_temp_hp = False
        sp_repeat = None
        sp_condition_on_hit = False
        sp_post_hit_save = None
        sp_variants = None
        sp_condition_ends_on_damage = False

    # SRD 5.1: a spell attack rolls the caster's attack bonus. An NPC caster that is not a
    # registered combatant and supplied no bonus has none the engine can trust: refuse rather
    # than roll its spell attacks at a fabricated +0 that flips every hit.
    if (not is_player_caster and sp_attack_type == "attack_roll"
            and not attack_modifier_supplied and _registry_entry(actor) is None):
        return {
            "success": False,
            "error": "spell_attack_modifier_required",
            "spell_name": spell_name,
            "reason": f"{actor} is not a registered combatant and no spell_attack_modifier was "
                      f"given, so {spell_name}'s spell attack bonus is unknown.",
            "gm_instruction": "Register the caster with its spellcasting block, or pass "
                              "spell_attack_modifier, then cast the spell again.",
        }

    # ── SPELL KNOWLEDGE (the player casts only what the character has) ──
    # A player can only cast a cantrip from `cantrips`, or a leveled spell from
    # `spells_prepared` / `spells_known` / `always_prepared` (a wizard may also use the
    # `spellbook` when casting as a ritual). Matched case-insensitively. A miss casts nothing
    # and SPENDS NO ACTION -- the engine hands the GM what the character CAN cast, so the
    # player can choose again (SRD: an unprepared spell simply is not cast).
    if is_player_caster and cursor is not None and not is_scroll and not maintain_spell:
        sc_now = _db_val(cursor, "spellcasting", {}) or {}
        # Only a *configured* caster is gated: a sheet with no `spellcasting` block at all has
        # no list to check against (an unconfigured caster, a magic item, the GM's word).
        if isinstance(sc_now, dict) and sc_now:
            known = _castable_spells(cursor)
            if not _player_knows_spell(known, spell_key, sp_level, ritual=ritual):
                refusal = {
                    "success": False,
                    "error": f"{spell_name} is not prepared or known by {actor}.",
                    "spell_name": spell_name,
                    "known_cantrips": known["cantrips"],
                    "known_spells": known["spells"],
                    "gm_instruction": "The cast did not happen and no action is spent -- offer "
                                      "the character's real options from the lists above.",
                }
                if known["ritual_spells"]:
                    refusal["ritual_spells"] = known["ritual_spells"]
                    refusal["gm_instruction"] += " A spellbook spell may be cast as a ritual."
                return refusal

    # ── CONFIG WINS + WARN ──
    # A spell in config/spells.yml is authoritative. When the GM explicitly passes a
    # resolution-critical field that contradicts the config, the config is used and the
    # conflict is reported (never silently obeyed) so a wrong data row surfaces loudly.
    config_conflicts = {}
    if spell:
        for field, provided, actual in (
            ("attack_type", attack_type, sp_attack_type),
            ("save_type", save_type, sp_save_type),
            ("damage_dice", damage_dice, sp_damage_dice),
            ("damage_type", damage_type, sp_damage_type),
            ("higher_levels", higher_levels, sp_higher_levels),
        ):
            if provided is None:
                continue
            p = str(provided).strip().lower()
            a = str(actual).strip().lower() if actual is not None else ""
            if p and p != a:
                config_conflicts[field] = {"provided": provided, "config": actual}
        if damage_modifier and damage_modifier != sp_damage_modifier:
            config_conflicts["damage_modifier"] = {"provided": damage_modifier,
                                                   "config": sp_damage_modifier}

    # ── PER-CAST VARIANT (Eyebite) ──
    # A variant spell (Eyebite) chooses its effect at cast time; only the named variant's
    # condition (and any repeating save) is applied. Omitted: the generic top-level condition.
    if sp_variants and eyebite_effect:
        variant = sp_variants.get(str(eyebite_effect).strip().lower())
        if variant:
            if variant.get("condition"):
                sp_condition = variant["condition"]
            if variant.get("condition_duration"):
                sp_condition_duration = variant["condition_duration"]
            if variant.get("repeat_save"):
                sp_repeat = variant["repeat_save"]
            if variant.get("ends_on_damage"):
                sp_condition_ends_on_damage = True

    # ── MAINTAINING AN ACTIVE SPELL (Eyebite's per-turn re-target) ──
    # Re-targets the player's own active concentration spell: no slot, and a creature that
    # already saved against this casting is refused.
    if maintain_spell:
        if not is_player_caster or cursor is None:
            return {"success": False, "error": "maintain_requires_player_caster",
                    "hint": "maintain_spell re-targets the player's own active concentration spell."}
        active_spell = str((_get_concentration(cursor) or {}).get("spell") or "")
        if active_spell.strip().lower() != spell_name.strip().lower():
            return {"success": False, "error": "not_concentrating", "active": active_spell,
                    "hint": f"Not concentrating on {spell_name}; cast it first."}
        already = list(((_registry_entry(actor) or {}).get("spell_saved_targets") or {})
                       .get(spell_name) or [])
        requested = [t.get("name") for t in (targets or [])
                     if isinstance(t, dict) and t.get("name")]
        if target_name:
            requested.append(target_name)
        blocked = [n for n in requested if n in already]
        if blocked:
            return {"success": False, "error": "already_saved_target", "targets": blocked,
                    "hint": "This casting cannot target a creature that already saved against it."}

    # ── DUPLICATE ACTIVE EFFECT CHECK ──
    if sp_buffs and state.DB_CONNECTION is not None:
        buff_data_raw = _db_val(cursor, "_active_buff_data", {})
        if isinstance(buff_data_raw, str):
            buff_data_raw = json.loads(buff_data_raw)
        lookup_entries = {k.lower(): k for k in buff_data_raw}
        if spell_key in lookup_entries:
            return _action_failure(
                f"{spell_name} is already active.",
                f"{actor} {spell_name} — already active, not recast",
                spell_name=spell_name,
                hint=(
                    f"Remove it first via update_player_list(key='active_effects', "
                    f"item='{lookup_entries[spell_key]}', action='remove') before recasting."
                ),
            )

    # ── SPELLBOOK CHECK ──
    # A caster whose spell list lives in a spellbook (wizard-style) cannot cast
    # leveled spells while the book is missing from the inventory. Cantrips are
    # memorised and keep working; scrolls carry their own magic.
    if (state.DB_CONNECTION is not None and cursor is not None
            and not is_npc_attack and not is_npc_vs_npc and not is_scroll
            and sp_level > 0 and _needs_spellbook(cursor) and not _has_spellbook_item(cursor)):
        return _action_failure(
            f"Cannot cast {spell_name}: the spellbook is missing.",
            f"{actor} {spell_name} — not cast: the spellbook is missing",
            spell_name=spell_name,
            hint="The character's spellbook is not in their inventory. "
                 "Recover it before casting leveled spells.",
        )
    cast = CastState(
        spell_name=spell_name,
        actor=actor,
        spell_save_dc=spell_save_dc,
        spell_attack_modifier=spell_attack_modifier,
        target_ac=target_ac,
        target_name=target_name,
        slot_level=slot_level,
        is_scroll=is_scroll,
        maintain_spell=maintain_spell,
        ritual=ritual,
        is_npc_attack=is_npc_attack,
        is_npc_vs_npc=is_npc_vs_npc,
        targets=targets,
        cursor=cursor,
        spell=spell,
        enc_disadvantage=enc_disadvantage,
        encumbrance_sources=encumbrance_sources,
        config_conflicts=config_conflicts,
        sp_attack_type=sp_attack_type,
        sp_damage_dice=sp_damage_dice,
        sp_damage_modifier=sp_damage_modifier,
        sp_damage_type=sp_damage_type,
        sp_save_type=sp_save_type,
        sp_save_half=sp_save_half,
        sp_healing=sp_healing,
        sp_cantrip_scaling=sp_cantrip_scaling,
        sp_higher_levels=sp_higher_levels,
        sp_level=sp_level,
        sp_no_damage=sp_no_damage,
        sp_instant_kill=sp_instant_kill,
        sp_instant_kill_threshold=sp_instant_kill_threshold,
        sp_damage_on_miss=sp_damage_on_miss,
        sp_extra_damage_dice=sp_extra_damage_dice,
        sp_extra_damage_type=sp_extra_damage_type,
        sp_hp_pool=sp_hp_pool,
        sp_condition=sp_condition,
        sp_condition_duration=sp_condition_duration,
        sp_requires_concentration=sp_requires_concentration,
        sp_duration=sp_duration,
        sp_buffs=sp_buffs,
        sp_temp_hp=sp_temp_hp,
        sp_repeat=sp_repeat,
        sp_condition_on_hit=sp_condition_on_hit,
        sp_post_hit_save=sp_post_hit_save,
        sp_condition_ends_on_damage=sp_condition_ends_on_damage,
    )
    if spell:
        cast.sp_flat_healing = sp_flat_healing
    return cast
