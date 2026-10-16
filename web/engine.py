"""Headless, streaming game engine for the Project Infinity web client.

A headless, streaming re-implementation of the game loop (the terminal client
has been removed). It reads `GameMaster_MCP.md` as the GM protocol, owns the
timeline helpers locally, and launches the same `dice_server.py` MCP engine.

Concurrency model
-----------------
Each session owns one long-lived background task (`_run`) that holds the MCP
stdio context managers for the whole session and consumes commands from a
queue. Events are pushed to an output queue that `events()` yields from. This
keeps the MCP anyio task group alive across many independent HTTP/WS requests.

Event stream (server -> client)
-------------------------------
ready, assistant_start, thinking_delta, narrative_delta, assistant_end,
tool_call, tool_result, context, paused, awakening_end, turn_end, stats,
notice, timeline, busy, error, fatal, closed
"""

from .session_base import SessionBase
from .session_dev import DevMixin
from .session_device import DeviceMixin
from .session_scene import SceneMixin
from .session_save import SaveMixin
from .session_turn import TurnMixin


class GameSession(SessionBase, DevMixin, DeviceMixin, SceneMixin, SaveMixin, TurnMixin):
    """One in-memory, long-lived game session (single user, local server)."""
