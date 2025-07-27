# Project Infinity: A Dynamic, Text-Based RPG World Engine

**Project Infinity is a sophisticated, procedural world-generation engine designed to create fully interactive, text-based role-playing experiences. It addresses the challenge of static, repetitive game worlds by generating a unique, detailed, and logically consistent environment for every playthrough, ready to be brought to life by a Large Language Model (LLM).**

This engine is not a game in itself, but a powerful "Forge" that outputs a comprehensive world-state file. This file acts as a master key, containing every detail of the world—from its continental layout and political conflicts down to the loot in a specific dungeon and the abilities sold by a guild master. When this key is given to a capable LLM, it unlocks an unscripted, dynamic D&D-style adventure where the player can do anything, unbound by predefined choices.

---

## Core Architectural Philosophy

This project was engineered with modularity, scalability, and maintainability as its core tenets. The architecture is designed to be robust and easily extensible.

*   **Modular Pipeline:** The world generation process is a clean, sequential pipeline. Each component (map generation, population, quest creation, etc.) is a self-contained Python module that can be modified, tested, or replaced without impacting the rest of the system.

*   **Configuration-Driven Design:** All game data—races, classes, items, abilities, quest templates, and creature stats—is defined in simple, human-readable `.yml` files. This decouples the game's content from its core logic, allowing for rapid content expansion and modification by designers without needing to touch the Python codebase.

*   **Data-Centric & Validated:** The entire world state is built around a set of strict Pydantic models. This ensures data integrity, prevents runtime errors, and serves as a self-documenting schema for the project's data structures.

*   **Token-Efficient Output:** The final world-state file (`.wwf`) is a custom, token-efficient format designed specifically for LLMs. It uses delimiters like `|` and `::` to structure data, providing maximum information density while minimizing the token overhead associated with formats like JSON.

## Key Features

*   **Truly Interactive Gameplay:** The generated world is designed to be used with a powerful LLM, allowing players to interact with the world using natural language, free from the constraints of multiple-choice options.

*   **Procedural World Generation:** Leverages Perlin noise to create unique and natural-looking continents, oceans, and a massive 100x100 grid map for every world.

*   **Dynamic Faction & Quest System:** Generates multiple kingdoms with unique alignments and predefined political states of war or neutrality. The quest system is context-aware, creating logical objectives based on the world's geography, factions, and inhabitants.

*   **Deep Character Progression:** Features a full D&D 5e-style interactive character creator with a point-buy stat system. Character progression is driven by a rich item and ability system, with abilities being taught by guild masters based on the player's standing.

*   **Complex Social Structures:** The world is populated with a variety of guilds (Mage, Thief, Assassin, etc.), each with its own leadership and presence determined by the kingdom's culture and alignment.

## Technology Stack

*   **Backend:** Python 3
*   **Data Validation:** Pydantic
*   **Configuration:** PyYAML
*   **Procedural Generation:** NumPy, noise

## The "Lock & Key" System

The engine's core design principle is the separation of the world's rules from the world's data.

*   **The Lock (`GameMaster.md`):** This is a comprehensive protocol document that acts as the "operating system" for the Game Master AI. It instructs the LLM on how to interpret the world data, manage game mechanics, and respond to the player.

*   **The Key (`output/electronistu_weave.wwf`):** This is a pre-generated world-state file included in the repository. It is a complete, detailed snapshot of a unique world, ready to be loaded by the Game Master AI.

## Getting Started

### 1. Prerequisites

*   Python 3.8+
*   `git`

### 2. Installation

```bash
# Clone the repository
git clone <repository_url>
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
    *   Select the **Gemini 2.5 Pro** model.
    *   Set the **Temperature** to `0` for maximum consistency.
    *   Follow the two-step "Lock & Key" process below.

2.  **Gemini CLI (Advanced):**
    *   For users comfortable with the command line, the Gemini CLI provides a powerful and efficient way to play.
    *   Follow the two-step "Lock & Key" process below.

### The "Lock & Key" Process:

1.  **Load the "Lock":** Start your session by providing the contents of the `GameMaster.md` file to your chosen AI platform.

2.  **Await Confirmation:** The AI should respond with the words: `Awaiting Key.`

3.  **Provide the "Key":** Paste the entire contents of the `output/electronistu_weave.wwf` file.

4.  **Begin Your Adventure:** The Game Master will parse the world and begin your unique, text-based adventure.
