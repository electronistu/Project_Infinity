"""Capture a pre-compression baseline for the GM, so a later behaviour change
can be attributed to a specific compression pass.

Two independent baselines, both written under ``output/gm_baseline/``:

  input     Deterministic. Snapshots exactly what the GM is sent -- the rendered
            protocol (scenes on/off) and the full MCP tool listing (descriptions
            + JSON schemas) -- with sha256 and token counts in a manifest.
            Reproducible; no model needed.

  behavior  Opt-in. Replays ``scenarios.json`` against a live GM model through the
            real ``web.engine.GameSession`` (same protocol, same MCP server) and
            records the tool-call sequence, pauses, narrative size and the
            provider's own prompt-token count for each scenario. Needs a model
            (Ollama Cloud by default) and takes minutes.

Usage (from the repo root, with the venv python):

    venv\\Scripts\\python.exe tools\\gm_snapshot.py input
    venv\\Scripts\\python.exe tools\\gm_snapshot.py behavior --model deepseek-v4.1-flash:cloud
    venv\\Scripts\\python.exe tools\\gm_snapshot.py behavior --only check_arcane,save_dodge
    venv\\Scripts\\python.exe tools\\gm_snapshot.py compare <base.json> <new.json>

The behaviour run copies the player fixture to a temp file, so the real save is
never touched. It never calls save, so no .player/.timeline is written.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import token_budget as tb  # noqa: E402
from web.tool_schema import compact_schema  # noqa: E402

BASELINE = REPO.parent / "gm_baseline"  # kept one level up (outside the repo)
INPUT_DIR = BASELINE / "input"
BEHAVIOR_DIR = BASELINE / "behavior"
SCENARIOS = BASELINE / "scenarios.json"
LEDGER = REPO.parent / "TRANSIENT_rules_ledger.md"  # kept one level up (outside the repo)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


# ── input baseline ────────────────────────────────────────────────────────

def cmd_input(args) -> int:
    out = Path(args.out) if args.out else INPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    label, count = tb._counter()

    proto_text = (REPO / "GameMaster_MCP.md").read_text(encoding="utf-8")
    proto = {
        "on": tb._render_protocol(proto_text, True),
        "off": tb._render_protocol(proto_text, False),
    }
    for bucket, text in proto.items():
        (out / f"protocol_scenes_{bucket}.md").write_text(text, encoding="utf-8")

    tools_doc = []
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "git_head": _git_head(), "tokenizer": label, "schema_compaction": True,
                "protocol": {}, "tools": {}}
    tool_tokens = 0
    for name, desc, schema in tb.tool_listing():
        schema = compact_schema(schema)   # the GM is sent the compacted schema
        schema_text = json.dumps(schema, ensure_ascii=True, sort_keys=True)
        tools_doc.append({"name": name, "description": desc, "inputSchema": schema})
        dt, st = count(desc), count(schema_text)
        tool_tokens += dt + st
        manifest["tools"][name] = {
            "params": len((schema or {}).get("properties", {}) or {}),
            "desc_chars": len(desc), "desc_tokens": dt,
            "schema_chars": len(schema_text), "schema_tokens": st,
            "total_tokens": dt + st, "sha256": _sha(desc + "\n" + schema_text),
        }
    (out / "tools.json").write_text(
        json.dumps(tools_doc, ensure_ascii=True, indent=2), encoding="utf-8")

    for bucket, text in proto.items():
        manifest["protocol"][bucket] = {
            "chars": len(text), "tokens": count(text), "sha256": _sha(text)}
    manifest["ledger_sha256"] = _sha(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else ""
    manifest["tool_tokens_total"] = tool_tokens
    manifest["static_prefix_tokens"] = {
        bucket: tool_tokens + count(proto[bucket]) for bucket in proto}
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8")

    print(f"input snapshot -> {out}")
    print(f"  tools: {len(tools_doc)}  tool tokens: {tool_tokens}")
    print(f"  static prefix tokens: scenes ON={manifest['static_prefix_tokens']['on']} "
          f"OFF={manifest['static_prefix_tokens']['off']}")
    return 0


# ── behavior baseline ─────────────────────────────────────────────────────

def _load_scenarios(path: Path, only: str | None):
    data = json.loads(path.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios", [])
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        scenarios = [s for s in scenarios if s.get("id") in wanted]
    return data, scenarios


def _resolve_player(args, data) -> Path:
    scen_path = Path(args.scenarios)
    src = Path(args.player or data.get("player") or "electronistu.player")
    if not src.is_absolute():
        for candidate in (REPO / src, scen_path.parent / src, REPO / "output" / src):
            if candidate.exists():
                src = candidate
                break
        else:
            src = REPO / src
    if not src.exists():
        raise SystemExit(f"player fixture not found: {src}")
    return src


def _base_result(args, provider, src) -> dict:
    return {
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "model": args.model,
        "provider": provider,
        "isolated": bool(getattr(args, "isolated", False)),
        "scene_images": bool(args.scenes),
        "temperature": args.temperature,
        "player_fixture": str(src.relative_to(REPO)) if src.is_relative_to(REPO) else str(src),
        "protocol_sha256": _sha((REPO / "GameMaster_MCP.md").read_text(encoding="utf-8")),
        "awakening": {"tool_calls": [], "narrative_chars": 0, "prompt_tokens": None},
        "scenarios": [],
        "pauses": [],
        "errors": [],
    }


async def _drive_session(player_path, scenarios, args, provider):
    """Run ONE live session: its awakening, then each scenario in turn.

    Returns (awakening, entries, pauses, errors). Every scenario is submitted
    only after the session reaches ``awakening_end``; the engine auto-resumes
    pause tokens, so a scenario's tools include its full mechanical loop.
    """
    from web.engine import GameSession

    session = GameSession(REPO, args.model, provider=provider, scene_images=args.scenes,
                          temperature=args.temperature, verbose=False, debug=False)
    awakening = {"tool_calls": [], "narrative_chars": 0, "prompt_tokens": None}
    entries: list[dict] = []
    pauses: list[str] = []
    errors: list[dict] = []
    loop = asyncio.get_event_loop()
    deadline = None
    current = awakening
    phase = "awakening"
    pending = list(scenarios)
    idx = -1

    await session.start(str(player_path))
    gen = session.events()

    async def next_event():
        nonlocal deadline
        if deadline is None:
            return await asyncio.wait_for(gen.__anext__(), timeout=args.event_timeout)
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(gen.__anext__(), timeout=remaining)

    try:
        while True:
            try:
                evt = await next_event()
            except asyncio.TimeoutError:
                errors.append({"phase": phase, "error": "timeout"})
                break
            except StopAsyncIteration:
                break
            etype = evt.get("type")
            if etype == "tool_call":
                current["tool_calls"].append({"name": evt.get("name"),
                                              "arguments": evt.get("arguments", {})})
            elif etype == "narrative_delta":
                current["narrative_chars"] += len(evt.get("text", ""))
            elif etype == "context":
                current["prompt_tokens"] = evt.get("tokens")
            elif etype == "paused":
                pauses.append(phase)
            elif etype == "error":
                errors.append({"phase": phase, "message": evt.get("message")})
            elif etype == "fatal":
                errors.append({"phase": phase, "fatal": evt.get("message")})
                break
            elif etype == "awakening_end":
                idx = 0
                if not pending:
                    break
                current = _start_scenario(entries, pending[idx])
                phase = pending[idx]["id"]
                deadline = loop.time() + args.turn_timeout
                await session.submit(pending[idx]["text"])
            elif etype == "turn_end" and idx >= 0:
                idx += 1
                if idx >= len(pending):
                    break
                current = _start_scenario(entries, pending[idx])
                phase = pending[idx]["id"]
                deadline = loop.time() + args.turn_timeout
                await session.submit(pending[idx]["text"])
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
    return awakening, entries, pauses, errors


async def _run_behavior(args) -> dict:
    from web.models import resolve_model

    data, scenarios = _load_scenarios(Path(args.scenarios), args.only)
    src = _resolve_player(args, data)
    provider = (resolve_model(args.model) or {}).get("provider", "ollama")
    result = _base_result(args, provider, src)

    if args.isolated:
        # One fresh session (and fresh player copy) per scenario: no narrative
        # cascade, so a diff isolates the scenario instead of the whole run.
        for scenario in scenarios:
            tmp = Path(tempfile.mkdtemp(prefix="gm_behavior_"))
            try:
                player = tmp / "snapshot.player"
                shutil.copy(src, player)
                awakening, entries, pauses, errors = await _drive_session(
                    player, [scenario], args, provider)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if not result["awakening"]["tool_calls"]:
                result["awakening"] = awakening
            if entries:
                entries[0]["awakening_tools"] = [c["name"] for c in awakening["tool_calls"]]
                entries[0]["session_pauses"] = list(pauses)
            result["scenarios"].extend(entries)
            result["pauses"].extend(pauses)
            result["errors"].extend(errors)
    else:
        tmp = Path(tempfile.mkdtemp(prefix="gm_behavior_"))
        try:
            player = tmp / "snapshot.player"
            shutil.copy(src, player)
            result["awakening"], entries, pauses, errors = await _drive_session(
                player, scenarios, args, provider)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        result["scenarios"] = entries
        result["pauses"] = pauses
        result["errors"] = errors
    return result


def _start_scenario(target: list, scenario: dict) -> dict:
    entry = {"id": scenario.get("id"), "probes": scenario.get("probes", []),
             "text": scenario.get("text", ""), "tool_calls": [],
             "narrative_chars": 0, "prompt_tokens": None}
    target.append(entry)
    return entry


def cmd_behavior(args) -> int:
    BEHAVIOR_DIR.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(_run_behavior(args))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_model = args.model.replace(":", "_").replace("/", "_")
    out = Path(args.out) if args.out else BEHAVIOR_DIR / f"{safe_model}_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"behavior snapshot -> {out}")
    print(f"  mode: {'isolated (fresh session per scenario)' if result.get('isolated') else 'shared session'}")
    print(f"  awakening: {[c['name'] for c in result['awakening']['tool_calls']]}")
    for s in result["scenarios"]:
        extra = f"  aw={s['awakening_tools']}" if 'awakening_tools' in s else ""
        print(f"  {s['id']:14s} probes={','.join(s['probes']) or '-':12s} "
              f"tools={[c['name'] for c in s['tool_calls']]}{extra}")
    if result["pauses"]:
        print(f"  pauses: {result['pauses']}")
    if result["errors"]:
        print(f"  errors: {result['errors']}")
    return 0


# ── compare two behavior snapshots ────────────────────────────────────────

def cmd_compare(args) -> int:
    a = json.loads(Path(args.base).read_text(encoding="utf-8"))
    b = json.loads(Path(args.new).read_text(encoding="utf-8"))
    print(f"base: {args.base}  ({a.get('model')})")
    print(f"new : {args.new}  ({b.get('model')})\n")

    def names(snap, sid):
        for s in snap.get("scenarios", []):
            if s.get("id") == sid:
                return [c["name"] for c in s.get("tool_calls", [])]
        return None

    ids = [s.get("id") for s in a.get("scenarios", [])]
    changed = 0
    for sid in ids:
        na, nb = names(a, sid), names(b, sid)
        if nb is None:
            print(f"  {sid:14s} MISSING in new")
            changed += 1
            continue
        marker = "==" if na == nb else "!="
        if na != nb:
            changed += 1
        print(f"  {sid:14s} {marker}  base={na}")
        if na != nb:
            print(f"  {'':14s}     new ={nb}")
    print(f"\n{changed}/{len(ids)} scenarios changed tool-call sequence")
    if a.get("protocol_sha256") != b.get("protocol_sha256"):
        print("protocol changed between snapshots")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="GM baseline snapshots")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_in = sub.add_parser("input", help="deterministic static-prefix snapshot")
    p_in.add_argument("--out", default=None)
    p_in.set_defaults(func=cmd_input)

    p_be = sub.add_parser("behavior", help="replay scenarios against a live GM model")
    p_be.add_argument("--model", default="deepseek-v4.1-flash:cloud")
    p_be.add_argument("--player", default=None, help="override the scenarios.json player fixture")
    p_be.add_argument("--scenarios", default=str(SCENARIOS))
    p_be.add_argument("--only", default=None, help="comma-separated scenario ids")
    p_be.add_argument("--isolated", action="store_true",
                      help="run each scenario in a fresh session (one awakening each; no narrative cascade)")
    p_be.add_argument("--scenes", action="store_true", help="enable storyline imagery protocol")
    p_be.add_argument("--temperature", type=float, default=1.0)
    p_be.add_argument("--turn-timeout", type=float, default=600.0,
                      help="seconds allowed per turn before aborting the run")
    p_be.add_argument("--event-timeout", type=float, default=600.0,
                      help="seconds allowed with no event before aborting")
    p_be.add_argument("--out", default=None)
    p_be.set_defaults(func=cmd_behavior)

    p_cmp = sub.add_parser("compare", help="diff two behavior snapshots")
    p_cmp.add_argument("base")
    p_cmp.add_argument("new")
    p_cmp.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
