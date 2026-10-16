import sys

from rules.app import mcp
from rules.db import init_player_db
from rules import state

# Import order is registration order: FastMCP lists the tools in the order they are
# registered, and that listing is the order the GM sees. Keep each module's block in the
# original sequence (player, scene, play, combat, attack, magic).
from rules.tools_player import (
    attune_item, debug_device, debug_set_field, dump_player_db, dump_player_save_state,
    equip_item, lookup, modify_player_numeric, read_player_field, set_player_field,
    update_player_list
)
from rules.tools_scene import note_place, register_npcs, request_scene_image
from rules.tools_play import (
    device_jump_reset, make_death_save, modify_exhaustion, perform_check, rest, roll_dice
)
from rules.tools_combat import (
    end_combat, register_combatants, register_companion, update_combatant
)
from rules.attack import resolve_attack
from rules.magic import resolve_magic


if __name__ == "__main__":
    state.DEBUG_MODE = "--debug" in sys.argv
    player_file = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    if player_file:
        init_status = init_player_db(player_file)
        print(f"Server DB Init: {init_status}", file=sys.stderr)

    mcp.run(transport="stdio")
