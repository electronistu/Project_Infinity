# Project Infinity: A Dynamic, Text-Based RPG World Engine

Project Infinity is a sophisticated, procedural world-generation engine and AI agent architecture. It transforms a general-purpose Large Language Model (LLM) into a specialized Game Master by combining a codified agent protocol with an external mechanical authority, ensuring a consistent, fair, and deep RPG experience.

![Project Infinity TUI](screenshot.png)

---

## 🎮 How to Play: The Authoritative Experience

This mode utilizes an external **Model Context Protocol (MCP)** server to act as the absolute authority for game mechanics. By offloading logic to a dedicated server, it eliminates "LLM luck" and hallucinations regarding stats and dice rolls.

**The MCP Advantage:**
- **Verified Dice:** All rolls are performed externally and returned to the AI.
- **State Authority:** Player progress is tracked in a real-time SQLite database, preventing "memory drift."
- **Fairness:** Every mechanical result is mathematically accurate and transparent.

**Requirements:**
- Python 3.11+
- Install dependencies:
  ```bash
  pip install -r requirements.txt
  ```

### Ollama Backend

- [Ollama](https://ollama.ai/) installed and running.
- Supported model: `glm-5.1:cloud`
- **Note:** While other models may follow the Game Master protocol effectively, they tend to struggle with correctly awarding XP on creature/NPC kills. `glm-5.1:cloud` is currently the only model that handles this reliably.

**Quick Start:**
1. Launch the game:
   ```bash
   python3 play.py
   ```
2. Select your model and world file (`.wwf`).

### Gemini Backend

- A Google AI API key with access to Gemini models.
- Supported models: `gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `gemini-2.5-pro`
- **Note:** `gemini-3.1-pro-preview` and `gemini-3-flash-preview` have known issues. See [Known Issues](#known-issues) for details and workarounds.

**Quick Start:**
1. Set your API key:
   ```bash
   export GEMINI_API_KEY=your-api-key
   ```
2. Launch the game:
   ```bash
   python3 play_with_gemini.py
   ```
3. Select your model and world file (`.wwf`).

---

## 🔬 Technical Architecture

Project Infinity ensures game consistency through these authoritative systems:

### The Roll Engine
To ensure fairness, the engine splits mechanical outcomes into two distinct layers:
- **Complexity Checks (The d20):** Uses `perform_check` to determine binary success or failure for both players and NPCs against a Difficulty Class (DC).
- **Magnitude & Damage (The Multi-Dice):** Uses `roll_dice` to determine the impact of actions for all participants (players and creatures), including damage, healing, and quantity.
- **Verification:** All rolls MUST be output in a transparent formula: `{actor} {notation}: {total} ({rolls} + {mod})`.

### State Authority
To solve the problem of LLM "forgetfulness," the engine implements a dynamic state-tracking system:
- **In-Memory SQLite Engine:** Upon boot, the MCP server initializes a queryable database from the player file.
- **Real-Time Synchronization:** The Game Master updates the player database via MCP tools immediately as changes occur in the narrative.
- **Periodic State Synchronization:** To prevent long-term divergence, the system triggers a mandatory synchronization cycle every 4 prompts, forcing the GM to verify and reconcile the database state via a `dump_player_db` handshake.

### Cognitive Load Management
To prevent "model collapse" during high-complexity turns, the engine implements a **Phased Resolution Protocol**:
- **Mechanical Resolution Phase:** The GM resolves all mechanical truths (rolls, state updates) via a **Linear Resolution Sequence**. The GM emits a batch of all required tool calls and must emit a pause token (`{{_NEED_AN_OTHER_PROMPT}}`) immediately after receiving the results.
- **Sync Handshake:** The system intercepts the pause token and injects a resume signal (`{{_CONTINUE_EXECUTION}}`). This handshake ensures the GM has internally audited all mechanical results before moving to narrative.
- **Narrative Phase:** Only after the sync handshake is complete does the GM transition to storytelling, ensuring the narrative is based on the complete, verified state.

---

## 🛠 The World Forge

Use the **World Forge** to create a world tailored to your character.

Run the forge:
```bash
python3 main.py
```
The Forge guides you through character creation and procedurally generates a world knowledge graph (`.wwf` file) and a corresponding character state file (`.player`) in the `output/` directory. Together, these files serve as the complete source of truth for your adventure.

When you launch `play.py` (Ollama) or `play_with_gemini.py` (Gemini), the system initializes the LLM with the `GameMaster_MCP.md` protocol as the system prompt and injects the `.wwf` file as the activation key to awaken the Game Master. Simultaneously, the game engine initializes `dice_server.py` using the `.player` file to boot the SQLite database.

---

## 🌟 The Game Master's Codex

- **Verbose Mode:** Use the `--verbose` or `-v` flag when launching `play.py` or `play_with_gemini.py` to see detailed MCP tool calls and responses.
- **Developer Debug Mode:** Use the `--debug` or `-d` flag for deep inspection. This displays the raw JSON responses from the LLM—including internal reasoning and thought processes—and automatically enables Verbose Mode.

---

## ⚠️ Known Issues

### Gemini 3.1 pro-preview & Gemini 3 Flash preview

These preview models exhibit bugs when used with function calling. The engine includes workarounds for each.

**MALFORMED_FUNCTION_CALL Responses**

Both models intermittently return malformed tool calls, causing the API to strip the entire response content. Failure rate varies significantly with temperature:

| Temperature | Failure Rate (3.1 pro) | Failure Rate (3 flash) |
|-------------|----------------------|----------------------|
| 0.0 | ~60% | similar |
| 1.0 | ~10% | similar |

*Workaround:* Default temperature is set to `1.0`. On MALFORMED_FUNCTION_CALL, the engine performs a graduated retry sequence: the first retry includes a corrective message with the full tool schema; the second retry sends a stronger warning; up to 3 additional silent retries follow. This provides the model with context about its error and breaks deterministic retry loops.

Override the default with `--temperature`:
```bash
python3 play_with_gemini.py --temperature 0.7
```

**Thinking Leakage** (gemini-3.1-pro-preview only)

The model outputs its internal reasoning as regular text (prefixed with `thinking\n...`) instead of using the SDK's structured `part.thought` attribute. This reasoning text appears in the game narrative. `gemini-3-flash-preview` does not exhibit this behavior.

*No workaround available.* A heuristic-based strip was attempted but is unreliable — the thinking block typically contains multiple paragraphs separated by `\n\n`, and there is no reliable way to detect the thinking→narrative boundary without structured `part.thought` support from the model. When `--thinking-level` is enabled and the model supports it, debug mode displays structured thinking in a separate yellow panel.

**Structured Thinking Incompatibility**

The Google GenAI SDK's `ThinkingConfig` enables clean separation of reasoning and content via the `part.thought` attribute. However, enabling it with `gemini-3.1-pro-preview` causes `MALFORMED_FUNCTION_CALL` at near-100% rates.

*Workaround:* Structured thinking is disabled by default. The `--thinking-level` flag is preserved for future model versions and can be enabled experimentally:
```bash
python3 play_with_gemini.py --thinking-level MEDIUM
```
When enabled, debug mode displays model thinking in a yellow panel labeled "Thinking (structured)".

**Thinking-Only Responses**

With `--thinking-level` enabled, the model may complete its reasoning but produce no final text or tool calls (the "thinking-only" bug).

*Workaround:* The engine auto-injects `"Continue"` (up to 3 attempts) to prompt the model to produce output. If all retries are exhausted, a placeholder message is shown: *"The GM pauses, deep in thought..."*

---

## 🛠 Technology Stack

**Core Dependencies (shared):**
- `mcp`: Model Context Protocol for external tool integration.
- `rich`: High-fidelity Terminal User Interface (TUI).
- `pydantic`: Data validation and settings management.
- `prompt_toolkit`: Interactive terminal input with line editing and slash commands.
- `pyyaml`: Protocol and schema configuration.

**Backend Dependencies:**
- `ollama`: LLM orchestration via Ollama (Ollama backend).
- `google-genai`: Google Gemini API client (Gemini backend).

**Infrastructure:**
- Python 3
- SQLite (In-memory engine)
- Graph RAG architecture
