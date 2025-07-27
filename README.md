# Python virtual environment
venv/

# Python cache
__pycache__/
*.pyc

## How to Play

This project includes a pre-generated world file, `output/electronistu_weave.wwf`, so you can start playing immediately without needing to generate a new world.

To play, it is highly recommended to use a powerful Large Language Model with a large context window.

### Recommended Platforms:

1.  **Google AI Studio (Easiest):**
    *   Go to [aistudio.google.com](https://aistudio.google.com).
    *   Select the **Gemini 1.5 Pro** model.
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

