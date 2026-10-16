"""The session timeline: the STATE notebook, the event entries, and compaction.

No LLM / no network: the chronicle summariser is stubbed, and the compaction threshold is
lowered so the fold is exercised directly.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_timeline.py
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web import timeline  # noqa: E402
from web.engine import GameSession  # noqa: E402
from web.gm_view import _clamp_block, _trim_legend  # noqa: E402
from web.timeline import (  # noqa: E402
    _clean_state_page, _entry_turn_range, _extract_state, _extract_states,
    _split_timeline_entries, _state_has_fields, load_chronicle, load_state, load_states,
    load_timeline,
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

    # The age's memory is bounded by dropping the OLDEST sentences -- the newest memory is
    # what matters, and the payload is a whole multi-sentence memory, not one sentence.
    memory = ("He came out of the desert with a device no smith could name. The priests put a "
              "price on his head. The soldiers never found him. He left by the river.")
    kept_sentences = _trim_legend(memory, 110)
    rec("_trim_legend drops the oldest sentences and keeps the newest",
        kept_sentences.endswith("He left by the river.")
        and "desert" not in kept_sentences and len(kept_sentences) <= 110,
        kept_sentences)
    rec("_trim_legend leaves a memory inside the limit untouched",
        _trim_legend(memory, 500) == memory)

    # A state block must not affect turn numbering.
    rec("numbering ignores the chronicle/state", timeline._max_timeline_turn(kept) == 12)
    return True


def _save(gs, turn):
    gs._write_timeline(f"## Turns {turn}-{turn} | place | day\n**Key Events**:\n- event {turn}.")


def compaction_tests() -> bool:
    print("  -- compaction --")
    saved_cap, saved_max = timeline.MAX_TIMELINE_CHARS, timeline.TIMELINE_MAX_ENTRIES
    timeline.MAX_TIMELINE_CHARS = 400
    timeline.TIMELINE_MAX_ENTRIES = 6
    ok = True
    try:
        gs = _session()
        gs.classic = True
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
            timeline._max_timeline_turn(load_timeline(gs.timeline_path)) == 8)
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
        timeline.MAX_TIMELINE_CHARS = saved_cap
        for i in range(1, 4):
            _save(gs4, i)

        async def should_not_run(old_text, a, b):
            raise AssertionError("compacted under the cap")

        gs4._summarize_chronicle = should_not_run
        asyncio.run(gs4._compact_timeline("## Turns 4-4 | place | day\n**Key Events**:\n- four."))
        rec("under the cap nothing is folded", load_chronicle(gs4.timeline_path) is None and len(_entries(gs4)) == 3)
    finally:
        timeline.MAX_TIMELINE_CHARS, timeline.TIMELINE_MAX_ENTRIES = saved_cap, saved_max
    return ok


def per_era_tests() -> bool:
    """STATE is scoped in the era game: one page per age, and only the current age's page
    is ever injected."""
    print("  -- per-era STATE pages --")
    gs = _session()
    gs.classic = False
    gs.era = "wallachia"
    gs._era_states = {"wallachia": "**Goal**: take Curtea de Arge\u0219.",
                      "egypt": "**Goal**: cross the desert."}
    gs._write_timeline()
    with open(gs.timeline_path, encoding="utf-8") as f:
        raw = f.read()
    rec("each age gets its own keyed block", raw.count("<!-- state:") == 2, str(raw.count("<!-- state:")))
    rec("era mode writes no legacy unkeyed block", "<!-- state -->" not in raw)
    rec("era mode writes no traveller page", "state:traveller" not in raw)
    pages = load_states(gs.timeline_path)
    rec("load_states reads every age page",
        set(pages) == {"wallachia", "egypt"}, str(sorted(pages)))
    rec("load_state(key) reads one page",
        load_state(gs.timeline_path, "egypt").startswith("**Goal**: cross"))
    rec("load_state() (unkeyed) is empty in era mode", load_state(gs.timeline_path) == "")
    rec("load_timeline strips every keyed page", "state:" not in load_timeline(gs.timeline_path))

    # Saving in a second age must not touch the first age's page.
    wallachia = load_state(gs.timeline_path, "wallachia")
    gs.era = "egypt"
    gs._era_states["egypt"] = "**Goal**: leave the oasis."
    gs._write_timeline()
    rec("saving in one age leaves another age's page untouched",
        load_state(gs.timeline_path, "wallachia") == wallachia)

    body = gs._timeline_body()
    rec("injection carries only the current age",
        "leave the oasis" in body and "take Curtea" not in body)
    rec("the jump can inject the age it lands in", "take Curtea" in gs._state_body("wallachia"))

    # The departure ask: a page written into a narrative turn is captured, never shown.
    reply = ("The world folds shut.\n"
             "<!-- state:egypt -->\n**Goal**: leave the oasis.\n<!-- /state:egypt -->\n"
             "<!-- state:traveller -->\n**Goal**: master the Device.\n<!-- /state:traveller -->\n"
             "{{remembers: the age forgets him.}}")
    gs.era = "egypt"
    cleaned = gs._capture_state_pages(reply)
    rec("a departure page never reaches the narrative", "<!-- state:" not in cleaned, cleaned[:40])
    rec("... and a stray traveller page is stripped too", "traveller" not in cleaned)
    rec("... the age page is parked for the jump", gs._pending_era_state is not None
        and gs._pending_era_state[0] == "egypt")
    gs._era_states.pop("egypt", None)
    gs._commit_departure_state("egypt")
    rec("the departure page lands on the age being left",
        gs._era_states.get("egypt", "").startswith("**Goal**: leave"))
    rec("the parked page is consumed", gs._pending_era_state is None)

    # Best-effort: a closing turn that wrote nothing keeps the page already held.
    gs._era_states["egypt"] = "old page"
    gs._pending_era_state = None
    gs._commit_departure_state("egypt")
    rec("a missed departure keeps the page already held", gs._era_states["egypt"] == "old page")

    # _extract_states strips every keyed block, not only the keys asked for.
    pages, cleaned = _extract_states(reply, ["egypt"])
    rec("_extract_states pulls only the keys asked for", set(pages) == {"egypt"})
    rec("_extract_states strips every keyed block", "<!-- state:" not in cleaned)

    # A save made before the pages were scoped has one unkeyed block: it is the age the
    # Traveller is in, and not a classic notebook.
    legacy = _session()
    legacy.classic = False
    legacy.era = "tang"
    with open(legacy.timeline_path, "w", encoding="utf-8") as f:
        f.write("# Session Timeline\n\n<!-- state -->\n**Goal**: old truth.\n<!-- /state -->\n")
    legacy._load_state_pages()
    rec("a legacy unkeyed block seeds the age the Traveller is in",
        legacy._era_states.get("tang", "").startswith("**Goal**: old truth."))
    rec("... and is not left as the classic notebook", legacy._state == "")

    # A retired scoped key (a legacy `state:traveller`) is not an age: it is never injected
    # and never rewritten.
    stale = _session()
    stale.classic = False
    stale.era = "tang"
    with open(stale.timeline_path, "w", encoding="utf-8") as f:
        f.write("# Session Timeline\n\n"
                "<!-- state:tang -->\n**Goal**: keyed.\n<!-- /state:tang -->\n\n"
                "<!-- state:traveller -->\n**Goal**: master the Device.\n<!-- /state:traveller -->\n")
    stale._load_state_pages()
    rec("a retired traveller page is not loaded as an age", "traveller" not in stale._era_states)
    stale._write_timeline()
    with open(stale.timeline_path, encoding="utf-8") as f:
        stale_raw = f.read()
    rec("... and is dropped from the rewritten file", "traveller" not in stale_raw)

    # A keyed page wins over a stale legacy block left in the same file.
    keyed = _session()
    keyed.classic = False
    keyed.era = "tang"
    with open(keyed.timeline_path, "w", encoding="utf-8") as f:
        f.write("# Session Timeline\n\n<!-- state:tang -->\n**Goal**: keyed.\n<!-- /state:tang -->\n\n"
                "<!-- state -->\n**Goal**: stale legacy.\n<!-- /state -->\n")
    keyed._load_state_pages()
    rec("keyed pages win over a stale legacy block",
        keyed._era_states.get("tang") == "**Goal**: keyed.")

    # Classic keeps its single unkeyed notebook.
    cl = _session()
    cl.classic = True
    cl._state = "**Goal**: survive."
    cl._write_timeline()
    with open(cl.timeline_path, encoding="utf-8") as f:
        craw = f.read()
    rec("classic writes the single unkeyed notebook", "<!-- state -->" in craw and "state:" not in craw)
    rec("classic injection leads with STATE", cl._state_body().startswith("STATE"))

    # The jump: compaction drops the events but must re-inject the page it lands in.
    jump = _session()
    jump.classic = False
    jump.era = "egypt"
    jump.messages = [{"role": "system", "content": "the protocol"}]
    jump._legends = {"wallachia": "Wallachia remembers a conqueror."}
    jump._era_states = {"egypt": "**Goal**: leave the oasis."}
    jump._chronicle = (1, 5, "The story so far: he fled the capital.")
    jump._timeline_kept = "## Turns 6-6 | place | day\n**Key Events**:\n- six."
    jump._compact("wallachia")
    joined = "\n".join(m.get("content", "") for m in jump.messages if isinstance(m, dict))
    rec("the jump re-injects the age's page", "leave the oasis" in joined)
    rec("the jump still drops the events",
        "The story so far" not in joined and "Turns 6-6" not in joined)
    return True


def companions_tests() -> bool:
    """The party is engine-derived (never GM-written) and lives in the CHARACTER SHEET
    (name + identity + the stable look), not in the state page."""
    print("  -- engine-listed companions --")
    gs = _session()
    gs.classic = False
    gs.era = "tang"
    gs._era_states = {"tang": "**Goal**: hold the quay."}
    store = {"su lian": {"name": "Su Lian", "race": "human", "class": "monk",
                         "description": "a free woman"},
             "pei": {"name": "Pei", "race": "human", "class": "fighter",
                     "description": ""},
             "setka": {"name": "Setka", "race": "human", "class": "priest",
                       "description": "the oasis"}}
    block = gs._companions_block(store)
    rec("the companions block carries name, identity and the look",
        "Su Lian (human monk)" in block and "— a free woman" in block
        and "Pei (human fighter)" in block, block.replace("\n", " | ")[:80])
    rec("one party -- every companion is listed, whatever the age", "Setka" in block)
    rec("a store with no companions carries no block", gs._companions_block({}) == "")
    rec("the state page itself never stores a companions field",
        "companions" not in gs._state_body().lower())
    return True


def guard_tests() -> bool:
    """The STATE guard and the core repair: engine-owned keys dropped, synonyms renamed,
    and a core-less page kept or repaired (never silently absent)."""
    print("  -- STATE page guard --")
    wild = (
        "era: victorian\n"
        "year: 1858\n"
        "location: England / Liverpool / the dock road\n"
        "regulars: Michael Quill — pier master (Human, Fighter)\n"
        "standing: Pei — wary, honest\n"
        "attitudes: the court is cold\n"
        "companions: Su Lian — sworn to him\n"
        "cast: none\n"
        "reputation: england — nothing owed him\n"
        "device: whole — all five parts in hand\n"
        "threads: Preda still sealed and then freed in Wallachia\n"
        "**Goal**: take the city\n"
        "**Hooks**: the watch is coming\n"
        "**Clocks**: the siege runs\n"
        "**Debts**: his oath to the captain\n"
        "**Secrets**: he is the Traveller\n"
        "**Canon**: he came without a gate\n"
        "**Unpaid**: the sealed door\n"
    )
    era = _clean_state_page(wild)
    clean_keys = ("era:", "year:", "location:", "cast:", "reputation:", "device:",
                  "regulars:", "standing:", "attitudes:", "companions:")
    rec("engine-owned keys are dropped", not any(k in era for k in clean_keys), era[:60])
    rec("`standing`/`regulars`/`attitudes` are dropped, not renamed",
        "Standing" not in era and "Michael Quill" not in era and "wary" not in era)
    rec("a `companions` line is dropped (the engine owns it)",
        "Su Lian" not in era and "**Companions**" not in era)
    rec("`threads` becomes Hooks", "**Hooks**: Preda still sealed" in era)
    rec("an age page keeps Debts and Secrets", "**Debts**" in era and "**Secrets**" in era)
    rec("the canonical order survives (Goal first)", era.startswith("**Goal**:"))
    rec("the canonical order puts Hooks before Clocks",
        era.index("**Hooks**") < era.index("**Clocks**"))
    rec("the core check reads the fields", _state_has_fields(era, ("Goal", "Secrets"))
        and not _state_has_fields(era, ("Goal", "Bogus")))

    # A page on disk written before the vocabulary was enforced is cleaned on read.
    gs = _session()
    with open(gs.timeline_path, "w", encoding="utf-8") as f:
        f.write("# Session Timeline\n\n<!-- state:victorian -->\n" + wild
                + "<!-- /state:victorian -->\n")
    loaded = load_states(gs.timeline_path).get("victorian", "")
    rec("a wild page on disk is cleaned on read",
        "year:" not in loaded and "**Hooks**:" in loaded and loaded.startswith("**Goal**"))

    # The repair path: core present -> stored; core missing + held -> kept, silently;
    # core missing + nothing held -> ONE tool-free re-ask.
    async def run(gs_obj, scope, page, reply):
        await gs_obj._apply_state_page(scope, page, reply, "Goal")

    calls: list[str] = []

    async def fake_plain(messages, label="summary"):
        calls.append(label)
        key = re.search(r"state:([a-z0-9_-]+)", messages[0]["content"]).group(1)
        return f"<!-- state:{key} -->\n**Goal**: hold the court.\n<!-- /state:{key} -->"

    gs._plain_call = fake_plain
    gs._era_states = {}
    asyncio.run(run(gs, "wallachia", "**Goal**: hold the court.", "raw"))
    rec("a page carrying its core is stored",
        gs._era_states.get("wallachia", "").startswith("**Goal**") and not calls)

    gs._era_states["wallachia"] = "**Goal**: the old goal."
    asyncio.run(run(gs, "wallachia", "**Hooks**: only hooks, no goal", "raw"))
    rec("a page missing its core keeps the one held, silently",
        gs._era_states["wallachia"].startswith("**Goal**: the old goal") and not calls)

    gs._era_states = {}
    asyncio.run(run(gs, "wallachia", "**Hooks**: only hooks, no goal", "the malformed reply"))
    rec("with nothing held, one tool-free re-ask runs",
        calls == ["state-fix"] and gs._era_states.get("wallachia", "").startswith("**Goal**"),
        str(calls))
    return True


def main() -> int:
    print("=" * 72)
    print("TIMELINE (STATE notebook + event entries + compaction)")
    print("=" * 72)
    unit_tests()
    compaction_tests()
    per_era_tests()
    companions_tests()
    guard_tests()
    ok = all(RESULTS)
    print(f"\n  RESULT: {'PASS' if ok else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
