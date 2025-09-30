# Project Infinity: A Dynamic, Text-Based RPG World Engine

Project Infinity is a sophisticated, procedural world-generation engine and AI agent architecture. It demonstrates a novel solution to several critical challenges in modern AI, including state management, factual consistency, and the creation of highly efficient, specialized agents. The latest version introduces a radically improved agent protocol that enables more dynamic, emergent storytelling and achieves a new level of LLM-agnostic portability.

## A Case Study in Next-Generation AI Architecture

This project serves as a proof-of-concept for building highly capable, consistent, and secure AI agents. By integrating a procedural generation engine with a knowledge-grounded Large Language Model (LLM), Project Infinity successfully overcomes several critical challenges in the field.

### Key Innovations

*   **Knowledge-Grounded Generative System (Graph RAG):**
    At its core, Project Infinity utilizes a Graph RAG architecture. A "World Forge" engine first generates a comprehensive knowledge graph (`The Key`) that serves as a "single source of truth" for the AI. This graph is not just a list of entities, but a deeply interconnected world model of lore, politics, and geography. Grounding the agent in this graph solves the core problem of model hallucination.

*   **Codified Agent Protocol:**
    The project's primary innovation is its method for agent specialization. The `GameMaster.md` file (`The Lock`) is not a natural language prompt, but a highly structured, token-efficient protocol. Written as a YAML-based schema, it defines the agent's core logic, operational states, and behavioral directives in a format optimized for LLM-to-LLM communication. Crucially, the protocol now includes priming meta-instructions, making it robustly compatible across different foundational models (including Gemini, ChatGPT, and Mistral), ensuring the agent behaves consistently in any environment.

*   **Proprietary Narrative Engine (L.I.C. Matrix):**
    Beyond simple factual retrieval, the agent's storytelling is governed by the **L.I.C. (Logic, Imagination, Coincidence) Matrix**. This proprietary framework acts as an "imagination driver," guiding the AI to weave facts from the knowledge graph with emergent story elements in a way that feels meaningful, creative, and alive.

### Broader Implications

While demonstrated within a complex gaming simulation, the architecture of Project Infinity serves as a powerful blueprint for a new class of enterprise-grade AI agents. The project's success in achieving stateful consistency and intrinsic security via its codified protocol presents a viable path forward for developing specialized AI that is not only highly capable but also reliable and safe for critical applications.

---

## Technology Stack

*   **Backend:** Python 3
*   **Data Validation:** Pydantic
*   **Configuration:** PyYAML
*   **Procedural Generation:** NumPy, noise

## The "Lock & Key" System

The engine's core design principle is the separation of the agent's rules from the world's data.

*   **The Lock (`GameMaster.md`):** This file is the **Codified Agent Protocol**. It is a YAML-based schema that instructs a general LLM on how to interpret world data, manage game mechanics, and execute its core logic. The latest version is a fraction of the size of its natural language predecessor, resulting in a huge leap in token efficiency and performance.

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

### Compatible Platforms

The protocol is designed to be LLM-agnostic and has been successfully tested on the following platforms. For best results, use the latest available models and set the **Temperature** to `0` for maximum consistency.

*   **Google:** Gemini 2.5 Pro (via AI Studio, Gemini CLI, etc.)
*   **OpenAI:** ChatGPT-5
*   **Mistral AI:** chat.mistral.ai

### The "Lock & Key" Process:

1.  **Load the "Lock":** Start your session by providing the contents of the `GameMaster.md` file to your chosen AI platform.

2.  **Await Confirmation:** The AI should respond with the words: `Awaiting Key...`

3.  **Provide the "Key":** Paste the entire contents of the generated `.wwf` file (e.g. `output/electronistu_weave.wwf`).

4.  **Begin Your Adventure:** The Game Master will parse the world and begin your unique, text-based adventure.

### Emergent Agent Personas

A fascinating outcome of this project is observing the distinct "personalities" that emerge when the same `GameMaster.md` protocol is executed by different foundational models. While the core rules and logic remain identical, the *flavor* of the Game Master changes, revealing the unique architectural biases of each LLM.

*   **Gemini as "The Cinematic Narrator":** Gemini tends to produce a highly immersive, story-focused experience. Its output is often cinematic, with descriptive prose that sets a rich scene and immediately draws the player into a narrative, much like the opening of a film.

*   **ChatGPT as "The Interactive Guide":** ChatGPT often adopts the role of a classic Game Master. It presents the world in a slightly more gamified manner, clearly outlining choices (often with numbered lists) and explicitly referencing game concepts, creating an experience reminiscent of a classic gamebook.

*   **Mistral as "The World Simulator":** Mistral acts like a data-rich world simulator. Its output is incredibly structured, often presenting the player with a detailed dashboard of the current world state, including emergent quests, notable NPCs with stats, and environmental details. This empowers the player with a wealth of information, encouraging tactical and strategic decision-making.

This demonstrates that even with a rigid, codified protocol, the underlying model's "imagination" still shapes the final experience, making the choice of LLM a creative decision in itself.
