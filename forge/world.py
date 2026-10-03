"""The static world scaffold (`config/world.yml`).

The world is identical for every save: one shared history and a fixed set of
kingdoms and guilds. The GM invents NPCs on the fly, so none are generated
here — only the political scaffold, which the engine injects into the GM at
awakening and which seeds the `.player` reputation map.
"""

import json
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .models import Guild, Kingdom

DEFAULT_WORLD_PATH = Path(__file__).resolve().parent.parent / "config" / "world.yml"


def load_world(path=None) -> dict:
    """Read the static world config -> {"history": [...], "kingdoms": [...]}."""
    p = Path(path) if path else DEFAULT_WORLD_PATH
    if yaml is None:
        raise RuntimeError("PyYAML is required to read config/world.yml")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {
        "history": [str(x) for x in (data.get("history") or [])],
        "kingdoms": list(data.get("kingdoms") or []),
    }


def build_kingdoms(world: dict | None = None) -> list[Kingdom]:
    """Kingdom objects (guilds only, no NPCs) for the `.player` reputation map."""
    world = world or load_world()
    out: list[Kingdom] = []
    for k in world.get("kingdoms") or []:
        guilds = [
            Guild(name=str(g.get("name") or ""), reports_to=g.get("reports_to"))
            for g in (k.get("guilds") or [])
        ]
        out.append(Kingdom(
            name=str(k.get("name") or ""),
            capital=str(k.get("capital") or ""),
            alignment=str(k.get("align") or ""),
            relations=dict(k.get("relations") or {}),
            guilds=guilds,
        ))
    return out


def render_world_text(world: dict | None = None) -> str:
    """The world text injected into the GM at awakening (history + kingdoms)."""
    world = world or load_world()
    lines = ["history:"]
    for entry in world.get("history") or []:
        lines.append(f"  - {entry}")
    lines.append("kingdoms:")
    for k in world.get("kingdoms") or []:
        lines.append(f"  - name: {k.get('name', '')}")
        lines.append(f"    capital: {k.get('capital', '')}")
        lines.append(f"    align: {k.get('align', '')}")
        lines.append(f"    relations: {json.dumps(k.get('relations') or {})}")
        lines.append("    guilds:")
        for g in k.get("guilds") or []:
            lines.append(f"      - name: {g.get('name', '')}")
            if g.get("reports_to"):
                lines.append(f"        reports_to: {g['reports_to']}")
    return "\n".join(lines)
