# Project Infinity

*A text-based RPG where an AI Dungeon Master runs real D&D 5e — with mechanical rolls, persistent state, and SRD 5.1 rules enforced by a dedicated game engine. Played in your browser.*

*Local web client · Ollama Cloud models*

![Project Infinity](screenshot-1.png)
![Project Infinity](screenshot-2.png)

---

## What Makes This Different

Most AI RPGs let the language model make up numbers. Project Infinity runs every dice roll, every stat change, and every combat action through an external game engine the AI can read but not invent. The result is a Dungeon Master that actually plays by the rules.

- **Fair Dice** — All rolls performed by a dedicated server. The AI sees results, it doesn't generate them.
- **Persistent Character** — Stats, inventory, gold, spell slots, and reputation live in a real database. No forgetting.
- **Full Combat Resolution** — Weapon attacks, spell attacks, saving throws, cantrip scaling, upcasting, spell slot consumption, crits, kill detection, and XP awards in a single call.
- **Multi-Target AoE** — Fireball, Sleep, and all area spells resolve every target in one call. Per-target saves, HP pool exhaustion, one slot consumed.
- **Temporary Hit Points** — False Life, Armor of Agathys, and Heroism auto-apply and auto-expire. NPC attacks drain THP before real HP.
- **Reputation System** — Faction standing persists between sessions. Heroic deeds and crimes tracked, visible on your character sheet.
- **Saving Throw Spells** — Hold Person, Charm, Banishment, and 50+ others roll saves with full dice disclosure.
- **Active Buffs** — Shield, Mage Armor, Longstrider auto-modify stats and auto-revert on removal.
- **Scroll Casting** — Cast from scrolls without slots. Ability checks for scrolls above your level.
- **Rest & Recovery** — Short and long rests auto-apply hit dice, slot recovery, Arcane Recovery, and effect clearing per SRD 5.1.
- **Leveling Up** — XP thresholds auto-trigger HP, proficiency, hit dice, and spell slot progression.
- **Combat Registry** — The GM registers all combatants once per battle. Initiative is auto-rolled for everyone and every combatant's HP is tracked across attacks.
- **Combat Healing** — Cure Wounds, Healing Word, Heal, and all SRD healing spells resolve through the combat registry, capped at maximum HP, for player-to-NPC, NPC-to-player, and NPC-to-NPC healing alike.

---

## Quick Start

### 1. Prerequisites

- **Python 3.11** or newer
- **[Ollama](https://ollama.ai/)** installed and signed in — the models are Ollama Cloud tags

### 2. Install

```bash
git clone <repo-url>
cd Project_Infinity
python3 -m venv venv
source venv/bin/activate      # Linux/macOS
# venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

### 3. Run the server

```bash
# Windows
venv\Scripts\python.exe web_server.py

# Linux / macOS
python3 web_server.py
```

Then open **http://127.0.0.1:8000** in your browser. The server binds to localhost only.

Useful flags: `--port 8000`, `--host 127.0.0.1`, `--reload` (auto-restart on code changes), `--log-level info`.

### 4. Create a character

On the start screen, choose **Create character**. The in-browser **Character Forge** walks you through the full SRD 5.1 creation flow — race, class, background, point-buy stats, skills, equipment, and more. On completion it writes two files into `output/`:

- `yourcharacter.wwf` — the world (kingdoms, NPCs, guilds, history)
- `yourcharacter.player` — your character's stats and inventory

(A third file, `yourcharacter.timeline`, is created the first time you save.)

### 5. Play

Pick your world and a model on the start screen, then begin. The Game Master awakens and your adventure starts. Type actions in plain English — the GM handles the rest.

---

## Playing

### Choosing a model

The start screen lets you pick from the curated Ollama Cloud models and set a sampling temperature.

| Model | Context | Notes |
|-------|---------|-------|
| `deepseek-v4.1-flash:cloud` | 1,048,576 | Default |
| `deepseek-v4-pro:cloud` | 1,000,000 | |
| `kimi-k2.6:cloud` | 250,000 | |

### Controls

| Control | Where | What it does |
|---------|-------|--------------|
| **Sync** | Header | Force a full database sync so the GM's memory matches your actual state |
| **Save** | Header | Save your progress under a name of your choosing (see below) |
| **Load** | Header | Load a saved game — from the start screen or mid-session |
| **End** | Header | End the session (**Save & end**, **End without saving**, or Cancel) |
| **Refresh** | Character sheet | Reload your character sheet from the engine |
| **Tools** | Header | Show or hide the GM's tool calls behind the scenes |
| **Thinking** | Header | Show or hide the GM's reasoning |
| **Theme** | Header | Toggle **Candlelit Codex** (dark, default) and **Vellum** (light) |

### Saving and loading

**There is no autosave — progress persists only when you save.** When you save, the engine writes three things under your chosen name:

- the world (`.wwf`)
- your character (`.player`)
- a session **timeline** (`.timeline`)

The GM writes the timeline at save time: a concise, comprehensive summary of everything that happened since your last save (key events, NPCs met, and loose plot threads). When you load that save, the timeline is injected so the story continues coherently.

Your existing saves are listed on the start screen and in the **Load** picker, and can be deleted from there.

### The character sheet

The sidebar shows your live sheet: ability scores and modifiers, combat stats with an HP bar, spellcasting, proficiencies, inventory (with item descriptions on hover/tap), consumables, active effects, and reputation. Each section folds independently — use **collapse** / **expand** to fold them all at once. The sheet refreshes automatically after each of your actions.

### When the GM forgets something

The GM is an AI — it can forget to award quest XP, apply gold payments, or remove an item from your inventory. If you notice a missing update:

1. **Press Sync first.** This forces a full database refresh; the GM reviews your state and may self-correct.
2. **If that doesn't work, address the GM directly** in plain language, exactly as you would a human DM. Be specific about what was missed.

This works for any forgotten mechanic: gold, inventory changes, spell slot recovery, hit dice, conditions, or items.

### Reputation between sessions

Your standing with every guild in every kingdom is stored in the `reputation` dictionary inside your `.player` file. The GM records your deeds as `title: description` entries. Because they persist in the save, the GM can reference them in the opening scene and throughout play. Check your current standing any time on the character sheet.

---

## How It Works

The browser is just a client; the rules live in an external engine.

- **The engine is authoritative.** The game runs as a local **MCP (Model Context Protocol)** server with an in-memory SQLite database initialized from your `.player` file. The AI cannot invent rolls, stats, or outcomes — every mechanical action is a verified tool call that returns a result the GM must respect. The GM's full personality, combat rules, and constraints live in [`GameMaster_MCP.md`](GameMaster_MCP.md), loaded as the system prompt.
- **Session startup.** Four steps bring the world to life: the GM protocol is loaded, your `.wwf` world is injected, the character is dumped from the database, and only then is the opening scene narrated.
- **Dice & checks.** `perform_check` rolls `d20 + modifier` vs a DC with natural-20/1 criticals. `roll_dice` supports any notation (e.g. `3d6+2`) and must be used for every random magnitude.
- **Combat.** `resolve_attack` runs the full attack sequence (attack roll vs AC, advantage, crits, HP application, kill detection, XP). `resolve_magic` handles spells end to end: slot validation and consumption, cantrip scaling, upcasting, attack/save/automatic types, multi-target AoE, HP-pool spells like Sleep, healing, temporary HP, conditions and concentration, and active buff tracking that reverts on removal. `register_combatants` builds a battle registry so targets are looked up by name and HP carries forward between hits.
- **Rest, recovery & leveling.** The `rest` tool applies short/long-rest mechanics per SRD 5.1 (hit dice, slot recovery, Arcane Recovery, effect clearing). Crossing an XP threshold triggers automatic level-up of HP, proficiency, hit dice, and spell slots.
- **State authority.** `modify_player_numeric` and `update_player_list` manage all numeric and list state, with HP clamped to `[0, max]` and status tags (Healthy → Unconscious). Reaching 0 HP triggers death saves.
- **Phased resolution.** To keep complex turns accurate, the GM resolves all mechanics first (pausing with a sync token), and only then writes the narrative — so results are mechanically correct before the story is told.

Every roll is shown in a readable format, e.g.:

```
Guard Attack: 17 vs AC 15 (Success) (15 + 2)
TestHero Fireball: 28 vs DEX Save DC 15 (Failure) (3 + 2 + 6 + 5 + 5 + 7)
```

---

## Technology

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| Game engine | MCP (Model Context Protocol) server + in-memory SQLite |
| Web server | FastAPI + Uvicorn, streaming over WebSocket |
| Character sheet | Vanilla JS / HTML / CSS (no build step) |
| Data validation | Pydantic |
| Config | YAML |
| Models | Ollama Cloud |

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
