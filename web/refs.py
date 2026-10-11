"""On-demand tool references -- the long explanations the GM fetches with ``lookup``.

A tool's description carries only the **call contract**: WHEN to call it, the fields, and the
shape of the arguments. The material that is a *reference* -- a vocabulary, a rules list --
lives in ``config/tool_refs.yml`` and is returned by ``lookup('ref:TOOL')``. Nothing here is
always-on: the tool description holds a one-line pointer, and the body is paid only on the
turns it is fetched.

Reachability: ``lookup`` is offered in **both** games (it is the one generic read-only tool),
so a classic session can fetch a reference as well as the era game.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REFS_PATH = Path(__file__).resolve().parent.parent / "config" / "tool_refs.yml"


def _load() -> dict[str, str]:
    """``{tool name: reference text}``, or ``{}`` when yaml/the file is unavailable."""
    if yaml is None or not REFS_PATH.is_file():
        return {}
    data = yaml.safe_load(REFS_PATH.read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in data.items() if str(v or "").strip()}


def tool_ref_ids() -> list[str]:
    """Every tool with an on-demand reference, sorted."""
    return sorted(_load())


def render_tool_ref(name: str) -> str:
    """The reference body for ``name``, or a listing of the known names on a miss."""
    refs = _load()
    key = str(name or "").strip().lower().replace("-", "_")
    if key in refs:
        return refs[key]
    known = ", ".join(f"ref:{k}" for k in sorted(refs))
    return f"no tool reference for {name!r}. Known references: {known}"


def is_tool_ref(query: str) -> bool:
    """True when a `lookup` query asks for a tool reference rather than era lore."""
    return str(query or "").strip().lower().startswith(("ref:", "tool:"))
