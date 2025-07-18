---
---

# Project Infinity

A Python-based procedural world generator and AI-driven role-playing game engine. Project Infinity allows you to forge a unique, personalized RPG world and interact with it through a specialized AI Game Master with a personality of its own.

```
.
├── GameMaster.MD
├── README.md
├── electronistu_mage_weave.wwf
├── formatter.py
├── geopolitical_generator.py
├── main.py
├── models.py
└── sociological_generator.py
```

---

### Core Concept: The Key & The Lock

Project Infinity operates on a simple but powerful "Key/Lock" protocol:

1.  **The Forge (The Python Pipeline):** The `main.py` script is the **Forge**. It acts as a key-maker, guiding you through character creation and then procedurally generating a unique world based on your choices.

2.  **The World-Weave Key (`.wwf` file):** The Forge's output is a bespoke `.wwf` file. This is your **Key**—a hyper-condensed text file containing your entire world state, optimized for an AI to read efficiently. An example key, `electronistu_mage_weave.wwf`, is included so you can play immediately.

3.  **The Game Master (The LLM Persona):** The `GameMaster.MD` file contains the instructions to turn a generic Large Language Model (LLM) into the **Lock**. When you provide your unique Key, the Lock opens, and the Game Master awakens, ready to run your personalized adventure.

---

### Features

*   **Procedural World Generation:** Create a new, unique world with its own geography, settlements, factions, and population every time you run the Forge.
*   **Token-Efficient World-Weave Format (`.wwf`):** A custom-designed data format that encodes a rich world state into a minimal number of tokens, maximizing compatibility with LLM context windows.
*   **Advanced LLM Persona:** The Game Master isn't just a narrator. It features a unique dual-consciousness: an objective "World-Soul" and a jaded, witty "Inner Voice" that communicates directly with you, the player.
*   **Modular & Extensible Architecture:** The generation pipeline is built with a clean separation of concerns, allowing for easy modification or the addition of new generation layers.
*   **No External Dependencies (Almost!):** The project runs on standard Python with only one external library, `pydantic`, for robust data validation.

---

### File Manifest

*   `main.py`: **The Orchestrator.** The main script you run to generate a new world.
*   `models.py`: **The Data Schema.** Defines the Pydantic models that give structure to the generated world.
*   `geopolitical_generator.py`: **Generation Layer 1.** Creates the world map, locations, and biomes.
*   `sociological_generator.py`: **Generation Layer 2.** Populates the world with factions, families, and NPCs, ensuring a balanced political landscape.
*   `formatter.py`: **The Weaver.** Translates the generated world data into the final `.wwf` string format.
*   `GameMaster.MD`: **The Lock.** The instruction set for the AI. You will copy the contents of this file to prime the Game Master.
*   `electronistu_mage_weave.wwf`: **Example Key.** A pre-generated world key for you to test the project immediately without running the generator.

---

### How to Play

**Prerequisites**
*   Python 3.x
*   The `pydantic` library. Install it via pip:
    ```bash
    pip install pydantic
    ```

**Option A: Quick Start (Using the Included Key)**

This is the fastest way to play.

1.  **Prime the Game Master:**
    *   Go to your preferred LLM interface (e.g., Google AI Studio, OpenAI's Playground, etc.).
    *   Copy the *entire contents* of the `GameMaster.MD` file.
    *   Paste this into the **first prompt** of a new chat session and send it.
    *   The AI **must** respond with only the words: `Awaiting Key.` If it responds with anything else, the priming failed. Start a new chat and try again.

2.  **Turn the Key:**
    *   Once the AI is awaiting the key, open the included `electronistu_mage_weave.wwf` file.
    *   Copy its *entire contents*.
    *   Paste the `.wwf` content as the **second prompt** in your chat.
    *   The Game Master will awaken and immediately begin the game by describing your opening scene.

**Option B: Forge Your Own World**

1.  **Run the Forge:**
    *   Open your terminal in the project directory.
    *   Run the command: `python main.py`
    *   Follow the on-screen prompts to create your character. A new `.wwf` file named after your character will be created.

2.  **Play the Game:**
    *   Follow the "Prime the Game Master" and "Turn the Key" steps from Option A, but use your newly created `.wwf` file in the final step.

---

### License and Disclaimer of Responsibility

**License:**
This project and its associated files are provided for **non-commercial use only**. You are free to view, use, and modify the source code for personal, educational, or non-profit purposes. You may not use this project, or any derivatives of it, for any commercial activities.

**Disclaimer of Responsibility:**
The user is solely responsible for their interactions with the AI Game Master. The choices you make and the content generated by the Large Language Model (LLM) after the initial game state are your own responsibility. The developers of Project Infinity are not liable for any content generated, actions taken, or consequences that arise from the use of this software.

**Warranty:**
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
