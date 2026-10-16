"""The engine's rules package: the MCP app object, mutable state, immutable tables, dice maths,
the player database layer and the spell catalogue, plus the helper families the tools drive --
the derived sheet, equipped items, HP/exhaustion/concentration/death saves, conditions and
damage modifiers, the combat registry and companions, spell effects and spell knowledge, and
reputation scopes.

The MCP tools live here too -- `tools_player`, `tools_scene`, `tools_play`, `tools_combat`,
`attack` and `magic` -- so `dice_server.py` is only the entry point that imports them and runs
the server. Nothing in this package imports `dice_server`.
"""
