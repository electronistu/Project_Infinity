# Project Infinity

A text-based RPG where an AI acts as your Dungeon Master — with real dice rolls, real character tracking, and real D&D 5e rules. No hallucinated stats. No forgotten inventory. The AI rolls fairly, tracks your HP, and levels you up automatically.

![Project Infinity TUI](screenshot.png)

---

## What Makes This Different?

Most AI RPGs let the language model make up numbers. Project Infinity doesn't. Every dice roll, every stat change, every level-up goes through an external game engine that the AI can read but not fake. The result is a Dungeon Master that actually plays by the rules.

- **Fair Dice** — All rolls are performed by a dedicated server and verified. The AI sees the results, it doesn't generate them.
- **Persistent Character** — Your stats, inventory, gold, and spell slots live in a real database that updates in real time. No "forgetting" that you used your last potion.
- **D&D 5e Rules** — Leveling up, spell slot recovery, proficiency bonuses — all handled automatically by the engine.
- **Procedural Worlds** — The World Forge creates a unique world, NPCs, guilds, and political relationships every time.
- **In-Game Commands** — Check your stats, force a database sync, or get help without leaving the game.

---

## Quick Start

### 1. Prerequisites

- **Python 3.11** or newer
- **One AI backend** (pick one):
  - **Ollama** — free, runs locally on your machine
  - **OpenAI** — cloud-based, requires a paid API key
  - **Gemini** — cloud-based, requires a paid API key

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

#### Option A: Ollama (Free, Local)

1. Install [Ollama](https://ollama.ai/) and make sure it's running.
2. Download a supported model:
   ```bash
   ollama pull glm-5.1:cloud
   ```
   Supported models: `glm-5.1:cloud`, `kimi-k2.6:cloud`

#### Option B: OpenAI (Cloud, Paid)

Set your API key as an environment variable:
```bash
export OPENAI_API_KEY=your-api-key
```
Supported models: `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`

#### Option C: Gemini (Cloud, Paid)

Set your API key as an environment variable:
```bash
export GEMINI_API_KEY=your-api-key
```
Supported models: `gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `gemini-2.5-pro`

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

### Roll Engine

All game mechanics are split into two categories:

- **Success/Failure Checks** — Attack rolls, skill checks, and saving throws use a d20 system (`perform_check`). The AI cannot decide outcomes — it must call the tool and report the result.
- **Damage & Magnitude** — Damage rolls, healing, and quantity use multi-dice notation (`roll_dice`). Again, the AI calls the tool; it doesn't make up numbers.
- **Transparency** — Every roll is shown to you in a standard format: `Guard Attack: 17 vs DC 15 (Success) (15 + 2)`

### State Authority

Your character lives in an in-memory database that the AI updates through tool calls. Your HP, gold, inventory, spell slots — all of it is tracked precisely.

- The AI is required to update state immediately when changes happen.
- Every 4 prompts, the engine forces a full database sync to catch any drift.
- You can manually force a sync at any time with `/sync`.

### Phased Resolution

To prevent the AI from "collapsing" on complex turns (trying to narrate and calculate at the same time), the engine uses a two-phase protocol:

1. **Mechanical Phase** — The AI resolves all dice rolls and state updates first, pausing with a sync token.
2. **Narrative Phase** — Only after all mechanics are verified does the AI produce its story output.

This means you always get mechanically accurate results before the narrative.

---

## Advanced Options

All three play scripts accept the following flags:

| Flag | Description |
|------|-------------|
| `--verbose`, `-v` | Show all tool calls and their results behind the scenes |
| `--debug`, `-d` | Show raw AI responses including internal reasoning (also enables `--verbose`) |

**Gemini and OpenAI only:**

| Flag | Description |
|------|-------------|
| `--temperature` | Set sampling temperature (Gemini default: 1.0, OpenAI default: 0.0) |
| `--thinking-level` | Enable structured AI reasoning (`LOW`, `MEDIUM`, `HIGH`). Experimental — see Known Issues. |

Examples:
```bash
python3 play_with_gemini.py --temperature 0.7
python3 play_with_gpt.py --debug
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

### GPT-5.4 Models

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
| AI Backends | Ollama, OpenAI, Google Gemini |

---

## License

This project is licensed under the [MIT License](LICENSE).