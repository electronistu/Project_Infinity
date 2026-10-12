# How It Works

The technical half of Project Infinity. For what the game is and how to play it, see the [README](README.md).

Most AI RPGs let the language model make up numbers. Project Infinity runs every dice roll, stat change and combat action through an external game engine the AI can read but not invent.

- **Real Rules, Real Dice** — every attack, save, spell, crit, kill and XP award is resolved by the engine in one call; the GM sees the results and cannot generate them.
- **A Persistent World** — stats, inventory, gold, spell slots, equipped gear and reputation live in a real database; standing is kept per power inside the age it was earned in, and saving records a session timeline so a long campaign stays coherent.
- **What You Wear Matters** — armour class is derived from the equipped set (one suit of armour, two hands, one shield); attunement limits magic items; donning/doffing and hand requirements are enforced; carrying capacity uses the SRD variant encumbrance rules.
- **Spell & Item Effects** — damage resistance, immunity and vulnerability and condition immunity from worn items **and** active spells are applied automatically when the player takes damage; typed active effects (resistances, spell dice, speed, HP-per-level) are tracked and revert cleanly when they end.
- **NPCs With Stat Blocks** — the GM registers each combatant once with HP, AC, per-ability saves, declared attacks and resistances; the engine resolves their attacks, damage-type math and conditions.
- **Multi-Target AoE** — Fireball, Sleep and area spells resolve every target in one call: per-target saves, HP-pool exhaustion, one slot consumed.
- **Conditions** — blinded, grappled, incapacitated, prone, restrained, poisoned, frightened, invisible, paralyzed, petrified, stunned and unconscious drive advantage, forced criticals and failed saves. Exhaustion is tracked as a level 0–6 with its full SRD table (checks, speed, attacks/saves, halved HP maximum, death). A condition a spell applies is written onto the combatant automatically; the GM only removes it when it ends. Every `buffs` field an active spell declares is consumed by the engine — Haste's speed and AC, Foresight's advantages, Mind Blank's psychic immunity, Pass Without Trace's Stealth bonus, Beacon of Hope's saves, Resistance's one-shot die — not stored as a passive note.
- **Refused Actions** — attacking with a weapon not in hand, casting with both hands busy, or casting in non-proficient armour is refused and the action is spent. No do-overs.
- **Rest & Leveling** — short and long rests and XP thresholds auto-apply hit dice, slots, Arcane Recovery, HP, proficiency and progression per SRD 5.1.

Under the hood:

- **The engine is authoritative.** A local **MCP server** with an in-memory SQLite database, initialised from your `.player`. Every action is a verified tool call; the GM's protocol lives in [`GameMaster_MCP.md`](GameMaster_MCP.md), and each engine tool's contract is its own description (surfaced from `dice_server.py`).
- **Dice & checks** — `perform_check` (`d20 + modifier` vs a DC, natural-20/1 criticals) and `roll_dice` (any notation, e.g. `3d6+2`). Player ability/skill checks are engine-derived (ability + skill proficiency, doubled for Expertise or halved for Jack of All Trades, + item bonuses), and passive Perception/Investigation/Insight (10 + modifier) are exposed on the sheet.
- **State authority** — `modify_player_numeric` and `update_player_list` own all state; HP clamps to `[0, max]`; dropping to 0 starts death saves (`make_death_save`), massive leftover damage is instant death, and healing any real HP clears the counters.
- **Exhaustion & concentration** — `modify_exhaustion` tracks the SRD level 0–6 and the engine applies the whole table (checks, speed, attacks/saves, halved HP maximum, death); a long rest removes one level. The engine also tracks the player's concentration spell and rolls the CON save (DC 10 or half the damage) when damage lands, ending the spell on a failure. The sheet's **Condition** card shows the exhaustion level, the active concentration spell and the death-save counters.
- **Combat authority** — `register_combatants` declares a full stat block once; `resolve_attack` and `resolve_magic` derive AC, saves, damage types and conditions from it; `update_combatant` adjusts the registry mid-fight; `end_combat` closes one the fiction has ended (the engine also ends it on its own once every hostile is killed, fled or surrendered). A declared **companion** is registered by name alone, its sheet filled in by the engine, and its checks and saves are derived from it exactly as the player's are. **The registry is fight state and is emptied the moment a fight ends**, so nothing about a combatant — a companion's hit points above all — can carry into the next one.
- **Phased resolution** — the GM resolves all mechanics first (pausing between the two phases on a system handshake), then narrates, so results are mechanically correct before the story is told.

---

## A turn, mechanically

1. **The mechanics phase.** The GM emits its tool calls in one batch — dice, attacks, spells, state changes. The engine runs each one, and hands back only what the GM does not already hold: the **delta** of a state change, the **exact mechanics** of a roll or attack, and any **error**. Redundant snapshots (inventory, carrying, equipment) are not re-sent; a value the sheet omits is one `read_player_field(fields=[...])` away. The **client keeps the complete, untrimmed result**, shown behind the **Tools** toggle.
2. **The pause handshake.** After a batch the GM emits a pause token and waits. The engine resumes it, and the GM's next response is the narrative.
3. **The narrative phase.** The GM writes the prose. It never writes mechanics, a heading or a placeholder.
4. **The Mechanics panel** is **composed by the engine** from the tool results and appended at the end of the narrative. Every line names who acted on whom (`Actor casts Spell → Target`, `Actor Attack → Target`), shows a green, hoverable **Initiative Order** (tooltip = the order) and each NPC's live sheet as a hover tooltip on their name. The GM narrates the outcomes, emphasizes critical results (a crit, near-death HP, a condition), and explains rules when the player needs them.
5. **The engine guarantees the beat.** If a turn reaches the end with no prose — the GM echoed the pause token, attached the illustration alone, or emitted an empty response — the engine takes one non-quiet recovery round and asks for it. The guarantee is driven by whether narrative text actually reached you, never by what the model claimed.

---

## What the Game Master sees

**The character sheet is engine-injected, and the engine decides what a caster knows.** Each session builds a lean **CHARACTER SHEET** block from the live database — identity, hit points, gold, speed with its encumbrance, conditions, features, the pack by name, what is worn and held, consumables, the era-scoped **reputation** shape to write against, and the **companions** of the party with their stable look — and re-injects it after every Device jump. It carries exactly what the Game Master narrates from and the keys it addresses a write with; the combat math it must never invent (ability scores, saves, skills, proficiencies, AC, hit dice) and the spell lists are absent, because the engine owns them. So a caster may only cast a spell the character has: a cantrip from the cantrip list, a leveled spell that is prepared or known, a **domain / oath / circle** spell (always prepared, and never counted against the number that can be prepared), or a spellbook spell cast as a **ritual**; a scroll carries its own. A miss casts nothing and spends no action — the engine returns the character's real options, and the Game Master offers them.

**A session loads, in order:** the **GM protocol** ([`GameMaster_MCP.md`](GameMaster_MCP.md)); the **ERA INDEX** — the era ladder, one line each with its exact year (the era you are in, and the others it could throw you into); the save's **session memory** — the **STATE page** (the current goal, live hooks, clocks, debts, secrets, canon and unpaid foreshadowing; in the era game it is the page of the age you are standing in) followed by **EVENTS** (the compacted chronicle plus the recent entries kept verbatim); and the **known places** — a `kingdom → area → place path` tree (realm → settlement → district → spot → any nested rooms, any depth) with each place's **main NPC names** (e.g. *The Drowned Lantern → Common Room · NPCs: Maera, the grandson*). The tree is **scoped to the current era**, **capped at the 12 most recently used places**, and deliberately **names-only** — place descriptions stay in the save. An NPC's **identity** — race, class and role — does not live in the tree: it comes back with the character itself, because whenever one walks on stage the result reminds the GM of it (`on stage: Maera (dwarf rogue, the innkeeper)`). With images off the tree comes from the places the GM declares with `note_place` instead of from the illustrator's manifest; the engine keeps the same registry either way. A main NPC the Traveller has **killed** is fed back to the GM **marked dead** -- *`Maera (dead)`* -- on her own place's line only. The engine commits the death when the age ends or the game is saved, so a reload or a return to that age never resurrects her; a heal before that point still saves her. (Recurring storyline characters live in the timeline, not the tree.)

The character is then pulled live from the database — stats, HP, inventory, equipped gear and the carrying/encumbrance block. The current era's scaffold ([`config/eras/`](config/eras/)) arrives with the awakening, and only **that** era's history and polities are ever in play.

The static prefix — the tool schemas plus the protocol — is the same on every turn, so it is measured and gated (see **Development** below). Nothing about the mode you are not playing is in it.

---

## The state model

A save is two files in `output/`, plus its images:

| File | Holds |
| --- | --- |
| `{name}.player` | The character: abilities, HP, hit dice, inventory and equipped set, spellbook and slots, conditions, gold, and reputation |
| `{name}.timeline` | The session memory the GM writes when you save: the event entries, the **state pages**, the compacted **chronicle** of older events, and the era legends |
| `output/images/{name}/` | Portraits, place seeds and cached illustrations for that save |

**Saving is in place** — a save rewrites its own files and never renames them, because its images are keyed to its name. The engine's database is in-memory and seeded from the `.player` at session start, so the file is the record and the running session is authoritative for the turn in front of you.

**Reputation is scoped.** In the era game it is stored per age — `{era: {polity: {faction: [entries]}}}` — so standing earned in Egypt is never offered in Wallachia and never lost when you leave; the GM still writes the short path and the engine resolves the age. In the classic game it is a flat map keyed by kingdom. Each age's memory of you lives in the timeline as a one-line **legend**, written the first time that age sees you vanish.

**The state notebook is scoped too, in the era game.** An age is a perspective, so the timeline holds **one page per age** with the seven fields that belong to that age (goal, hooks, clocks, debts, secrets, canon and unpaid foreshadowing), and only the age the Traveller is standing in is ever injected — the ages you are not in cost nothing. `Standing` is gone: a power's attitude is the engine's own era-scoped **reputation**, a name is the place tree's, and anything left is a hook, a debt or canon. Companions are not written by the Game Master either: the engine lists them in the injected character sheet (see below). A page is rewritten on Save, and an age's page is also rewritten on the **closing turn** as you leave it, because once an age is left no save can ever happen there again. The engine checks a page's field names and order when it is written and again when it is read — it drops what another store already owns (the era, the year, the place and its regulars, reputation, standing, the companions, the Device) and keeps the Game Master's prose untouched — and a page that loses its `Goal` keeps the one already held, or is asked for once. The classic game has no ages and keeps a single notebook. A save made before the pages were scoped has one unkeyed block: the engine reads it as the age the Traveller is in and rewrites it keyed on the next save.

**Every age is dated to one exact year** (`meta.when`, e.g. Wallachia `1460`, Egypt `2560 BC`), shown in the always-on ERA INDEX (`- wallachia (1460) -- …`). A period would let the Game Master drift and would invite the notebook to invent its own year.

**A companion is a combat sheet, not a prompt block.** A companion is a full character marked `ally`, at most three in the party. The Device carries your companions with you, so the party crosses the ages; in the classic game the four kingdoms are one world, so it crosses them too. The Game Master declares the sheet once with `register_companion` — its **ability scores are required**, plus AC, hit points, attacks, `advantages` (combat-only rules like *Sneak Attack 2d6*) and any resistances or immunities, plus the same stable physical look a recurring NPC gets, so the illustrator keeps the face — and from then on registers it in a fight **by name alone**. The engine fills the block from its store and hands the filled line back with the registration; its **name**, identity and look are in the injected **CHARACTER SHEET** (re-injected after every jump) — the sheet itself never enters the prompt as combat data. **No current HP is stored**: a fight states it, full by default, so a rest never has to heal a companion and a wound can never leak into the next fight. Because it has ability scores, the engine also rolls **its own checks and saves** (`perform_check(actor="…")`) from the sheet instead of trusting a number the Game Master computed. At three companions in the party, a fourth is refused until the player decides who leaves.

---

## Two modes, one engine

The mode is chosen at creation, stored on the save, and hidden from the GM.

| | Classic | Time Traveler |
| --- | --- | --- |
| World | A fixed invented realm — the political scaffold is injected at awakening | A ladder of ages — four historical, plus New York, the Traveller's own — one file each in `config/eras/` |
| Where you start | The one world | **Rolled** from the playable ages — but never New York, the Traveller's own. Later ages are yours to choose |
| Reputation | Flat, per kingdom | Per age, then per polity and faction |
| The Device | Player-controlled | Yours to command from the first turn: choose the age and the place (the age's own destinations, or a place already visited), and when. There is no countdown and no forced jump. Its **four parts** are real inventory items and the key to the future — New York is unreachable until every one is held |
| `lookup` tool | Offered — a tool's on-demand field reference (`ref:TOOL`) | Offered — any era's scaffold and factions, plus the same `ref:TOOL` references |

The mode costs nothing when it is not in play: the protocol is marker-gated and rendered per session, and the era index is its own system message. `lookup` is the one generic read-only tool and is offered in **both** games — in classic its era branches simply have no other era to reach.

---

## Storyline image continuity

**Images are recommended.** The place-and-NPC tree the GM sees, and the places a Time Traveler may aim at (each age's authored arrivals plus the last 12 places visited there), are kept in **both modes**: with images on they are read from the scene manifest the illustrator writes; with images off the GM declares each new place once with `note_place` and the engine keeps the same registry (a place with no picture). You choose the age and the place; only the new-character start is random. Images add the portrait and the per-turn illustration.

- **A place has an address** — `kingdom → area → place path` (realm → settlement → district → spot → nested rooms, any depth), named once and reused exactly.
- **Every place gets a hidden "seed"** — an empty, permanent establishing view, never shown to you.
- **Moments are drawn from the seed, never the previous picture** — the action image reproduces the establishing view with you and the cast, so rooms never rearrange and figures can never duplicate. Only the time of day, the weather and the moment's own damage may differ; nothing absent from the seed is invented. Action images are served once and discarded; the seed is the only scene picture a save keeps.
- **Returning and permanent changes** — a revisited place is drawn from the same seed; a lasting change (it burned down) regenerates it.
- **Names in, looks injected** — each recurring NPC is declared once with a **race**, a **class** and a stable **physical** look (no pose, position or action) and the engine injects the look, so a face never drifts. A place's **main NPCs** are declared as a list; the race/class/role **identity** is echoed back to the GM whenever that NPC appears on stage, so continuity costs nothing on the turns nobody is there. Every declared NPC states its race and class — an NPC is never left human by default. Recurring characters are declared as they appear; one-off extras need none.
- **Time, weather, and no posing** — the GM declares time and weather; the seed stays timeless and weather-neutral. Everyone is caught in the action, never facing the camera.
- **Disguises** — while an appearance-changing effect (Disguise Self, Alter Self) is active, the scene shows that appearance instead of your portrait.

### The icon library

Sheet icons are a **shared, cross-character library**, cached under `assets/{family}/{kind}/{slug}.png` — one store and one manifest per **image-model family**, and a family never serves another family's file. The sheet's treatment is chosen on the start screen: **cached icons only; no generation**, **cached icons + generate on new ones**, or **text only**.

---

## Technology

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| Game engine | MCP (Model Context Protocol) server + in-memory SQLite |
| Web server | FastAPI + Uvicorn, streaming over WebSocket |
| Client | Vanilla JS / HTML / CSS (no build step) |
| Data validation | Pydantic |
| Config | YAML |
| World | Two modes: a fixed invented realm (`config/world.yml`) and a ladder of historical eras (`config/eras/`) |
| Game Master models | Ollama Cloud, Google Gemini *(optional)* |
| Images *(recommended)* | Google Gemini image models, cached per save (portraits, icons, place seeds) |

---

## Development

```bash
venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # tiktoken, once
venv\Scripts\python.exe tools\verify.py
```

`tools/verify.py` is the whole gate in one command and exits non-zero on the first failure. Seven steps, in order:

| Step | What it asserts |
| --- | --- |
| `srd` | `tools/srd_gate.py` — every **name** this repository ships is an SRD 5.1 name |
| `spellstats` | `tools/spell_stats_gate.py` — every spell's **numbers** (level, resolution, save, damage, upcast, school, concentration, ritual, healing) match the frozen SRD 5.1 table |
| `spellcov` | `tools/spell_coverage_gate.py` — every spell, `condition` string and `buffs` field is **classified** (a real mechanic or a named GM-adjudicated tag) |
| `budget` | `tools/token_budget.py --gate` — the static-prefix ceilings in `tools/budgets.yml` |
| `dice` | The dice engine suite, `web/tests/dice/` |
| `web` | Every web suite, `web/tests/test_*.py` (no network, no GPU) |
| `node` | `node --check` on `web/static/app.js` |

`--only srd,budget,dice` runs a subset; `--json` gives a machine-readable summary.

**The token budget.** Every turn sends the GM a fixed **static prefix** — the tool schemas plus `GameMaster_MCP.md` — which is the game's largest recurring cost, so each ceiling is a written decision in `tools/budgets.yml` and a breach fails the build. `tools/token_budget.py --gate` runs the budget check alone, and **refuses to run** without `tiktoken` rather than gate on an estimate. `tools/gm_snapshot.py` captures and diffs what the GM is actually sent (`input` / `behavior` / `compare`).

Three levers keep the prefix down. **A declaration the engine already returns is not repeated in an always-on description** — the return teaches it at the moment it matters (a cap is stated by `limit` and by the refusal), so the duplicate would be paid every turn forever for nothing. The long **explanations** — `update_player_list`'s `effects` vocabulary, `resolve_magic`'s resolution rules — live in [`config/tool_refs.yml`](config/tool_refs.yml) and are fetched with `lookup('ref:TOOL')`, paid only on the turns they are actually used; the always-on price is the one-line pointer in the tool description. And the **compacted schema may omit a parameter that is only a duplicate spelling** — `update_player_list`'s flat ability aliases (`set_*` / `*_bonus`) are dropped from the prompt (a curated list in `web/tool_schema.py`) because the visible `effects` field reaches the same capability and the engine normalizes both into one entry; the MCP server still accepts them, so a call that uses the alias still works.

The fourth lever is what the GM gets **back** from a tool. Every GM-offered tool has an explicit projection decision in `web/engine.py` — keep only these keys, drop everything but these, or send it whole with a stated reason — and an unclassified tool fails closed rather than dumping its payload. A coverage test boots the tool list and fails the build for a tool with no decision, and the always-drop keys (the mechanics disclosure, the state snapshots) are stripped at any depth, because a mechanics tool embeds other tools' results. `tool_results_gm_view` is enforced, so this half is measured too.

**Layout.** `dice_server.py` is the engine and the MCP tools; `web/` is the server, the session, the client and the tests; `forge/` is the character creation and the world generation; `config/` is all rules data; `assets/` is the icon library; `tools/` is the gate.

---

## SRD 5.1 conformance

All embedded D&D 5e rules data in `config/` is derived from **SRD 5.1**, and three gates plus an engine smoke suite keep it that way — **names, numbers, and vocabulary**.

**Names — [`tools/srd_gate.py`](tools/srd_gate.py).** `tools/srd/srd-5.1.json` is the whitelist: **names only**, with the provenance of every list recorded in it — which lists were fetched from the published SRD, which are the repository's own reviewed values, and which are an explicit decision to allow original content. The gate checks every name in `config/*.yml`, the Forge's own literals, and every committed icon's name against it. It runs first in the gate, so the claim cannot rot. Where SRD 5.1 renames a spell to avoid a protected character name, the **SRD spelling is what ships**, and the alias is recorded rather than used.

**Numbers — [`tools/spell_stats_gate.py`](tools/spell_stats_gate.py).** `tools/srd/spell-stats.json` is a frozen, offline table of all **319** spells (built once by `tools/srd/build_spell_stats.py`), and the gate checks that `config/spells.yml` agrees on level, resolution (roll vs save), save ability and half-on-save, base damage and type, and per-slot scaling — plus school, concentration, ritual and healing. Reviewed `EXEMPT` maps record every deliberate deviation and the source data's own gaps, so the gate reports real drift, not noise.

**Vocabulary and behaviour — [`tools/spell_coverage_gate.py`](tools/spell_coverage_gate.py) + [`web/tests/dice/test_spell_smoke.py`](web/tests/dice/test_spell_smoke.py).** The coverage gate fails when a spell, a `condition` string or a `buffs` field is not classified — a real engine mechanic, or a named GM-adjudicated tag. The smoke suite runs **every** shipped spell through the real engine once, so no spell is silently unresolvable. Together with the dice suite they prove all 319 spells run and that every declared mechanic is accounted for.

**What the gates cannot check:** the prose inside a description, and whatever the running Game Master invents from a general-purpose model's training. That last limitation is real and is disclosed in the README. The line the engine *does* draw — which mechanics it resolves and which it leaves to the GM — is written down in the next section.

See [`tools/srd/README.md`](tools/srd/README.md) for what each list is and what to do when the name gate fails.

---

## What the engine decides, what the Game Master decides

The engine owns the **mechanical skeleton**: attack rolls, saves, damage, hit points, the thirteen conditions and their generic effects, resistance/immunity/vulnerability, concentration, death saves, exhaustion, upcast scaling, active buffs and recurring saves. Everything below is the Game Master's call by design — not an accident, and not a gap the code is hiding.

- **Condition effects with no generic rule.** 27 condition tags across 31 spells apply a state the engine cannot resolve on its own, so the GM runs the effect and passes `advantage`/`disadvantage` or applies results. Control and removal (`Banished`, `Bound`, `Trapped`, `Possessed`, `Time Stopped`, `Magic Suppressed`, …), transformation (`Polymorphed`, `Gaseous`, `Levitating`), rider debuffs (`Weakened`, `Slowed`, `Outlined`, `Confused`, `Feebleminded`, `Cursed`, `Diseased`) and meta effects (`Spell Interrupted`, `Teleported`, `Altered Memories`, `Illusory Duplicates`, `Varies by symbol type`). A few of these already run a partial engine rule (Slow, Confusion and Contagion run their repeating save; Ray of Enfeeblement runs its post-hit save) — the GM owns only the condition's own effect.
- **Effects with no engine consumer.** Mind Blank's divination immunity, Haste's extra action, Expeditious Retreat's bonus-action Dash, and Jump's tripled distance. Action economy is not tracked at all.
- **Purely narrative spells.** 144 of the 319 resolve as automatic with no roll and no condition or buff — cantrips and utility from Detect Magic to Wish. The engine validates the slot and records the cast; the effect is the fiction.
- **Timing.** There is no clock: spell durations (1 minute / 1 hour / 24 hours / until dispelled), the expiry of a concentration, and the action to shake or slap a sleeping creature awake are all the GM's to end with `update_combatant` / `update_player_list`.
- **Action economy and reactions.** Bonus-action casts, reactions (Shield, Counterspell, Hellish Rebuke) and Haste's extra action. The engine resolves the spell, not whether the actor still had the action to spend.
- **Combat and environment.** Cover (+2 / +5), disadvantage for ranged attacks in melee, the once-per-24-hours long rest, item charges and curses, underwater, mounted, surprise and hiding, and grapple/shove contests.
- **Targeting and interaction.** A charmed or dominated creature's "cannot attack the charmer", illusion disbelief, invisibility against See Invisibility or Truesight, and polymorph or wild-shape stat blocks.

This boundary is **maintained, not merely documented**: [`tools/spell_coverage_gate.py`](tools/spell_coverage_gate.py) fails the build when a new spell, condition string or buffs field is neither a real engine mechanic nor a reviewed, named entry — so the list above cannot silently grow.
