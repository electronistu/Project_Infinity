# Project Infinity

*A text-based RPG where an AI Dungeon Master runs real D&D 5e — with mechanical rolls, persistent state, and SRD 5.1 rules enforced by a dedicated game engine. Played in your browser.*

*Local web client · Ollama Cloud & Google Gemini models*

![Project Infinity](screenshot-1.png)

---

## Quick Start

### 1. Prerequisites

- **Python 3.11** or newer
- **[Ollama](https://ollama.ai/)** installed and signed in — the default models are Ollama Cloud tags
- *(Optional)* a Google AI Studio API key, for the Gemini Game Master models and image generation

### 2. Install

```bash
git clone <repo-url>
cd Project_Infinity
python3 -m venv venv
source venv/bin/activate      # Linux/macOS
# venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

### 3. Configure (optional)

Copy `.env.example` to `.env` and set `GEMINI_API_KEY` to enable the **Google Gemini Game Master models** and the **image features**. Everything else works without it.

### 4. Run

```bash
venv\Scripts\python.exe web_server.py     # Windows
python3 web_server.py                     # Linux / macOS
```

Open **http://127.0.0.1:8000**. The server binds to localhost only. Flags: `--host`, `--port`, `--reload`, `--log-level`.

### 5. Create a character

Choose **Create character** on the start screen. The in-browser **Character Forge** walks the full SRD 5.1 creation flow — race, class, background, point-buy stats, skills, spells, starting equipment — and writes `output/yourcharacter.player`. The world (its history and kingdoms) is fixed for every save in [`config/world.yml`](config/world.yml).

### 6. Play

Pick a world and a model, then begin. Type actions in plain English — the GM handles the rest.

---

## Playing

### Choosing a model

The start screen offers curated models and, for the Ollama Cloud models, a sampling temperature.

| Model | Provider | Context |
|-------|----------|---------|
| `deepseek-v4.1-flash:cloud` | Ollama Cloud | 1,048,576 *(default)* |
| `deepseek-v4-pro:cloud` | Ollama Cloud | 1,000,000 |
| `kimi-k2.6:cloud` | Ollama Cloud | 250,000 |
| `gemini-3.8-flash` | Google | 1,048,576 |
| `gemini-3.6-flash` | Google | 1,048,576 |
| `gemini-3.5-flash` | Google | 1,048,576 |
| `gemini-3.5-flash-lite` | Google | 1,048,576 |
| `gemini-3.1-pro-preview` | Google | 1,048,576 |

The Google entries appear only when `GEMINI_API_KEY` is set. Gemini ignores custom sampling: `temperature`/`top_p`/`top_k` are deprecated and fixed to the model's optimal defaults, so the temperature control is disabled for Gemini models and each model uses its own `thinking_level` (reasoning effort). Ollama models keep the temperature control.

### Images

Image generation is **opt-in** and **cached** — nothing is drawn unless you ask. Tick **images** on the start screen (or in the Load dialog) to enable:

- **Character portrait** — shown on the start screen and sheet; the **↻** button repaints it from your current level and gear.
- **Storyline scenes** — one cinematic 16:9 illustration per turn. Each place keeps a hidden establishing reference, so places and regulars stay consistent; **hover an illustration to see it full size**. Each is captioned with its area, place, time of day and weather.
- **Sheet icons** — the shared, cross-character library used by the sheet (items, spells, skills, abilities, conditions).

Both image models are Google Gemini (the same `GEMINI_API_KEY`): **Nano Banana 2.1** (`gemini-nano-banana-2.1`) draws portraits and scenes at `medium` thinking effort; **Nano Banana 2 Lite** (`gemini-3.1-flash-lite-image`) draws sheet icons. The start screen also sets the sheet's icon treatment: **cached icons only; no generation**, **cached icons + generate on new ones**, or **text only**.

Everything is cached under `output/images/{stem}/` and regenerates only when the data behind it changes. A portrait is drawn from your character's **equipped** gear only. (Transcript illustrations are drawn fresh each turn and are not kept.)

### Controls

| Control | Where | What it does |
|---------|-------|--------------|
| **Save** | Header | Save the game in place (see below) |
| **Load** | Header | Load a saved game — from the start screen or mid-session |
| **End** | Header | End the session (**Save & end**, **End without saving**, or Cancel) |
| **Tools** | Header | Show or hide the GM's tool calls behind the scenes; each block shows the full result plus the green **GM sees** view |
| **Thinking** | Header | Show or hide the GM's reasoning |
| **Theme** | Header | Toggle **Candlelit Codex** (dark, default) and **Vellum** (light) |
| **☰ Character sheet** | Header | Open the live sidebar sheet |
| **Refresh** | Character sheet | Reload the sheet from the engine |
| **↻** | Character sheet | Regenerate the character portrait |
| **Hover a green combatant name** | Transcript | Show that combatant's **live sheet** — HP, AC, conditions, attacks, saves, traits. Names are highlighted wherever they appear, not just in the initiative order. |

### Saving and loading

**There is no autosave — progress persists only when you save.** Saving is **in place**: a save writes its own files and never renames them, because its images live under a folder keyed to its name. A save rewrites your character (`.player`) and appends a session **timeline** (`.timeline`) — the GM's concise summary of the story so far. Loading injects that timeline, so a long campaign stays coherent. Saves are listed on the start screen and in the **Load** picker.

### When the GM forgets something

The GM is an AI and can miss an update. If you spot one, **tell the GM directly** — exactly as you would a human DM. The engine is authoritative, so a direct instruction is all it takes to correct your state (or the GM will re-derive it on the next rule call).

This works for any missed mechanic: gold, inventory, spell slots, hit dice, conditions, or an item whose state changed (a note opened, a lamp lit). The sheet's **Refresh** button reloads your own view straight from the engine at any time.

---

## How It Works

Most AI RPGs let the language model make up numbers. Project Infinity runs every dice roll, stat change and combat action through an external game engine the AI can read but not invent.

- **Real Rules, Real Dice** — every attack, save, spell, crit, kill and XP award is resolved by the engine in one call; the GM sees the results and cannot generate them.
- **A Persistent World** — stats, inventory, gold, spell slots, equipped gear and reputation live in a real database; guild standing is kept per kingdom, and saving records a session timeline so a long campaign stays coherent.
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

### What the Game Master sees

A session loads, in order: the **GM protocol** ([`GameMaster_MCP.md`](GameMaster_MCP.md)); the **world** (history and kingdoms from [`config/world.yml`](config/world.yml) — there is no per-save world file); the save's **timeline**; and, when images are on, the **known image places** — a `kingdom → area → location → sublocation` tree with each place's description and its **main NPCs' names and roles** (e.g. *The Drowned Lantern → Common Room · main NPCs: Maera (the innkeeper), the grandson (the table-runner)*). The character is then pulled live from the database — stats, HP, inventory, equipped gear and the carrying/encumbrance block.

During play the GM receives the result of **every tool call**, trimmed to what it does not already hold: the **delta** of a state change, the **exact mechanics** of a roll or attack, and any **error**. Redundant snapshots (inventory, carrying, equipment) are not re-sent; a full sheet is one deliberate `dump_player_db` away. The **client keeps the complete, untrimmed result** for debugging.

The **Mechanics panel is composed by the engine** from those results and appended at the end of the narrative — the GM never writes mechanics, a heading or a placeholder. Every line names who acted on whom (`Actor → Target`), shows a green, hoverable **Initiative Order** (tooltip = the order) and each NPC's live sheet as a hover tooltip on their name. The GM narrates the outcomes, emphasizes critical results (a crit, near-death HP, a condition), and explains rules when the player needs them.

### Storyline image continuity

- **A place has an address** — `kingdom → area → location → sublocation`, named once and reused exactly.
- **Every place gets a hidden "seed"** — an empty, permanent establishing view, never shown to you.
- **Moments are drawn from the seed, never the previous picture** — the action image reproduces the establishing view with you and the cast, so rooms never rearrange and figures can never duplicate. Only the time of day, the weather and the moment's own damage may differ; nothing absent from the seed is invented. Action images are served once and discarded; the seed is the only scene picture a save keeps.
- **Returning and permanent changes** — a revisited place is drawn from the same seed; a lasting change (it burned down) regenerates it.
- **Names in, looks injected** — each recurring NPC is declared once with a stable **physical** look (no pose, position or action) and the engine injects it, so a face never drifts. A place's **main NPCs** are declared as a list, each with a **role**. Recurring characters are declared as they appear; one-off extras need none.
- **Time, weather, and no posing** — the GM declares time and weather; the seed stays timeless and weather-neutral. Everyone is caught in the action, never facing the camera.
- **Disguises** — while an appearance-changing effect (Disguise Self, Alter Self) is active, the scene shows that appearance instead of your portrait.

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
| World | Static scaffold in `config/world.yml` (fixed history + kingdoms) |
| Game Master models | Ollama Cloud, Google Gemini *(optional)* |
| Images *(optional)* | Google Gemini image models, cached per save (portraits, icons, place seeds) |

---

## Licensing & Legal

This project has a **dual-license structure** to comply with copyright law and Wizards of the Coast's intellectual property rights.

### Source Code — MIT License

All **original source code** in this project (the game engine, web client, MCP server, dice server, world forge, and all `.py` source files) is licensed under the [MIT License](LICENSE). See `LICENSE` for the full terms.

### D&D 5e Rules Content — CC-BY-4.0 (via SRD 5.1)

All **embedded D&D 5e rules data** — including spell descriptions, class features, racial traits, background features, weapon statistics, and game mechanics found in `config/` files — is derived from the **System Reference Document 5.1 (SRD 5.1)**, published by Wizards of the Coast LLC under the **Creative Commons Attribution 4.0 International License (CC-BY-4.0)**.

This content is **NOT covered by the MIT license**. It is used under a separate open license that permits public use with proper attribution. See `SRD_LICENSE.txt` for the full attribution text and additional details.

### Required SRD Attribution

> "This work includes material taken from the System Reference Document 5.1 ("SRD 5.1") by Wizards of the Coast LLC, available at https://dnd.wizards.com/resources/systems-reference-document. The SRD 5.1 is licensed under the Creative Commons Attribution 4.0 International License available at https://creativecommons.org/licenses/by/4.0/legalcode."

### What Is NOT Included

To maintain legal compliance, this project has been audited to exclude all **non-SRD content** from its distributed files. Specifically:

- **Product Identity** (Beholders, Mind Flayers, Displacer Beasts, Carrion Crawlers, Githyanki, Githzerai, Kuo-Toa, Slaadi, etc.)
- **Named Characters** (Strahd, Bigby, Mordenkainen, Tasha, Drizzt, etc.)
- **Non-SRD Spells** (Booming Blade, Green-Flame Blade, Absorb Elements, Toll the Dead, Chaos Bolt, etc.)
- **Non-SRD Races/Subraces** (Drow, Stout Halfling, etc.)
- **Non-SRD Classes** (Artificer)
- **Proprietary Setting Lore** (Forgotten Realms geography, unique deities, faction names)

### AI-Generated Content at Runtime

The AI Game Master operates on general-purpose models that may have been trained on the broader D&D corpus. Despite prompt-level restrictions instructing the GM to adhere strictly to SRD content, **the project cannot guarantee that AI-generated narrative will never reference non-SRD material at runtime**. This is a known limitation of AI-powered applications and is explicitly disclosed.

### Not Affiliated with Wizards of the Coast

This project is **not affiliated with, endorsed by, sponsored by, or connected to Wizards of the Coast LLC** in any way. Dungeons & Dragons and D&D are trademarks of Wizards of the Coast LLC in the USA and other countries.

---

## License

- **Original source code**: [MIT License](LICENSE)
- **D&D 5e rules data (config files)**: [Creative Commons Attribution 4.0 International](SRD_LICENSE.txt), via the [D&D SRD 5.1](https://dnd.wizards.com/resources/systems-reference-document)
