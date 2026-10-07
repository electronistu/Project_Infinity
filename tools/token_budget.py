"""Measure the static prefix sent to the GM every turn.

The GM receives exactly two things before the player says anything:

  1. ``GameMaster_MCP.md`` (the protocol; scene blocks stripped when images are off)
  2. the live MCP tool list from ``dice_server.py`` -- each tool's *docstring*
     (``description``) plus the JSON Schema built from its type annotations.

The code body of ``dice_server.py`` is never sent. This script boots the MCP
server in-process, pulls the real tool listing, and reports the per-tool and
total character/token cost so a refactor can be measured and guarded.

Run from the repo root (needs the venv: ``mcp`` and ``pyyaml``):

    venv\\Scripts\\python.exe tools\\token_budget.py
    venv\\Scripts\\python.exe tools\\token_budget.py --json
    venv\\Scripts\\python.exe tools\\token_budget.py --budget 9000   # exit 1 if over

Token counts are exact when ``tiktoken`` is installed, otherwise a labelled
chars/3.6 estimate (the payload mixes prose, code identifiers and JSON, which
tokenize worse than plain English).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROTOCOL = REPO / "GameMaster_MCP.md"
ENGINE = REPO / "dice_server.py"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from web.tool_schema import compact_schema  # noqa: E402

_TEXT_KEYS = ("desc_text", "schema_text")


def _counter():
    """(label, fn) -- exact via tiktoken when present, else a chars heuristic."""
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return "tiktoken:cl100k_base", lambda s: len(enc.encode(s))
    except Exception:
        return ("estimate chars/3.6 (pip install tiktoken for exact counts)",
                lambda s: round(len(s) / 3.6))


def _render_protocol(text: str, scene_images: bool) -> str:
    """Reuse the engine's renderer so the count never drifts from production."""
    try:
        from web.engine import render_protocol

        return render_protocol(text, scene_images)
    except Exception:  # pragma: no cover - fallback if web deps are unavailable
        scene_block = re.compile(
            r"(?ms)^[ \t]*<!-- SCENE:ON -->.*?^[ \t]*<!-- SCENE:END -->[ \t]*\n?")
        markers = re.compile(r"(?m)^[ \t]*<!-- SCENE:(?:ON|END) -->[ \t]*\n?")
        return markers.sub("", text) if scene_images else scene_block.sub("", text)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def tool_listing():
    """Raw live MCP tools: list of (name, description, inputSchema dict)."""
    spec = importlib.util.spec_from_file_location("dice_server_budget", ENGINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def listing():
        return await module.mcp.list_tools()

    out = []
    for tool in _run(listing()):
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
        out.append((tool.name, tool.description or "", schema))
    return out


def _tool_rows(count):
    """Per-tool description + schema sizes from the live FastMCP listing."""
    rows = []
    for name, desc, schema in tool_listing():
        raw_text = json.dumps(schema, ensure_ascii=True, sort_keys=True)
        schema_text = json.dumps(compact_schema(schema), ensure_ascii=True, sort_keys=True)
        rows.append({
            "name": name,
            "params": len((schema or {}).get("properties", {}) or {}),
            "desc_chars": len(desc),
            "raw_schema_chars": len(raw_text),
            "schema_chars": len(schema_text),
            "total_chars": len(desc) + len(schema_text),
            "desc_tokens": count(desc),
            "raw_schema_tokens": count(raw_text),
            "schema_tokens": count(schema_text),
            "desc_text": desc,
            "schema_text": schema_text,
        })
    rows.sort(key=lambda r: -r["total_chars"])
    return rows


def _public(row):
    return {k: v for k, v in row.items() if k not in _TEXT_KEYS}


def main() -> int:
    ap = argparse.ArgumentParser(description="GM static-prefix token budget")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--budget", type=int, default=None,
                    help="fail (exit 1) if the scenes-on static prefix exceeds this many tokens")
    args = ap.parse_args()

    label, count = _counter()
    rows = _tool_rows(count)

    proto_text = PROTOCOL.read_text(encoding="utf-8")
    proto_on = _render_protocol(proto_text, True)
    proto_off = _render_protocol(proto_text, False)
    proto_on_tokens, proto_off_tokens = count(proto_on), count(proto_off)

    tool_chars = sum(r["total_chars"] for r in rows)
    tool_tokens = sum(r["desc_tokens"] + r["schema_tokens"] for r in rows)
    raw_schema_chars = sum(r["raw_schema_chars"] for r in rows)
    compact_schema_chars = sum(r["schema_chars"] for r in rows)
    raw_schema_tokens = sum(r["raw_schema_tokens"] for r in rows)
    compact_schema_tokens = sum(r["schema_tokens"] for r in rows)
    on_tokens = tool_tokens + proto_on_tokens
    off_tokens = tool_tokens + proto_off_tokens

    if args.json:
        print(json.dumps({
            "tokenizer": label,
            "tools": [_public(r) for r in rows],
            "tool_chars": tool_chars, "tool_tokens": tool_tokens,
            "schema_raw_chars": raw_schema_chars, "schema_compact_chars": compact_schema_chars,
            "schema_raw_tokens": raw_schema_tokens, "schema_compact_tokens": compact_schema_tokens,
            "schema_saved_tokens": raw_schema_tokens - compact_schema_tokens,
            "protocol_on_chars": len(proto_on), "protocol_on_tokens": proto_on_tokens,
            "protocol_off_chars": len(proto_off), "protocol_off_tokens": proto_off_tokens,
            "total_on_tokens": on_tokens, "total_off_tokens": off_tokens,
        }, indent=2))
    else:
        print("GM STATIC PREFIX -- token budget")
        print(f"tokenizer: {label}\n")
        print(f"{'tool':24s} {'params':>6s} {'desc':>7s} {'schema':>7s} {'total':>7s} {'~tok':>7s}")
        print(f"{'':24s} {'':6s} {'':7s} {'(compact)':>7s}")
        print("-" * 66)
        for r in rows:
            print(f"{r['name']:24s} {r['params']:6d} {r['desc_chars']:7d} {r['schema_chars']:7d} "
                  f"{r['total_chars']:7d} {r['desc_tokens'] + r['schema_tokens']:7d}")
        print("-" * 66)
        print(f"{'TOOLS TOTAL':24s} {'':6s} {'':7s} {'':7s} {tool_chars:7d} {tool_tokens:7d}")
        print(f"schema compaction: {raw_schema_chars} -> {compact_schema_chars} chars, "
              f"{raw_schema_tokens} -> {compact_schema_tokens} tok "
              f"(saved {raw_schema_tokens - compact_schema_tokens} tok)\n")
        print(f"GameMaster_MCP.md  scenes ON : {len(proto_on):6d} chars  {proto_on_tokens:6d} tok")
        print(f"GameMaster_MCP.md  scenes OFF: {len(proto_off):6d} chars  {proto_off_tokens:6d} tok")
        print(f"\nSTATIC PREFIX  scenes ON : {tool_chars + len(proto_on):7d} chars  "
              f"{on_tokens:6d} tok")
        print(f"STATIC PREFIX  scenes OFF: {tool_chars + len(proto_off):7d} chars  "
              f"{off_tokens:6d} tok")

    if args.budget is not None and on_tokens > args.budget:
        print(f"\nBUDGET EXCEEDED: {on_tokens} > {args.budget} tokens (scenes on)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
