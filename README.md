# Project Infinity

A text-based RPG where an AI acts as your Dungeon Master — with real dice rolls, real character tracking, and real D&D 5e rules. No hallucinated stats. No forgotten inventory. The AI rolls fairly, tracks your HP, and levels you up automatically.

*Running on gemini-2.5-pro:*

![Project Infinity TUI — gemini-2.5-pro](screenshot.png)
---

## What Makes This Different?

Most AI RPGs let the language model make up numbers. Project Infinity doesn't. Every dice roll, every stat change, every level-up goes through an external game engine that the AI can read but not fake. The result is a Dungeon Master that actually plays by the rules.

- **Fair Dice** — All rolls are performed by a dedicated server and verified. The AI sees the results, it doesn't generate them.
- **Persistent Character** — Your stats, inventory, gold, and spell slots live in a real database that updates in real time. No "forgetting" that you used your last potion.
- **D&D 5e Rules** — Leveling up, spell slot recovery, proficiency bonuses — all handled automatically by the engine.
- **Combat Resolution** — Weapon attacks, spell attacks, saving throws, cantrip scaling, upcasting, spell slot consumption, crits, kill detection, and XP awards are all resolved mechanically in a single tool call. The AI cannot fudge damage or forget to deduct a slot.
- **In-Game Commands** — Check your stats, force a database sync, or get help without leaving the game.

---

## Quick Start

### 1. Prerequisites

- **Python 3.11** or newer
- **One AI backend** (pick one):
  - **Ollama** — cloud-based, free and paid
  - **OpenAI** — cloud-based, requires a paid API key
  - **Gemini** — cloud-based, requires a paid API key
  - **Claude** — cloud-based, requires a paid API key

### 2. Install

```bash
git clone <repo-url>
cd Project_Infinity
python3 -m venv venv
source venv/bin/activate      # Linux/macOS
# venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

### 3. Choose Your AI Backend

| Backend | Requirements | Supported Models | Play Command |
|---------|-------------|------------------|--------------|
| **Ollama** | Install [Ollama](https://ollama.ai/), pull your model | `kimi-k2.6:cloud`, `deepseek-v4-flash:cloud`, `deepseek-v4-pro:cloud` | `python3 play.py` |
| **OpenAI** | `export OPENAI_API_KEY=your-api-key` | `gpt-5.5-pro`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano` | `python3 play_with_gpt.py` |
| **Gemini** | `export GEMINI_API_KEY=your-api-key` | `gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `gemini-2.5-pro` | `python3 play_with_gemini.py` |
| **Claude** | `export ANTHROPIC_API_KEY=your-api-key` | `claude-opus-4-7`, `claude-opus-4-6` | `python3 play_with_claude.py` |

> **Note:** The Gemini preview models have known issues. See [Known Issues](#known-issues) for details.

### 4. Create Your World

Before you can play, you need to create a character and generate a world. The World Forge walks you through picking a race, class, background, distributing stats, choosing equipment, and more — all following D&D 5e rules.

```bash
python3 main.py
```

This generates two files in the `output/` directory:
- `yourcharacter_weave.wwf` — the world data (kingdoms, NPCs, guilds, history)
- `yourcharacter_weave.player` — your character's stats and inventory

### 5. Play!

Launch the game with the script that matches your backend:

| Backend | Command |
|---------|---------|
| Ollama | `python3 play.py` |
| OpenAI | `python3 play_with_gpt.py` |
| Gemini | `python3 play_with_gemini.py` |
| Claude | `python3 play_with_claude.py` |

You'll be prompted to:
1. **Select a model** — pick from the list of supported models
2. **Select a world file** — pick the `.wwf` file you generated in Step 4

Then the Game Master awakens and your adventure begins. Type actions in plain English. The GM handles the rest.

---

## In-Game Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/stats` | Display your current character stats, inventory, and spell slots |
| `/sync` | Force a database sync to make sure the GM's memory matches your actual state |
| `/quit` | Exit the game |

---

## How It Works

If you're curious about what's happening under the hood, here's a high-level overview.

### MCP Tool Server

The game engine runs as a local **MCP (Model Context Protocol)** server with an in-memory SQLite database initialized from your `.player` file at startup. The AI cannot invent rolls, stats, or outcomes — every mechanical action is a verified tool call that returns a `narrative_format` string the GM must include verbatim.

### Checks & Generic Rolls

- **Skill Checks & Saves** — `perform_check` rolls `d20 + modifier` vs DC with native Critical Success/Failure on natural 20/1. Every result is formatted for direct inclusion in the narrative.
- **Damage, Healing & Quantity** — `roll_dice` supports any standard notation (e.g. `3d6+2`). The AI must use this for all random magnitudes; it cannot make up damage numbers.

### Weapon & Unarmed Combat

`resolve_attack` handles the **full attack sequence** in a single call:
- Attack roll vs AC. Supports **Advantage** (roll twice, take higher) and **forced crits** (unconscious targets within 5 feet).
- Critical hits double **primary** damage dice but not extra damage dice (e.g. elemental riders).
- Automatic HP application, kill detection, and XP award using the D&D 5e CR/XP table.
- Works for player-vs-NPC, NPC-vs-player, and NPC-vs-NPC.

### Spell Combat

`resolve_magic` resolves **all spell resolution** in one call:
- **Spell Database** — Properties are looked up from `config/spells.yml`. Custom spells can be cast with override parameters.
- **Automatic Spell Slot Management** — Validates slot availability before rolling; consumes the slot automatically. Rejects under-level slots or empty slots with a clear error.
- **Cantrip Scaling** — Automatically scales base dice at levels 5, 11, and 17.
- **Upcasting** — Damage and healing scale automatically when a spell is cast in a higher-level slot (per the spell's `higher_levels` field).
- **Attack Types** — Supports `attack_roll` (vs AC), `saving_throw` (half damage on save if `save_half`), and `automatic` (always hits).
- **Healing & HP Pools** — Healing spells restore HP. HP pool spells (e.g. *Sleep*, *Color Spray*) roll a total HP pool against the target's HP.
- **Conditions & Concentration** — Applies conditions with duration and concentration flags. Supports instant-kill spells (Power Word-style HP threshold checks).
- **Kill Detection & XP** — Identical to weapon attacks: NPC deaths award XP automatically.

### State Authority

Your character lives in an in-memory SQLite database that the AI updates through tool calls. Your HP, gold, inventory, spell slots — all of it is tracked precisely.

- **Numeric State** — `modify_player_numeric` handles everything from gold to XP. Crossing a level threshold triggers **automatic level-up** (HP, proficiency bonus, hit dice, spell slots, DC, attack modifier). The AI must manually apply class features, new spells, ASIs, and subclass features.
- **List State** — `update_player_list` manages inventory, known/prepared spells (capacity enforced for prepared casters), features, skills, proficiencies, and languages.
- **HP Clamping** — HP is bounded to `[0, max_HP]`. Hitting 0 returns an "Unconscious" status tag, triggers death saves, and clamps all damage to 0.
- **HP Status Tags** — Every HP change returns a structured status: Healthy, Wounded, Bloodied, Critical, or Unconscious.
- The AI is required to update state immediately when changes happen.
- Every 4 prompts, the engine forces a full database sync to catch any drift.
- You can manually force a sync at any time with `/sync`.

### Phased Resolution

To prevent the AI from "collapsing" on complex turns (trying to narrate and calculate at the same time), the engine uses a two-phase protocol:

1. **Mechanical Phase** — The AI resolves all dice rolls and state updates first, pausing with a sync token.
2. **Narrative Phase** — Only after all mechanics are verified does the AI produce its story output.

This means you always get mechanically accurate results before the narrative.

### Transparency

Every roll is shown in a standard format:
```
Guard Attack: 17 vs AC 15 (Success) (15 + 2)
TestHero Fireball: 28 vs DEX Save DC 15 (Failure) (3 + 2 + 6 + 5 + 5 + 7)
```

---

## Advanced Options

All play scripts accept the following flags:

| Flag | Description |
|------|-------------|
| `--verbose`, `-v` | Show all tool calls and their results behind the scenes |
| `--debug`, `-d` | Show raw AI responses including internal reasoning (also enables `--verbose`) |
| `--temperature`, `-t` | Set sampling temperature (Ollama/OpenAI/Claude default: 0.0, Gemini default: 1.0). Ignored for GPT-5.5 and Claude Opus 4.7 models (temperature is deprecated). |
| `--think` | Enable thinking/reasoning for the model as a boolean toggle (Ollama only) |
| `--thinking-level` | Enable structured AI reasoning with effort level: `LOW`, `MEDIUM`, `HIGH`, `XHIGH`. `XHIGH` is exclusive to Pro/Enterprise-tier models. See Known Issues. |
| `--verbosity` | Control output verbosity for GPT-5.5 models: `low`, `medium`, `high` (default: `medium`). Ignored by other backends. |
| `--max-output-tokens` | Maximum output tokens for GPT-5.5 and Claude models (default: 16384). Includes thinking tokens. Ignored by other backends. |

Examples:
```bash
python3 play.py --temperature 0.6 --think
python3 play_with_gemini.py --temperature 0.7
python3 play_with_gpt.py --debug
python3 play_with_claude.py --thinking-level MEDIUM
python3 play_with_gpt.py --thinking-level HIGH --verbosity low
python3 play_with_gpt.py --thinking-level XHIGH --max-output-tokens 32000
python3 play.py --thinking-level MEDIUM --verbose
python3 play_with_gemini.py --thinking-level MEDIUM --verbose
```

### Debug Mode

When the game is running with `--debug`, the engine logs:

- Every tool call and response (same as `--verbose`)
- Raw JSON responses from the AI model
- AI thinking/reasoning panels (when available)
- Automatic retry messages for empty or malformed responses

---

## Known Issues

### GPT-5.5 Models (Responses API)

GPT-5.5 models consume "thinking tokens" from the total `--max-output-tokens` budget. If this is set too low, the model may run out of space before finishing its reasoning, leading to truncated responses. Increase `--max-output-tokens` if you see incomplete output.

Temperature is deprecated for GPT-5.5 models (internally locked to 1.0). Use `--thinking-level` and `--verbosity` to control behavior instead.

### GPT-5.4 Models (Legacy)

GPT-5.4 models sometimes return completely empty responses (no text, no tool calls). The engine automatically detects this and re-sends your last action as a new message, up to 3 times. If retries are exhausted, the empty response is shown as-is.

### Gemini Preview Models

`gemini-3.1-pro-preview` and `gemini-3-flash-preview` have bugs with function calling:

- **Malformed tool calls** — These models intermittently send broken function calls. The engine retries with corrective messages to guide the model back on track.
- **Thinking leakage (3.1-pro only)** — The model may output its internal reasoning as visible text in the game narrative. There is currently no workaround.
- **Thinking-only responses** — When `--thinking-level` is enabled, the model may think without producing any output. The engine auto-injects "Continue" to prompt a response.

The default temperature for Gemini is set to `1.0` (instead of the usual `0.0`) because this significantly reduces malformed call rates. You can override this with `--temperature`.

---

## Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| Game Engine | MCP (Model Context Protocol) server + SQLite |
| Terminal UI | Rich + prompt_toolkit |
| Data Validation | Pydantic |
| Config | YAML |
| AI Backends | Ollama, OpenAI, Google Gemini, Anthropic Claude |

---

## License

This project is licensed under the [MIT License](LICENSE).
