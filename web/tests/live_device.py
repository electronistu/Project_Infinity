"""Live Device landing smoke: does the GM know exactly where the Device put the Traveller?

Four release shapes are driven through the REAL session command queue (the same path the
browser's WebSocket uses -- so the ward survives too):

  * same age, exact place     x text / images
  * other age, exact place    x text / images
  * same age, ward only       x text

The assertion is the thing the player saw fail on 2026-10-18: the prompt must carry the WHOLE
chosen path and the landing must stay current after the turn. A sibling place the GM invents
is recorded but may not replace the landing.

Opt-in and deliberately NOT in the gate (`tools/verify.py` globs `test_*.py`). It needs the
real model and, for images mode, never generates: `SceneService.generate` is a raiser.

    venv/bin/python web/tests/live_device.py --mode both

Artifacts land under `output/live_device/<stamp>_<mode>/`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from forge.character_creator import create_debug_character  # noqa: E402
from forge.config_loader import load_config  # noqa: E402
from forge.formatter import get_player_json  # noqa: E402
from live_gm import Drive, drop_fixture, no_generation, provider_ready, seed_pictures  # noqa: E402
from web.engine import GameSession  # noqa: E402
from web.eras import era_reputation_seed  # noqa: E402
from web.models import gemini_thinking_level, resolve_model  # noqa: E402
from web.places import _scene_key, path_label  # noqa: E402
from web.protocol import DEFAULT_CONTEXT_WINDOW  # noqa: E402
from web.scenes import SceneService, commit_scene_manifest  # noqa: E402

OUT = REPO / "output"
TANG, TANG_KINGDOM, TANG_AREA = "tang", "the Tang empire", "Chang'an"
SAME = ["Chongren Ward", "the Jianfu pagoda", "the reliquary crypt"]
START = ["West Market", "the camel yard behind the silk bazaar"]
CROSS_ERA, CROSS_KINGDOM, CROSS_AREA = "wallachia", "Wallachia", "Târgoviște"
CROSS = ["the princely court", "the great hall"]

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""),
          flush=True)


def build_fixture(stem: str, images: bool) -> Path:
    """A Tang save with a chosen place in another ward and a Wallachia landing."""
    with contextlib.redirect_stdout(io.StringIO()):
        pc = create_debug_character(load_config())
    pc.mode = "time_traveler"
    player = OUT / f"{stem}.player"
    player.write_text(
        get_player_json(pc, era_reputation_seed(TANG), TANG, "West Market, in Chang'an",
                        mode="time_traveler"),
        encoding="utf-8")
    svc = SceneService(output_dir=OUT)
    svc.record_place(stem, era=TANG, kingdom=TANG_KINGDOM, area=TANG_AREA, place=SAME,
                     main_npcs=[{"name": "Warden Gao Lian", "role": "gate warden",
                                 "race": "human", "class": "fighter"}])
    svc.record_place(stem, era=CROSS_ERA, kingdom=CROSS_KINGDOM, area=CROSS_AREA,
                     place=CROSS, main_npcs=[])
    # LAST write sets the manifest's `current`: the save stands in the West Market.
    svc.record_place(stem, era=TANG, kingdom=TANG_KINGDOM, area=TANG_AREA, place=START,
                     main_npcs=[])
    if images:
        seed_pictures(stem)
    # `session.start()` drops the live manifest and reloads it from disk.
    commit_scene_manifest(OUT, stem)
    return player


async def run_device(drive: Drive, label: str, cmd: dict) -> dict:
    """Submit a real Device command through the session queue and collect until its cadence.

    A cross-age release is a closing turn AND a jump turn, so the runner cannot stop at the
    first `turn_end`; the command ends with the `cadence` event (`_jump`/`_move_within_era`).
    """
    session = drive.session
    deadline = time.monotonic() + drive.turn_timeout
    before_messages = len(session.messages)
    before_journal = len(session._gm_journal)
    frames: list[dict] = []
    await session.submit_device("release", cmd.get("era", ""), cmd.get("area", ""),
                                cmd.get("place", ""), cmd.get("group", ""))
    while True:
        evt = await drive._next(deadline)
        if evt is None:
            drive.errors.append(f"{label}: no busy frame")
            break
        if evt.get("type") == "ready":
            drive.ready = evt
        elif evt.get("type") == "busy" and evt.get("value") is True:
            break
        else:
            drive.session_frames.append(evt)
    while True:
        evt = await drive._next(deadline)
        if evt is None:
            drive.errors.append(f"{label}: no cadence within {drive.turn_timeout:.0f}s")
            break
        frames.append(evt)
        if evt.get("type") == "cadence":
            break
    record = {"label": label, "cmd": cmd, "frames": frames,
              "messages": session.messages[before_messages:],
              "journal": session._gm_journal[before_journal:]}
    drive.turns.append(record)
    return record


async def run_case(args, mode: str, tag: str, cmd: dict, expect: dict) -> dict:
    images = mode == "images"
    stem = f"livedev_{mode}_{tag}"
    print(f"\n=== {mode} / {tag} ==", flush=True)
    player = build_fixture(stem, images)
    spec = resolve_model(args.model) or {}
    session = GameSession(REPO, args.model, provider=args.provider, scene_images=images,
                          temperature=args.temperature, dev=True,
                          context_window=int(spec.get("context") or DEFAULT_CONTEXT_WINDOW),
                          thinking_level=gemini_thinking_level(spec))
    drive = Drive(session, args.event_timeout, args.turn_timeout)
    try:
        await session.start(str(player))
        await drive.run_turn("awakening", None)
        session.has_device = True
        before_journey = list(session.journey or [])
        before_legends = dict(session._legends)
        before_count = len(session.messages)
        record = await run_device(drive, tag, cmd)
        after_count = len(session.messages)
        # A cross-age jump COMPACTS, so the message slice from before the command no longer
        # exists. The jump prompt is the last user message in the post-command history; the
        # closing turn's frames and declarations sit before the jump turn's `busy true`.
        user_messages = [str(m.get("content") or "") for m in session.messages
                         if m.get("role") == "user"]
        # The last user message is not always the jump: the pause handshake appends
        # `{{resume}}`, and a corrective round appends the engine's own nudge. The turn's
        # prompt is the one carrying the situation marker.
        prompts = [c for c in user_messages
                   if "SITUATION: DEVICE MOVE" in c or "SITUATION: DEVICE JUMP" in c]
        prompt = prompts[-1] if prompts else (user_messages[-1] if user_messages else "")
        assistant = "\n".join(str(m.get("content") or "") for m in session.messages
                              if m.get("role") == "assistant")
        frames = record["frames"]
        last_busy = max((i for i, e in enumerate(frames)
                         if e.get("type") == "busy" and e.get("value") is True), default=0)
        jump_frames = frames[last_busy:]
        current = session._current_place or {}
        place_frames = [f.get("arguments", {}).get("place")
                        for f in jump_frames
                        if f.get("type") == "tool_call" and f.get("name") == "note_place"]
        scene_frames = [f.get("place") for f in jump_frames
                        if f.get("type") == "scene_request"]
        wanted = expect.get("place") or []
        wanted_key = _scene_key(wanted)
        name = f"{mode} {tag}"

        if wanted:
            full = path_label(wanted)
            rec(f"{name}: the prompt carries the WHOLE chosen path",
                f"You arrive at {full}, in {expect['area']}." in prompt,
                prompt[-260:])
            rec(f"{name}: the prompt fixes the landing",
                "The Device has put the Traveller exactly here" in prompt)
            rec(f"{name}: the chosen place is current after the turn",
                _scene_key(current.get("place")) == wanted_key
                and current.get("area") == expect["area"], str(current))
            invented = [p for p in place_frames if _scene_key(p) != wanted_key]
            rec(f"{name}: the GM's own declarations reuse the landing",
                not invented, str(invented))
        else:
            rec(f"{name}: the prompt names the chosen ward",
                f"You arrive in {expect['group']}, in {expect['area']}; "
                "choose the exact place inside it." in prompt, prompt[-260:])
            current_key = _scene_key(current.get("place"))
            rec(f"{name}: the ward survives the real queue (area + a place inside it)",
                current.get("area") == expect["area"]
                and (not current_key
                     or current_key[:1] == _scene_key([expect["group"]])), str(current))

        if images:
            rec(f"{name}: the illustration is the landing",
                len(scene_frames) == 1 and _scene_key(scene_frames[0]) == wanted_key,
                str(scene_frames))
        else:
            rec(f"{name}: no scene call in text mode", not scene_frames, str(scene_frames))

        if expect["cross"]:
            rec(f"{name}: the age changed",
                session.era == expect["era"] and session.journey[-1] == expect["era"],
                f"{session.era} {session.journey[-3:]}")
            rec(f"{name}: the jump carried its scaffold",
                "THE AGE YOU HAVE ENTERED" in prompt and "SITUATION: DEVICE JUMP" in prompt)
            rec(f"{name}: the compacted prefix names the new age",
                session._era_index_at is not None and session._era_index_at < len(session.messages)
                and "Wallachia" in str(session.messages[session._era_index_at].get("content")),
                str(session.messages[session._era_index_at].get("content"))[:80])
            rec(f"{name}: the same-age move prompt is absent",
                "SITUATION: DEVICE MOVE" not in prompt)
        else:
            rec(f"{name}: the age did not change",
                session.era == expect["era"] and session.journey == before_journey,
                f"{session.era} {session.journey}")
            rec(f"{name}: the history and the age memory continue",
                after_count > before_count and session._legends == before_legends,
                f"{before_count} -> {after_count}")
            rec(f"{name}: the move prompt says it is the same story",
                "SITUATION: DEVICE MOVE" in prompt
                and "This is not an age crossing" in prompt)

        leaf = path_label(wanted).split(" — ")[-1] if wanted else expect["group"]
        print(f"  [info] {name}: prose mentions {leaf!r}: "
              f"{leaf.lower() in assistant.lower()}", flush=True)
        return {
            "mode": mode, "tag": tag, "cmd": cmd,
            "messages_before": before_count, "messages_after": after_count,
            "records": [{
                "frames": [f for f in frames
                           if f.get("type") not in ("narrative_delta", "thinking_delta")],
                "jump_frames": [f for f in jump_frames
                                if f.get("type") not in ("narrative_delta", "thinking_delta")],
                "messages": session.messages, "journal": record["journal"]}],
            "current": current, "prompt": prompt, "prose": assistant,
        }
    finally:
        with contextlib.suppress(Exception):
            await session.close()
        drop_fixture(stem)


async def run_matrix(args) -> list[dict]:
    modes = ("text", "images") if args.mode == "both" else (args.mode,)
    only = {p.strip() for p in str(args.only or "").split(",") if p.strip()}
    cases = []
    for mode in modes:
        if not only or "same" in only:
            cases.append(await run_case(
                args, mode, "same", {"era": TANG, "area": TANG_AREA,
                                     "place": path_label(SAME)},
                {"place": SAME, "area": TANG_AREA, "era": TANG, "cross": False}))
        if not only or "cross" in only:
            cases.append(await run_case(
                args, mode, "cross", {"era": CROSS_ERA, "area": CROSS_AREA,
                                      "place": path_label(CROSS)},
                {"place": CROSS, "area": CROSS_AREA, "era": CROSS_ERA, "cross": True}))
    if not only or "ward" in only:
        cases.append(await run_case(
            args, "text", "ward", {"era": TANG, "area": TANG_AREA,
                                   "group": "Chongren Ward"},
            {"group": "Chongren Ward", "area": TANG_AREA, "era": TANG, "cross": False}))
    return cases


def save_artifacts(cases: list[dict], stamp: str) -> Path:
    out = OUT / "live_device" / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "verdict.json").write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    return out


async def run() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-v4.1-flash:cloud")
    ap.add_argument("--provider", default="")
    ap.add_argument("--mode", default="both", choices=("text", "images", "both"))
    ap.add_argument("--only", default="", help="same,cross,ward (comma-separated)")
    ap.add_argument("--event-timeout", type=float, default=60.0)
    ap.add_argument("--turn-timeout", type=float, default=420.0)
    ap.add_argument("--temperature", type=float, default=1.0)
    args = ap.parse_args()
    provider, why = await provider_ready(args.model)
    if not provider:
        print(f"PRECONDITION FAILED: {why}")
        return 2
    no_generation()
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"_{args.mode}"
    cases = await run_matrix(args)
    out = save_artifacts(cases, stamp)
    failed = [n for n, ok, _d in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f"LIVE DEVICE SMOKE -- {args.model} ({provider}), {args.mode}")
    print(f"  checks: {len(RESULTS) - len(failed)}/{len(RESULTS)} pass, {len(failed)} failed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL  {name}  ({detail})")
    print(f"  artifacts: {out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
