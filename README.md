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
- Python 3.8+
- [Ollama](https://ollama.ai/) installed and running.
- Supported models: `gemma4:31b-cloud`, `deepseek-v3.2:cloud`, `qwen3.5:397b-cloud`, `qwen3.5:cloud`, or `glm-5.1:cloud`.

**Quick Start:**
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Launch the game:
   ```bash
   python3 play.py
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
- **Periodic State Synchronization:** To prevent long-term divergence, the system triggers a mandatory synchronization cycle every 5 prompts, forcing the GM to verify and reconcile the database state via a `dump_player_db` handshake.

### Cognitive Load Management
To prevent "model collapse" during high-complexity turns, the engine implements a **Phased Resolution Protocol**:
- **Mechanical Resolution Phase:** The GM resolves all mechanical truths (rolls, state updates) using a **Batch-Sync Cycle**. It groups independent tool calls into batches and must emit a pause token (`{{_NEED_AN_OTHER_PROMPT}}`) after every batch of results.
- **Recursive Synchronization:** The system intercepts the pause token and injects a resume signal (`{{_CONTINUE_EXECUTION}}`). This cycle repeats recursively until all mechanical updates are finalized.
- **Narrative Phase:** Only after the final mechanical state is resolved and synced does the GM transition to storytelling, ensuring the narrative is based on the complete, verified state.

---

## 🛠 The World Forge

Use the **World Forge** to create a world tailored to your character.

Run the forge:
```bash
python3 main.py
```
The Forge guides you through character creation and procedurally generates a world knowledge graph (`.wwf` file) and a corresponding character state file (`.player`) in the `output/` directory. Together, these files serve as the complete source of truth for your adventure.

When you launch `play.py`, the system initializes the LLM with the `GameMaster_MCP.md` protocol as the system prompt and injects the `.wwf` file as the activation key to awaken the Game Master. Simultaneously, `play.py` initializes `dice_server.py` using the `.player` file to boot the SQLite database.

---

## 🌟 The Game Master's Codex

- **Model Selection:** Larger models generally produce richer narratives and better adhere to the complex MCP protocols.
- **Verbose Mode:** Use the `--verbose` or `-v` flag when launching `play.py` to see detailed MCP tool calls and responses.
- **Developer Debug Mode:** Use the `--debug` or `-d` flag for deep inspection. This displays the raw JSON responses from the LLM—including internal reasoning and thought processes—and automatically enables Verbose Mode.

---

## 🛠 Technology Stack

**Core Dependencies:**
- `mcp`: Model Context Protocol for external tool integration.
- `ollama`: Local LLM orchestration.
- `rich`: High-fidelity Terminal User Interface (TUI).
- `pydantic`: Data validation and settings management.
- `numpy`: Procedural generation logic.
- `pyyaml`: Protocol and schema configuration.

**Infrastructure:**
- Python 3
- SQLite (In-memory engine)
- Graph RAG architecture
