# Project Infinity v2: The World Simulation Engine

Project Infinity is a Python-based procedural world simulation engine and an AI-driven role-playing game framework. It allows you to forge a unique, deeply detailed, and internally consistent RPG world, then interact with it through a specialized, high-fidelity AI Game Master.

### Table of Contents
1.  [File Structure](#file-structure)
2.  [Core Concept: The Key & The Lock](#core-concept-the-key--the-lock)
3.  [What's New in v2?](#whats-new-in-v2)
4.  [Features](#features)
5.  [How to Play](#how-to-play)
6.  [Recommended AI Setup](#recommended-ai-setup)
7.  [License and Disclaimer](#license-and-disclaimer-of-responsibility)

---

### File Structure
```
.
├── GameMaster.MD
├── README.md
├── economic_generator.py
├── electronistu_mage_weave_v2.wwf  <-- Example Key
├── formatter.py
├── geopolitical_generator.py
├── items.json
├── main.py
├── models.py
├── quest_generator.py
├── sociological_generator.py
└── world_details_generator.py
```

---

### Core Concept: The Key & The Lock

Project Infinity operates on a simple but powerful "Key/Lock" protocol, designed for maximum stability and performance:

*   **The Forge (The Python Pipeline):** The `main.py` script is the Forge. It is a powerful local application that acts as a key-maker. It guides you through character creation and then procedurally generates a deeply detailed, static world state based on a multi-layered generation cascade.

*   **The World-Weave Key (`.wwf` file):** The Forge's output is a bespoke `_v2.wwf` file. This is your Key—a hyper-condensed text file containing your entire world state, from the position of every creature to the inventory of every NPC, optimized for an AI to parse efficiently. An example key, `electronistu_mage_weave_v2.wwf`, is included so you can play immediately.

*   **The Game Master (The LLM Persona):** The `GameMaster.MD` file contains the instructions to turn a generic Large Language Model (LLM) into the Lock. When you provide your unique Key, the Lock opens, and the Game Master awakens, ready to run your personalized adventure by *interpreting* the world, not inventing it.

---

### What's New in v2?

Version 2 is a complete architectural overhaul, transforming the project from a proof-of-concept into a true World Simulation Engine.

*   **Expanded, Anchored World:** The world is now a `42x42` grid featuring a large, central **Capital City** that acts as a narrative and geographical anchor.
*   **Deeper Locations:** Settlements and Dungeons now have physical footprints on the map (Small, Medium, Big). Settlements contain **Sub-Locations** like taverns, shops, and guild dens, each with a pre-assigned NPC operator.
*   **Living Economy:** The world has a **finite, closed-loop economy**. A master `items.json` database defines all items. A fixed amount of gold is distributed across all NPCs and creatures according to a clear hierarchy, and their inventories and loot tables are pre-generated.
*   **Structured Narrative:** A new **Quest & Reputation Engine** generates tiered quest chains (Easy, Medium, Hard) based on your character's alignment (`Lawful`, `Neutral`, `Chaotic`). Faction relationships are pre-defined, and your choices matter.
*   **Creature Ecology:** The world is populated with a variety of **creatures**, each with a defined difficulty, challenge level, and specific location within dungeons or wilderness areas.
*   **Pre-Computed World Logic:** To ensure maximum stability, the Forge now pre-calculates complex world details:
    *   **The Chronicle Engine:** Creates `Trigger -> Outcome` events. If a faction leader is killed, the world's reaction is pre-written, not invented by the AI.
    *   **The Environmental Engine:** Generates a library of high-quality, atmospheric descriptions for different biomes, times of day, and weather conditions.

---

### Features

*   **Deep Procedural World Generation:** Create a new, unique world with its own geography, a capital city, settlements, dungeons, factions, NPCs, and creature populations every time you run the Forge.
*   **Token-Efficient World-Weave Format (`.wwf`):** A custom-designed data format that encodes a rich, complex world state into a minimal number of tokens.
*   **Advanced High-Fidelity LLM Persona:** The Game Master is a sophisticated interpreter, not a creator. It features a unique dual-consciousness: an objective "World-Soul" and a jaded, witty "Inner Voice" that communicates directly with you, the player, using strict formatting and knowledge constraints.
*   **Finite Closed-Loop Economy:** A fixed amount of gold and items exist in the world. Player actions have a real economic impact.
*   **Tiered Quest System:** A structured narrative framework with chained quests that unlock as you build your reputation with the world's factions.
*   **Modular & Extensible Architecture:** The generation pipeline is built with a clean separation of concerns, allowing for easy modification or the addition of new generation layers.

---

### How to Play

You have two options to start your adventure.

#### **Option A: Quick Start (Using the Included Key)**

This is the fastest way to play, using the pre-generated world file.

1.  **Prime the Game Master:**
    *   Go to your preferred LLM interface that supports the recommended model (see below).
    *   Copy the entire contents of the `GameMaster.MD` file.
    *   Paste this into the **first prompt** of a new chat session and send it.
    *   The AI **must** respond with only the words: `Awaiting Key.` If it responds with anything else, the priming failed. Start a new chat and try again.

2.  **Turn the Key:**
    *   Once the AI is awaiting the key, open the included `electronistu_mage_weave_v2.wwf` file.
    *   Copy its entire contents.
    *   Paste the `.wwf` content as the **second prompt** in your chat.
    *   The Game Master will awaken and immediately begin the game by describing your opening scene and presenting the world map.

#### **Option B: Forge Your Own World**

Create a brand new, unique world from scratch.

**Prerequisites**
*   Python 3.x
*   The `pydantic` library. Install it via pip:
    ```
    pip install pydantic
    ```

**Steps**

1.  **Run the Forge:**
    *   Open your terminal in the project directory.
    *   Run the command: `python main.py`

2.  **Create Your Character:**
    *   Follow the on-screen prompts to create your character.
    *   The Forge will then run its full generation cascade and create a new `.wwf` file named after your character (e.g., `yourname_class_weave_v2.wwf`).

3.  **Play the Game:**
    *   Follow the "Prime the Game Master" and "Turn the Key" steps from **Option A**, but use your newly created `.wwf` file in the final step.

---

### Recommended AI Setup

*   **IMPORTANT:** This project was developed and tested exclusively with **Google's Gemini 2.5 Pro** model. For the intended, stable experience, it is crucial to set the model's **temperature to 0**.
*   Other models or different settings (e.g., higher temperatures) have not been tested and may produce unpredictable, unstable, or undesirable results. Use other configurations at your own risk.

---

### License and Disclaimer of Responsibility

**License:** This project and its associated files are provided for non-commercial use only. You are free to view, use, and modify the source code for personal, educational, or non-profit purposes. You may not use this project, or any derivatives of it, for any commercial activities.

**Disclaimer of Responsibility:** The user is solely responsible for their interactions with the AI Game Master. The choices you make and the content generated by the Large Language Model (LLM) after the initial game state are your own responsibility. The developers of Project Infinity are not liable for any content generated, actions taken, or consequences that arise from the use of this software.

Furthermore, the user assumes all responsibility for the choice of Large Language Model (LLM) and its configuration. The recommended setup is provided as a guideline for stability. The use of other models or different settings is done entirely at the user's own risk.

**Warranty:** THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
