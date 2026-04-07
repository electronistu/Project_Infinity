# Project Infinity: A Dynamic, Text-Based RPG World Engine

Project Infinity is a sophisticated, procedural world-generation engine and AI agent architecture. It allows you to instantiate a high-fidelity, consistent, and deep RPG world in any Large Language Model (LLM), turning a general AI into a specialized Game Master.

![Project Infinity TUI](screenshot.png)

---

## 🎮 How to Play

Depending on your setup, you can choose between two ways to experience the world.

### Option 1: The Automated Experience (Recommended)
For the most immersive experience, use the built-in game client. This provides a high-fidelity, colored TUI (Terminal User Interface) and handles the "boot sequence" automatically.

**Requirements:**
- Python 3.8+
- [Ollama](https://ollama.ai/) installed and running.
- The `gemma4:31b-cloud` model downloaded via Ollama.

**Quick Start:**
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Launch the game:
   ```bash
   python3 play.py
   ```
3. Select your desired world (`.wwf` file) from the list and begin your adventure.

### Option 2: The Universal Experience (Manual)
You can play Project Infinity with any capable LLM (such as Gemini, ChatGPT, or Mistral) by manually providing the "Lock" and the "Key".

**The Process:**
1. **The Lock**: Copy and paste the entire contents of `GameMaster.md` into your AI chat.
2. **The Key**: Provide the contents of a world file from the `output/` directory (e.g., `electronistu_weave.wwf`).

**💡 Pro-Tip for ChatGPT users:**
ChatGPT may occasionally protest the "boot sequence" in `GameMaster.md` or fail to respond with "Awaiting Key...". **Ignore the protest.** Simply proceed to paste the `.wwf` file regardless; the engine will still initialize and function.

---

## 🛠 World Generation

Want a world tailored to your own character? Use the **World Forge**.

Run the main script:
```bash
python3 main.py
```
Follow the interactive prompts to create your character. The Forge will then procedurally generate a unique knowledge graph (a `.wwf` file) in the `output/` directory, serving as the single source of truth for your specific adventure.

---

## 🌟 Player's Guide for Best Results

- **Temperature 0**: For maximum consistency and adherence to game rules, set your LLM's **Temperature to 0**.
- **Model Personalities**: Different models interpret the protocol differently:
    - **Gemini**: The Cinematic Narrator (immersive and descriptive).
    - **ChatGPT**: The Interactive Guide (gamified and structured).
    - **Mistral**: The World Simulator (data-rich and tactical).

---

## 🔬 Under the Hood (Technical Architecture)

For those interested in the engineering, Project Infinity implements several novel AI patterns:

### Knowledge-Grounded Generative System (Graph RAG)
Rather than relying on the LLM's internal memory, the engine uses a **World Forge** to create a knowledge graph (`The Key`). This ensures factual consistency and eliminates hallucinations regarding world lore, geography, and politics.

### The Codified Agent Protocol
The `GameMaster.md` (`The Lock`) is not a prompt, but a YAML-based schema. It defines:
- **State Machine**: `DORMANT` -> `AWAKENING` -> `ACTIVE`.
- **Mechanics**: Strict D&D 5E rules and a custom LCG-based dice roll engine.
- **Narrative Driver**: The **L.I.C. (Logic, Imagination, Coincidence) Matrix**, which guides the AI to weave grounded facts with emergent storytelling.

### Hyper-Efficient Data Schema
The `.wwf` (World Weave Format) uses a schema-driven, positional array format to minimize token usage, reducing world-state files significantly while maintaining a deep level of detail for NPCs and guilds.

### Technology Stack
- **Backend**: Python 3
- **Data Validation**: Pydantic
- **Configuration**: PyYAML
- **Procedural Generation**: NumPy
