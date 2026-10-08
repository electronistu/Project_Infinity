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
    venv\\Scripts\\python.exe tools\\token_budget.py --gate         # assert tools/budgets.yml

Token counts are exact when ``tiktoken`` is installed, otherwise a labelled
chars/3.6 estimate (the payload mixes prose, code identifiers and JSON, which
tokenize worse than plain English). ``--gate`` refuses to run on that estimate:
a budget asserted against a guess is not asserted.
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
    """(label, fn, exact) -- exact via tiktoken when present, else a chars heuristic."""
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return "tiktoken:cl100k_base", (lambda s: len(enc.encode(s))), True
    except Exception:
        return ("estimate chars/3.6 (pip install tiktoken for exact counts)",
                lambda s: round(len(s) / 3.6), False)


def _render_protocol(text: str, scene_images: bool, mode: str | None = None) -> str:
    """Reuse the engine's renderer so the count never drifts from production."""
    try:
        from web.engine import MODE_TIME_TRAVELER, render_protocol

        return render_protocol(text, scene_images, "hard", mode or MODE_TIME_TRAVELER)
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
    """Per-tool description + schema sizes from the live FastMCP listing.

    Tools the GM is never offered (ENGINE_ONLY_TOOLS) are skipped: the gate measures what
    actually enters the prompt, not everything the server can serve."""
    try:
        from web.engine import ENGINE_ONLY_TOOLS
    except Exception:  # pragma: no cover - the engine is importable in every real run
        ENGINE_ONLY_TOOLS = ()
    rows = []
    for name, desc, schema in tool_listing():
        if name in ENGINE_ONLY_TOOLS:
            continue
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


def _synthetic_places(n: int) -> list[dict]:
    """N representative places for the KNOWN IMAGE PLACES priming measure."""
    out = []
    for i in range(n):
        out.append({
            "kingdom": "Eldoria" if i % 3 else "Zarthus",
            "area": "Eldoria City" if i % 3 else "Zarthus City",
            "place": [f"District {i // 3}", f"Landmark {i}"],
            "description": "",
            "main_npcs": [{"name": f"NPC{i}A", "role": "the keeper", "description": ""},
                          {"name": f"NPC{i}B", "role": "the table-runner", "description": ""}],
        })
    return out


def known_places_block(n: int, count):
    """The awakening KNOWN IMAGE PLACES message for N places (header + tree)."""
    from web.engine import KNOWN_PLACES_HEADER, format_known_places
    text = KNOWN_PLACES_HEADER + "\n" + format_known_places(_synthetic_places(n))
    return text, count(text)


BUDGETS = REPO / "tools" / "budgets.yml"
ERA_DIR = REPO / "config" / "eras"
PLACE_TREE_CAP = 12


def check_gate_ready(exact: bool) -> tuple[bool, str]:
    """The gate runs only on exact counts -- an estimate cannot gate a budget."""
    if exact:
        return True, ""
    return False, ("exact token counts require tiktoken; refusing to gate on the "
                   "chars/3.6 estimate (pip install -r requirements-dev.txt)")


def _metric_place_tree(count):
    """KNOWN IMAGE PLACES at the S0.3 cap, or None if the engine is unavailable."""
    try:
        _, tokens = known_places_block(PLACE_TREE_CAP, count)
        return tokens
    except Exception:
        return None


def _metric_era_file_bytes():
    """Largest config/eras/*.yml, or None until the era files exist."""
    if not ERA_DIR.is_dir():
        return None
    sizes = [p.stat().st_size for p in ERA_DIR.glob("*.yml")]
    return max(sizes) if sizes else None


def _era_index_text() -> str:
    """The always-on ERA INDEX, or "" when web.eras is unavailable."""
    try:
        from web.eras import render_era_index

        return render_era_index()
    except Exception:
        return ""


def _metric_era_scaffold(count):
    """The largest era scaffold -- the one fetched once per era via lookup."""
    try:
        from web.eras import era_ids, render_era_text

        sizes = [count(render_era_text(e)) for e in era_ids()]
        return max(sizes) if sizes else None
    except Exception:
        return None


def _metric_lookup_schema(rows):
    """The `lookup` tool row -- description + schema, i.e. what actually enters the prefix."""
    for r in rows or []:
        if r["name"] == "lookup":
            return r["desc_tokens"] + r["schema_tokens"]
    return None


def _metrics(prefix_on, prefix_off, era_index_tokens, count, rows=None,
             classic_prefix_on=None) -> dict:
    scaffold = _metric_era_scaffold(count)
    tree = _metric_place_tree(count)
    # The honest per-turn floor: everything re-sent every turn until a compaction. An era
    # game pays it; a classic one has no era to fetch, so it has no floor to report.
    always_on = None
    if scaffold is not None and era_index_tokens:
        always_on = prefix_on + scaffold + (tree or 0)
    return {
        "static_prefix_scenes_on": prefix_on,
        "static_prefix_scenes_off": prefix_off,
        # The other game's prefix, measured separately: its protocol omits the era rules
        # and it is never offered `lookup`, so this row is normally the smaller of the two.
        "static_prefix_classic": classic_prefix_on,
        "era_index": era_index_tokens,
        "era_scaffold": scaffold,
        "place_tree": tree,
        "era_file_bytes": _metric_era_file_bytes(),
        "lookup_schema": _metric_lookup_schema(rows),
        "always_on_total": always_on,
        "always_on_total_classic": classic_prefix_on,
        # Not built yet: named so a manifest row resolves; value unknown.
        "tool_results_gm_view": None,
    }


def load_budgets(path: Path = BUDGETS) -> list[dict]:
    """The ceilings manifest (tools/budgets.yml) as a list of rows."""
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return list(data.get("budgets") or [])


def evaluate_budgets(budgets, metrics) -> tuple[list[dict], bool]:
    """Resolve every ceiling against the measured metrics.

    An enforced row with no measurement is a breach -- never a pass.
    """
    results, ok = [], True
    for b in budgets:
        name = b.get("name", "?")
        ceiling = b.get("ceiling")
        status = (b.get("status") or "target").lower()
        measured = metrics.get(b.get("measure") or name)
        if status == "enforced":
            if measured is None:
                breach, reason = True, "no measurement available"
            elif measured > ceiling:
                breach, reason = True, f"{measured} > {ceiling}"
            else:
                breach, reason = False, f"{measured} <= {ceiling}"
        else:
            breach = False
            reason = "not built yet" if measured is None else f"measured {measured}"
            if isinstance(ceiling, int) and measured is not None and measured > ceiling:
                reason += f" -- over the {ceiling} target"
        if breach:
            ok = False
        results.append({
            "name": name, "ceiling": ceiling, "unit": b.get("unit", "tokens"),
            "status": status, "milestone": b.get("milestone"), "measured": measured,
            "ok": not breach, "reason": reason, "note": b.get("note", ""),
        })
    return results, ok


def _print_gate(results, ok):
    print("\nTOKEN BUDGET GATE")
    print(f"{'ceiling':26s} {'status':9s} {'measured':>9s} {'limit':>9s}  result")
    print("-" * 70)
    for r in results:
        measured = "-" if r["measured"] is None else str(r["measured"])
        limit = "-" if r["ceiling"] is None else str(r["ceiling"])
        mark = "target" if r["status"] != "enforced" else ("PASS" if r["ok"] else "FAIL")
        print(f"{r['name']:26s} {r['status']:9s} {measured:>9s} {limit:>9s}  {mark}")
    print("-" * 70)
    print("GATE:", "GREEN" if ok else "RED")


def main() -> int:
    ap = argparse.ArgumentParser(description="GM static-prefix token budget")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--budget", type=int, default=None,
                    help="fail (exit 1) if the scenes-on static prefix (tools + protocol + "
                         "ERA INDEX) exceeds this many tokens")
    ap.add_argument("--places", type=int, default=20,
                    help="KNOWN IMAGE PLACES to measure in the priming (0 to skip)")
    ap.add_argument("--gate", action="store_true",
                    help="assert every ceiling in tools/budgets.yml (requires tiktoken)")
    ap.add_argument("--budgets", default=str(BUDGETS),
                    help="path to the ceilings manifest (default: tools/budgets.yml)")
    args = ap.parse_args()

    label, count, exact = _counter()
    rows = _tool_rows(count)

    proto_text = PROTOCOL.read_text(encoding="utf-8")
    proto_on = _render_protocol(proto_text, True)
    proto_off = _render_protocol(proto_text, False)
    proto_on_tokens, proto_off_tokens = count(proto_on), count(proto_off)
    # The classic game: its own protocol wording, and no `lookup` in the tool list.
    proto_classic = _render_protocol(proto_text, True, "classic")
    classic_tool_tokens = None

    tool_chars = sum(r["total_chars"] for r in rows)
    tool_tokens = sum(r["desc_tokens"] + r["schema_tokens"] for r in rows)
    raw_schema_chars = sum(r["raw_schema_chars"] for r in rows)
    compact_schema_chars = sum(r["schema_chars"] for r in rows)
    raw_schema_tokens = sum(r["raw_schema_tokens"] for r in rows)
    compact_schema_tokens = sum(r["schema_tokens"] for r in rows)
    # The static prefix is everything always-on: tools + protocol + the ERA INDEX.
    era_index_text = _era_index_text()
    era_index_tokens = count(era_index_text) if era_index_text else 0
    on_tokens = tool_tokens + proto_on_tokens + era_index_tokens
    off_tokens = tool_tokens + proto_off_tokens + era_index_tokens
    classic_tool_tokens = tool_tokens - (_metric_lookup_schema(rows) or 0)
    classic_on_tokens = classic_tool_tokens + count(proto_classic)
    places_text, places_tokens = (known_places_block(args.places, count)
                                  if args.places > 0 else ("", 0))
    total_on_tokens = on_tokens + places_tokens

    gate_results, gate_ok = None, True
    if args.gate:
        ready, why = check_gate_ready(exact)
        if not ready:
            print(f"GATE REFUSED: {why}", file=sys.stderr)
            return 2
        gate_results, gate_ok = evaluate_budgets(
            load_budgets(Path(args.budgets)),
            _metrics(on_tokens, off_tokens, era_index_tokens, count, rows,
                     classic_on_tokens))

    if args.json:
        payload = {
            "tokenizer": label,
            "exact": exact,
            "tools": [_public(r) for r in rows],
            "tool_chars": tool_chars, "tool_tokens": tool_tokens,
            "schema_raw_chars": raw_schema_chars, "schema_compact_chars": compact_schema_chars,
            "schema_raw_tokens": raw_schema_tokens, "schema_compact_tokens": compact_schema_tokens,
            "schema_saved_tokens": raw_schema_tokens - compact_schema_tokens,
            "protocol_on_chars": len(proto_on), "protocol_on_tokens": proto_on_tokens,
            "protocol_off_chars": len(proto_off), "protocol_off_tokens": proto_off_tokens,
            "known_places_n": args.places,
            "known_places_chars": len(places_text), "known_places_tokens": places_tokens,
            "total_on_tokens": on_tokens, "total_off_tokens": off_tokens,
            "total_on_with_places_tokens": total_on_tokens,
        }
        if gate_results is not None:
            payload["gate"] = {"ok": gate_ok, "budgets": gate_results}
        print(json.dumps(payload, indent=2))
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
        print(f"ERA INDEX (always on)        : {len(era_index_text):6d} chars  {era_index_tokens:6d} tok")
        if args.places > 0:
            print(f"KNOWN IMAGE PLACES (N={args.places}) : {len(places_text):6d} chars  "
                  f"{places_tokens:6d} tok")
        print(f"\nSTATIC PREFIX  scenes ON : {tool_chars + len(proto_on) + len(era_index_text):7d} chars  "
              f"{on_tokens:6d} tok  (tools + protocol + index)")
        print(f"STATIC PREFIX  scenes OFF: {tool_chars + len(proto_off) + len(era_index_text):7d} chars  "
              f"{off_tokens:6d} tok  (tools + protocol + index)")
        if args.places > 0:
            print(f"TOTAL          scenes ON : {tool_chars + len(proto_on) + len(places_text):7d} chars  "
                  f"{total_on_tokens:6d} tok  (incl. {args.places} places)")
        if gate_results is not None:
            _print_gate(gate_results, gate_ok)

    if args.gate and not gate_ok:
        print("\nTOKEN BUDGET GATE FAILED (see the table above)", file=sys.stderr)
        return 1
    if args.budget is not None and on_tokens > args.budget:
        print(f"\nSTATIC PREFIX (scenes ON) BUDGET EXCEEDED: {on_tokens} > {args.budget} tokens",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
