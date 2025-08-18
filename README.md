# Project Infinity: A Dynamic, Text-Based RPG World Engine

Project Infinity is a sophisticated, procedural world-generation engine and AI agent architecture that demonstrates a novel solution to several critical challenges in modern AI, including state management, factual consistency, and prompt injection resistance.

## A Case Study in Next-Generation AI Architecture

This project serves as a proof-of-concept for building highly capable, consistent, and secure AI agents. By integrating a procedural generation engine with a knowledge-grounded Large Language Model (LLM), Project Infinity successfully overcomes several critical challenges in the field.

### Key Innovations

*   **Knowledge-Grounded Generative System (Graph RAG):**
    At its core, Project Infinity utilizes a Graph RAG architecture. A "World Forge" engine first generates a comprehensive knowledge graph that serves as a "single source of truth" for the AI. This graph is not just a list of entities, but a deeply interconnected world model that includes:
    *   A persistent **100x100 tile world map** that provides a concrete geographical sandbox for exploration.
    *   Multiple **kingdoms**, each with a unique set of guilds and political alignments.
    *   A **procedurally generated history** of conflicts and relationships between the kingdoms.
    The LLM agent is grounded in this rich, multi-faceted graph, which solves the core problem of hallucination by giving the AI a solid foundation of lore, politics, and geography to base its narrative on. The architecture is also designed for scalability, allowing for easy extension.

*   **Emergent World-Building & Extensibility:**
    The Game Master's primary role is to breathe life into the foundational scaffold. It dynamically generates the personality and descriptions for all characters, the layouts of dungeons, and the atmosphere of cities based on the core rules provided. Furthermore, it can create new content on the fly, such as emergent quests or new creatures (e.g., "giant rats") with appropriate stats and XP rewards. This ensures the world feels reactive and alive. The architecture is also highly extensible, allowing the foundational scaffold to be easily expanded with new entities.

*   **Advanced Narrative Engine (L.I.C. Matrix & Unified Field):**
    Beyond simple extrapolation, the agent's storytelling is governed by a set of sophisticated narrative protocols. These include the **Unified Field** paradigm, which ensures a cohesive world by treating all conflicts as part of an interconnected whole, and the **L.I.C. (Logic, Imagination, Coincidence) Matrix**, which guides the AI to weave facts from the scaffold with emergent story elements in a way that feels meaningful and alive.

*   **Intrinsic Persona-Based Security:**
    The project demonstrates a world-class solution to prompt injection. Security is not an external filter but an emergent property of the agent's deeply specified operational protocol. The protocol creates a robust "cognitive sandbox" that instructs the agent to interpret malicious or out-of-context inputs as internal anomalies rather than commands to be obeyed.

### Broader Implications

While demonstrated within a complex gaming simulation, the architecture of Project Infinity serves as a powerful blueprint for a new class of enterprise-grade AI agents. The project's success in achieving stateful consistency and intrinsic security presents a viable path forward for developing specialized AI that is not only highly capable but also reliable and safe for critical applications.

---

## Technology Stack

*   **Backend:** Python 3
*   **Data Validation:** Pydantic
*   **Configuration:** PyYAML
*   **Procedural Generation:** NumPy, noise

## The "Lock & Key" System

The engine's core design principle is the separation of the world's rules from the world's data.

*   **The Lock (`GameMaster.md`):** This is a comprehensive protocol document that acts as the "operating system" for the Game Master AI. It instructs the LLM on how to interpret the world data, manage game mechanics, and respond to the player.

*   **The Key (`output/<character_name>_weave.wwf`):** This is a pre-generated world-state file that contains the core, static data of a unique world. It is designed to be loaded by the Game Master AI.

## Getting Started

### 1. Prerequisites

*   Python 3.8+
*   `git`

### 2. Installation

```bash
# Clone the repository
git clone https://github.com/electronistu/Project_Infinity
cd project_infinity

# Create and activate a Python virtual environment
python3 -m venv venv
source venv/bin/activate

# Install the required dependencies
pip install -r requirements.txt
```

### 3. World Generation (Optional)

To forge your own unique world, run the main script:

```bash
python3 main.py
```

This will launch the interactive character creator. Follow the prompts to build your character, after which the Forge will generate your world. The output will be saved as a new `.wwf` file in the `output/` directory, named after your character.

For development, you can bypass the interactive prompts using the `--debug` flag:

```bash
python3 main.py --debug
```

## How to Play

This project includes a pre-generated world file, `output/electronistu_weave.wwf`, so you can start playing immediately.

It is highly recommended to use a powerful Large Language Model with a large context window to run the game.

### Recommended Platforms:

1.  **Google AI Studio (Easiest):**
    *   Go to [aistudio.google.com](https://aistudio.google.com).
    *   Select the **Gemini 2.5 Pro** or **Gemini 2.5 Flash** model.
    *   Set the **Temperature** to `0` for maximum consistency.
    *   Follow the two-step "Lock & Key" process below.

2.  **Gemini CLI (Advanced):**
    *   For users comfortable with the command line, the Gemini CLI provides a powerful and efficient way to play.
    *   Follow the two-step "Lock & Key" process below.

### The "Lock & Key" Process:

1.  **Load the "Lock":** Start your session by providing the contents of the `GameMaster.md` file to your chosen AI platform.

2.  **Await Confirmation:** The AI should respond with the words: `Waiting for the .wwf key`.

3.  **Provide the "Key":** Paste the entire contents of the generated `.wwf` file (e.g. `output/electronistu_weave.wwf`).

4.  **Begin Your Adventure:** The Game Master will parse the world and begin your unique, text-based adventure.
