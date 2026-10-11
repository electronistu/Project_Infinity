"""Compact the MCP tool schemas before they are advertised to the GM.

The GM receives each tool's JSON Schema alongside its docstring. FastMCP builds
that schema from the Python signature, and pydantic is verbose: every optional
parameter becomes ``{"anyOf": [{"type": X}, {"type": "null"}], "default": null,
"title": "..."}`` even though the title is the parameter name restated and the
null branch only means "omittable".

Compacting is a pure prompt-side transformation -- it never touches
``dice_server.py``, and the MCP server still validates the real arguments. Three
rules only:

  * drop ``title`` (the property key already names the parameter);
  * collapse ``X | null`` to ``X`` and drop a ``null`` default (the parameter
    stays optional via ``required``; a real default such as ``True`` or
    ``"Check"`` is preserved);
  * drop the curated ``_HIDDEN_PROPERTIES`` for the tool, if any -- aliases whose
    capability is still reachable through a visible parameter.

A union with more than one concrete branch is left untouched.
"""

from __future__ import annotations

import copy
from typing import Any


# Properties kept OFF the prompt schema: their names are still accepted -- compaction never
# touches ``dice_server.py`` -- but they are not advertised. Every entry here must be a
# *duplicate spelling* with a VISIBLE generic route (``effects``), which ``equipment.item_effects``
# normalizes into the same entry, so nothing is lost and the always-on prefix pays ~10 fewer
# tokens per property. Their semantics live in ``config/tool_refs.yml`` under
# ``lookup('ref:update_player_list')``. A property with no alternative must NOT be listed here:
# that would silently remove a capability the GM can no longer discover.
_HIDDEN_PROPERTIES: dict[str, tuple[str, ...]] = {
    "update_player_list": (
        "set_str", "set_dex", "set_con", "set_int", "set_wis", "set_cha",
        "str_bonus", "dex_bonus", "con_bonus", "int_bonus", "wis_bonus", "cha_bonus",
        "str_bonus_max", "dex_bonus_max", "con_bonus_max", "int_bonus_max",
        "wis_bonus_max", "cha_bonus_max",
    ),
}


def hidden_properties(tool_name: str) -> tuple[str, ...]:
    """The properties dropped from ``tool_name``'s prompt schema (never from the server's)."""
    return _HIDDEN_PROPERTIES.get(str(tool_name or "").strip(), ())


def compact_schema(schema: Any, tool_name: str = "") -> Any:
    """Return a token-thinner copy of a JSON Schema (never mutates the input).

    ``tool_name`` selects the curated properties to leave off the prompt (see
    ``_HIDDEN_PROPERTIES``); the server schema is unaffected.
    """
    if not isinstance(schema, dict):
        return schema
    out = copy.deepcopy(schema)
    out.pop("title", None)
    props = out.get("properties")
    if isinstance(props, dict):
        required = set(out.get("required") or ())
        for name in hidden_properties(tool_name):
            if name not in required:  # never hide a required parameter
                props.pop(name, None)
        for value in props.values():
            if not isinstance(value, dict):
                continue
            value.pop("title", None)
            branches = value.get("anyOf")
            if isinstance(branches, list):
                concrete = [b for b in branches
                            if isinstance(b, dict) and b.get("type") != "null"]
                if len(concrete) == 1:
                    value.pop("anyOf")
                    if value.get("default", 0) is None:
                        value.pop("default", None)
                    # merge the concrete branch (carries `items`, `enum`, ...)
                    value.update(concrete[0])
                    continue
            if value.get("default", 0) is None:
                value.pop("default", None)
    return out
