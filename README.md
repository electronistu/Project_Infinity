# Project Infinity: A Dynamic, Text-Based RPG World Engine

Project Infinity is a sophisticated, procedural world-generation engine and AI agent architecture that demonstrates a novel solution to several critical challenges in modern AI, including state management, factual consistency, and prompt injection resistance.

## A Case Study in Next-Generation AI Architecture

This project serves as a proof-of-concept for building highly capable, consistent, and secure AI agents. By integrating a procedural generation engine with a knowledge-grounded Large Language Model (LLM), Project Infinity successfully overcomes several critical challenges in the field.

### Key Innovations

*   **Knowledge-Grounded Generative System (Graph RAG):**
    At its core, Project Infinity utilizes a Graph RAG architecture. A "World Forge" engine first generates a comprehensive knowledge graph (`The Key`) that serves as a "single source of truth" for the AI. This graph is not just a list of entities, but a deeply interconnected world model of lore, politics, and geography. Grounding the agent in this graph solves the core problem of model hallucination.

*   **Prompt-Based Operating System (PBOS):**
    The project's primary innovation is the creation of a **Prompt-Based Operating System**. The `GameMaster.md` file (`The Lock`) is not merely a prompt; it is a new class of software. It's a comprehensive, text-based protocol that constrains a powerful, general LLM into a specialized, secure, and predictable agent. This PBOS dictates the agent's personality, its rules of engagement, and its allowed actions, creating a robust "cognitive sandbox" that is intrinsically resistant to prompt injection. This is the state-of-the-art in agentic control.

*   **Proprietary Narrative Engine (L.I.C. Matrix):**
    Beyond simple factual retrieval, the agent's storytelling is governed by the **L.I.C. (Logic, Imagination, Coincidence) Matrix**. This proprietary framework acts as an "imagination driver," guiding the AI to weave facts from the knowledge graph with emergent story elements in a way that feels meaningful, creative, and alive.

### Broader Implications

While demonstrated within a complex gaming simulation, the architecture of Project Infinity serves as a powerful blueprint for a new class of enterprise-grade AI agents. The project's success in achieving stateful consistency and intrinsic security via the **Prompt-Based Operating System** presents a viable path forward for developing specialized AI that is not only highly capable but also reliable and safe for critical applications.

---

## Technology Stack

*   **Backend:** Python 3
*   **Data Validation:** Pydantic
*   **Configuration:** PyYAML
*   **Procedural Generation:** NumPy, noise

## The "Lock & Key" System

The engine's core design principle is the separation of the world's rules from the world's data.

*   **The Lock (`GameMaster.md`):** This file is the **Prompt-Based Operating System (PBOS)**. It is a comprehensive protocol that instructs a general LLM on how to interpret the world data, manage game mechanics, and embody a specific, specialized agent persona.

*   **The Key (`output/<character_name>_weave.wwf`):** This is the **Knowledge Graph**. It is a pre-generated world-state file that contains the core, static data of a unique world and serves as the agent's single source of truth.

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
