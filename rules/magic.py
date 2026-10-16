"""The `resolve_magic` MCP tool: spell resolution, from a cast to its effects.

The three phases live beside it: `magic_cast` (the caster, the gates, the spell's own
facts), `magic_slots` (the slot, the upcast, the result scaffold) and `magic_resolve`
(delivery: attacks, saves, damage, healing, HP, kills, XP).
"""

from .app import mcp
from .magic_cast import prepare_cast
from .magic_resolve import resolve_spell_effects
from .magic_slots import prepare_slot

@mcp.tool()
def resolve_magic(
    spell_name: str,
    actor: str = "{player_name}",
    spell_attack_modifier: int | None = None,
    spell_save_dc: int | None = None,
    target_ac: int | None = None,
    target_name: str = "",
    target_current_hp: int | None = None,
    target_max_hp: int | None = None,
    challenge_rating: float | None = None,
    target_save_modifier: int | None = None,
    player_situational_modifier: int | None = None,
    slot_level: int | None = None,
    is_npc_attack: bool = False,
    is_scroll: bool = False,
    maintain_spell: bool = False,
    attack_type: str | None = None,
    save_type: str | None = None,
    save_half: bool = True,
    damage_dice: str | None = None,
    damage_modifier: int = 0,
    damage_type: str | None = None,
    cantrip_scaling: bool = False,
    higher_levels: str | None = None,
    boost_damage_type: str | None = None,
    delayed_rounds: int | None = None,
    eyebite_effect: str | None = None,
    healing: bool = False,
    aoe: bool = False,
    ritual: bool = False,
    is_npc_vs_npc: bool = False,
    caster_level: int | None = None,
    advantage: bool = False,
    force_crit: bool = False,
    targets: list[dict] | None = None,
    components: str | None = None,
) -> dict:
    """Resolve a full spell: slot, attack/save, damage/heal, HP, kills, XP.

    WHEN: any spell is cast. Weapon attacks -> resolve_attack; checks -> perform_check.
    FIELDS:
    - spell_name: from config/spells.yml; a custom spell also needs attack_type + damage_dice.
    - actor: the caster's name (player name, or an NPC name).
    - attack_type: 'attack_roll' | 'saving_throw' | 'automatic'. save_type: the save ability. save_half: half damage on a successful save (default True). For a spell in the database the config wins; a contradicting field you pass is reported in `config_warning`.
    - spell_attack_modifier / spell_save_dc: omit for a registered NPC caster (uses its `spellcasting` block); an unregistered NPC caster of an attack-roll spell that supplies no spell_attack_modifier is REFUSED ('spell_attack_modifier_required') — never rolled at a guessed +0.
    - target_ac: omit for a registry target (uses its AC). target_name/target_current_hp/target_max_hp: registry lookup; pass HP for a wounded target (a healing ceiling comes from the registry or the sheet). A save target passed no current_hp/save_modifier that is not registered is REFUSED ('save_target_not_registered') — never resolved against a guessed 0 HP / +0 save.
    - challenge_rating: CR for XP (auto-looked up if omitted). target_save_modifier: save bonus for a single-target save (omit for a registry target). player_situational_modifier: situational add-on to the player's save when is_npc_attack=True (never pass the base modifier).
    - slot_level: upcast level (defaults to the spell's level). is_scroll: no slot consumed, above-level scrolls need a check. maintain_spell: True re-targets the player's own active concentration spell (Eyebite) -- no slot, and a creature that already saved against this casting is refused. is_npc_attack: NPC->player, damage auto-applied, no slot. is_npc_vs_npc: NPC->NPC, no player HP, no auto XP. caster_level: for NPC-vs-NPC cantrip scaling.
    - damage_dice/damage_modifier/damage_type: custom damage (needed for unknown spells). cantrip_scaling: auto-scale at 5/11/17. higher_levels: upcast string (e.g. '+1d6'). healing: heals instead. aoe: area. ritual: no slot. advantage/force_crit: attack_roll only. components: override (else config/components.yml; unknown = V,S,M). boost_damage_type: which component of Flame Strike the upcast empowers. delayed_rounds: rounds a Delayed Blast Fireball waited. eyebite_effect: Eyebite's chosen effect — asleep, panicked, or sickened (sickened repeats its save).
    - targets: multi-target list. Shapes by type -- HP pool {name, current_hp}; save {name} (HP/save/CR auto from the registry) or {name, current_hp, save_modifier, challenge_rating} (add {is_player: True} for the player); projectiles {name, darts} (darts must total the count; omit to send them all at one target); attack_roll = one attack per target; automatic = names only (the spell has no per-target roll, so the list only names whom it touched).

    RULES: how it resolves -- slot consumption, AoE / HP-pool / multi-projectile order, resistance and temp-HP order, components, and the healing cap: lookup('ref:resolve_magic').

    EX:
    resolve_magic(spell_name='Fireball', actor='{player_name}', spell_save_dc=15, targets=[{"name":"Goblin","current_hp":7,"save_modifier":2,"challenge_rating":0.25}])
    resolve_magic(spell_name='Fire Bolt', actor='{player_name}', spell_attack_modifier=6, target_ac=14, target_name='Orc', target_current_hp=18, challenge_rating=0.5)
    """
    cast = prepare_cast(
        spell_name=spell_name,
        actor=actor,
        spell_attack_modifier=spell_attack_modifier,
        spell_save_dc=spell_save_dc,
        target_ac=target_ac,
        target_name=target_name,
        slot_level=slot_level,
        is_npc_attack=is_npc_attack,
        is_scroll=is_scroll,
        maintain_spell=maintain_spell,
        attack_type=attack_type,
        save_type=save_type,
        save_half=save_half,
        damage_dice=damage_dice,
        damage_modifier=damage_modifier,
        damage_type=damage_type,
        cantrip_scaling=cantrip_scaling,
        higher_levels=higher_levels,
        eyebite_effect=eyebite_effect,
        healing=healing,
        aoe=aoe,
        ritual=ritual,
        is_npc_vs_npc=is_npc_vs_npc,
        targets=targets,
        components=components,
    )
    if isinstance(cast, dict):
        return cast

    refused = prepare_slot(cast, boost_damage_type=boost_damage_type,
                           delayed_rounds=delayed_rounds, caster_level=caster_level)
    if refused is not None:
        return refused

    return resolve_spell_effects(
        cast,
        advantage=advantage,
        force_crit=force_crit,
        target_current_hp=target_current_hp,
        target_max_hp=target_max_hp,
        challenge_rating=challenge_rating,
        target_save_modifier=target_save_modifier,
        player_situational_modifier=player_situational_modifier,
    )
