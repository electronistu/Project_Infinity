"""Difficulty modes: creation prompt -> `.player` -> GM protocol gating.

No network. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_difficulty.py
"""

import json
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from forge.config_loader import load_config  # noqa: E402
from forge.character_creator import create_debug_character  # noqa: E402
from forge.formatter import get_player_json  # noqa: E402
from web.creation import CreationBridge, _run_creation  # noqa: E402
from web.engine import render_protocol, _player_difficulty  # noqa: E402

RESULTS = []
OUTPUT = REPO / "output"


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def auto_answer_easy(step, counters, name):
    kind = step["kind"]
    if kind == "text":
        counters["text"] += 1
        return name if counters["text"] == 1 else (step.get("default") or "Unknown")
    if kind == "number":
        index = counters["number"]
        counters["number"] += 1
        return 15 if index < 3 else 8
    if kind == "pointbuy":
        return {a["key"]: (15 if i < 3 else 8) for i, a in enumerate(step["abilities"])}
    if kind == "single":
        if step.get("prompt") == "Choose your difficulty":
            counters["difficulty"] = counters.get("difficulty", 0) + 1
            for opt in step["options"]:
                if str(opt["label"]).lower().startswith("easy"):
                    return opt["id"]
            return step["options"][-1]["id"]
        return step["options"][0]["id"]
    if kind == "multi":
        count = step.get("min_choices") or 1
        return [opt["id"] for opt in step["options"][:count]]
    raise ValueError(f"unknown step kind: {kind}")


def run_creation(name):
    bridge = CreationBridge(load_config(), OUTPUT)
    thread = threading.Thread(target=_run_creation, args=(bridge,), daemon=True)
    thread.start()
    counters = {"text": 0, "number": 0}
    prompts = 0
    terminal = None
    while True:
        steps, terminal = bridge.take_steps(timeout=120)
        if terminal is not None:
            break
        prompts += 1
        bridge.submit_answer(auto_answer_easy(steps[-1], counters, name))
    thread.join(timeout=30)
    return terminal, prompts, counters


def main() -> int:
    print("=" * 72)
    print("DIFFICULTY MODES")
    print("=" * 72)

    # 0) protocol gating
    proto = "A\n<!-- EASY:ON -->\nEASY RULES\n<!-- EASY:END -->\nB"
    easy = render_protocol(proto, False, "easy")
    hard = render_protocol(proto, False, "hard")
    rec("easy keeps the rules, strips the markers",
        "EASY RULES" in easy and "EASY:" not in easy, repr(easy))
    rec("hard drops the rules, strips the markers",
        "EASY RULES" not in hard and "EASY:" not in hard, repr(hard))
    rec("hard keeps the surrounding rules", "A" in hard and "B" in hard)
    rec("default difficulty is hard", "EASY RULES" not in render_protocol(proto, False))

    real = (REPO / "GameMaster_MCP.md").read_text(encoding="utf-8")
    rec("real protocol shows the block on easy",
        "DIFFICULTY: EASY" in render_protocol(real, True, "easy"))
    rec("real protocol hides the block on hard",
        "DIFFICULTY: EASY" not in render_protocol(real, True, "hard"))
    rec("easy protocol carries the low DC bands",
        "A normal check is 5" in render_protocol(real, True, "easy")
        and "never go above 15" in render_protocol(real, True, "easy"))
    rec("hard protocol omits the DC bands",
        "A normal check is 5" not in render_protocol(real, True, "hard"))
    rec("real protocol still strips the EASY markers either way",
        "EASY:ON" not in render_protocol(real, True, "easy")
        and "EASY:ON" not in render_protocol(real, True, "hard"))

    # 1) _player_difficulty
    with tempfile.TemporaryDirectory() as td:
        for value, expect in [("easy", "easy"), ("EASY", "easy"), (" hard ", "hard"),
                              ("hard", "hard"), ("", "hard"), ("weird", "hard")]:
            path = Path(td) / "x.player"
            path.write_text(json.dumps({"difficulty": value}), encoding="utf-8")
            rec(f"_player_difficulty({value!r}) -> {expect}",
                _player_difficulty(str(path)) == expect)
        rec("missing file -> hard", _player_difficulty(str(Path(td) / "nope.player")) == "hard")
        bad = Path(td) / "bad.player"
        bad.write_text("{not json", encoding="utf-8")
        rec("malformed json -> hard", _player_difficulty(str(bad)) == "hard")

    # 2) model default + formatter round-trip
    pc = create_debug_character(load_config())
    rec("PlayerCharacter defaults to hard", pc.difficulty == "hard")
    rec("formatter emits difficulty",
        json.loads(get_player_json(pc)).get("difficulty") == "hard")

    # 3) end-to-end: forcing the easy option writes easy into the `.player`
    name = "difficulty_probe"
    for path in OUTPUT.glob(f"{name}*.player"):
        path.unlink()
    terminal, prompts, counters = run_creation(name)
    done = bool(terminal) and terminal.get("type") == "done"
    rec("easy creation completes", done, f"prompts={prompts}")
    if done:
        player = OUTPUT / terminal["player"]
        data = json.loads(player.read_text(encoding="utf-8"))
        rec("creation asked the difficulty question", counters.get("difficulty") == 1)
        rec("easy creation stores difficulty=easy", data.get("difficulty") == "easy")
        player.unlink()
    elif terminal:
        print(f"    terminal: {terminal.get('type')} {terminal.get('message', '')}")

    print(f"\n  RESULT: {'PASS' if all(RESULTS) else 'FAIL'}")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
