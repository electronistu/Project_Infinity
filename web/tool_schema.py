"""Compact the MCP tool schemas before they are advertised to the GM.

The GM receives each tool's JSON Schema alongside its docstring. FastMCP builds
that schema from the Python signature, and pydantic is verbose: every optional
parameter becomes ``{"anyOf": [{"type": X}, {"type": "null"}], "default": null,
"title": "..."}`` even though the title is the parameter name restated and the
null branch only means "omittable".

Compacting is a pure prompt-side transformation -- it never touches
``dice_server.py``, and the MCP server still validates the real arguments. Two
rules only:

  * drop ``title`` (the property key already names the parameter);
  * collapse ``X | null`` to ``X`` and drop a ``null`` default (the parameter
    stays optional via ``required``; a real default such as ``True`` or
    ``"Check"`` is preserved).

A union with more than one concrete branch is left untouched.
"""

from __future__ import annotations

import copy
from typing import Any


def compact_schema(schema: Any) -> Any:
    """Return a token-thinner copy of a JSON Schema (never mutates the input)."""
    if not isinstance(schema, dict):
        return schema
    out = copy.deepcopy(schema)
    out.pop("title", None)
    props = out.get("properties")
    if isinstance(props, dict):
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
