"""The token-budget gate tests itself (no LLM / no network).

Locks the token-budget tooling:
  - `--budget` keys off the STATIC PREFIX, not the prefix + place tree;
  - the gate refuses to run on the chars/3.6 estimate;
  - a `target` ceiling is reported but never enforced.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_budget.py
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _load():
    spec = importlib.util.spec_from_file_location("token_budget", REPO / "tools" / "token_budget.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tb = _load()


def _run_budget(*args):
    return subprocess.run([sys.executable, "tools/token_budget.py", *args],
                          cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main():
    # -- the ceilings manifest ------------------------------------------------
    budgets = tb.load_budgets()
    rec("budgets.yml loads", len(budgets) >= 8, f"{len(budgets)} rows")
    rec("every row has name/ceiling/status/measure (ceiling may be null = report-only)",
        all(b.get("name") and (b.get("ceiling") is None or isinstance(b.get("ceiling"), int))
            and b.get("status") in ("enforced", "target") and b.get("measure")
            for b in budgets))
    by_name = {b["name"]: b for b in budgets}
    rec("always_on_total is report-only by design",
        by_name.get("always_on_total", {}).get("status") == "target"
        and by_name.get("always_on_total", {}).get("ceiling") is None)
    rec("static prefix is enforced",
        by_name.get("static_prefix_scenes_on", {}).get("status") == "enforced")
    rec("place tree is enforced",
        by_name.get("place_tree", {}).get("status") == "enforced")

    # -- exactness ------------------------------------------------------------
    label, count, exact = tb._counter()
    rec("counter reports exactness", isinstance(exact, bool), f"{label}, exact={exact}")
    ready, why = tb.check_gate_ready(True)
    rec("gate is ready on exact counts", ready and not why)
    refused, why = tb.check_gate_ready(False)
    rec("gate refuses an estimate", (not refused) and "tiktoken" in why)

    # -- ceiling evaluation ---------------------------------------------------
    metrics = {"at": 100, "over": 101, "none": None}
    rows = [{"name": "at", "ceiling": 100, "status": "enforced", "measure": "at"},
            {"name": "over", "ceiling": 100, "status": "enforced", "measure": "over"},
            {"name": "unmeasured", "ceiling": 100, "status": "enforced", "measure": "none"},
            {"name": "future", "ceiling": 10, "status": "target", "measure": "over"}]
    results, ok = tb.evaluate_budgets(rows, metrics)
    got = {r["name"]: r["ok"] for r in results}
    rec("enforced ceiling exactly at the limit passes", got["at"])
    rec("enforced ceiling over the limit fails", not got["over"])
    rec("enforced ceiling with no measurement fails", not got["unmeasured"])
    rec("target ceiling over its value still passes", got["future"])
    rec("a single breach reddens the gate", not ok)

    # -- the live metrics -----------------------------------------------------
    place_tree = tb._metric_place_tree(count)
    rec("place tree is measured at the 12-place cap", isinstance(place_tree, int), f"{place_tree} tok")
    rec("the 12-place worst case is inside 400", place_tree <= 400, f"{place_tree} tok")
    era_bytes = tb._metric_era_file_bytes()
    rec("era_file_bytes measures config/eras/*.yml",
        isinstance(era_bytes, int) and era_bytes <= 5120, f"{era_bytes} B")
    era_index_tokens = count(tb._era_index_text())
    rec("era_index is measured and within its ceiling",
        era_index_tokens <= 150, f"{era_index_tokens} tok")
    metrics = tb._metrics(0, 0, era_index_tokens, count)
    rec("always_on_total is reported", isinstance(metrics.get("always_on_total"), int),
        f"{metrics.get('always_on_total')} tok")
    lookup_row = tb._metric_lookup_schema(tb._tool_rows(count))
    rec("lookup tool row is measured and inside its ceiling",
        isinstance(lookup_row, int) and lookup_row <= 90, f"{lookup_row} tok")
    era_scaffold_tokens = tb._metric_era_scaffold(count)
    rec("era_scaffold is measured and within its ceiling",
        isinstance(era_scaffold_tokens, int) and era_scaffold_tokens <= 450, f"{era_scaffold_tokens} tok")

    # -- the two games are measured apart (D31, D32) ---------------------------
    rows = tb._tool_rows(count)
    proto_text = tb.PROTOCOL.read_text(encoding="utf-8")
    tt_on = count(tb._render_protocol(proto_text, True))
    classic_on = count(tb._render_protocol(proto_text, True, "classic"))
    lookup_tok = tb._metric_lookup_schema(rows) or 0
    tool_tok = sum(r["desc_tokens"] + r["schema_tokens"] for r in rows)
    classic_prefix = tool_tok - lookup_tok + classic_on
    tt_prefix = tool_tok + tt_on + era_index_tokens
    two = tb._metrics(tt_prefix, tt_prefix, era_index_tokens, count, rows, classic_prefix)
    rec("the classic prefix is measured separately",
        two.get("static_prefix_classic") == classic_prefix, str(classic_prefix))
    rec("the game not being played is the smaller prompt",
        classic_prefix < two["static_prefix_scenes_on"],
        f"classic {classic_prefix} < time-traveler {two['static_prefix_scenes_on']}")
    rec("both games report their own per-turn floor",
        isinstance(two.get("always_on_total_classic"), int)
        and isinstance(two.get("always_on_total"), int))

    # -- regression: --budget keys off the static prefix, not the total -------
    info = _run_budget("--json")
    rec("token_budget --json runs", info.returncode == 0, f"exit {info.returncode}")
    payload = json.loads(info.stdout)
    prefix = payload["total_on_tokens"]              # tools + protocol (scenes ON)
    with_places = payload["total_on_with_places_tokens"]
    rec("the place tree makes the total strictly larger", prefix < with_places,
        f"{prefix} < {with_places}")
    gated = _run_budget("--budget", str(prefix))
    rec("--budget asserts the static prefix (not prefix + places)",
        gated.returncode == 0, f"exit {gated.returncode} at --budget {prefix}")

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
