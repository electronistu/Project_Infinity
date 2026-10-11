"""The session timeline: the STATE notebook, the event entries, and compaction.

No LLM / no network: the chronicle summariser is stubbed, and the compaction threshold is
lowered so the fold is exercised directly.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_timeline.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web import engine  # noqa: E402
from web.engine import (  # noqa: E402
    GameSession, _clamp_block, _entry_turn_range, _extract_state, _split_timeline_entries,
    load_chronicle, load_state, load_timeline,
)

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _session() -> GameSession:
    gs = GameSession(base_dir=REPO, model="test")
    gs.timeline_path = os.path.join(tempfile.mkdtemp(), "x.timeline")
    return gs


def _entries(gs):
    return _split_timeline_entries(load_timeline(gs.timeline_path))


def unit_tests() -> bool:
    print("  -- parsing --")
    text = (
        "# Session Timeline\n\n"
        "<!-- legend:egypt --> Egypt remembers a stranger.\n\n"
        "<!-- state -->\n**Goal**: survive.\n**Hooks**: it hunts.\n<!-- /state -->\n\n"
        "<!-- chronicle:1-9 -->\nThe story so far.\n<!-- /chronicle -->\n\n"
        "## Turns 10-11 | London | dusk\n**Key Events**:\n- one\n\n"
        "## Turns 12-12 | London | night\n**Key Events**:\n- two\n"
    )
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.timeline")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)

    kept = load_timeline(p)
    rec("load_timeline strips the header", "# Session Timeline" not in kept)
    rec("load_timeline strips the legends", "legend:" not in kept)
    rec("load_timeline strips the STATE block", "<!-- state -->" not in kept and "Goal" not in kept)
    rec("load_timeline strips the chronicle", "<!-- chronicle" not in kept)
    rec("load_timeline keeps the entries", "Turns 10-11" in kept and "Turns 12-12" in kept)

    rec("load_state reads the notebook", load_state(p).startswith("**Goal**: survive."))
    chron = load_chronicle(p)
    rec("load_chronicle reads the range + body", chron == (1, 9, "The story so far."), str(chron))

    parts = _split_timeline_entries(kept)
    rec("_split_timeline_entries finds both entries", len(parts) == 2, str(len(parts)))
    rec("_entry_turn_range reads a range", _entry_turn_range(parts[0]) == (10, 11))
    rec("_entry_turn_range reads a single turn", _entry_turn_range(parts[1]) == (12, 12))

    state, cleaned = _extract_state("prose\n<!-- state -->\n**Goal**: go.\n<!-- /state -->")
    rec("_extract_state pulls the block", state == "**Goal**: go.")
    rec("_extract_state cleans the reply", "<!-- state -->" not in cleaned)
    bad, _ = _extract_state("<!-- state -->\n<placeholder>\n<!-- /state -->")
    rec("_extract_state rejects the placeholder echo", bad == "")

    clamped = _clamp_block("a\n" * 100, 8)
    rec("_clamp_block bounds at a line boundary", len(clamped) <= 8 and not clamped.endswith("a\n"))

    # A state block must not affect turn numbering.
    rec("numbering ignores the chronicle/state", engine._max_timeline_turn(kept) == 12)
    return True


def _save(gs, turn):
    gs._write_timeline(f"## Turns {turn}-{turn} | place | day\n**Key Events**:\n- event {turn}.")


def compaction_tests() -> bool:
    print("  -- compaction --")
    saved_cap, saved_max = engine.MAX_TIMELINE_CHARS, engine.TIMELINE_MAX_ENTRIES
    engine.MAX_TIMELINE_CHARS = 400
    engine.TIMELINE_MAX_ENTRIES = 6
    ok = True
    try:
        gs = _session()
        gs._state = "**Goal**: survive.\n**Hooks**: the hunter."
        for i in range(1, 8):
            _save(gs, i)

        async def fake(old_text, a, b):
            return f"The story so far: turns {a}-{b}, the Traveller pressed on."

        gs._summarize_chronicle = fake
        asyncio.run(gs._compact_timeline("## Turns 8-8 | place | day\n**Key Events**:\n- eight."))
        gs._write_timeline("## Turns 8-8 | place | day\n**Key Events**:\n- eight.")
        with open(gs.timeline_path, encoding="utf-8") as f:
            raw = f.read()

        chron = load_chronicle(gs.timeline_path)
        rec("the older entries folded into one chronicle", chron is not None and chron[:2] == (1, 5),
            str(chron[:2] if chron else None))
        rec("exactly one chronicle marker on disk", raw.count("<!-- chronicle:") == 1)
        kept = _entries(gs)
        rec("the recent entries are kept verbatim", [e.split("|")[0].strip() for e in kept]
            == ["## Turns 6-6", "## Turns 7-7", "## Turns 8-8"], str(len(kept)))
        rec("the folded entries are gone from the injected view",
            "Turns 1-1" not in load_timeline(gs.timeline_path))
        rec("the STATE notebook survives the fold", load_state(gs.timeline_path).startswith("**Goal**"))
        rec("turn numbering continues past the fold",
            engine._max_timeline_turn(load_timeline(gs.timeline_path)) == 8)
        rec("one header on disk", raw.count("# Session Timeline") == 1)
        with open(gs.timeline_path + ".bak", encoding="utf-8") as f:
            backup = f.read()
        rec("the pre-fold file is kept as .bak insurance", "Turns 1-1" in backup, str(len(backup)))

        body = gs._timeline_body()
        rec("the injected body leads with STATE", body.startswith("STATE"))
        rec("the injected body carries EVENTS", "EVENTS" in body and "story so far" in body)

        # A second compaction folds the kept entries + the old chronicle into one, keeping `a`.
        before_a = load_chronicle(gs.timeline_path)[0]
        for i in range(9, 15):
            _save(gs, i)
        asyncio.run(gs._compact_timeline("## Turns 15-15 | place | day\n**Key Events**:\n- fifteen."))
        gs._write_timeline("## Turns 15-15 | place | day\n**Key Events**:\n- fifteen.")
        chron2 = load_chronicle(gs.timeline_path)
        with open(gs.timeline_path, encoding="utf-8") as f:
            raw2 = f.read()
        rec("a second compaction keeps a single chronicle", raw2.count("<!-- chronicle:") == 1)
        rec("... and preserves the original coverage start", chron2[0] == before_a, str(chron2[0]))
        rec("... and still leaves kept entries", len(_entries(gs)) >= 2)

        # A failed summariser must never lose history.
        gs2 = _session()
        for i in range(1, 8):
            _save(gs2, i)

        async def empty(old_text, a, b):
            return ""

        gs2._summarize_chronicle = empty
        asyncio.run(gs2._compact_timeline("## Turns 8-8 | place | day\n**Key Events**:\n- x."))
        rec("a failed chronicle writes no block", load_chronicle(gs2.timeline_path) is None)
        rec("... and leaves every entry on disk", len(_entries(gs2)) == 7, str(len(_entries(gs2))))

        # A non-shrinking chronicle is rejected too.
        async def same(old_text, a, b):
            return old_text

        gs3 = _session()
        for i in range(1, 8):
            _save(gs3, i)
        gs3._summarize_chronicle = same
        before = open(gs3.timeline_path, encoding="utf-8").read()
        asyncio.run(gs3._compact_timeline("## Turns 8-8 | place | day\n**Key Events**:\n- x."))
        rec("a non-shrinking chronicle is rejected",
            open(gs3.timeline_path, encoding="utf-8").read() == before and load_chronicle(gs3.timeline_path) is None)

        rec("no compaction under the cap keeps every entry", True)
        gs4 = _session()
        engine.MAX_TIMELINE_CHARS = saved_cap
        for i in range(1, 4):
            _save(gs4, i)

        async def should_not_run(old_text, a, b):
            raise AssertionError("compacted under the cap")

        gs4._summarize_chronicle = should_not_run
        asyncio.run(gs4._compact_timeline("## Turns 4-4 | place | day\n**Key Events**:\n- four."))
        rec("under the cap nothing is folded", load_chronicle(gs4.timeline_path) is None and len(_entries(gs4)) == 3)
    finally:
        engine.MAX_TIMELINE_CHARS, engine.TIMELINE_MAX_ENTRIES = saved_cap, saved_max
    return ok


def main() -> int:
    print("=" * 72)
    print("TIMELINE (STATE notebook + event entries + compaction)")
    print("=" * 72)
    unit_tests()
    compaction_tests()
    ok = all(RESULTS)
    print(f"\n  RESULT: {'PASS' if ok else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
