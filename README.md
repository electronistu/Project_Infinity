# Project Infinity: A Dynamic, Text-Based RPG World Engine

Project Infinity is a sophisticated, procedural world-generation engine and AI agent architecture. It transforms a general-purpose Large Language Model (LLM) into a specialized Game Master by combining a codified agent protocol with an external mechanical authority, ensuring a consistent, fair, and deep RPG experience.

![Project Infinity TUI](screenshot.png)

---

## 🎮 Entry Points: The Two Paths to Adventure

Depending on your setup, you can experience the world through two different levels of mechanical authority.

### 1. The Automated Path (The Authoritative Experience)
This mode utilizes an external **Model Context Protocol (MCP)** server to act as the absolute authority for game mechanics. By offloading logic to a dedicated server, it eliminates "LLM luck" and hallucinations regarding stats and dice rolls.

**The MCP Advantage:**
- **Verified Dice:** All rolls are performed externally and returned to the AI.
- **State Authority:** Player progress is tracked in a real-time SQLite database, preventing "memory drift."
- **Fairness:** Every mechanical result is mathematically accurate and transparent.

**Requirements:**
- Python 3.8+
- [Ollama](https://ollama.ai/) installed and running.
- Supported models: `gemma4:31b-cloud` or `qwen3.5:cloud`.

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

### 2. The Universal Path (The Manual Experience)
Play with any capable LLM (Gemini, ChatGPT, Mistral) by manually providing the "Lock" and the "Key."

**The Process:**
1. **The Lock:** Copy and paste the entire contents of `GameMaster.md` into your AI chat.
2. **The Key:** Provide a world file from the `output/` directory (e.g., `electronistu_weave.wwf`).

**The Trade-off:**
Since standard chat interfaces cannot communicate with the MCP server, the Game Master uses an internal deterministic formula (LCG) and chat history to manage state. This experience is more prone to memory drift and is less transparent than the MCP-powered automated path.

---

## 🔬 Technical Architecture

Project Infinity ensures game consistency through a trio of authoritative systems.

### The Roll Engine
To ensure fairness, the engine splits mechanical outcomes into two distinct layers:
- **Complexity Checks (The d20):** Uses `perform_check` to determine binary success or failure for both players and NPCs against a Difficulty Class (DC).
- **Magnitude & Damage (The Multi-Dice):** Uses `roll_dice` to determine the impact of actions for all participants (players and creatures), including damage, healing, and quantity.
- **Verification:** All rolls MUST be output in a transparent formula: `{actor} {notation}: {total} ({rolls} + {mod})`.

### State Authority
To solve the problem of LLM "forgetfulness," the engine implements a dynamic state-tracking system:
- **The `.player` Sidecar:** Each world (`.wwf`) is paired with a JSON file containing the character's current state.
- **In-Memory SQLite Engine:** Upon boot, the MCP server initializes a queryable database from the player file.
- **Real-Time Synchronization:** The Game Master updates the database via MCP tools immediately as HP, XP, or inventory changes occur in the narrative.

### World Weave Format (.wwf)
The engine uses a schema-driven, positional array format (Graph RAG) to store world lore. This ensures factual consistency across geography, politics, and NPCs while significantly reducing token usage compared to standard text descriptions.

---

## 🛠 The World Forge

Tired of generic settings? Use the **World Forge** to create a world tailored to your character.

Run the forge:
```bash
python3 main.py
```
The Forge guides you through character creation and procedurally generates a unique knowledge graph (`.wwf` file) in the `output/` directory, which serves as the single source of truth for your specific adventure.

---

## 🌟 The Game Master's Codex

For the most immersive and consistent experience:
- **Temperature 0:** Set your LLM's **Temperature to 0**. This is critical for maximum rule adherence and consistency.
- **Model Selection:** Larger models generally produce richer narratives and better adhere to the complex MCP protocols.

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
