# How It Works

The technical half of Project Infinity. For what the game is and how to play it, see the [README](README.md).

Most AI RPGs let the language model make up numbers. Project Infinity runs every dice roll, stat change and combat action through an external game engine the AI can read but not invent.

- **Real Rules, Real Dice** — every attack, save, spell, crit, kill and XP award is resolved by the engine in one call; the GM sees the results and cannot generate them.
- **A Persistent World** — stats, inventory, gold, spell slots, equipped gear and reputation live in a real database; standing is kept per power inside the age it was earned in, and saving records a session timeline so a long campaign stays coherent.
- **What You Wear Matters** — armour class is derived from the equipped set (one suit of armour, two hands, one shield); attunement limits magic items; donning/doffing and hand requirements are enforced; carrying capacity uses the SRD variant encumbrance rules.
- **Spell & Item Effects** — damage resistance, immunity and vulnerability and condition immunity from worn items **and** active spells are applied automatically when the player takes damage; typed active effects (resistances, spell dice, speed, HP-per-level) are tracked and revert cleanly when they end.
- **NPCs With Stat Blocks** — the GM registers each combatant once with HP, AC, per-ability saves, declared attacks and resistances; the engine resolves their attacks, damage-type math and conditions.
- **Multi-Target AoE** — Fireball, Sleep and area spells resolve every target in one call: per-target saves, HP-pool exhaustion, one slot consumed.
- **Conditions** — blinded, prone, restrained, poisoned, frightened, invisible, paralyzed, petrified, stunned and unconscious drive advantage, forced criticals and failed saves. Exhaustion is tracked as a level 0–6 with its full SRD table (checks, speed, attacks/saves, halved HP maximum, death). A condition a spell applies is written onto the combatant automatically; the GM only removes it when it ends.
- **Refused Actions** — attacking with a weapon not in hand, casting with both hands busy, or casting in non-proficient armour is refused and the action is spent. No do-overs.
- **Rest & Leveling** — short and long rests and XP thresholds auto-apply hit dice, slots, Arcane Recovery, HP, proficiency and progression per SRD 5.1.

Under the hood:

- **The engine is authoritative.** A local **MCP server** with an in-memory SQLite database, initialised from your `.player`. Every action is a verified tool call; the GM's protocol lives in [`GameMaster_MCP.md`](GameMaster_MCP.md), and each engine tool's contract is its own description (surfaced from `dice_server.py`).
- **Dice & checks** — `perform_check` (`d20 + modifier` vs a DC, natural-20/1 criticals) and `roll_dice` (any notation, e.g. `3d6+2`). Player ability/skill checks are engine-derived (ability + skill proficiency, doubled for Expertise or halved for Jack of All Trades, + item bonuses), and passive Perception/Investigation/Insight (10 + modifier) are exposed on the sheet.
- **State authority** — `modify_player_numeric` and `update_player_list` own all state; HP clamps to `[0, max]`; dropping to 0 starts death saves (`make_death_save`), massive leftover damage is instant death, and healing any real HP clears the counters.
- **Exhaustion & concentration** — `modify_exhaustion` tracks the SRD level 0–6 and the engine applies the whole table (checks, speed, attacks/saves, halved HP maximum, death); a long rest removes one level. The engine also tracks the player's concentration spell and rolls the CON save (DC 10 or half the damage) when damage lands, ending the spell on a failure. The sheet's **Condition** card shows the exhaustion level, the active concentration spell and the death-save counters.
- **Combat authority** — `register_combatants` declares a full stat block once; `resolve_attack` and `resolve_magic` derive AC, saves, damage types and conditions from it; `update_combatant` adjusts the registry mid-fight.
- **Phased resolution** — the GM resolves all mechanics first (pausing between the two phases on a system handshake), then narrates, so results are mechanically correct before the story is told.

---

## A turn, mechanically

1. **The mechanics phase.** The GM emits its tool calls in one batch — dice, attacks, spells, state changes. The engine runs each one, and hands back only what the GM does not already hold: the **delta** of a state change, the **exact mechanics** of a roll or attack, and any **error**. Redundant snapshots (inventory, carrying, equipment) are not re-sent; a full sheet is one deliberate `dump_player_db` away. The **client keeps the complete, untrimmed result**, shown behind the **Tools** toggle.
2. **The pause handshake.** After a batch the GM emits a pause token and waits. The engine resumes it, and the GM's next response is the narrative.
3. **The narrative phase.** The GM writes the prose. It never writes mechanics, a heading or a placeholder.
4. **The Mechanics panel** is **composed by the engine** from the tool results and appended at the end of the narrative. Every line names who acted on whom (`Actor → Target`), shows a green, hoverable **Initiative Order** (tooltip = the order) and each NPC's live sheet as a hover tooltip on their name. The GM narrates the outcomes, emphasizes critical results (a crit, near-death HP, a condition), and explains rules when the player needs them.
5. **The engine guarantees the beat.** If a turn reaches the end with no prose — the GM echoed the pause token, attached the illustration alone, or emitted an empty response — the engine takes one non-quiet recovery round and asks for it. The guarantee is driven by whether narrative text actually reached you, never by what the model claimed.

---

## What the Game Master sees

A session loads, in order: the **GM protocol** ([`GameMaster_MCP.md`](GameMaster_MCP.md)); the **ERA INDEX** — the era ladder, one line each (the era you are in, and the others it could throw you into); the save's **timeline**; and the **known places** — a `kingdom → area → place path` tree (realm → settlement → district → spot → any nested rooms, any depth) with each place's **main NPC names** (e.g. *The Drowned Lantern → Common Room · NPCs: Maera, the grandson*). The tree is **scoped to the current era**, **capped at the 12 most recently used places**, and deliberately **names-only** — place descriptions stay in the save. An NPC's **identity** — race, class and role — does not live in the tree: it comes back with the character itself, because whenever one walks on stage the result reminds the GM of it (`on stage: Maera (dwarf rogue, the innkeeper)`). With images off the tree comes from the places the GM declares with `note_place` instead of from the illustrator's manifest; the engine keeps the same registry either way.

The character is then pulled live from the database — stats, HP, inventory, equipped gear and the carrying/encumbrance block. The current era's scaffold ([`config/eras/`](config/eras/)) arrives with the awakening, and only **that** era's history and polities are ever in play.

The static prefix — the tool schemas plus the protocol — is the same on every turn, so it is measured and gated (see **Development** below). Nothing about the mode you are not playing is in it.

---

## The state model

A save is two files in `output/`, plus its images:

| File | Holds |
| --- | --- |
| `{name}.player` | The character: abilities, HP, hit dice, inventory and equipped set, spellbook and slots, conditions, gold, and reputation |
| `{name}.timeline` | The session summaries the GM writes when you save, and the era legends |
| `output/images/{name}/` | Portraits, place seeds and cached illustrations for that save |

**Saving is in place** — a save rewrites its own files and never renames them, because its images are keyed to its name. The engine's database is in-memory and seeded from the `.player` at session start, so the file is the record and the running session is authoritative for the turn in front of you.

**Reputation is scoped.** In the era game it is stored per age — `{era: {polity: {faction: [entries]}}}` — so standing earned in Egypt is never offered in Wallachia and never lost when you leave; the GM still writes the short path and the engine resolves the age. In the classic game it is a flat map keyed by kingdom. Each age's memory of you lives in the timeline as a one-line **legend**, written the first time that age sees you vanish.

---

## Two modes, one engine

The mode is chosen at creation, stored on the save, and hidden from the GM.

| | Classic | Time Traveler |
| --- | --- | --- |
| World | A fixed invented realm — the political scaffold is injected at awakening | A ladder of ages — four historical, plus New York, the Traveller's own — one file each in `config/eras/` |
| Where you start | The one world | **Rolled** from the playable ages — but never New York, the Traveller's own — as is every later jump |
| Reputation | Flat, per kingdom | Per age, then per polity and faction |
| The Device | Never armed | Counts **ordinary** turns only — a turn where any combat was resolved is not one of them, so a fight holds the counter, and a jump can never interrupt one. Its five parts are real inventory items; each owns a power (the Mainspring holds the Device, the Escapement releases it, the Compass Rose and the Regulator aim the age, the Vernier the place), and the Mainspring's charge stops the countdown entirely. Control depends on **which** parts are held, not how many |
| `lookup` tool | Not offered | Offered — fetches any era's scaffold and factions |

The mode costs nothing when it is not in play: the protocol is marker-gated and rendered per session, the era index is its own system message, and the `lookup` tool is filtered out of a classic session entirely.

---

## Storyline image continuity

**Images are recommended.** The place-and-NPC tree the GM sees, and the places a Time Traveler jump can land in, are kept in **both modes**: with images on they are read from the scene manifest the illustrator writes; with images off the GM declares each new place once with `note_place` and the engine keeps the same registry (a place with no picture). The jump pool is the era's own places plus the ones you have visited, drawn at random. Images add the portrait and the per-turn illustration.

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

`tools/verify.py` is the whole gate in one command and exits non-zero on the first failure. Five steps, in order:

| Step | What it asserts |
| --- | --- |
| `srd` | `tools/srd_gate.py` — every **name** this repository ships is an SRD 5.1 name |
| `budget` | `tools/token_budget.py --gate` — the static-prefix ceilings in `tools/budgets.yml` |
| `dice` | The dice engine suite, `web/tests/dice/` |
| `web` | Every web suite, `web/tests/test_*.py` (no network, no GPU) |
| `node` | `node --check` on `web/static/app.js` |

`--only srd,budget,dice` runs a subset; `--json` gives a machine-readable summary.

**The token budget.** Every turn sends the GM a fixed **static prefix** — the tool schemas plus `GameMaster_MCP.md` — which is the game's largest recurring cost, so each ceiling is a written decision in `tools/budgets.yml` and a breach fails the build. `tools/token_budget.py --gate` runs the budget check alone, and **refuses to run** without `tiktoken` rather than gate on an estimate. `tools/gm_snapshot.py` captures and diffs what the GM is actually sent (`input` / `behavior` / `compare`).

**Layout.** `dice_server.py` is the engine and the MCP tools; `web/` is the server, the session, the client and the tests; `forge/` is the character creation and the world generation; `config/` is all rules data; `assets/` is the icon library; `tools/` is the gate.

---

## SRD 5.1 conformance

All embedded D&D 5e rules data in `config/` is derived from **SRD 5.1**, and the repository is checked so that it stays that way:

- **`tools/srd/srd-5.1.json`** is the whitelist: **names only**, with the provenance of every list recorded in it — which lists were fetched from the published SRD, which are the repository's own reviewed values, and which are an explicit decision to allow original content.
- **`tools/srd_gate.py`** checks every name in `config/*.yml`, the Forge's own literals, and every committed icon's name against it. It runs first in the gate, so the claim cannot rot.
- Where SRD 5.1 renames a spell to avoid a protected character name, the **SRD spelling is what ships**, and the alias is recorded rather than used.

**What the gate cannot check:** the prose inside a description, and whatever the running Game Master invents from a general-purpose model's training. That last limitation is real and is disclosed in the README.

See [`tools/srd/README.md`](tools/srd/README.md) for what each list is and what to do when the gate fails.
