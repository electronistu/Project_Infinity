"""Historical eras.

One file per era under ``config/eras/``. Each has a ``meta:`` header -- the only
part that ever reaches the GM *before* the era is entered, and the source of the
always-on ERA INDEX -- and a ``body:`` (the scaffold, fetched once per era).

Wired into the engine: ``render_era_index(current)`` is injected as the always-on
system message after the protocol, and the awakening receives ``render_era_text(current)``
as the ERA_FILE. There is no single world file; a new ``.player``
carries its ``era`` and a reputation map from ``era_reputation_seed``. ``lookup``
fetches any other era, and ``era_kingdoms()`` / ``era_faction_keys()`` back the
era-scoped reputation map.

Budget (asserted by ``tools/budgets.yml`` via ``tools/verify.py``):

  - one era file    <= 5,120 bytes
  - one scaffold    <=   450 tokens
  - the ERA INDEX   <=   150 tokens
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from forge.models import Guild, Kingdom

ERAS_DIR = Path(__file__).resolve().parent.parent / "config" / "eras"

# The fallback era when a `.player` names none. A save always carries its own `era` -- the
# first one is rolled at creation (`pick_start_era`) and every jump rolls the next -- so this
# is only what an era-game save that cannot say where it is falls back to.
START_ERA = "egypt"

# `arrivals` is required (and >= 2) only for a playable era; the frame has nowhere to enter.
# `order` is validated separately -- it is the one non-string required field.
REQUIRED_META = ("id", "name", "when", "character")


def era_files() -> list[Path]:
    """Every era file, by filename. A non-existent directory is not an error."""
    if not ERAS_DIR.is_dir():
        return []
    return sorted(ERAS_DIR.glob("*.yml"))


def _read(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is required to read config/eras/")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    meta = dict(data.get("meta") or {})
    body = dict(data.get("body") or {})
    if not meta.get("id"):
        meta["id"] = path.stem
    meta.setdefault("playable", False)
    return {"meta": meta, "body": body}


def load_era(era_id: str) -> dict:
    """One era by id -> ``{"meta": {...}, "body": {...}}``."""
    path = ERAS_DIR / f"{era_id}.yml"
    if not path.exists():
        raise FileNotFoundError(f"unknown era: {era_id!r} ({path})")
    return _read(path)


def load_all() -> list[dict]:
    return [_read(p) for p in era_files()]


def list_era_meta() -> list[dict]:
    """Every era's ``meta`` only, ordered -- the ERA INDEX source. No bodies."""
    metas = [dict(e["meta"]) for e in load_all()]
    metas.sort(key=lambda m: (m.get("order") if isinstance(m.get("order"), int) else 99,
                              str(m.get("id") or "")))
    return metas


def era_ids() -> list[str]:
    return [str(m.get("id") or "") for m in list_era_meta()]


def playable_eras() -> list[str]:
    return [str(m.get("id") or "") for m in list_era_meta() if m.get("playable")]


def pick_start_era(rng=None) -> str:
    """The era the Device throws a brand-new Traveler into: any playable era, at random.

    The first age used to be fixed at Egypt. It is drawn like every later one now -- the
    Compass Rose was missing from the start -- so no era is a scripted tutorial and a new
    character can open anywhere on the ladder. `rng` is injectable so tests are deterministic.
    """
    options = playable_eras()
    return (rng or random).choice(options) if options else START_ERA


def render_era_index(current: Optional[str] = None) -> str:
    """The always-on ERA INDEX: id and a one-line character. Nothing else.

    The arrival point is deliberately absent: it is rolled at random (the Device
    has no Compass Rose), so it is a property of the save, not of the era.
    """
    lines = [f"ERAS{' (current: ' + current + ')' if current else ''}:"]
    for m in list_era_meta():
        lines.append(f"- {m.get('id')} -- {m.get('character', '')}")
    return "\n".join(lines)


def era_arrivals(era_id: str) -> list[str]:
    """The era's possible arrival points (short, real places)."""
    meta = load_era(era_id)["meta"]
    return [str(a).strip() for a in (meta.get("arrivals") or []) if str(a or "").strip()]


def pick_arrival(era_id: str, rng=None) -> str:
    """One arrival point, drawn at random -- the Device cannot aim.

    `rng` is injectable so tests are deterministic.
    """
    options = era_arrivals(era_id)
    if not options:
        return ""
    return (rng or random).choice(options)


def _polity_lines(p: dict) -> list[str]:
    """One polity as YAML-ish lines -- shared by the scaffold renderer."""
    lines = [f"  - name: {p.get('name', '')}"]
    if p.get("ruler"):
        lines.append(f"    ruler: {p['ruler']}")
    lines.append(f"    capital: {p.get('capital', '')}")
    lines.append(f"    relations: {json.dumps(p.get('relations') or {}, ensure_ascii=False)}")
    names = [str(f.get("name") or "") for f in (p.get("factions") or []) if f.get("name")]
    if names:
        lines.append("    factions: " + " \u00b7 ".join(names))
    return lines


def render_era_text(era_id: str) -> str:
    """The scaffold the GM reads once the era is entered.

    Deliberately the same YAML-ish shape as the world scaffold it replaced, so the
    swap is a drop-in -- but leaner: relations on one line, factions as a
    single dot-joined line rather than one nested block per faction.
    """
    body = load_era(era_id)["body"]
    lines = ["history:"]
    for entry in body.get("history") or []:
        lines.append(f"  - {entry}")
    lines.append("polities:")
    for p in body.get("polities") or []:
        lines.extend(_polity_lines(p))
    hooks = body.get("visual_hooks") or []
    if hooks:
        lines.append("visual_hooks:")
        lines.extend(f"  - {h}" for h in hooks)
    return "\n".join(lines)


def render_polity(p: dict) -> str:
    """One polity on its own (what a polity-scoped lookup returns)."""
    return "\n".join(["polities:", *_polity_lines(p)])


def lookup(query: str, current: str = "") -> str:
    """Resolve a `lookup` query to era text -- the whole brain of the `lookup` tool.

    Two forms are the contract, kept deliberately small so the tool's schema stays
    inside its budget:

      - ``"here"`` (or empty) -> the current era's scaffold;
      - an era id             -> that era's scaffold.

    Anything else is matched best-effort: a polity in the current era, then an era
    by name. A miss lists the known era ids rather than failing.
    """
    q = " ".join(str(query or "").split()).strip().lower()
    current = str(current or "").strip().lower()
    known = era_ids()
    if not current:
        current = START_ERA
    if not q or q in ("here", "current", "this era", "now"):
        if current in known:
            return render_era_text(current)
        return f"unknown current era {current!r}. Known eras: {', '.join(known)}"
    if q in known:
        return render_era_text(q)
    if current in known:
        for polity in load_era(current)["body"].get("polities") or []:
            if q in str(polity.get("name") or "").lower():
                return render_polity(polity)
    for meta in list_era_meta():
        if q in str(meta.get("name") or "").lower() or q in str(meta.get("id") or ""):
            return render_era_text(str(meta["id"]))
    return (f"no match for {q!r}. Known eras: {', '.join(known)}. "
            "Try 'here' for the era you are in.")


def era_kingdoms(era_id: str) -> list[Kingdom]:
    """Kingdom/Guild objects for one era -- the reputation-map hook."""
    body = load_era(era_id)["body"]
    out: list[Kingdom] = []
    for p in body.get("polities") or []:
        guilds = [Guild(name=str(f.get("name") or ""), reports_to=None)
                  for f in (p.get("factions") or []) if f.get("name")]
        out.append(Kingdom(
            name=str(p.get("name") or ""),
            capital=str(p.get("capital") or ""),
            alignment="",
            relations=dict(p.get("relations") or {}),
            guilds=guilds,
        ))
    return out


def era_faction_keys(era_id: str) -> dict[str, list[str]]:
    """``{polity: [faction key]}`` -- stable keys for the era-scoped reputation map."""
    body = load_era(era_id)["body"]
    out: dict[str, list[str]] = {}
    for p in body.get("polities") or []:
        keys = [str(f.get("key") or "") for f in (p.get("factions") or []) if f.get("key")]
        if keys:
            out[str(p.get("name") or "")] = keys
    return out


def era_reputation_seed(era_id: str) -> dict:
    """The reputation map a new `.player` is seeded with, **scoped to the era**.

    `{era: {polity: {faction: []}}}`, plus a `ruler` bucket per polity and a per-era
    `others` -- the same shape the engine writes, so `update_player_list` and
    `_resolve_reputation_bucket` are untouched by the scoping. Standing earned in one era
    is neither lost nor offered in another; the GM's write path stays short because the
    engine resolves the era.
    """
    seed: dict = {}
    for p in load_era(era_id)["body"].get("polities") or []:
        name = str(p.get("name") or "").strip().lower()
        if not name:
            continue
        bucket: dict = {"ruler": []}
        for f in p.get("factions") or []:
            key = str(f.get("key") or "").strip()
            if key:
                bucket[key] = []
        seed[name] = bucket
    seed["others"] = {}
    return {era_id: seed}


def validate_era(era: dict, stem: Optional[str] = None) -> list[str]:
    """Format problems in one era, as human-readable strings (``[]`` is valid).

    The format is data, so it is checked in code rather than trusted: an
    unquoted comma inside a YAML flow mapping silently turns a relation value
    into a second key, and a scaffold that is not lean is a budget breach.
    """
    problems: list[str] = []
    meta = era.get("meta") or {}
    body = era.get("body") or {}

    for key in REQUIRED_META:
        if not (isinstance(meta.get(key), str) and meta[key].strip()):
            problems.append(f"meta.{key} must be a non-empty string")
    if not isinstance(meta.get("order"), int) or isinstance(meta.get("order"), bool):
        problems.append("meta.order must be an integer")
    if not isinstance(meta.get("playable"), bool):
        problems.append("meta.playable must be a boolean")
    arrivals = meta.get("arrivals") or []
    if not isinstance(arrivals, list) or not all(isinstance(a, str) and a.strip() for a in arrivals):
        problems.append("meta.arrivals must be a list of non-empty strings")
    else:
        # The chosen arrival is injected verbatim into the prompt and the fallback legend, so
        # keep it a short phrase -- a sentence-length entry would cost tokens and read badly.
        for entry in arrivals:
            text = str(entry).strip()
            if not (3 <= len(text) <= 60):
                problems.append(f"meta.arrivals entries must be 3-60 characters: {text!r}")
        if meta.get("playable") and len(arrivals) < 2:
            problems.append("meta.arrivals needs at least 2 entries when playable")
    part = meta.get("part")
    if part is not None and not (isinstance(part, str) and part.strip()):
        problems.append("meta.part must be a string or null")
    legend = meta.get("legend")
    if legend is not None and not (isinstance(legend, str) and legend.strip()):
        problems.append("meta.legend must be a non-empty string when present")
    if stem and meta.get("id") != stem:
        problems.append(f"meta.id {meta.get('id')!r} does not match the filename {stem!r}")

    history = body.get("history") or []
    if not isinstance(history, list) or not all(isinstance(h, str) and h.strip() for h in history):
        problems.append("body.history must be a list of non-empty strings")
    for i, p in enumerate(body.get("polities") or []):
        where = f"body.polities[{i}]"
        if not (isinstance(p.get("name"), str) and p["name"].strip()):
            problems.append(f"{where}.name must be a non-empty string")
        if not (isinstance(p.get("capital"), str) and p["capital"].strip()):
            problems.append(f"{where}.capital must be a non-empty string")
        relations = p.get("relations")
        if not isinstance(relations, dict):
            problems.append(f"{where}.relations must be a mapping")
        else:
            for rk, rv in relations.items():
                if not (isinstance(rk, str) and rk.strip()):
                    problems.append(f"{where}.relations has a non-string key")
                if not (isinstance(rv, str) and rv.strip()):
                    problems.append(f"{where}.relations[{rk!r}] must be a non-empty string")
        for j, f in enumerate(p.get("factions") or []):
            if not (isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"].strip()):
                problems.append(f"{where}.factions[{j}].name must be a non-empty string")
            if not (isinstance(f, dict) and isinstance(f.get("key"), str) and f["key"].strip()):
                problems.append(f"{where}.factions[{j}].key must be a non-empty string")
    return problems
