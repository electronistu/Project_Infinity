"""On-demand tool references: ``config/tool_refs.yml`` + the ``lookup('ref:TOOL')`` route.

Locks the contract:
  - every reference resolves by name, and a miss lists the known names;
  - the reference bodies carry the material that was moved off the always-on descriptions;
  - ``lookup`` dispatches ``ref:``/``tool:`` to the reference store without touching the era
    machinery (the branch works with no database at all), and leaves era lore to ``web.eras``.

No LLM / no network. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_tool_refs.py
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web import refs  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _load_dice_server():
    spec = importlib.util.spec_from_file_location("dice_server_refs", REPO / "dice_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ids = refs.tool_ref_ids()
    rec("the reference store loads", bool(ids), ", ".join(ids))
    rec("the two moved references exist",
        {"update_player_list", "resolve_magic"} <= set(ids), str(ids))

    upl = refs.render_tool_ref("update_player_list")
    rec("the update_player_list reference carries the effects vocabulary + the when predicates",
        "unmodelled_effects" in upl and "ability_set" in upl and "no_armor" in upl, f"{len(upl)} chars")
    rec("it carries the ability floor/bonus semantics moved off the description",
        "FLOOR" in upl and "*_bonus_max" in upl)

    rm = refs.render_tool_ref("resolve_magic")
    rec("the resolve_magic reference carries the resolution rules",
        "Multi-target AoE" in rm and "Healing is capped" in rm, f"{len(rm)} chars")

    rec("a miss lists the known references instead of failing",
        "Known references" in refs.render_tool_ref("no_such_tool"))
    rec("is_tool_ref accepts ref: and tool: and rejects era lore",
        refs.is_tool_ref("ref:resolve_magic") and refs.is_tool_ref("tool:update_player_list")
        and not refs.is_tool_ref("here") and not refs.is_tool_ref("egypt"))

    # The dispatch works with no database: a `ref:` query must never reach the era lookup.
    ds = _load_dice_server()
    rec("lookup('ref:update_player_list') returns the reference body",
        "unmodelled_effects" in ds.lookup("ref:update_player_list"))
    rec("lookup('tool:resolve_magic') is accepted too",
        "Multi-target AoE" in ds.lookup("tool:resolve_magic"))
    rec("lookup('ref:<unknown>') names the known references",
        "Known references" in ds.lookup("ref:nope"))
    rec("an era query still routes to the era machinery (no DB, still era text)",
        "the Old Kingdom" in ds.lookup("egypt") or "polities:" in ds.lookup("egypt"))

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
