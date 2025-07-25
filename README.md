# Project Infinity v3: The Dynamic World Forge

**Project Infinity is a sophisticated, procedural text-based RPG world generator that creates a unique and dynamic world from a single seed: the player's character.** This project represents a complete overhaul (v3) of the original concept, focusing on a clean, modular architecture and a unique gameplay loop.

The core philosophy of Project Infinity is the separation of the **"Key"** and the **"Lock."**

*   **The Key (`The Forge`):** The world generator itself, a Python application (`main.py`) that orchestrates a complex, multi-layered generation cascade. It takes a player's character choices as the initial seed and procedurally builds an entire world around them—its history, geography, factions, NPCs, economies, and quests. The final output is a highly compressed, token-efficient **World-Weave Key (`.wwf`)**, a file that contains the entire generated world state.

*   **The Lock (`The Game Master`):** A Large Language Model (LLM) primed with the `GameMaster.md` persona. This persona instructs the LLM to act as a text-based RPG Game Master. The game begins when the player provides the World-Weave Key to the Game Master, which then "unlocks" the world and allows the player to interact with it. For optimal performance, **Gemini 2.5 Pro** with a temperature of **0.0** is recommended.

## Features

*   **Dynamic, Seed-Based World Generation:** Every world is unique and directly influenced by the player's initial character creation choices (name, race, class, alignment, and stats).
*   **Modular Generation Cascade:** The world is built in a series of logical, reactive layers, ensuring a cohesive and internally consistent world state.
*   **AI-Powered Game Master:** Leverages the power of modern LLMs to provide a flexible and responsive gameplay experience.
*   **Stat-Based Point-Buy System:** A D&D-inspired stat allocation system where the cost of increasing a stat is influenced by the character's chosen class.
*   **Token-Efficient World State:** The custom `.wwf` (World-Weave Format) is designed to be as compact as possible, allowing for large and complex worlds to be loaded into an LLM's context window.
*   **Rich World Simulation:** The generated world includes:
    *   A grid-based world map with diverse biomes and locations.
    *   Procedurally generated roads connecting locations.
    *   Multiple factions with their own dispositions and relationships.
    *   A simulated economy with world-level gold tracking.
    *   Unique NPCs with family ties, roles, and faction memberships.
    *   A dynamic quest system with prerequisites and rewards.
    *   An ability shop for character progression.

## The World Generation Cascade

The heart of Project Infinity is the `main.py` script, which orchestrates the world generation cascade. Each step in the cascade is handled by a dedicated module that adds a new layer of detail to the `WorldState` object. The cascade order is as follows:

1.  `geopolitical_generator.py`: Establishes the high-level political landscape, including major locations and factions.
2.  `sociological_generator.py`: Populates the world with NPCs, families, and social structures.
3.  `economic_generator.py`: Simulates the world's economy, distributing gold and resources.
4.  `quest_generator.py`: Creates a series of interconnected quests for the player to discover.
5.  `world_details_generator.py`: Adds flavor text, environmental details, and other finishing touches.
6.  `abilities_generator.py`: Generates a unique set of abilities available for purchase.
7.  `roads_generator.py`: Creates a logical road network connecting the various locations on the world map.
8.  `time_and_event_generator.py`: Initializes the world's clock and any starting events.
9.  `formatter.py`: The final step, which takes the completed `WorldState` object and "weaves" it into the compressed `.wwf` string.

## How to Play

1.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

2.  **Forge Your World-Weave Key:**
    ```bash
    python main.py
    ```
    This will launch the character creation interview. Your choices will be used to generate a unique `.wwf` file (e.g., `yourname_yourclass_weave_v3.wwf`).

3.  **Awaken the Game Master:**
    *   Use an LLM interface (such as a custom API script, LM Studio, etc.) that allows for a custom system prompt/persona.
    *   Load the contents of `GameMaster.md` as the system prompt.
    *   Paste the **entire contents** of your generated `.wwf` file as the first user message.
    *   The Game Master will then greet you, and your adventure will begin.

## The World-Weave Format (`.wwf`)

The `.wwf` file is a custom, token-efficient format designed to represent the entire world state in a way that is easily parsable by an LLM. It uses a simple key-value and delimited format to structure the data. This includes:

*   `[WORLD_STATE]`: Metadata about the world instance.
*   `[PLAYER_CHARACTER_V3]`: The player's character sheet.
*   `[EQUIPMENT]`: A list of all equipped items for the player and NPCs.
*   `[CODED_MAP_V3]`: The grid-based world map.
*   `[LOCATIONS]`: Details for every major location.
*   `[SUB_LOCATIONS]`: Details for sub-locations within major locations.
*   `[FACTIONS]` & `[FACTION_RELATIONS]`: Information on the world's factions and their relationships.
*   `[NPCS]`: A roster of all non-player characters.
*   `[CREATURES]`: A list of all creatures in the world.
*   `[QUESTS]`: The quest log for the entire world.
*   `[ABILITY_SHOP]`: The list of available abilities for purchase.
*   `[CHRONICLE_TRIGGERS]` & `[ENVIRONMENTAL_PROSE]`: Flavor text and other details.

## For Developers

The code is designed to be modular and extensible. The `models.py` file, using `pydantic`, defines the core data structures for the entire application, providing a strong, typed foundation for the world state. Each generator module is self-contained and can be modified or replaced to alter the world generation process.
