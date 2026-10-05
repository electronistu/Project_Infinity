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

- `yourcharacter.player` — your character: stats, inventory, spells, and what they are wearing and wielding

A second file, `yourcharacter.timeline`, is created the first time you save. The world itself — its history and kingdoms — is fixed for every save, defined in [`config/world.yml`](config/world.yml).

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
- **Storyline scenes** — the GM attaches one cinematic 16:9 illustration to a turn's narrative. Each place keeps a hidden establishing reference image: every illustration is drawn from it, so a place and its regulars stay consistent when you return. **Hover an illustration to see it full size** (see [How It Works](#how-it-works)).
- **Sheet icons** — the shared, cross-character icon library used by the character sheet (items, spells, skills, abilities, conditions, and more).

Two pickers choose the models that draw them (both require `GEMINI_API_KEY`):

| Picker | What it draws | Default | Also available |
|--------|---------------|---------|----------------|
| **Story model** | Portrait + storyline scenes | Nano Banana 2 (`gemini-3.1-flash-image`) | Nano Banana Pro (`gemini-3-pro-image`) |
| **Icon model** | Character-sheet icons | Nano Banana 2 Lite (`gemini-3.1-flash-lite-image`) | — |

The start screen also sets the sheet's icon treatment: **icons** (use the shared library), **generate icons on the fly**, or **text only**.

Portraits, sheet icons and each place's hidden establishing image are cached under `output/images/{stem}/` and regenerate only when the data behind them changes (the illustrations in the transcript are drawn fresh each turn and are not kept). A portrait is drawn from your character's **equipped** gear and nothing else — never from items merely carried in the pack.

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

### Saving and loading

**There is no autosave — progress persists only when you save.** Saving is **in place**: the game writes the save's own files and never renames them, because a save's images live under a folder keyed to its name. A save:

- rewrites your character (`.player`) with the current state,
- appends a session **timeline** (`.timeline`) — the GM's concise summary of everything that happened since your last save (key events, NPCs met, loose plot threads).

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
- **Illustrations That Remember** — places, their light, and their regulars stay consistent across a whole campaign. Every scene is anchored to a hidden reference of the place, so a tavern never grows a new room and its barkeep never changes face; each recurring NPC is described once and then referred to by name.

Under the hood:

- **The engine is authoritative.** The game runs as a local **MCP (Model Context Protocol)** server with an in-memory SQLite database initialized from your `.player` file. Every mechanical action is a verified tool call that returns a result the GM must respect. The GM's personality, combat rules, and constraints live in [`GameMaster_MCP.md`](GameMaster_MCP.md), loaded as the system prompt.
- **Dice & checks.** `perform_check` rolls `d20 + modifier` vs a DC with natural-20/1 criticals. `roll_dice` supports any notation (e.g. `3d6+2`) and must be used for every random magnitude.
- **State authority.** `modify_player_numeric` and `update_player_list` manage all numeric and list state, with HP clamped to `[0, max]` and status tags (Healthy → Unconscious). Reaching 0 HP triggers death saves.
- **Phased resolution.** To keep complex turns accurate, the GM resolves all mechanics first (pausing with a sync token), and only then writes the narrative — so results are mechanically correct before the story is told.

### What the Game Master sees

The GM is an AI, so everything it "knows" has to be handed to it. A session starts by loading, in order:

- **The GM protocol** ([`GameMaster_MCP.md`](GameMaster_MCP.md)) — its rules, combat and narration discipline, and (when images are on) the scene-imagery instructions.
- **The world** — the shared history and the four kingdoms with their capitals, relations and guilds, from [`config/world.yml`](config/world.yml). This *is* the world; there is no per-save world file.
- **The save's timeline** (`{stem}.timeline`) — the GM's own summary of everything that happened before this session, so a long campaign stays coherent.
- **The known image places** (when images are on) — every place already drawn for this save, as a `kingdom → area → location → sublocation` tree, each with its short description and its **main NPC's name** — for example *The Drowned Lantern → Common Room — low-ceilinged, peat fire · main NPC: Maera*. The GM reuses these exact names, so a place reads the same whenever you return; an NPC's appearance is kept by the engine, not restated to the GM.

The character is then pulled live from the engine's database — stats, HP, inventory, equipped gear (with the derived armour-class breakdown) and the carrying/encumbrance block — and the opening scene is narrated.

During play the GM also receives the result of **every tool call** — the dice, the mechanics, and the `narrative_format` it must weave into prose — and, of course, the player's own input. The engine is the only source of numbers; the GM cannot invent any of them.

### Storyline image continuity

When **images** are on, the GM narrates with an illustration — and the engine keeps those
illustrations consistent across the whole campaign.

- **A place has an address.** Each scene is declared as `kingdom → area → location →
  sublocation` (e.g. *Kingdom of Eldoria → Eldoria City → The Drowned Lantern → Common Room*).
  `area` is whatever fits — a city, a town, or a border region. The GM names a place once,
  when it is first drawn, and reuses those exact names afterwards.
- **Every place gets a hidden "seed".** The first time you enter a place, the engine also
  draws an **establishing view** of it — empty, with no people or creatures, just the
  atmosphere. This seed is **never shown to you**; it is the place's visual anchor and it is
  permanent, so it survives between sessions.
- **Moments are drawn from the seed — never from the previous picture.** The picture you see
  is the **action** image: the establishing view **reproduced with you and the people in it**,
  so a room never rearranges and a character can never be duplicated out of the previous frame.
  The only things that change are the time of day, the weather, and whatever the moment itself
  breaks or spills. Nothing absent from the establishing view is invented — no door appears
  where the place has none. Each action image is served once and then discarded; the seed is the
  only scene picture a save keeps.
- **Leaving and returning.** The seed is permanent, so a room you revisit — later in the same
  session or in a later one — is drawn from the same establishing view it had the first time.
- **Permanent changes.** If a place changes for good — it burns down, a wall collapses — the
  GM regenerates its seed from the original and the very next image already reflects it.
  Passing conditions (a fire tonight, a storm) live only in the action image, so the GM
  restates them while they last.
- **Who is on stage — names in, looks injected.** Each recurring NPC is declared **once** with a
  stable look (name + description); after that the GM refers to them by **name only**. The engine
  looks the stored description up and hands it to the illustrator, so an NPC's face never drifts
  with the GM's wording. A place's **main NPC** (its smith, its innkeeper) is declared with the
  seed; recurring characters are declared as they appear. One-off extras need no declaration.
- **Time and weather are explicit.** The GM declares the time of day and the weather, and the
  engine feeds them to the illustrator — night is truly dark, lit only by its lamps and fires,
  and a rain-soaked street is wet. The seed itself is drawn timeless and weather-neutral, so a
  place's permanent picture never bakes in tonight's storm.
- **Nobody poses for the camera.** Everyone in the frame is caught in the action — figures face
  what they are doing, not the viewer, and being seen from behind is normal.

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
