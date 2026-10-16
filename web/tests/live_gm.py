"""Live GM smoke: the turn contract and the place-declaration surface, per mode (opt-in).

NOT collected by `tools/verify.py` -- that step globs `web/tests/test_*.py`, and this file is
named `live_gm.py` on purpose: it drives a REAL model, so it is run when a change to what the
GM sees or does needs observing, never on every gate.

What it checks (and nothing else -- the GM's gameplay competence is the player's own watch):

  * the TURN CONTRACT, asserted on every turn of the run (frames + `session.messages` + the
    dev GM-prompt journal): a batch round carries tools and no prose; the pause token arrives
    alone in its own round; one `paused` per turn that had a batch and none when it had none;
    the resume round is preceded by exactly `{{resume}}` (the awakening by `AWAKENING_RESUME`),
    never by `RESUME_GUARD_NUDGE`; the narrative round carries no tool call in text mode and
    exactly one `request_scene_image` in images mode; the engine's own corrective rounds
    (`narrative-fix` / `scene-fix` / `continue`, triggers `recovery` / `thinking`) NEVER fire;
    no marker drift (`{{_`/`{{__`), no marker in `thinking`, no marker left in the narrative,
    no `tool_not_available` refusal anywhere, and no turn narrated twice.
  * the PLACE SURFACE, per mode: the offered tool set (`note_place` only with images off,
    `request_scene_image` + `register_npcs` only with images on); the hidden tool is never
    called and never refused; a declared move adopts the registry's EXACT saved spelling; the
    `establishing` view is sent on the first call for a place the registry does not hold; text
    mode declares only on movement and persists through the real server handler
    `web.server._persist_place_note`; images mode emits exactly one `scene_request` per
    narrative turn, attached to the prose (`kind == "story"`, never the engine's `auto`
    fallback), carrying the recorded address, `time_of_day` and `weather`.

No image is ever generated: nothing consumes `scene_request` in-process, and
`SceneService.generate` -- the seam `web/backends.py` documents for stubs -- is replaced with
a raiser, so an accidental generation fails the run loudly.

The fixture is generated, never a user save: a debug character written with a pinned era and
arrival, plus a place registry seeded through the real writer (`SceneService.record_place` +
`commit_scene_manifest`, because `session.start()` drops the live manifest and reloads from
disk). Both are deleted at the end.

    venv/bin/python web/tests/live_gm.py --mode text
    venv/bin/python web/tests/live_gm.py --mode images --only stay,return
    venv/bin/python web/tests/live_gm.py --mode both --model gemini-3.8-flash

Exit code: 0 every hard check passed, 1 a hard check failed, 2 a precondition failed.
Artifacts (frames + verdict) land under `output/live_gm/<stamp>_<mode>/`; `output/` is
gitignored.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import io
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.engine import GameSession  # noqa: E402
from web.models import MODELS, gemini_thinking_level, resolve_model  # noqa: E402
from web.places import _scene_key  # noqa: E402
from web.protocol import (  # noqa: E402
    AWAKENING_RESUME, DEFAULT_CONTEXT_WINDOW, NARRATIVE_PHASE_TOOLS, PAUSE_MARKER_RE,
    PLACE_TOOL, REMEMBERS_RE, RESUME_GUARD_NUDGE, RESUME_MARKER_RE, SCENE_TOOL,
    has_unknown_marker, is_pause, strip_markers,
)
from web.scenes import (  # noqa: E402
    SceneService, commit_scene_manifest, current_scene_place, discard_scene_manifest,
    known_scene_places,
)

OUT = REPO / "output"
ERA = "wallachia"
KINGDOM = "Wallachia"
SETTLEMENT = "Târgoviște"
LOWER_TOWN = "the lower town"
ARRIVAL = f"the princely court, in {SETTLEMENT}"

# The seeded registry: two areas (so the block renders an anchor line for the other one) and
# one nested room. The first entry is where the save last stood (`current`).
COURT = ["the princely court", "the great hall"]
GUARD = ["the princely court", "the guard room"]
NESTED = ["the princely court", "the great hall", "the prince's antechamber"]
LANTERN = ["the Drowned Lantern", "the common room"]
REGISTRY = [
    (KINGDOM, SETTLEMENT, NESTED, []),
    (KINGDOM, SETTLEMENT, GUARD, [{"name": "Captain Ilie", "role": "the captain of the guard",
                                   "race": "half-orc", "class": "fighter"}]),
    (KINGDOM, LOWER_TOWN, LANTERN, []),
    # LAST: `record_place` sets the manifest's `current` pointer, so the place the save last
    # stood in must be the final write.
    (KINGDOM, SETTLEMENT, COURT, [{"name": "Steward Radu", "role": "the prince's steward",
                                  "race": "human", "class": "rogue"}]),
]

# The script, in order. `expect` = the registry path the declaration must reuse, `new` = a
# turn that moves to a place the registry does not hold, `stay` = a turn that must NOT move.
SCENARIOS = [
    {"id": "stay", "expect": "stay",
     "text": "I stay where I am and ask the steward who paid for the wine."},
    {"id": "return", "expect": GUARD,
     "text": "I take my leave of the great hall and go back down to the guard room, where "
             "Captain Ilie is holding the prisoner."},
    {"id": "new", "expect": "new",
     "text": "I take the servants' stair down beneath the court, into the cellar where the "
             "prince's wine is kept and the ledgers are hidden."},
    {"id": "other_area", "expect": LANTERN,
     "text": "I leave the court behind and go down into the lower town, to the Drowned "
             "Lantern by the river gate, to hear what the boatmen are saying."},
]

MECHANICS_IN_PROSE = re.compile(
    r"\bAC\s*\d+|\b\d+\s*(?:points? of\s*)?damage\b|\brolled\s+(?:a\s+)?\d+\b"
    r"|\binitiative\b|\b\d+\s*HP\b", re.I)
THIRD_PERSON = re.compile(r"^(?:He|She|They)\s+\w+", re.M)

RESULTS: list[tuple[str, bool, str, bool]] = []


def rec(name: str, ok: bool, detail: str = "", soft: bool = False) -> None:
    RESULTS.append((name, bool(ok), detail, soft))
    tag = "pass" if ok else ("soft" if soft else "FAIL")
    print(f"  [{tag:>5}] {name}" + (f" - {detail}" if detail else ""), flush=True)


# ── the fixture ───────────────────────────────────────────────────────────

def seed_pictures(stem: str) -> None:
    """Give the seeded places an establishing view (a real PNG + its manifest fields).

    Without this the registry is file-less -- the shape a save played with images OFF has --
    and `establishing` for a known place is legitimate (the engine would draw the missing
    seed). With a picture on disk, re-establishing a known place is a wasted regeneration,
    which is the behaviour under test."""
    from PIL import Image

    from web.scenes import live_scene_manifest

    scenes = OUT / "images" / stem / "scenes"
    scenes.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 25, 20)).save(buf, format="PNG")
    data = live_scene_manifest(OUT, stem)
    for index, entry in enumerate((data.get("seeds") or {}).values()):
        name = f"seed-{index}.png"
        (scenes / name).write_bytes(buf.getvalue())
        entry.update({"file": name, "slug": name[:-4], "style": "default", "mime": "image/png"})


def build_fixture(stem: str, images: bool) -> Path:
    """A pinned debug save + a seeded place registry. Returns the `.player` path."""
    from forge.character_creator import create_debug_character
    from forge.config_loader import load_config
    from forge.formatter import get_player_json
    from web.eras import era_reputation_seed

    with contextlib.redirect_stdout(io.StringIO()):  # the creator prints its banner
        pc = create_debug_character(load_config())
    pc.mode = "time_traveler"
    player = OUT / f"{stem}.player"
    player.write_text(
        get_player_json(pc, era_reputation_seed(ERA), ERA, ARRIVAL, mode="time_traveler"),
        encoding="utf-8")
    svc = SceneService(output_dir=OUT)
    for kingdom, area, path, npcs in REGISTRY:
        svc.record_place(stem, era=ERA, kingdom=kingdom, area=area, place=path, main_npcs=npcs)
    if images:
        seed_pictures(stem)
    # `session.start()` drops the live manifest and reloads it from disk: commit, or the
    # session sees no places at all and every place conclusion is void.
    commit_scene_manifest(OUT, stem)
    return player


def drop_fixture(stem: str) -> None:
    discard_scene_manifest(OUT, stem)          # bump the ticket: no late writer can leak
    (OUT / f"{stem}.player").unlink(missing_ok=True)
    (OUT / f"{stem}.timeline").unlink(missing_ok=True)
    shutil.rmtree(OUT / "images" / stem, ignore_errors=True)


def no_generation() -> None:
    """Make any image generation attempt a loud failure (`web/backends.py` names the seam)."""
    def _raise(*_a, **_k):
        raise AssertionError("the live smoke must never generate an image")

    SceneService.generate = _raise  # type: ignore[method-assign]


# ── the driver ────────────────────────────────────────────────────────────

class Drive:
    """One live session: the awakening, then one turn per scenario, with its evidence."""

    def __init__(self, session, event_timeout: float, turn_timeout: float):
        self.session = session
        self.event_timeout = event_timeout
        self.turn_timeout = turn_timeout
        self.gen = session.events()
        self.session_frames: list[dict] = []
        self.ready: dict = {}
        self.turns: list[dict] = []
        self.errors: list[str] = []

    async def _next(self, deadline: float) -> dict | None:
        remaining = min(self.event_timeout, max(0.0, deadline - time.monotonic()))
        if remaining <= 0:
            return None
        try:
            return await asyncio.wait_for(self.gen.__anext__(), timeout=remaining)
        except (asyncio.TimeoutError, StopAsyncIteration):
            return None

    async def run_turn(self, label: str, action: str | None) -> dict | None:
        """Submit an action and collect that turn's frames, messages and journal slice."""
        deadline = time.monotonic() + self.turn_timeout
        before_messages = len(self.session.messages)
        before_journal = len(self.session._gm_journal)
        if action is not None:
            # Submit FIRST: `busy true` is the turn's opening frame and only arrives once the
            # queued action starts -- waiting for it before submitting deadlocks.
            await self.session.submit(action)
        # Skip the previous turn's trailing frames (`busy false`, `cadence`) up to this turn's
        # own `busy true`; the awakening is already running (it begins inside `start()`).
        while True:
            evt = await self._next(deadline)
            if evt is None:
                self.errors.append(f"{label}: no busy frame")
                return None
            if evt.get("type") == "ready":
                self.ready = evt
            elif evt.get("type") == "busy" and evt.get("value") is True:
                break
            else:
                self.session_frames.append(evt)
        frames: list[dict] = []
        while True:
            evt = await self._next(deadline)
            if evt is None:
                self.errors.append(f"{label}: no turn_end within {self.turn_timeout:.0f}s")
                break
            frames.append(evt)
            if evt.get("type") in ("turn_end", "awakening_end"):
                break
        record = {
            "label": label, "action": action or "", "frames": frames,
            "messages": self.session.messages[before_messages:],
            "journal": self.session._gm_journal[before_journal:],
            "flags": {"prose": bool(self.session._narrative_emitted_turn),
                      "scene": bool(self.session._scene_requested_turn),
                      "rejected": bool(self.session._scene_rejected)},
        }
        self.turns.append(record)
        return record


def rounds_of(frames: list[dict]) -> list[dict]:
    """Reconstruct the model rounds of one turn from its frames.

    Tools executed after a round's `assistant_end` belong to that round (the engine runs them
    before asking the model again), which is what makes the per-round shape checkable."""
    rounds: list[dict] = []
    for evt in frames:
        kind = evt.get("type")
        if kind == "assistant_start":
            rounds.append({"content": "", "thinking": "", "tools": []})
        elif not rounds:
            continue
        elif kind == "assistant_end":
            rounds[-1]["content"] = str(evt.get("text") or "")
            rounds[-1]["thinking"] = str(evt.get("thinking") or "")
        elif kind == "tool_call":
            rounds[-1]["tools"].append(str(evt.get("name") or ""))
    return rounds


def prose_of(content: str) -> str:
    text = strip_markers(content)
    return "" if has_unknown_marker(text) else text.strip()


def markers_of(text: str) -> list[str]:
    return re.findall(r"\{\{[^}]{0,80}\}\}", str(text or ""))


# ── the checks ────────────────────────────────────────────────────────────

def check_turn(record: dict) -> None:
    """The turn contract, asserted on one turn (frames + stored messages + the journal)."""
    label = f"{record.get('mode', '')} {record['label']}".strip()
    frames = record["frames"]
    flags = record.get("flags") or {}
    images = bool(record.get("images"))
    rounds = rounds_of(frames)
    if not rounds:
        rec(f"{label}: a model round happened", False, "no assistant round in the frames")
        return
    # A *mechanical* batch: the image call rides the narrative prose, so it is not one (the
    # engine's own `NARRATIVE_PHASE_TOOLS` is the line).
    batch_rounds = [i for i, r in enumerate(rounds)
                    if any(t not in NARRATIVE_PHASE_TOOLS for t in r["tools"])]
    prose_rounds = [i for i, r in enumerate(rounds) if prose_of(r["content"])]
    token_rounds = [i for i, r in enumerate(rounds) if is_pause(r["content"])]
    paused = sum(1 for f in frames if f.get("type") == "paused")

    # A1a -- a batch round carries tools and NO prose (the images narrative round is the one
    # exception, and only for the single image call the protocol attaches to the prose).
    bad = [r for r in rounds if r["tools"] and prose_of(r["content"])
           and not (images and r["tools"] == [SCENE_TOOL])]
    rec(f"{label}: no prose rides a tool batch", not bad,
        str([(r["tools"], prose_of(r["content"])[:40]) for r in bad]))

    # A1b -- the pause token goes in `content` and NEVER alongside a tool call (the engine
    # ignores it there, so the batch would loop and the model would have to recover).
    both = [i for i, r in enumerate(rounds) if r["tools"] and is_pause(r["content"])]
    rec(f"{label}: the pause token never shares a round with a tool call", not both, str(both))

    # A1c -- one `paused` per turn that had a mechanical batch, none when it had none, and
    # only after it ("emit it only when a batch was emitted").
    once = paused in (0, 1) and (paused == 1) == bool(batch_rounds) and (
        paused == 0 or (len(token_rounds) == 1 and token_rounds[0] > batch_rounds[0]))
    rec(f"{label}: the pause handshake happens once, after the batch", once,
        f"paused={paused} batch_rounds={batch_rounds} token_rounds={token_rounds}")

    # A1d -- the resume nudge is the canonical `{{resume}}` (the awakening's one re-anchor),
    # never the guard nudge, and never more than once per turn.
    nudges = [str(m.get("content") or "") for m in record["messages"] if m.get("role") == "user"]
    guard = [n for n in nudges if n.strip() == RESUME_GUARD_NUDGE.strip()]
    resumes = [n for n in nudges if n.strip() in ("{{resume}}", AWAKENING_RESUME.strip())]
    rec(f"{label}: the resume handshake is canonical and single",
        not guard and len(resumes) <= 1, f"nudges={[n[:30] for n in nudges]}")

    # A1e -- the narrative round carries no tool call in text mode, and exactly the one image
    # call in images mode.
    narrative = rounds[prose_rounds[-1]] if prose_rounds else None
    want = [SCENE_TOOL] if images else []
    rec(f"{label}: the narrative round carries only the image call",
        narrative is not None and narrative["tools"] == want,
        f"tools={narrative['tools'] if narrative else None}")

    # A2 -- the engine's guarantees were never needed: no corrective round, no thinking retry.
    labels = sorted({str(j.get("label") or "") for j in record["journal"]})
    triggers = sorted({str(j.get("trigger") or "") for j in record["journal"]})
    rec(f"{label}: no corrective round fired (the GM never flubbed)",
        not (set(labels) & {"narrative-fix", "scene-fix", "continue"})
        and not (set(triggers) & {"recovery", "thinking"}),
        f"labels={labels} triggers={triggers}")
    rec(f"{label}: the engine recorded the turn's prose and illustration",
        bool(flags.get("prose")) and (not images or bool(flags.get("scene")))
        and not flags.get("rejected"), str(flags))

    # A3 -- one image call per narrative turn, the GM's own (`story`), with what the generator
    # needs: a non-empty moment, the time of day and the weather.
    if images:
        scenes = [f for f in frames if f.get("type") == "scene_request"]
        rec(f"{label}: exactly one image call, attached to the prose", len(scenes) == 1,
            str([(s.get("kind"), s.get("place")) for s in scenes]))
        if scenes:
            s = scenes[0]
            rec(f"{label}: the image call is the GM's own, not the engine's fallback",
                s.get("kind") == "story", str(s.get("kind")))
            rec(f"{label}: the image call carries moment + time + weather",
                bool(str(s.get("description") or "").strip())
                and bool(str(s.get("time_of_day") or "").strip())
                and bool(str(s.get("weather") or "").strip()),
                f"desc={len(str(s.get('description') or ''))} chars "
                f"tod={s.get('time_of_day')!r} weather={s.get('weather')!r}")

    # A4 -- the markers: no drift, nothing in `thinking`, nothing left in the narrative, no
    # refusal, and no turn told twice.
    stored = [(str(m.get("content") or ""), str(m.get("thinking") or ""))
              for m in record["messages"] if m.get("role") == "assistant"]
    rec(f"{label}: no marker drift", not [c for c, t in stored if "{{_" in c or "{{_" in t], "")
    shaped = [m for c, _t in stored for m in markers_of(c)]
    rec(f"{label}: every marker in stored content is a canonical one",
        all(PAUSE_MARKER_RE.fullmatch(m) or RESUME_MARKER_RE.fullmatch(m)
            or REMEMBERS_RE.fullmatch(m) for m in shaped), str(shaped))
    # Soft, and measured: the default model reasons about the handshake in its `thinking` and
    # quotes the tokens there on nearly every turn (6/6 batch turns in the 2026-10-16 sample).
    # Invariant 3 forbids it, but the engine never reads `thinking` for the handshake, so a
    # hard failure would only make this file permanently red and hide the checks that matter.
    rec(f"{label}: no marker in the thinking channel",
        not [t for _c, t in stored if markers_of(t)], "", soft=True)
    rec(f"{label}: the narrative carries no marker",
        all(not markers_of(prose_of(rounds[i]["content"])) for i in prose_rounds), "")
    rec(f"{label}: no unoffered tool was refused",
        not [f for f in frames if f.get("type") == "tool_result"
             and "tool_not_available" in str(f.get("text") or "")], "")
    texts = [prose_of(rounds[i]["content"]) for i in prose_rounds]
    dupes = [(a[:30], b[:30]) for i, a in enumerate(texts) for b in texts[i + 1:]
             if min(len(a), len(b)) > 200
             and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8]
    rec(f"{label}: the turn was narrated once", not dupes, str(dupes))

    # A5 (soft) -- prose hygiene: no transposed mechanics, no third person.
    for text in texts:
        rec(f"{label}: no transposed mechanics in the prose",
            not MECHANICS_IN_PROSE.search(text),
            "; ".join(MECHANICS_IN_PROSE.findall(text))[:100], soft=True)
        rec(f"{label}: the prose is second person", not THIRD_PERSON.search(text),
            "; ".join(THIRD_PERSON.findall(text))[:80], soft=True)


def declared_place(record: dict) -> list[str]:
    """The turn's declared place path, from whichever tool this mode declares it with."""
    kind = "scene_request" if record.get("images") else "place_note"
    for f in record["frames"]:
        if f.get("type") == kind:
            return [str(p) for p in (f.get("place") or [])]
    return []


def check_place(record: dict, session, scenario: dict) -> None:
    label, expect = f"{record.get('mode', '')} {record['label']}".strip(), scenario["expect"]
    images = bool(record.get("images"))
    path = declared_place(record)
    if expect == "stay":
        if images:
            rec(f"{label}: the illustration stays where the story is",
                _scene_key(path) == _scene_key(COURT), str(path))
        else:
            rec(f"{label}: a turn that does not move declares nothing", not path, str(path))
        return
    if not path:
        rec(f"{label}: the movement was declared", False,
            f"no {'scene_request' if images else 'place_note'} this turn")
        return
    if expect == "new":
        if images:
            scene = next(f for f in record["frames"] if f.get("type") == "scene_request")
            rec(f"{label}: a place the registry does not hold gets its establishing view",
                bool(str(scene.get("establishing") or "").strip()), str(path))
        rec(f"{label}: the new place is filed under its area (the record decides)",
            str((session._current_place or {}).get("area") or "") == SETTLEMENT,
            str(session._current_place))
        return
    saved = [str(s) for s in expect]
    rec(f"{label}: the saved spelling is reused exactly",
        [s.strip().lower() for s in path] == [s.strip().lower() for s in saved],
        f"declared={path} saved={saved}")
    rec(f"{label}: the engine's idea of where the story is follows the move",
        _scene_key((session._current_place or {}).get("place") or []) == _scene_key(saved),
        str(session._current_place))
    if images:
        scene = next(f for f in record["frames"] if f.get("type") == "scene_request")
        # Not hard: the engine ignores `establishing` when the seed exists (the picture is
        # neither redrawn nor replaced), so sending it costs tokens and nothing else -- while
        # for a FILE-LESS place (a save played with images off) it is what supplies the seed's
        # description. Recorded, never failed.
        rec(f"{label}: a place the registry holds is not re-established",
            not str(scene.get("establishing") or "").strip(),
            f"establishing={str(scene.get('establishing') or '')[:60]!r}", soft=True)


def check_surface(mode: str, session, turns: list[dict]) -> None:
    """The tool surface itself: what this mode offers, and what it must never call."""
    images = mode == "images"
    names = {t["function"]["name"] for t in session.tools_schema}
    if images:
        rec("images on: request_scene_image + register_npcs offered, note_place hidden",
            SCENE_TOOL in names and "register_npcs" in names and PLACE_TOOL not in names,
            str(sorted(names)))
    else:
        rec("images off: note_place offered, the scene tools hidden",
            PLACE_TOOL in names and SCENE_TOOL not in names and "register_npcs" not in names,
            str(sorted(names)))
    called = [str(f.get("name")) for turn in turns for f in turn["frames"]
              if f.get("type") == "tool_call"]
    hidden = PLACE_TOOL if images else SCENE_TOOL
    rec(f"the hidden tool ({hidden}) is never called", hidden not in called, str(called))
    kinds = {f.get("type") for turn in turns for f in turn["frames"]}
    if images:
        rec("images mode emits no place_note", "place_note" not in kinds, str(sorted(kinds)))
    else:
        rec("text mode emits no scene_request", "scene_request" not in kinds,
            str(sorted(kinds)))


def check_persistence(session, turns: list[dict], ran_ids: list[str], images: bool) -> None:
    """The registry after the run: what reached it, through which writer."""
    before = known_scene_places(OUT, session.active_name, ERA)
    before_keys = {_scene_key(p["place"]) for p in before}
    if images:
        # No browser: nothing POSTs /api/scene, so nothing is recorded. This is the current
        # design (see the open decision in the wiki) -- pin it so changing it is deliberate.
        rec("images mode records no place without a browser (the current design)",
            {_scene_key(p["place"]) for p in before} == before_keys and len(before) == len(REGISTRY),
            f"{len(before)} entries")
        return
    from web.server import _persist_place_note

    if not [i for i in ran_ids if i != "stay"]:
        print("  [ info] no movement scenario ran: the place_note path is untested here",
              flush=True)
        return
    evts = [f for turn in turns for f in turn["frames"] if f.get("type") == "place_note"]
    rec("text mode declares a place through place_note", bool(evts), f"{len(evts)} events")
    if not evts:
        return
    for evt in evts:
        _persist_place_note(session, evt)
    after = known_scene_places(OUT, session.active_name, ERA)
    if "new" in ran_ids:
        fresh = [p for p in after if _scene_key(p["place"]) not in before_keys]
        rec("a move into an unrecorded place reaches the registry (the real server handler)",
            len(fresh) == 1 and bool(str(fresh[0].get("area") or "")),
            str([(p["area"], p["place"]) for p in fresh]))
    current = current_scene_place(OUT, session.active_name) or {}
    declared = [str(x) for x in evts[-1].get("place") or []]
    rec("the registry's current pointer follows the last move",
        _scene_key(current.get("place") or []) == _scene_key(declared), str(current))


def places_block(session) -> str:
    """The KNOWN PLACES system message as of the last model call (informational)."""
    for snap in reversed(session._gm_journal):
        for msg in reversed(snap.get("messages") or []):
            text = str(msg.get("content") or "")
            if msg.get("role") == "system" and "KNOWN PLACES" in text:
                return text
    return ""


# ── one mode ──────────────────────────────────────────────────────────────

async def run_mode(args, mode: str) -> list[dict]:
    stem = f"livegm_{mode}"
    images = mode == "images"
    print(f"\n=== mode {mode} -- {stem}.player, era {ERA}, images "
          f"{'ON (no generation)' if images else 'OFF'} ===", flush=True)
    player = build_fixture(stem, images)
    spec = resolve_model(args.model) or {}
    session = GameSession(REPO, args.model, provider=args.provider, scene_images=images,
                           temperature=args.temperature, dev=True,
                           context_window=int(spec.get("context") or DEFAULT_CONTEXT_WINDOW),
                           thinking_level=gemini_thinking_level(spec))
    drive = Drive(session, args.event_timeout, args.turn_timeout)
    started = time.monotonic()
    try:
        await session.start(str(player))
        ran = [s for s in SCENARIOS if not args.only or s["id"] in args.only]
        record = await drive.run_turn("awakening", None)
        if record is not None:
            record["images"] = images
            record["mode"] = mode
            check_turn(record)
        for scenario in ran:
            record = await drive.run_turn(scenario["id"], scenario["text"])
            if record is None:
                continue
            record["images"] = images
            record["mode"] = mode
            check_turn(record)
            check_place(record, session, scenario)
        check_surface(mode, session, drive.turns)
        check_persistence(session, drive.turns, [s["id"] for s in ran], images)
        marker = next((ln.strip() for ln in places_block(session).splitlines()
                       if "← you are here" in ln), "")
        print(f"  [ info] KNOWN PLACES now: {marker or '(no current place line)'}", flush=True)
    finally:
        with contextlib.suppress(Exception):
            await session.close()
        drop_fixture(stem)
    mins = (time.monotonic() - started) / 60
    print(f"  [ info] {len(drive.turns)} turns in {mins:.1f} min, "
          f"context {session.current_context_tokens} tokens, errors {drive.errors}", flush=True)
    for turn in drive.turns:
        print(f"  [shape] {turn['label']:<10} {shape_line(turn)}", flush=True)
    return drive.turns


# ── preconditions and artifacts ───────────────────────────────────────────

async def provider_ready(model: str) -> tuple[str, str]:
    spec = resolve_model(model)
    if spec is None:
        return "", f"unknown model {model!r} (known: {', '.join(m['id'] for m in MODELS)})"
    if spec.get("provider") == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            return "", "GEMINI_API_KEY is not set (the gemini provider needs it)"
        return "gemini", ""
    from ollama import AsyncClient
    try:
        await AsyncClient().list()
    except Exception as e:  # noqa: BLE001
        return "", f"the ollama daemon is unreachable ({type(e).__name__}: {e})"
    return "ollama", ""


def turn_summary(turn: dict) -> dict:
    """One turn as the artifact records it: the shape, the declaration, the journal skeleton.

    The full frames live in `frames.jsonl`; the prompt journal (whole messages per model call)
    is deliberately not written -- it is large and re-derivable by re-running."""
    rounds = rounds_of(turn["frames"])
    scene = next((f for f in turn["frames"] if f.get("type") == "scene_request"), None)
    note = next((f for f in turn["frames"] if f.get("type") == "place_note"), None)
    return {
        "mode": turn.get("mode"), "label": turn["label"], "action": turn["action"],
        "flags": turn.get("flags") or {},
        "rounds": [{"tools": r["tools"], "chars": len(prose_of(r["content"])),
                    "thinking_chars": len(r["thinking"]), "token": is_pause(r["content"])}
                   for r in rounds],
        "paused": sum(1 for f in turn["frames"] if f.get("type") == "paused"),
        "scene_request": None if scene is None else {
            k: scene.get(k) for k in ("kind", "kingdom", "area", "place", "time_of_day",
                                      "weather", "mood", "description")},
        "place_note": None if note is None else {
            k: note.get(k) for k in ("era", "kingdom", "area", "place", "main_npcs", "cast")},
        "journal": [{"label": j.get("label"), "trigger": j.get("trigger"), "round": j.get("round"),
                     "message_tokens": j.get("message_tokens"),
                     "tool_tokens": j.get("tool_tokens"),
                     "actual_tokens": j.get("actual_tokens")} for j in turn.get("journal", [])],
        "prose": [prose_of(r["content"]) for r in rounds if prose_of(r["content"])],
    }


def shape_line(turn: dict) -> str:
    """The one-line shape of a turn, for the console and the report."""
    rounds = rounds_of(turn["frames"])
    return " | ".join(
        f"{'+'.join(r['tools']) or ('pause' if is_pause(r['content']) else 'prose')}"
        f"({len(prose_of(r['content']))}c)" for r in rounds)


def save_artifacts(args, provider: str, modes: list[dict], results) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = OUT / "live_gm" / f"{stamp}_{args.mode}"
    out.mkdir(parents=True, exist_ok=True)
    payload = {"model": args.model, "provider": provider, "temperature": args.temperature,
               "only": args.only,
               "modes": [{"mode": m["mode"],
                          "turns": [turn_summary(t) for t in m.get("turns", [])]}
                         for m in modes],
               "checks": [{"name": n, "ok": ok, "soft": soft, "detail": d}
                          for n, ok, d, soft in results]}
    (out / "verdict.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    with (out / "frames.jsonl").open("w", encoding="utf-8") as fh:
        for mode in modes:
            for turn in mode.get("turns", []):
                for evt in turn["frames"]:
                    # Streamed deltas are noise: the finished text is in `assistant_end`.
                    if evt.get("type") in ("narrative_delta", "thinking_delta"):
                        continue
                    fh.write(json.dumps({"mode": mode["mode"], "turn": turn["label"],
                                         "evt": evt}, ensure_ascii=False) + "\n")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="live GM smoke (opt-in; not part of verify.py)")
    ap.add_argument("--mode", default="text", choices=("text", "images", "both"))
    ap.add_argument("--model", default="deepseek-v4.1-flash:cloud")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--only", default="",
                    help="comma-separated scenario ids: " + ", ".join(s["id"] for s in SCENARIOS))
    ap.add_argument("--event-timeout", type=float, default=300.0)
    ap.add_argument("--turn-timeout", type=float, default=600.0)
    args = ap.parse_args()
    args.only = [s for s in (args.only or "").split(",") if s]

    async def run() -> int:
        provider, why = await provider_ready(args.model)
        if not provider:
            print(f"SETUP: {why}", flush=True)
            return 2
        args.provider = provider
        no_generation()
        modes: list[dict] = []
        for mode in (("text", "images") if args.mode == "both" else (args.mode,)):
            turns = await run_mode(args, mode)
            modes.append({"mode": mode, "turns": turns})
        out = save_artifacts(args, provider, modes, RESULTS)
        hard = [(n, d) for n, ok, d, soft in RESULTS if not ok and not soft]
        soft = [(n, d) for n, ok, d, s in RESULTS if not ok and s]
        print("\n" + "=" * 72, flush=True)
        print(f"LIVE GM SMOKE -- {args.model} ({provider}), mode {args.mode}", flush=True)
        print(f"  checks: {len(RESULTS) - len(hard) - len(soft)}/{len(RESULTS)} pass, "
              f"{len(soft)} soft notes, {len(hard)} failed", flush=True)
        for name, detail in hard:
            print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""), flush=True)
        for name, detail in soft:
            print(f"  note  {name}" + (f"  ({detail})" if detail else ""), flush=True)
        print(f"  artifacts: {out}", flush=True)
        print(f"  RESULT: {'ALL HARD CHECKS PASS' if not hard else f'{len(hard)} FAILED'}",
              flush=True)
        return 0 if not hard else 1

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
