# Project Infinity v3.3: The Dynamic World Forge

**Project Infinity is a sophisticated, procedural text-based RPG world generator that creates a unique and dynamic world from a single seed: the player's character.** This project represents a complete overhaul (v3.3) of the original concept, focusing on a clean, modular architecture and a unique gameplay loop.

The core philosophy of Project Infinity is the separation of the **"Key"** and the **"Lock."**

*   **The Key (`The Forge`):** The world generator itself, a Python application (`main.py`) that orchestrates a complex, multi-layered generation cascade. It takes a player's character choices as the initial seed and procedurally builds an entire world around them—its history, geography, factions, NPCs, economies, and quests. The final output is a highly compressed, token-efficient **World-Weave Key (`.wwf`)**, a file that contains the entire generated world state.

*   **The Lock (`The Game Master`):** A Large Language Model (LLM) primed with the `GameMaster.md` persona. This persona instructs the LLM to act as a text-based RPG Game Master. The game begins when the player provides the World-Weave Key to the Game Master, which then "unlocks" the world and allows the player to interact with it. For optimal performance, **Gemini 1.5 Pro** with a temperature of **0.0** is recommended.

## Features v3.3

*   **Dynamic, Seed-Based World Generation:** Every world is unique and directly influenced by the player's initial character creation choices.
*   **Expanded Character Creation:**
    *   **9 Alignments:** Choose from the full spectrum of D&D 5e alignments (Lawful Good to Chaotic Evil).
    *   **All D&D 5e Races:** A comprehensive selection of races, each with a unique racial perk and stat discount.
    *   **Class-Based Stat Costs:** A point-buy system where stat costs are influenced by your chosen class.
*   **Vast, Procedurally Generated World:**
    *   **84x84 Grid:** A large world map with a mix of land and sea.
    *   **Coastal Capital & Islands:** The capital city always has sea access, and the world is dotted with mysterious islands.
    *   **Varied Location Sizes:** Settlements and dungeons come in multiple sizes, from tiny 1x1 squares to massive 6x6 capitals.
    *   **Diagonal Roads:** A more natural road system connects the world's locations.
*   **Deep Social & Political Simulation:**
    *   **Complex Faction Relationships:** Guilds now have alliances and rivalries, creating a dynamic political landscape.
    *   **Royal Court:** The capital is home to a fully realized royal family and their court.
    *   **Detailed NPCs:** The world is populated with a diverse cast of NPCs from all D&D 5e races, each with their own role, status, and guild affiliation.
*   **Progressive Quest & Ability System:**
    *   **10-Tier Difficulty:** A granular difficulty system for quests, creatures, and NPCs.
    *   **Guild-Based Abilities:** Purchase powerful, 5-tier abilities directly from Guild Masters, with costs scaling dramatically.
    *   **Epic Guild Questlines:** Embark on 10-step quest chains for each major guild, from simple initiation tasks to world-altering finales.

## The World Generation Cascade

The heart of Project Infinity is the `main.py` script, which orchestrates the world generation cascade. The order is crucial for a logical build:

1.  `geopolitical_generator.py`: Creates the world map, landmasses, and locations.
2.  `sociological_generator.py`: Populates the world with factions, the royal court, NPCs, and establishes their complex relationships.
3.  `economic_generator.py`: Simulates the world's economy, distributing gold and equipment.
4.  `abilities_generator.py`: Assigns the powerful, 5-tier abilities to the appropriate Guild Masters.
5.  `quest_generator.py`: Weaves a rich tapestry of quests, including the 10-tier guild questlines.
6.  `world_details_generator.py`: Adds flavor text, environmental details, and other finishing touches.
7.  `roads_generator.py`: Creates a logical road network connecting the various locations.
8.  `time_and_event_generator.py`: Initializes the world's clock.
9.  `formatter.py`: The final step, which takes the completed `WorldState` object and "weaves" it into the compressed `.wwf` string.

## How to Play

1.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

2.  **Forge Your World-Weave Key:**
    ```bash
    python3 main.py
    ```
    This will launch the character creation interview. Your choices will be used to generate a unique `.wwf` file (e.g., `yourname_yourclass_weave_v3-3.wwf`).

3.  **Awaken the Game Master:**
    *   Use an LLM interface that allows for a custom system prompt/persona.
    *   Load the contents of `GameMaster.md` as the system prompt.
    *   Paste the **entire contents** of your generated `.wwf` file as the first user message.
    *   The Game Master will then greet you, and your adventure will begin.

## The World-Weave Format (`.wwf`)

The `.wwf` file is a custom, token-efficient format designed to represent the entire world state. Key v3.3 additions include:

*   `[PLAYER_CHARACTER_V3.3]`: Now includes the `RACIAL_PERK` field.
*   `[NPCS]`: Can now include a `FOR_SALE_ABILITIES` field, listing the names of abilities sold by that NPC.
*   `[QUESTS]`: Now includes a `TIER` field, indicating its difficulty.

## For Developers

The code is designed to be modular and extensible. The `models.py` file, using `pydantic`, defines the core data structures for the entire application, providing a strong, typed foundation for the world state. Each generator module is self-contained and can be modified or replaced to alter the world generation process.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.