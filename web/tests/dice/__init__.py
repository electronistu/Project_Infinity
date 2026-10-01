"""Engine tests for `dice_server.py` (the MCP rules engine).

Run from the repo root:

    venv\\Scripts\\python.exe -m unittest discover -s web/tests/dice -t . -v

`harness.py` installs a fresh in-memory player DB before every test
(`EngineCase` / `load()`) and can script the RNG (`fixed_rolls` / `rolls_always`)
so damage/attack assertions are exact.

Two engine bugs found here were fixed in dice_server.py on 2026-10-01:
  - healing spells now add the caster's spellcasting ability modifier
    (`add_spellcasting_mod: true` in config for Cure Wounds / Healing Word /
    Prayer of Healing / Mass Healing Word / Mass Cure Wounds);
  - removing an active effect now persists the removal from `active_effects`.
"""
