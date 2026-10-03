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

Copy `.env.example` to `.env` and set `GEMINI_API_KEY` to enable the **Google Gemini Game Master models** and the **image features** (character portrait, storyline scenes, sheet icons). Everything else works without it — the key is read from the environment at startup.

### 4. Run the server

```bash
# Windows
venv\Scripts\python.exe web_server.py

# Linux / macOS
python3 web_server.py
```

Then open **http://127.0.0.1:8000** in your browser. The server binds to localhost only.

Useful flags: `--host`, `--port`, `--reload` (auto-restart on code changes), `--log-level`.

### 5. Create a character

On the start screen, choose **Create character**. The in-browser **Character Forge** walks you through the full SRD 5.1 creation flow — race, class, background, point-buy stats, skills, spells, and starting equipment. On completion it writes into `output/`:

- `yourcharacter.wwf` — the world (kingdoms, NPCs, guilds, history)
- `yourcharacter.player` — your character: stats, inventory, spells, and what they are wearing and wielding

A third file, `yourcharacter.timeline`, is created the first time you save.

### 6. Play

Pick a world and a model on the start screen, then begin. The Game Master awakens and your adventure starts. Type actions in plain English — the GM handles the rest.

---

## Playing

### Choosing a model

The start screen lets you pick from the curated models and set a sampling temperature.

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

The Google entries appear only when `GEMINI_API_KEY` is set; they use the same key as the images.

### Images

Image generation is **opt-in** and **cached** — nothing is drawn unless you ask for it. Tick **images** on the start screen (or in the Load dialog) to enable:

- **Character portrait** — a head-and-shoulders portrait of your character, shown on the start screen and the character sheet. The **↻** button on the sheet repaints it from the character's current level and gear.
- **Storyline scenes** — the GM attaches one cinematic 16:9 illustration to a turn's narrative at pivotal moments. A location's previous picture is fed back in as a reference, so a place stays visually consistent when you return.
- **Sheet icons** — the shared, cross-character icon library used by the character sheet (items, spells, skills, abilities, conditions, and more).

Two pickers choose the models that draw them (both require `GEMINI_API_KEY`):

| Picker | What it draws | Default | Also available |
|--------|---------------|---------|----------------|
| **Story model** | Portrait + storyline scenes | Nano Banana 2 (`gemini-3.1-flash-image`) | Nano Banana Pro (`gemini-3-pro-image`) |
| **Icon model** | Character-sheet icons | Nano Banana 2 Lite (`gemini-3.1-flash-lite-image`) | — |

The start screen also sets the sheet's icon treatment: **icons** (use the shared library), **generate icons on the fly**, or **text only**.

Images are cached per save under `output/images/{stem}/` and regenerate only when the data behind them changes. A portrait is drawn from your character's **equipped** gear and nothing else — never from items merely carried in the pack.

### Controls

| Control | Where | What it does |
|---------|-------|--------------|
| **Sync** | Header | Force a full database sync so the GM's memory matches your actual state |
| **Save** | Header | Save the game in place (see below) |
| **Load** | Header | Load a saved game — from the start screen or mid-session |
| **End** | Header | End the session (**Save & end**, **End without saving**, or Cancel) |
| **Tools** | Header | Show or hide the GM's tool calls behind the scenes |
| **Thinking** | Header | Show or hide the GM's reasoning |
| **Theme** | Header | Toggle **Candlelit Codex** (dark, default) and **Vellum** (light) |
| **☰ Character sheet** | Header | Open the live sidebar sheet |
| **Refresh** | Character sheet | Reload the sheet from the engine |
| **↻** | Character sheet | Regenerate the character portrait |

Typed commands in the input box: `/help`, `/stats`, `/save`, `/sync`, `/quit`.

### Saving and loading

**There is no autosave — progress persists only when you save.** Saving is **in place**: the game writes the world's own files and never renames them, because a world's images live under a folder keyed to its name. A save:

- rewrites your character (`.player`) with the current state,
- appends a session **timeline** (`.timeline`) — the GM's concise summary of everything that happened since your last save (key events, NPCs met, loose plot threads),
- leaves the world (`.wwf`) exactly as it was created.

Loading that save injects the timeline, so the story continues coherently. Your saves are listed on the start screen and in the **Load** picker, and can be deleted from there.

### When the GM forgets something

The GM is an AI — it can forget to award quest XP, apply gold payments, or remove an item from your inventory. If you notice a missing update:

1. **Press Sync first.** This forces a full database refresh; the GM reviews your state and may self-correct.
2. **If that doesn't work, address the GM directly** in plain language, exactly as you would a human DM. Be specific about what was missed.

This works for any forgotten mechanic: gold, inventory changes, spell slot recovery, hit dice, conditions, or items.

---

## How It Works

Most AI RPGs let the language model make up numbers. Project Infinity runs every dice roll, every stat change, and every combat action through an external game engine that the AI can read but not invent. The result is a Dungeon Master that actually plays by the rules.

What that buys you:

- **Fair Dice** — all rolls are performed by the engine. The AI sees results; it cannot generate them.
- **Persistent Character** — stats, inventory, gold, spell slots, equipped gear, and reputation live in a real database. No forgetting.
- **Equipped Gear Is Real** — your armour class is derived from what you are actually wearing and wielding, not from a snapshot of your pack. There is one suit of armour and two hands; a shield occupies a hand; attunement limits magic items; two-weapon fighting, donning and doffing armour, and a one-handed weapon's need for a free hand to reload are all enforced.
- **Weight Matters** — carrying capacity comes from Strength, and the SRD variant encumbrance rules apply: encumbered and heavily encumbered states, speed penalties, and disadvantage on the affected rolls.
- **Full Combat Resolution** — weapon attacks, spell attacks, saving throws, cantrip scaling, upcasting, spell slot consumption, crits, kill detection, and XP awards in a single call.
- **NPCs With Stat Blocks** — the GM registers every combatant once, with HP, AC, per-ability saves, declared attacks, damage resistances/immunities/vulnerabilities, and conditions. The engine then resolves their attacks, halves or zeroes damage by type, and auto-fails the Strength/Dexterity saves of paralyzed, petrified, stunned, or unconscious creatures.
- **Multi-Target AoE** — Fireball, Sleep, and all area spells resolve every target in one call: per-target saves, HP-pool exhaustion, one slot consumed.
- **Temporary HP, Buffs & Conditions** — False Life, Shield, Mage Armor and friends auto-apply and auto-revert; conditions (blinded, prone, restrained, poisoned, frightened, invisible, paralyzed, petrified, stunned, unconscious) drive advantage, disadvantage, forced criticals, and failed saves.
- **Refused Actions** — attacking with something you do not have in hand, casting with both hands occupied, or casting while wearing armour you are not proficient with is refused by the engine, and the action is spent. There are no do-overs.
- **Rest, Recovery & Leveling** — short and long rests auto-apply hit dice, slot recovery, Arcane Recovery, and effect clearing per SRD 5.1; crossing an XP threshold auto-applies HP, proficiency, hit dice, and spell-slot progression.
- **Reputation That Persists** — your standing with every guild is stored in your `.player` as `title: description` entries, so the GM can reference your deeds in the opening scene and throughout later sessions.
- **A Timeline, Not a Transcript** — saving records the GM's summary of the story so far, which is injected on load to keep a long campaign coherent.

Under the hood:

- **The engine is authoritative.** The game runs as a local **MCP (Model Context Protocol)** server with an in-memory SQLite database initialized from your `.player` file. Every mechanical action is a verified tool call that returns a result the GM must respect. The GM's personality, combat rules, and constraints live in [`GameMaster_MCP.md`](GameMaster_MCP.md), loaded as the system prompt.
- **Session startup.** Four steps bring the world to life: the GM protocol is loaded, your `.wwf` world is injected, the character is dumped from the database, and only then is the opening scene narrated.
- **Dice & checks.** `perform_check` rolls `d20 + modifier` vs a DC with natural-20/1 criticals. `roll_dice` supports any notation (e.g. `3d6+2`) and must be used for every random magnitude.
- **State authority.** `modify_player_numeric` and `update_player_list` manage all numeric and list state, with HP clamped to `[0, max]` and status tags (Healthy → Unconscious). Reaching 0 HP triggers death saves.
- **Phased resolution.** To keep complex turns accurate, the GM resolves all mechanics first (pausing with a sync token), and only then writes the narrative — so results are mechanically correct before the story is told.

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
| Game Master models | Ollama Cloud, Google Gemini *(optional)* |
| Images *(optional)* | Google Gemini image models, cached per save |

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
