# TEMP patch payload for the Step 4b smaller-docstring pass. Delete after use.

PATCH = {}

PATCH["equip_item"] = """
Equip or unequip something the character is carrying.

WHEN: worn/wielded gear changes. The item must already be in the inventory (use update_player_list first) -- this tool only moves it.
FIELDS:
- item: the inventory name exactly.
- action: 'equip' (default) | 'unequip'.
- slot: 'main_hand' | 'off_hand' | 'armor' | 'worn'. Omit to let the engine choose (armour -> armour slot; anything else -> a free hand). Cloaks, boots, gloves, gauntlets, bracers, headwear, rings and amulets go to 'worn'; clothing is worn automatically.
- replace: True stows whatever occupied the slot/hand instead of refusing (the stowed item stays in the inventory).
- instant: allow an armour change in combat (otherwise refused; use only for magic or a GM call). Shields take one action and are always allowed.

RULES:
- 5e has NO equipment slots -- only two hands and one suit of armour. A shield is held in a hand; a two-handed weapon needs BOTH hands (equip it only with the other hand free, or replace=True).
- Equipping recomputes AC from the equipped set and returns before->after + time_cost. Only one suit of armour and one shield benefit a creature.
- Unequipping never drops the item.
- The result carries the derived 'equipment' block (hands, hands_free, base_ac, ac_breakdown, warnings).
- The in-combat refusal checks the registry: hostiles all dead = no longer in combat.

EX:
equip_item(item='Chain Mail')
equip_item(item='Greatsword', replace=True)
"""

PATCH["update_combatant"] = """
Change a registered combatant mid-fight: conditions, exhaustion, and (NPC) HP/defence.

WHEN: a condition the fiction causes (a shove -> prone), a condition ends, a correction is needed, or an NPC's HP/AC changes.
FIELDS:
- name: the combatant exactly as registered.
- conditions_add / conditions_remove: adding an immune condition is refused (in 'blocked_conditions'); a condition a TOOL already applied returns 'already_present' and changes nothing.
- exhaustion_delta: add/remove exhaustion levels 0-6 (player: updates the sheet and re-derives max HP; NPC: updates the registry entry).
- hp_delta: (NPCs only) signed, clamped to [0, max_hp], sets 'killed' at 0. max_hp / ac: (NPCs only) correct the declared values.

RULES:
- Conditions drive the engine automatically (advantage/disadvantage, auto-failed saves, melee crits vs helpless, petrified resists all damage).
- The player's conditions live on their sheet; use modify_player_numeric for player HP.

EX:
update_combatant(name='Goblin', conditions_add=['prone'])
update_combatant(name='{player_name}', exhaustion_delta=1)
"""

PATCH["modify_player_numeric"] = """
Add to (or subtract from) a numeric player field, by dotted path.

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

PATCH["rest"] = """
Apply a short or long rest; all numeric changes are auto-applied.

WHEN: the player rests.
FIELDS:
- rest_type: 'short' | 'long'.
- prepared_spells: (long rest only) full replacement list of prepared spell names; validated against capacity (Wizards against the spellbook).

RULES:
- Short rest: auto-spends hit dice until HP is full or none remain. Warlocks restore Pact Magic; Wizards auto-apply Arcane Recovery (ceil(level/2) combined slot levels, lowest first, not 6th+).
- Long rest: full HP, regain max(level//2, 1) hit dice (capped at level), all slots restored, active effects cleared with deltas reverted, one exhaustion level removed.
- Long rest is rejected at 0 HP.
- Returns hints for class features needing manual recharge.

EX:
rest(rest_type='short')
rest(rest_type='long', prepared_spells=['Magic Missile', 'Shield', 'Mage Armor', 'Burning Hands'])
"""

PATCH["register_npcs"] = """
Declare recurring storyline NPCs so the illustrator draws them identically every time.

WHEN: a recurring character first appears.
FIELDS:
- npcs: list of {name, description}. name = the exact name you will keep using (a person's name or a stable handle like 'the harbourmaster'). description = the stable PHYSICAL look only (gender, build, distinguishing features, clothing/role-defining gear) -- never a pose, a position or a current action, and never a second person.

RULES:
- Declare each recurring character ONCE; afterwards refer by NAME ONLY and never repeat the description.
- Re-declaring a name updates its description.
- Use request_scene_image's `npcs` field instead when you also request the illustration in the same call.
"""

PATCH["attune_item"] = """
Attune to (or break attunement with) a magic item.

WHEN: after a rest, for an item whose magic needs attunement.
FIELDS:
- item: the inventory name exactly.
- action: 'attune' (default) | 'unattune'.
- instant: allow it in combat (otherwise refused -- attuning normally takes a short rest).

RULES:
- At most THREE attuned magic items, at most one copy of each. Fails if the item's declared `attunement_by` prerequisite (class, spellcaster, creature type, alignment) is unmet.
- Only items declared attunement=True can be attuned; their ac_bonus/attack_bonus/damage_bonus apply only while attuned (before that the 'equipment' block warns 'not_attuned').
- The item must be in the inventory first.

EX:
attune_item(item='Voidmail')
"""

PATCH["modify_exhaustion"] = """
Add or remove exhaustion levels on the player (0-6).

WHEN: exertion, a hazard or magic causes or clears exhaustion.
FIELDS:
- delta: signed number of levels. reason: an optional short note.

RULES:
- The engine applies the full level table and re-derives max HP, speed and roll disadvantage; never hand-apply the penalties.
- A long rest removes one level automatically; use this tool for every other source.
- Level 6 is death.

EX:
modify_exhaustion(delta=1, reason='forced march')
"""

PATCH["make_death_save"] = """
Roll a death saving throw for a player at 0 HP.

WHEN: the start of the player's turn while at 0 HP.
RULES:
- Three successes stabilise (counters clear, still 0 HP); three failures kill. A natural 20 regains 1 HP and clears the counters; a natural 1 counts as two failures.
- Healing any real HP clears the counters, and damage at 0 HP adds a failure automatically -- so call this only at the start of the turn, at 0 HP.

EX:
make_death_save()
"""

PATCH["roll_dice"] = """
Roll dice for damage, healing, loot quantity, or any random magnitude.

WHEN: a pure magnitude roll. For success/failure use perform_check.
FIELDS:
- dice_notation: dice only (e.g. '3d4') -- no modifiers.
- modifier: flat bonus/penalty on the total. actor: who rolls.

EX:
roll_dice(actor='Senna', dice_notation='3d4', modifier=3)
"""

PATCH["dump_player_db"] = """
Full dump of the in-memory player database.

WHEN: a state refresh is needed (e.g. at awakening).
NOTE: the derived `_carrying` (capacity/encumbrance/speed), `_equipment` (worn armour, hands, AC breakdown) and `_passive` (passive Perception/Investigation/Insight = 10 + modifier) blocks are computed, never saved.
"""
