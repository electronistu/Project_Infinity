"""The compacted prompt schema: the curated hidden properties are prompt-side only.

`update_player_list`'s flat ability aliases (`set_*` / `*_bonus` / `*_bonus_max`) are dropped
from the schema the GM sees -- they are duplicate spellings of `effects` entries the server
still accepts. This pins the contract:

  - the prompt schema omits them and keeps `effects`;
  - the SERVER schema (raw, from FastMCP) still names them, so the calls still validate;
  - the engine sends exactly what `tools/token_budget.py` measures (or the gate is a fiction);
  - a hidden property is never a required one.

No LLM / no network (the MCP server is booted in-process). Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_tool_schema.py
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web import protocol, tool_schema  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _load_token_budget():
    spec = importlib.util.spec_from_file_location("token_budget_schema", REPO / "tools" / "token_budget.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Tool:
    """A minimal stand-in for an MCP tool object (`_tool_schema` reads these three attrs)."""

    def __init__(self, name, desc, schema):
        self.name = name
        self.description = desc
        self.inputSchema = schema


def main():
    tb = _load_token_budget()
    hidden = set(tool_schema.hidden_properties("update_player_list"))
    rec("the hidden set is the whole ability family", len(hidden) == 18,
        ", ".join(sorted(hidden)))

    raw = next(schema for name, _desc, schema in tb.tool_listing()
               if name == "update_player_list")
    raw_props = set((raw or {}).get("properties", {}) or {})
    rec("the SERVER schema still names every hidden property (calls still validate)",
        hidden <= raw_props)
    rec("no hidden property is required", not (hidden & set((raw or {}).get("required", []) or [])))

    compacted = tool_schema.compact_schema(raw, "update_player_list")
    compact_props = set(compacted.get("properties", {}) or {})
    rec("the PROMPT schema drops them all", not (hidden & compact_props))
    rec("the visible generic route (`effects`) is kept", "effects" in compact_props)
    rec("the compaction is unchanged for a tool with no hidden list",
        set(tool_schema.compact_schema(raw, "resolve_magic").get("properties", {}))
        == set((raw or {}).get("properties", {}) or {}))

    # The engine must send exactly what the gate measures.
    engine_params = protocol._tool_schema(_Tool("update_player_list", "", raw))["function"]["parameters"]
    rec("the engine's schema drops the hidden set too (it passes the tool name)",
        not (hidden & set(engine_params.get("properties", {}) or {})))
    rec("the engine's schema equals the one token_budget measures",
        engine_params == compacted)

    upl_row = next(r for r in tb._tool_rows(tb._counter()[1]) if r["name"] == "update_player_list")
    rec("token_budget measures the compacted schema (no hidden name in its text)",
        "set_str" not in upl_row["schema_text"] and not any(
            f'"{n}"' in upl_row["schema_text"] for n in hidden))

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
