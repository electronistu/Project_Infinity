"""The scene side: the place/NPC registry and the scene image service.

`SceneService` draws a hidden establishing **seed** per place plus a visible one-shot
**action**. Its companion registry (known places -- with or without a picture -- and their
declared NPCs) lives in memory until the player saves (`commit_scene_manifest`) or the
session ends (`discard_scene_manifest`). Shared image plumbing -- the image backends, config
loading and the prompt helpers -- lives in `web.backends` / `web.images`; this module imports
them, never the reverse.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

from .backends import ImageBackend, ImageBackendHolder, _backend_from_config
from .images import (
    _DEFAULT_MODEL,
    _STYLE_GUARD,
    _appearance_effects_config,
    _gear_line,
    _int_or,
    _load_config,
    _thinking_level,
    appearance_override,
)
from .imageassets import (
    downscale_image,
    drop_stale,
    first_existing,
    image_mime,
    read_json_dict,
    slugify,
    sniff_image,
    write_bytes_atomic,
    write_json_atomic,
)
# Character identity is the same key everywhere (`Master_Guo` = `Master Guo`): the roster
# merge must not file one person twice because of a separator.
from .places import _npc_state, name_key


_PROTAGONIST_LINE = "The protagonist is a {race} {class}. {gear}"
_SCENE_APPEARANCE_LINE = (
    "The protagonist's appearance is currently: {appearance} Draw exactly that appearance; the "
    "protagonist's usual face, hair, build and clothing are hidden by the illusion."
)
_SCENE_APPEARANCE_GEAR = (
    "The illusion covers clothing, armour and weapons: depict only the garments and gear described "
    "in that appearance, never the protagonist's usual equipment."
)
_SCENE_PORTRAIT_LINE = (
    "The attached portrait is the protagonist — keep the same face, hair colour and build; place "
    "that character in the described action and pose."
)
_SCENE_PROTAGONIST_GUARD = (
    "The protagonist's appearance is fixed by the attached portrait — do not restate or "
    "alter their face, hair, build or clothing from the text; depict the scene around them. "
    "In the scene the protagonist does not look at or address the camera — turn them into the "
    "action despite the portrait's frontal pose: they face whatever they are doing, not the lens, "
    "and the camera may see their back. Never rotate the protagonist toward the camera to match "
    "the portrait."
)


def _npc_fields(value) -> dict:
    """Normalize a declared NPC to {name, description, role, race, class, state}. A legacy
    plain string is a description. `race`/`class` are required of the GM but default to ''."""
    if isinstance(value, dict):
        return {"name": re.sub(r"\s+", " ", str(value.get("name") or "")).strip(),
                "description": re.sub(r"\s+", " ", str(value.get("description") or "")).strip(),
                "role": re.sub(r"\s+", " ", str(value.get("role") or "")).strip(),
                "race": re.sub(r"\s+", " ", str(value.get("race") or "")).strip(),
                "class": re.sub(r"\s+", " ", str(value.get("class") or "")).strip(),
                "state": _npc_state(value.get("state"))}
    return {"name": "", "description": re.sub(r"\s+", " ", str(value or "")).strip(),
            "role": "", "race": "", "class": "", "state": ""}


def _merge_npc_lists(recorded, declared) -> list[dict]:
    """A place's roster after a declaration: declared names add or update, the rest STAY.

    A declaration is a DELTA. Live 2026-10-18 a re-declaration carrying only the regular it
    had just met wiped the other three: the roster must never be lost by omission, which is
    also what lets a one-entry `{"name": ..., "state": "petrified"}` be the whole update. A
    field the declaration leaves empty never blanks the recorded one."""
    merged: list[dict] = []
    index: dict[str, int] = {}
    for entry in recorded or []:
        fields = _npc_fields(entry)
        if not fields["name"]:
            continue
        index[name_key(fields["name"])] = len(merged)
        merged.append(fields)
    for entry in declared or []:
        fields = _npc_fields(entry)
        if not fields["name"]:
            continue
        at = index.get(name_key(fields["name"]))
        if at is None:
            index[name_key(fields["name"])] = len(merged)
            merged.append(fields)
            continue
        prior = merged[at]
        for field in ("name", "description", "role", "race", "class", "state"):
            if fields[field]:
                prior[field] = fields[field]
    return merged


def _sync_dead_states(entry: dict) -> None:
    """Keep a seed entry's death index in step with its regulars' declared `state`.

    `dead` is the engine's permanent index (a combat kill freezes into it at Save); a declared
    `state: dead` joins it. Any other state is the GM's word for the fiction and renders as
    given, while the index -- and so "a death is never cleared by silence" -- stays."""
    for npc in _seed_main_npcs(entry):
        if str(npc.get("state") or "").strip().lower() == "dead" and npc["name"]:
            entry["dead"] = sorted({*_dead_names(entry), npc["name"].strip().lower()} - {""})


def _merged_roster(main_npcs, prior) -> list[dict]:
    """The roster to store for a place: the declaration as a delta over what it already holds."""
    recorded = _seed_main_npcs(prior)
    return _merge_npc_lists(recorded, _npc_list(main_npcs or []))


def _npc_list(value) -> list[dict]:
    """Normalize a place's main NPCs to a list of {name, description, role, race, class}.

    A list is the canonical shape (v6); a legacy single dict/string becomes a one-item
    list. Entries with neither a name nor a description are dropped."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[dict] = []
    for item in items:
        fields = _npc_fields(item)
        if fields["name"] or fields["description"]:
            out.append(fields)
    return out


def _seed_main_npcs(entry) -> list[dict]:
    """A seed entry's main NPCs, reading the v6 `main_npcs` key or a legacy `main_npc`."""
    raw = (entry or {}).get("main_npcs")
    if raw is None:
        raw = (entry or {}).get("main_npc")
    return _npc_list(raw)


def _dead_names(entry) -> list[str]:
    """A seed entry's dead main-NPC names, lowercased.

    The flag lives OUTSIDE the NPC dicts (`entry["dead"]`): `_npc_fields` rebuilds each NPC
    to exactly name/description/role/race/class and would drop a `dead` key."""
    raw = (entry or {}).get("dead")
    return [str(n).strip().lower() for n in (raw if isinstance(raw, list) else [])
            if str(n or "").strip()]


def _prune_dead(entry: dict) -> None:
    """Keep `entry["dead"]` to names still declared as main NPCs of the place."""
    declared = {str(n.get("name") or "").strip().lower() for n in _seed_main_npcs(entry)}
    entry["dead"] = sorted(n for n in _dead_names(entry) if n in declared)


def _place_path(place) -> list[str]:
    """Normalize a place path: trimmed, non-empty string segments (deepest last)."""
    if isinstance(place, str):
        place = [place]
    if not isinstance(place, (list, tuple)):
        return []
    return [str(p).strip() for p in place if str(p or "").strip()]


def _manifest_entries(data) -> list[dict]:
    """Normalize a scene manifest (v1 flat / v2-v7) to a list of seed dicts."""
    if not isinstance(data, dict):
        return []
    if data.get("version") in (2, 3, 4, 5, 6, 7, 8) and isinstance(data.get("seeds"), dict):
        return [e for e in data["seeds"].values() if isinstance(e, dict)]
    # v1: {location_slug: entry}
    return [e for e in data.values() if isinstance(e, dict)
            and (e.get("location") or e.get("place"))]


# ── the live scene registry: in memory until the player saves ─────────────
#
# The scenes manifest (places + declared NPCs) is the save's companion, but it must change
# on disk ONLY when the player saves -- an unsaved session has to leave the previous save
# untouched. Every read/write during a session goes through this module-level registry,
# loaded from disk on first access. `commit_scene_manifest` writes it (and prunes image
# files the registry no longer lists); `discard_scene_manifest` drops it so the next read
# reloads the save. The engine owns the session boundary; the scene service only reads and
# mutates the dict.
#
# Writers run on two threads -- the engine, and the server's per-place/per-scene workers -- so
# every read-modify-write goes through `mutate_scene_manifest`, which holds `_SCENE_LOCK` across
# read + mutate + keep. And because a worker can outlive the session it belongs to, each save's
# registry carries a generation: `discard_scene_manifest` (the session boundary) bumps it, a
# session captures the new value when it starts and passes it to its writers, and a write from a
# stale generation is refused -- so a late worker can neither republish its old copy of the dict
# into the next session's registry nor leak its places into it.

_SCENE_MANIFESTS: dict[str, dict] = {}
_SCENE_GENERATIONS: dict[str, int] = {}  # write tickets, see `discard_scene_manifest`
_SCENE_LOCK = threading.RLock()  # scene writes run on the engine's thread and worker threads


def _scene_key(kingdom, area, place, era="") -> str:
    """A place's manifest key. The era is the root: two eras can share a place path,
    and they are different places with different seeds."""
    parts = [slugify(p) for p in (kingdom, area, *_place_path(place))]
    return "|".join(([slugify(era)] if era else []) + parts)


def _scenes_dir_for(output_dir, stem: str) -> Path:
    return Path(output_dir or ".") / "images" / stem / "scenes"


def _scene_cache_key(scenes: Path) -> str:
    try:
        return str(scenes.resolve())
    except OSError:  # pragma: no cover - only on a broken path
        return str(scenes)


def _empty_scene_manifest() -> dict:
    return {"version": 8, "seeds": {}, "cast": {}, "current": {}}


def _normalise_scene_manifest(data, scenes: Path, era: str = "") -> dict:
    """Normalize a scene manifest (v1 flat / v2-v8) to the v8 shape."""
    if not isinstance(data, dict):
        return _empty_scene_manifest()
    if data.get("version") in (2, 3, 4, 5, 6, 7, 8) and isinstance(data.get("seeds"), dict):
        if data.get("version") not in (4, 5, 6, 7, 8):
            # Action images became ephemeral one-shot files in v4: clean up any left over.
            for entry in (data.get("actions") or []):
                try:
                    (scenes / str((entry or {}).get("file") or "")).unlink()
                except OSError:
                    pass
        seeds: dict = {}
        for key, entry in data["seeds"].items():
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            entry.pop("last_cast", None)
            entry["main_npcs"] = _seed_main_npcs(entry)
            entry.pop("main_npc", None)
            place = _place_path(entry.get("place"))
            if not place:
                place = _place_path([entry.get("location"), entry.get("sublocation")])
            entry["place"] = place
            entry.pop("location", None)
            entry.pop("sublocation", None)
            # v7 and older carry no era: it defaults to the save's current one.
            entry["era"] = str(entry.get("era") or "").strip() or era
            seeds[_scene_key(entry.get("kingdom"), entry.get("area"), place, entry["era"])] = entry
        cast: dict = {}
        raw_cast = data.get("cast")
        if isinstance(raw_cast, dict):
            for entry in raw_cast.values():
                fields = _npc_fields(entry)
                if fields["name"]:
                    cast[slugify(fields["name"])] = fields
        current = data.get("current")
        if isinstance(current, dict) and current:
            cplace = _place_path(current.get("place"))
            if not cplace:
                cplace = _place_path([current.get("location"), current.get("sublocation")])
            current = {k: v for k, v in current.items() if k not in ("location", "sublocation")}
            current["place"] = cplace
            # A legacy pointer carries no era: like a seed, it belongs to the save's age.
            current["era"] = str(current.get("era") or "").strip() or era
        else:
            current = {}
        return {"version": 8, "seeds": seeds, "cast": cast, "current": current}
    # v1 flat manifest: {location_slug: entry}
    seeds: dict = {}
    now = int(time.time())
    for slug, entry in (data or {}).items():
        if not isinstance(entry, dict):
            continue
        place = _place_path([entry.get("location"), entry.get("sublocation")])
        if not place:
            continue
        kingdom = str(entry.get("kingdom") or "").strip()
        area = str(entry.get("area") or "").strip()
        seeds[_scene_key(kingdom, area, place, era)] = {
            "file": entry.get("file"), "slug": slug, "era": era,
            "kingdom": kingdom, "area": area, "place": place,
            "description": str(entry.get("description") or ""),
            "main_npcs": _npc_list(entry.get("main_npc")),
            "created": entry.get("created") or now,
            "mime": entry.get("mime") or "image/png",
        }
    return {"version": 8, "seeds": seeds, "cast": {}, "current": {}}


def _read_scene_manifest_file(scenes: Path, era: str = "") -> dict:
    return _normalise_scene_manifest(read_json_dict(scenes / "manifest.json"), scenes, era)


def _live_manifest_locked(key: str, scenes: Path, era: str) -> dict:
    """The live manifest for a scene key, loaded from disk on first access. Lock held."""
    data = _SCENE_MANIFESTS.get(key)
    if data is None:
        data = _read_scene_manifest_file(scenes, era)
        _SCENE_MANIFESTS[key] = data
    return data


def live_scene_manifest(output_dir, stem: str, era: str = "") -> dict:
    """The LIVE manifest for a save: from memory if a session holds it, else from disk.

    Mutate the returned dict in place. Nothing reaches disk until `commit_scene_manifest`.
    """
    scenes = _scenes_dir_for(output_dir, stem)
    with _SCENE_LOCK:
        return _live_manifest_locked(_scene_cache_key(scenes), scenes, era)


def mutate_scene_manifest(output_dir, stem: str, fn, era: str = "", generation=None) -> dict:
    """Read + mutate + keep the LIVE manifest under `_SCENE_LOCK` -- the one write path.

    `fn(data)` mutates the live dict in place and returns its result. A writer that belongs to
    a session passes that session's `generation` (captured when the session started, see
    `discard_scene_manifest`); if the registry has moved on since -- the session ended, or
    another session took the save over -- the write is refused and `{"stale": True}` is
    returned instead of `fn`'s result.
    """
    scenes = _scenes_dir_for(output_dir, stem)
    key = _scene_cache_key(scenes)
    with _SCENE_LOCK:
        if generation is not None and generation != _SCENE_GENERATIONS.get(key, 0):
            return {"stale": True}
        return fn(_live_manifest_locked(key, scenes, era))


def scene_generation(output_dir, stem: str) -> int:
    """The save's live-registry generation: bumped at every session boundary."""
    scenes = _scenes_dir_for(output_dir, stem)
    with _SCENE_LOCK:
        return _SCENE_GENERATIONS.get(_scene_cache_key(scenes), 0)


def _write_scene_manifest_file(scenes: Path, data: dict) -> None:
    write_json_atomic(scenes / "manifest.json", data)


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _prune_scene_images(scenes: Path, data: dict) -> list[str]:
    """Delete image files in the scenes dir that the committed registry does not list."""
    referenced = {Path(str((e or {}).get("file") or "")).name
                  for e in (data.get("seeds") or {}).values()
                  if str((e or {}).get("file") or "").strip()}
    removed: list[str] = []
    try:
        entries = list(scenes.iterdir())
    except OSError:
        return removed
    for path in entries:
        if path.is_file() and path.suffix.lower() in _IMAGE_EXTS and path.name not in referenced:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed


def commit_scene_manifest(output_dir, stem: str) -> dict | None:
    """Write the live manifest to disk (the Save) and prune unreferenced scene images."""
    scenes = _scenes_dir_for(output_dir, stem)
    key = _scene_cache_key(scenes)
    with _SCENE_LOCK:
        data = _SCENE_MANIFESTS.get(key)
        if data is None:
            return None
        # A save that never had a place gets no manifest file at all.
        if not (data.get("seeds") or data.get("cast") or data.get("current")) \
                and not (scenes / "manifest.json").exists():
            return {"seeds": 0, "cast": 0, "pruned": []}
        _write_scene_manifest_file(scenes, data)
        pruned = _prune_scene_images(scenes, data)
    return {"seeds": len(data.get("seeds") or {}), "cast": len(data.get("cast") or {}),
            "pruned": pruned}


def discard_scene_manifest(output_dir, stem: str, expected_generation: int | None = None) -> int:
    """Drop the live manifest so the next read reloads the save from disk.

    This is the session boundary, so it also bumps the save's registry generation. The session
    starting here captures the returned value and hands it to its writers; a write that still
    carries an older generation is refused (see `mutate_scene_manifest`).

    When `expected_generation` is given, the discard is a no-op unless that ticket is the
    current one. A session that outlived its own boundary must not clear a newer session's
    registry (the stale-close side of the same generation contract)."""
    scenes = _scenes_dir_for(output_dir, stem)
    key = _scene_cache_key(scenes)
    with _SCENE_LOCK:
        current = _SCENE_GENERATIONS.get(key, 0)
        if expected_generation is not None and expected_generation != current:
            return current
        _SCENE_MANIFESTS.pop(key, None)
        generation = current + 1
        _SCENE_GENERATIONS[key] = generation
        return generation


def current_scene_place(output_dir, stem: str, era: str | None = None) -> dict | None:
    """The save's current place pointer ({era, kingdom, area, place}), or None.

    Set by `record_place` / `ensure_scene`. The text-mode scene caption reads it: the engine
    knows the age and the place, never the time of day or the weather (only an image call
    carries those).

    `era` scopes the pointer to one age: a pointer filed in another age is None here (the
    Traveller has not stood anywhere in this one yet), and a legacy pointer with no era is
    read as this age's -- the same rule the places use. It is also the era the manifest's
    FIRST load is normalised with, so the session must make this read before any other.
    """
    data = live_scene_manifest(output_dir, stem, era or "")
    current = data.get("current") if isinstance(data, dict) else None
    if not isinstance(current, dict) or not current:
        return None
    want_era = None if era is None else str(era).strip().lower()
    current_era = str(current.get("era") or "").strip().lower()
    if want_era is not None:
        if current_era and current_era != want_era:
            return None
        current_era = current_era or want_era
    place = _place_path(current.get("place"))
    area = str(current.get("area") or "").strip()
    if not place and not area:
        return None
    return {"era": current_era,
            "kingdom": str(current.get("kingdom") or "").strip(),
            "area": area, "place": place}


def known_scene_places(output_dir, stem: str, era: str | None = None) -> list[dict]:
    """Known places for a save: {era, kingdom, area, place, description, main_npcs, used,
    file, seeded}.

    A place is KNOWN as soon as it is declared, with or without a picture; `seeded` says
    whether its hidden establishing view exists on disk. `file` is the manifest's own field
    (the seed's filename), or None for a file-less declaration (text mode, or an images-mode
    place whose picture has not been drawn yet). The engine treats known-but-unseeded as still
    needing `establishing`, so a recorded place cannot suppress the seed prompt.

    `era` scopes the list to exactly one era. An entry with no era is a legacy entry (a
    manifest written before v8, or a writer that omitted it); it is stamped with the save's
    era when the manifest is loaded (see `_normalise_scene_manifest`), so it matches only an
    unscoped read (`era=None`, the classic game). A place must never leak into another age's
    tree or the Device's picker.
    """
    data = live_scene_manifest(output_dir, stem, era or "")
    scenes = _scenes_dir_for(output_dir, stem)
    want_era = None if era is None else str(era).strip().lower()
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    out: list[dict] = []
    for entry in _manifest_entries(data):
        entry_era = str(entry.get("era") or "").strip()
        kingdom = str(entry.get("kingdom") or "").strip()
        area = str(entry.get("area") or "").strip()
        if want_era is not None and entry_era.lower() != want_era:
            continue
        place = _place_path(entry.get("place"))
        if not place:
            place = _place_path([entry.get("location"), entry.get("sublocation")])
        key = (entry_era.lower(), kingdom.lower(), area.lower(), tuple(p.lower() for p in place))
        if not place or key in seen:
            continue
        seen.add(key)
        file = str(entry.get("file") or "").strip()
        out.append({"era": entry_era, "kingdom": kingdom, "area": area, "place": place,
                    "description": str(entry.get("description") or "").strip(),
                    "main_npcs": _seed_main_npcs(entry),
                    "dead": _dead_names(entry),
                    "used": int(entry.get("used") or entry.get("created") or 0),
                    "file": file or None,
                    "seeded": bool(file) and (scenes / file).is_file()})
    return sorted(out, key=lambda p: (p["era"].lower(), p["kingdom"].lower(), p["area"].lower(),
                                      [s.lower() for s in p["place"]]))


def known_cast_people(output_dir, stem: str) -> list[dict]:
    """The save's recurring people NOT tied to one place, with their declared identity.

    The per-turn block shows place regulars only, so the on-demand record carries these as a
    footer: the GM declared them, and must never re-describe or re-invent them. Returns
    `{name, role, race, class}` in name order (no look: looks are image-only)."""
    data = live_scene_manifest(output_dir, stem)
    raw = data.get("cast") if isinstance(data, dict) else None
    people: list[dict] = []
    if isinstance(raw, dict):
        for entry in raw.values():
            fields = _npc_fields(entry)
            if fields["name"]:
                people.append({k: fields[k] for k in ("name", "role", "race", "class")})
    return sorted(people, key=lambda p: p["name"].lower())


def known_cast_names(output_dir, stem: str) -> list[str]:
    """The save's recurring people NOT tied to one place (the manifest's `cast`), by name."""
    return [p["name"] for p in known_cast_people(output_dir, stem)]


def known_npc_names(output_dir, stem: str) -> set[str]:
    """Declared NPC names for a save (place main NPCs + the storyline cast), lowercased."""
    data = live_scene_manifest(output_dir, stem)
    names: set[str] = set()
    for entry in _manifest_entries(data):
        for npc in _seed_main_npcs(entry):
            if npc["name"]:
                names.add(npc["name"].lower())
    raw_cast = data.get("cast") if isinstance(data, dict) else None
    if isinstance(raw_cast, dict):
        for entry in raw_cast.values():
            name = _npc_fields(entry)["name"]
            if name:
                names.add(name.lower())
    return names


def mark_place_dead(output_dir, stem: str, era: str, kingdom: str, area: str, place,
                    names) -> list[str]:
    """Permanently mark main NPCs of one place as dead (era- and place-scoped).

    `names` are matched case-insensitively. The flag is the seed entry's `dead` list,
    OUTSIDE the NPC dicts so `_npc_fields` never strips it. Mutates the live manifest;
    nothing reaches disk until `commit_scene_manifest`. Returns the names newly added."""
    wanted = {str(n).strip().lower() for n in (names or []) if str(n or "").strip()}
    if not wanted:
        return []

    def apply(data: dict) -> list[str]:
        entry = (data.get("seeds") or {}).get(_scene_key(kingdom, area, place, era))
        if not isinstance(entry, dict):
            return []
        dead = set(_dead_names(entry))
        added = sorted(wanted - dead)
        if added:
            entry["dead"] = sorted(dead | wanted)
        return added

    return mutate_scene_manifest(output_dir, stem, apply, era=era)


def known_scene_locations(output_dir, stem: str) -> list[str]:
    """Distinct place-path labels already seeded for a save (back-compat helper)."""
    seen: list[str] = []
    for entry in known_scene_places(output_dir, stem):
        label = " — ".join(entry.get("place") or [])
        if label and label.lower() not in {s.lower() for s in seen}:
            seen.append(label)
    return sorted(seen, key=str.lower)


_DEFAULT_SCENE_SEED = (
    "A wide establishing view of {address_line}, seen at eye level or a slight three-quarter "
    "angle. {establishing} World setting: {world}. Empty and unpopulated — no people, no "
    "creatures, no animals, no protagonist. Timeless and weather-neutral: no rain, snow, fog, "
    "mist or time-of-day light (no dawn, dusk or night); the light is neutral. Atmospheric, strong "
    "sense of place, painterly detail. No text, letters, numbers, runes, watermarks, logos or "
    "borders."
)
_SCENE_SEED_GUARD = (
    "The scene must contain no person, no creature and no animal of any kind — an empty place."
)
_SCENE_SEED_CHANGE_LINE = (
    "Apply this permanent change to the place: {change}. Keep the architecture, layout, colours "
    "and everything else consistent with the attached image."
)
_SCENE_ACTION_FROM_SEED = (
    "Attached references: (1) the establishing view of {location} — {sublocation}: this IS the "
    "picture — reproduce it unchanged (same camera, framing, architecture, furniture, props, "
    "colours, arrangement), then place the characters into it. Change nothing about the space "
    "itself, except the three things the moment legitimately changes: the time of day (relight "
    "the same scene for day or night), the weather (rain, fog, snow, wind — rendered as "
    "atmosphere, staying outdoors), and the action's own transient damage or mess — a table split, "
    "a chair toppled, a hearth scattered, spilled ale, a fire. Objects the action touches may be "
    "broken or displaced; the room is never rearranged. (2) the protagonist's portrait — that "
    "same character, in the described action."
)
_SCENE_ACTION_FROM_SEED_SOLO = (
    "Attached reference: the establishing view of {location} — {sublocation}: this IS the picture "
    "— reproduce it unchanged (same camera, framing, architecture, furniture, props, colours, "
    "arrangement), then place the characters into it. Change nothing about the space itself, "
    "except the three things the moment legitimately changes: the time of day (relight the same "
    "scene for day or night), the weather (rain, fog, snow, wind — rendered as atmosphere, "
    "staying outdoors), and the action's own transient damage or mess — a table split, a chair "
    "toppled, a hearth scattered, spilled ale, a fire. Objects the action touches may be broken "
    "or displaced; the room is never rearranged."
)
_SCENE_NO_EXTRA_FIGURES = (
    "Draw only the protagonist and the characters listed above: no other person, face, silhouette, "
    "figure, reflection or background crowd may appear."
)
_SCENE_NO_CAMERA_GAZE = (
    "No one looks at, faces or acknowledges the camera or the viewer — every figure is caught "
    "mid-action, absorbed in what they are doing and facing into the scene; every figure faces "
    "the ACTION at hand, and a figure seen from behind, back to the camera, is correct whenever "
    "the action is in front of them."
)
_SCENE_LIGHTING_GUARD = (
    "Match the declared time of day exactly. At night or deep night draw true night: deep "
    "darkness over everything except the local light sources — a fire, a lamp, a lantern, a "
    "moonlit doorway — each throwing only a small pool of light around itself. No twilight, no "
    "dusk glow, no sunset colours, no ambient daylight, no blue hour."
)
_SCENE_WEATHER_GUARD = (
    "Weather is an outdoor phenomenon: rain and snow fall only where the sky is open. If this "
    "place is an interior — the establishing view shows walls, a ceiling or a roof — keep "
    "precipitation outside: show it only through the windows, doorways and other openings "
    "(streaks on the glass, a sky beyond, weather-light falling through the opening) and keep the "
    "inside dry — no rain, snow, puddles, drips or wet interior surfaces. Never draw rain or snow "
    "falling or pooling inside an enclosed room. Fog, mist and haze may soften the interior air, "
    "but never as falling water. If the place is genuinely open to the sky (a courtyard, a ruined "
    "roofless hall, an open-sided shelter), render the weather directly where the sky is open."
)
_SCENE_LAYOUT_LOCK = (
    "The attached establishing view IS the scene: keep the same camera, framing, crop, architecture, "
    "walls, doors, windows, stairs, furniture, props, their positions, scale and proportions. Do "
    "not redesign, add, remove, resize, restyle or rearrange anything, and do not change the "
    "viewpoint, the framing or the composition. Only what the moment legitimately changes may "
    "differ: the time of day (relight this identical scene for day or night), the weather "
    "(atmosphere only — it stays outdoors and never alters the space), and the transient damage or "
    "mess of the action (a table split, a chair toppled, a fire, spilled ale). Then place the "
    "characters into that same space."
)
_SCENE_ABSENT_ELEMENT_GUARD = (
    "The attached establishing view is the ground truth for the space: never invent an element it "
    "does not show — no new door, doorway, window, opening, stair, beam, fixture or furniture. "
    "Place characters only where that view allows; if a character is described at a spot it does "
    "not contain (standing in a doorway that is not there), move them into the closest visible "
    "place; if that is impossible, leave them out of the picture. This applies to every figure, "
    "the protagonist included: never invent a place to keep them in frame."
)
_SCENE_TIME_LINE = "Time of day: {time_of_day}."
_SCENE_WEATHER_LINE = "Weather: {weather}."
_SCENE_CHARACTERS_LINE = "Characters present (render each exactly as described; add no one else):"
_SCENE_CHARACTERS_GUARD = (
    "Render every listed character exactly as described — a described woman must not be drawn "
    "as a man — and add no character who is not listed."
)
_DEFAULT_SCENE_ACTION = (
    "{location_line} A cinematic widescreen illustration of this moment: {description} "
    "The protagonist is a {race} {class}. {gear} Location: {address_line}. "
    "World setting: {world}. {atmosphere_line} {characters_line} Mood: {mood}. Wide establishing "
    "composition, dramatic atmospheric lighting. No text, letters, numbers, runes, watermarks, "
    "logos or borders."
)


class SceneService(ImageBackendHolder):
    """Generates + persists a hidden, permanent establishing **seed** per place
    `(kingdom, area, place path)` and a visible **action** image drawn
    fresh from that seed + the portrait. Actions are never referenced by later
    actions (that made the model duplicate the figures already in the frame) and
    are deleted as soon as they have been served once.

    `output/images/{stem}/scenes/manifest.json` is the v7 manifest:
        {version, seeds: {key: entry}, cast: {name_slug: {name, description, role}}, current: {...}}
    Each seed entry carries `main_npcs` = a list of {name, description, role}. The seed is
    never served to the client; it is the only persisted scene asset. The `cast` map keeps
    the declared NPC looks; the GM only ever passes their names.
    """

    def __init__(self, output_dir=None, config_path: Path | None = None,
                 backend: ImageBackend | None = None):
        cfg = _load_config(config_path)
        self.backend = backend or _backend_from_config(
            cfg,
            model=str(cfg.get("model") or _DEFAULT_MODEL),
            aspect_ratio=str(cfg.get("scene_aspect_ratio") or "16:9"),
            thinking_level=_thinking_level(cfg),
        )
        self.output_dir = Path(output_dir) if output_dir else None
        # Scenes share the portrait's locked painterly style unless overridden.
        self.style = str(cfg.get("scene_style") or cfg.get("style") or "").strip()
        self.style_guard = str(cfg.get("style_guard") or _STYLE_GUARD).strip()
        self.seed_template = str(cfg.get("scene_seed") or _DEFAULT_SCENE_SEED).strip()
        self.seed_guard = str(cfg.get("scene_seed_guard") or _SCENE_SEED_GUARD).strip()
        self.seed_change_line = str(cfg.get("scene_seed_change") or _SCENE_SEED_CHANGE_LINE).strip()
        self.template = str(cfg.get("scene") or _DEFAULT_SCENE_ACTION).strip()
        self.action_from_seed = str(
            cfg.get("scene_action_from_seed") or _SCENE_ACTION_FROM_SEED
        ).strip()
        self.no_extra_figures = str(
            cfg.get("scene_no_extra_figures") or _SCENE_NO_EXTRA_FIGURES
        ).strip()
        self.no_camera_gaze = str(
            cfg.get("scene_no_camera_gaze") or _SCENE_NO_CAMERA_GAZE
        ).strip()
        self.lighting_guard = str(
            cfg.get("scene_lighting_guard") or _SCENE_LIGHTING_GUARD
        ).strip()
        self.weather_guard = str(
            cfg.get("scene_weather_guard") or _SCENE_WEATHER_GUARD
        ).strip()
        self.layout_lock = str(
            cfg.get("scene_layout_lock") or _SCENE_LAYOUT_LOCK
        ).strip()
        self.absent_element_guard = str(
            cfg.get("scene_absent_element_guard") or _SCENE_ABSENT_ELEMENT_GUARD
        ).strip()
        self.portrait_reference = str(
            cfg.get("scene_portrait_reference_line") or _SCENE_PORTRAIT_LINE
        ).strip()
        self.protagonist_guard = str(
            cfg.get("scene_protagonist_guard") or _SCENE_PROTAGONIST_GUARD
        ).strip()
        self.protagonist_template = str(cfg.get("scene_protagonist") or _PROTAGONIST_LINE).strip()
        self.appearance_line = str(
            cfg.get("scene_appearance_line") or _SCENE_APPEARANCE_LINE
        ).strip()
        self.appearance_gear_guard = str(
            cfg.get("scene_appearance_gear") or _SCENE_APPEARANCE_GEAR
        ).strip()
        self.appearance_effects = _appearance_effects_config(cfg)
        self.action_from_seed_solo = str(
            cfg.get("scene_action_from_seed_solo") or _SCENE_ACTION_FROM_SEED_SOLO
        ).strip()
        self.time_line = str(cfg.get("scene_time_line") or _SCENE_TIME_LINE).strip()
        self.weather_line = str(cfg.get("scene_weather_line") or _SCENE_WEATHER_LINE).strip()
        self.characters_header = str(cfg.get("scene_characters_line") or _SCENE_CHARACTERS_LINE).strip()
        self.characters_guard = str(cfg.get("scene_characters_guard") or _SCENE_CHARACTERS_GUARD).strip()
        self.ref_max_side = _int_or(cfg.get("ref_max_side"), 512)
        self.ref_media_resolution = str(cfg.get("ref_media_resolution") or "MEDIA_RESOLUTION_MEDIUM").strip()
        try:
            self.max_side = int(cfg.get("scene_max_side") or 0)
        except (TypeError, ValueError):
            self.max_side = 0

    # ── paths / manifest ───────────────────────────────────────────────────

    def scenes_dir(self, stem: str) -> Path:
        base = self.output_dir or Path(".")
        return base / "images" / stem / "scenes"

    def manifest_path(self, stem: str) -> Path:
        return self.scenes_dir(stem) / "manifest.json"

    @staticmethod
    def _key(kingdom, area, place, era="") -> str:
        """A place's manifest key. The era is the root (see `_scene_key`)."""
        return _scene_key(kingdom, area, place, era)

    def _read_manifest(self, stem: str, era: str = "") -> dict:
        """The live manifest for this save: in memory, written to disk only on Save."""
        return live_scene_manifest(self.output_dir or Path("."), stem, era)

    def _mutate_manifest(self, stem: str, fn, era: str = "", generation=None) -> dict:
        """Read + mutate + keep the live manifest under the registry lock (the one write path).

        `fn(data)` mutates the live dict in place and returns its result; a stale `generation`
        refuses the write (`{"stale": True}`) -- see `mutate_scene_manifest`."""
        return mutate_scene_manifest(self.output_dir or Path("."), stem, fn, era=era,
                                     generation=generation)

    def _file_bytes(self, stem: str, entry) -> tuple[bytes, str] | None:
        path = self.scenes_dir(stem) / str((entry or {}).get("file") or "")
        if not path.exists():
            return None
        try:
            return (downscale_image(path.read_bytes(), self.ref_max_side), image_mime(path))
        except OSError:
            return None

    def _read_portrait(self, stem: str) -> tuple[bytes, str] | None:
        directory = (self.output_dir or Path(".")) / "images" / stem
        path = first_existing(directory, "portrait")
        if path is None:
            return None
        try:
            return (downscale_image(path.read_bytes(), self.ref_max_side), image_mime(path))
        except OSError:
            return None

    @staticmethod
    def _seed_by_place(seeds: dict, place, current=None, era: str = ""):
        """Resolve a seed from its place path when kingdom/area are omitted."""
        want = [slugify(p) for p in _place_path(place)]
        matches = [(k, v) for k, v in (seeds or {}).items()
                   if [slugify(x) for x in _place_path(v.get("place"))] == want
                   and (not era or str(v.get("era") or "") == era)]
        if not matches:
            return None, None
        cur = current or {}
        cur_k, cur_a = slugify(cur.get("kingdom")), slugify(cur.get("area"))
        for k, v in matches:
            if slugify(v.get("kingdom")) == cur_k and slugify(v.get("area")) == cur_a:
                return k, v
        if len(matches) == 1:
            return matches[0]
        return max(matches, key=lambda kv: int(kv[1].get("created") or 0))

    # ── serving (action images only; seeds are hidden) ──────────────────────

    def get_scene(self, stem: str, name: str) -> tuple[bytes, str] | None:
        """Serve an action image once, then delete it: actions are ephemeral.
        Seeds are internal and are never served."""
        if not str(name or "").startswith("action-"):
            return None
        directory = self.scenes_dir(stem)
        path = first_existing(directory, name)
        if path is None:
            return None
        try:
            data = path.read_bytes()
            mime = image_mime(path)
        except OSError:
            return None
        try:
            path.unlink()
        except OSError:
            pass
        return data, mime

    # ── prompts ─────────────────────────────────────────────────────────────

    @staticmethod
    def _clean(text) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _address_line(self, kingdom: str, area: str, place) -> str:
        label = " — ".join(_place_path(place))
        parts = [p for p in (label, self._clean(area), self._clean(kingdom)) if p]
        return ", ".join(parts) or "an unnamed place"

    def _atmosphere_line(self, time_of_day: str, weather: str) -> str:
        """The explicit time/weather sentence, omitting empty parts."""
        parts = []
        tod = self._clean(time_of_day)
        w = self._clean(weather)
        if tod:
            parts.append(self.time_line.format(time_of_day=tod))
        if w:
            parts.append(self.weather_line.format(weather=w))
        return " ".join(parts)

    def _characters_line(self, characters) -> str:
        """One bullet per on-stage character: `<identity look>: <action>`."""
        if not isinstance(characters, dict):
            return ""
        rows = []
        for key, value in characters.items():
            k = self._clean(key)
            if not k:
                continue
            v = self._clean(value)
            rows.append(f"- {k}: {v}" if v else f"- {k}")
        if not rows:
            return ""
        return self.characters_header + "\n" + "\n".join(rows)

    def seed_prompt(self, world: str, kingdom: str = "", area: str = "", place=None,
                    establishing: str = "", change: str = "",
                    has_seed_ref: bool = False, style: str | None = None) -> str:
        path = _place_path(place)
        loc = self._clean(path[0]) if path else "an unnamed place"
        sub = self._clean(" — ".join(path[1:]))
        fields = {
            "address_line": self._address_line(kingdom, area, path),
            "kingdom": self._clean(kingdom), "area": self._clean(area),
            "location": loc, "sublocation": sub or loc,
            "establishing": self._clean(establishing),
            "change": self._clean(change),
            "world": self._clean(world) or "a fantasy realm",
        }
        body = re.sub(r"[^\S\n]+", " ", self.seed_template.format(**fields)).strip()
        if has_seed_ref and fields["change"]:
            body = (body + " " + self.seed_change_line.format(**fields)).strip()
        if self.seed_guard:
            body = (body + " " + self.seed_guard).strip()
        effective_style = (style or self.style).strip()
        parts = [body]
        if effective_style:
            parts.append(effective_style)
        if self.style_guard:
            parts.append(self.style_guard)
        return " ".join(parts).strip()

    def action_prompt(self, player: dict, world: str, description: str, mood: str,
                      kingdom: str = "", area: str = "", place=None,
                      ref_kind: str = "", time_of_day: str = "", weather: str = "",
                      characters=None, appearance=None, style: str | None = None) -> str:
        player = player or {}
        path = _place_path(place)
        loc = self._clean(path[0]) if path else "an unnamed place"
        sub = self._clean(" — ".join(path[1:]))
        if ref_kind == "seed":
            template = self.action_from_seed_solo if appearance else self.action_from_seed
            ref_line = template.format(location=loc, sublocation=sub or loc)
        elif ref_kind == "portrait":
            ref_line = self.portrait_reference
        else:
            ref_line = ""
        if ref_kind and self.protagonist_guard and not appearance:
            ref_line = (ref_line + " " + self.protagonist_guard).strip()
        race = str(player.get("race") or "") or "adventurer"
        char_class = str(player.get("character_class") or "") or "adventurer"
        gear = _gear_line(player)
        if appearance and appearance.get("look"):
            protagonist_line = self.appearance_line.format(appearance=appearance["look"])
            if appearance.get("covers_gear") and self.appearance_gear_guard:
                protagonist_line = (protagonist_line + " " + self.appearance_gear_guard).strip()
            gear = ""
        else:
            protagonist_line = self.protagonist_template.format(
                race=race, **{"class": char_class}, gear=gear).strip()
        fields = {
            "description": self._clean(description),
            "race": race,
            "class": char_class,
            "gear": gear,
            "protagonist_line": protagonist_line,
            "world": self._clean(world) or "a fantasy realm",
            "kingdom": self._clean(kingdom), "area": self._clean(area),
            "location": loc, "sublocation": sub or loc,
            "address_line": self._address_line(kingdom, area, path),
            "mood": self._clean(mood) or "tense and atmospheric",
            "atmosphere_line": self._atmosphere_line(time_of_day, weather),
            "characters_line": self._characters_line(characters),
            "location_line": ref_line,
        }
        # Collapse horizontal whitespace but keep the character bullets on their own lines.
        body = re.sub(r"[^\S\n]+", " ", self.template.format(**fields)).strip()
        if fields["characters_line"] and self.characters_guard:
            body = (body + " " + self.characters_guard).strip()
        if self.no_extra_figures:
            body = (body + " " + self.no_extra_figures).strip()
        if self.no_camera_gaze:
            body = (body + " " + self.no_camera_gaze).strip()
        if self.lighting_guard:
            body = (body + " " + self.lighting_guard).strip()
        if self._clean(weather) and self.weather_guard:
            body = (body + " " + self.weather_guard).strip()
        if ref_kind == "seed" and self.layout_lock:
            body = (body + " " + self.layout_lock).strip()
        if ref_kind == "seed" and self.absent_element_guard:
            body = (body + " " + self.absent_element_guard).strip()
        effective_style = (style or self.style).strip()
        parts = [body]
        if effective_style:
            parts.append(effective_style)
        if self.style_guard:
            parts.append(self.style_guard)
        return " ".join(parts).strip()

    # ── generation ──────────────────────────────────────────────────────────

    def generate_seed(self, world: str, kingdom: str = "", area: str = "", place=None,
                      establishing: str = "", change: str = "", refs=None,
                      model: str | None = None, style: str | None = None) -> bytes:
        prompt = self.seed_prompt(world, kingdom, area, place, establishing,
                                  change, has_seed_ref=bool(refs), style=style)
        raw = self.generate(prompt, aspect_ratio=self.aspect_ratio, refs=refs,
                                   ref_media_resolution=self.ref_media_resolution,
                                   model=model or self.model,
                                   thinking_level=self.thinking_level)
        return downscale_image(raw, self.max_side) if self.max_side else raw

    def generate_action(self, player: dict, world: str, description: str, mood: str,
                        kingdom: str = "", area: str = "", place=None,
                        ref_kind: str = "", refs=None, model: str | None = None,
                        time_of_day: str = "", weather: str = "", characters=None,
                        appearance=None, style: str | None = None) -> bytes:
        prompt = self.action_prompt(player, world, description, mood, kingdom, area, place,
                                    ref_kind=ref_kind, time_of_day=time_of_day,
                                    weather=weather, characters=characters, appearance=appearance,
                                    style=style)
        raw = self.generate(prompt, aspect_ratio=self.aspect_ratio, refs=refs,
                                   ref_media_resolution=self.ref_media_resolution,
                                   model=model or self.model,
                                   thinking_level=self.thinking_level)
        return downscale_image(raw, self.max_side) if self.max_side else raw

    # ── orchestration ───────────────────────────────────────────────────────

    def _seed_slug(self, kingdom: str, area: str, place, era: str = "") -> str:
        path = _place_path(place)
        h = hashlib.sha1(self._key(kingdom, area, path, era).encode("utf-8")).hexdigest()[:6]
        head = slugify(path[0])[:16] if path else "place"
        tail = slugify(path[-1])[:16] if len(path) > 1 else "main"
        return f"seed-{head}-{tail}-{h}"

    def _action_slug(self, kingdom: str, area: str, place, era: str = "") -> str:
        """A unique name per request — actions are one-shot and never revisited."""
        path = _place_path(place)
        h = hashlib.sha1(self._key(kingdom, area, path, era).encode("utf-8")).hexdigest()[:6]
        token = hashlib.sha1(str(time.time_ns()).encode("ascii")).hexdigest()[:8]
        head = slugify(path[0])[:16] if path else "place"
        tail = slugify(path[-1])[:16] if len(path) > 1 else "main"
        return f"action-{head}-{tail}-{h}-{token}"

    def _store_seed(self, stem: str, manifest: dict, kingdom: str, area: str, place,
                    description: str, main_npcs, raw: bytes,
                    existing=None, style: str | None = None, era: str = "") -> dict:
        path = _place_path(place)
        ext, mime = sniff_image(raw) or ("png", "image/png")
        slug = (existing or {}).get("slug") or self._seed_slug(kingdom, area, path, era)
        file = f"{slug}.{ext}"
        directory = self.scenes_dir(stem)
        drop_stale(directory, slug, ext)
        write_bytes_atomic(directory / file, raw)
        now = int(time.time())
        entry = {
            "file": file, "slug": slug, "era": str(era or ""),
            "kingdom": str(kingdom or ""), "area": str(area or ""),
            "place": path,
            "description": str(description or ""), "main_npcs": _npc_list(main_npcs),
            "dead": _dead_names(existing),
            "style": (style or self.style),
            "mime": mime, "created": now, "used": now,
        }
        _prune_dead(entry)
        manifest.setdefault("seeds", {})[self._key(kingdom, area, path, era)] = entry
        return entry

    @staticmethod
    def _resolve_characters(characters, seed, cast) -> dict:
        """Map `{name: action}` to `{name — description: action}` for the image prompt.

        Names resolve against the place's main NPCs first, then the save's storyline cast;
        an undeclared name is drawn from its own key text (a one-off extra, not remembered)."""
        if not isinstance(characters, dict):
            return {}
        known: dict[str, dict] = {}
        for place in _seed_main_npcs(seed):
            if place["name"]:
                known[place["name"].lower()] = place
        if isinstance(cast, dict):
            for entry in cast.values():
                fields = _npc_fields(entry)
                if fields["name"]:
                    known.setdefault(fields["name"].lower(), fields)
        resolved: dict[str, str] = {}
        for key, value in characters.items():
            name = re.sub(r"\s+", " ", str(key or "")).strip()
            if not name:
                continue
            fields = known.get(name.lower())
            if fields:
                ident = " ".join(p for p in (fields.get("race"), fields.get("class")) if p)
                look = fields.get("description") or fields.get("role") or ""
                label = fields["name"]
                if ident:
                    label += f" ({ident})"
                if look:
                    label += f" — {look}"
            else:
                label = name
            resolved[label] = " ".join(str(value or "").split())
        return resolved

    def ensure_scene(self, stem: str, player: dict, world: str, description: str = "",
                     mood: str = "", kingdom: str = "", area: str = "", place=None,
                     establishing: str = "", main_npcs=None,
                     seed_change: str = "", npcs=None,
                     time_of_day: str = "", weather: str = "", characters=None,
                     model: str | None = None, style: str | None = None,
                     era: str = "", generation=None) -> dict:
        """Ensure the hidden seed (idempotent; regenerated on `seed_change` or an art-style
        change), upsert any newly declared NPCs, then generate the action fresh from the seed
        + the portrait and persist it as a one-shot file (served once, then deleted).

        `era` roots the place path (manifest v8): the same place in two eras is two seeds.
        `characters` arrives as `{name: action}`; the stored descriptions are injected here.
        `generation` is the requesting session's registry write ticket: a request that outlived
        its session is refused (`{"stale": True}`) instead of publishing its places into the
        next session's registry."""
        effective = model or self.model
        effective_style = (style or self.style).strip()
        era = str(era or "").strip().lower()
        if generation is not None and generation != scene_generation(
                self.output_dir or Path("."), stem):
            return {"stale": True}
        manifest = self._read_manifest(stem, era)
        cast = manifest.setdefault("cast", {})
        npcs_added: list[str] = []
        if isinstance(npcs, list):
            for entry in npcs:
                fields = _npc_fields(entry)
                if fields["name"]:
                    cast[slugify(fields["name"])] = fields
                    npcs_added.append(fields["name"])
        seeds = manifest.setdefault("seeds", {})
        path = _place_path(place)
        seed = seeds.get(self._key(kingdom, area, path, era))
        if seed is None and not establishing:
            # kingdom/area omitted (an action call, or a seed change) -> resolve from the place.
            key, found = self._seed_by_place(seeds, path, manifest.get("current"), era)
            if found is not None:
                seed = found
                kingdom = str(found.get("kingdom") or "")
                area = str(found.get("area") or "")
                path = _place_path(found.get("place")) or path
        # A file-less declaration (text mode, or an images-mode place whose picture was never
        # drawn) is seed-shaped but has no image. Treat it as unseeded so enabling images draws
        # it now -- preserving its declared main NPCs and description -- instead of treating the
        # metadata as an already-drawn seed.
        text_entry = None
        if seed is not None and self._file_bytes(stem, seed) is None:
            text_entry = seed
            seed = None
        seed_created = seed_regenerated = False
        style_changed = seed is not None and seed.get("style") != effective_style
        if seed is None:
            label = " — ".join(path) or "an unnamed place"
            seed_main = _merged_roster(main_npcs, text_entry)
            seed_desc = establishing or str((text_entry or {}).get("description") or "")
            raw = self.generate_seed(world, kingdom, area, path,
                                     seed_desc or f"An atmospheric view of {label}.",
                                     model=effective, style=effective_style)
            seed = self._store_seed(stem, manifest, kingdom, area, path,
                                    seed_desc, seed_main, raw, existing=text_entry,
                                    style=effective_style, era=era)
            seed_created = True
        elif seed_change or style_changed:
            # A permanent change redraws with the old seed as a reference; a pure style change
            # redraws WITHOUT it, so the new art style is not anchored to the old picture.
            old = self._file_bytes(stem, seed) if seed_change else None
            raw = self.generate_seed(world, kingdom, area, path,
                                     establishing or str(seed.get("description") or ""),
                                     change=seed_change, refs=[old] if old else None,
                                     model=effective, style=effective_style)
            seed = self._store_seed(stem, manifest, kingdom, area, path,
                                    establishing or str(seed.get("description") or ""),
                                    _merged_roster(main_npcs, seed), raw,
                                    existing=seed, style=effective_style, era=era)
            seed_regenerated = True
        elif main_npcs:
            # A declaration on an EXISTING place: the roster is a delta (a new regular, or a
            # state -- images mode has no `note_place`, so the scene call is its only carrier).
            seed["main_npcs"] = _merged_roster(main_npcs, seed)

        if seed is not None:
            _sync_dead_states(seed)

        # Always draw from the place's empty establishing seed + the portrait. Chaining
        # from the previous action made the model duplicate the figures already in it.
        # An appearance-changing effect (Disguise Self, ...) replaces the portrait: the disguise
        # text is authoritative and the real portrait is deliberately NOT attached.
        appearance = appearance_override(player, self.appearance_effects)
        if seed:
            ref_kind, place_ref = "seed", self._file_bytes(stem, seed)
        elif not appearance:
            ref_kind, place_ref = "portrait", None
        else:
            ref_kind, place_ref = "", None
        portrait = None if appearance else self._read_portrait(stem)
        refs = [r for r in (place_ref, portrait) if r]

        resolved = self._resolve_characters(characters, seed, cast)
        raw = self.generate_action(player, world, description, mood, kingdom, area, path,
                                   ref_kind=ref_kind, refs=refs, model=effective,
                                   time_of_day=time_of_day, weather=weather, characters=resolved,
                                   appearance=appearance, style=effective_style)
        ext = (sniff_image(raw) or ("png", "image/png"))[0]
        slug = self._action_slug(kingdom, area, path, era)
        write_bytes_atomic(self.scenes_dir(stem) / f"{slug}.{ext}", raw)
        created = int(time.time())
        if seed:
            # LRU for the priming cap: the place you drew from last is the one the GM
            # should be reminded of first. `seed` is the manifest entry itself.
            seed["used"] = created
        current = {"era": era, "kingdom": str(kingdom or ""), "area": str(area or ""),
                   "place": path, "updated": created}

        def apply(live: dict) -> dict:
            """Keep this call's places/NPCs and current place on the LIVE registry.

            The generation check in `_mutate_manifest` refuses a stale caller, so `live` is the
            dict read at the top of this call (a discard is the only thing that replaces it,
            and that bumps the generation); re-asserting is then idempotent."""
            live.setdefault("seeds", {}).update(manifest.get("seeds") or {})
            live.setdefault("cast", {}).update(manifest.get("cast") or {})
            live["current"] = current
            return {
                "action": {"url": f"/api/scenes/{stem}/{slug}", "created": created},
                "seed": ({"created": seed["created"], "regenerated": seed_regenerated,
                          "description": seed.get("description", "")}
                         if seed_created or seed_regenerated else None),
                "used_seed": ref_kind == "seed",
                "used_portrait_reference": bool(portrait),
                "appearance_applied": bool(appearance),
                "npcs_added": npcs_added,
                "kingdom": str(kingdom or ""), "area": str(area or ""),
                "place": path,
                "seed_created": seed_created, "seed_regenerated": seed_regenerated,
            }

        return self._mutate_manifest(stem, apply, era=era, generation=generation)

    # ── file-less declarations (text mode, or an images-mode place never drawn) ──

    def record_place(self, stem: str, era: str = "", kingdom: str = "", area: str = "",
                     place=None, main_npcs=None, cast=None, description: str = "",
                     generation=None) -> dict:
        """Remember a place (and its regulars) with no image: the file-less registry.

        Used for a place DECLARED without a picture: text mode, and an images-mode scene call
        whose image never arrives (no browser, no image backend). The entry is seed-shaped with
        `file: None`, and `ensure_scene` later treats a file-less entry as unseeded and draws
        it. Names + roles only: `cast` keeps its description empty (looks are image-only).
        `generation` is the declaring session's registry write ticket: a declaration whose
        session has already ended is refused (`{"recorded": False, "stale": True}`) instead of
        writing a place into the next session's registry.
        """
        path = _place_path(place)
        if not path:
            return {"recorded": False, "reason": "no place path"}
        era = str(era or "").strip().lower()
        kingdom = re.sub(r"\s+", " ", str(kingdom or "")).strip()[:80]
        area = re.sub(r"\s+", " ", str(area or "")).strip()[:80]
        main = _npc_list(main_npcs)[:20]
        declared = _npc_list(cast)[:20]

        def apply(manifest: dict) -> dict:
            seeds = manifest.setdefault("seeds", {})
            key = self._key(kingdom, area, path, era)
            now = int(time.time())
            entry = seeds.get(key)
            if entry is None:
                entry = {"file": None, "slug": None, "era": era,
                         "kingdom": kingdom, "area": area, "place": path,
                         "description": str(description or ""), "main_npcs": main,
                         "dead": [],
                         "created": now, "used": now}
                seeds[key] = entry
            else:
                # Never clobber an existing image seed's file/slug: update the address and
                # the declared people only.
                entry["kingdom"] = kingdom or str(entry.get("kingdom") or "")
                entry["area"] = area or str(entry.get("area") or "")
                entry["place"] = path
                if description:
                    entry["description"] = str(description)
                if main:
                    # A declaration is a DELTA: add/update, never wipe the recorded roster (a
                    # state-only entry is the whole update, and a wipe loses the dead marks).
                    entry["main_npcs"] = _merge_npc_lists(_seed_main_npcs(entry), main)
                entry["used"] = now
            _sync_dead_states(entry)
            _prune_dead(entry)
            cast_map = manifest.setdefault("cast", {})
            added: list[str] = []
            for npc in declared:
                if not npc["name"]:
                    continue
                slug = slugify(npc["name"])
                # A declaration is a DELTA, like the place roster: a field it names updates
                # the cast entry, an empty field keeps what is recorded. The identity
                # (role/race/class) is the GM's, not a look; the description stays image-only.
                merged = _npc_fields(cast_map.get(slug) or {})
                for field in ("name", "description", "role", "race", "class"):
                    if npc.get(field):
                        merged[field] = npc[field]
                cast_map[slug] = merged
                added.append(npc["name"])
            manifest["current"] = {"era": era, "kingdom": kingdom, "area": area,
                                   "place": path, "updated": now}
            return {"recorded": True, "place": path, "era": era, "kingdom": kingdom,
                    "area": area,
                    "main_npcs": [n["name"] for n in _seed_main_npcs(entry) if n["name"]],
                    "cast": added, "file": entry.get("file")}

        result = self._mutate_manifest(stem, apply, era=era, generation=generation)
        if result.get("stale"):
            return {"recorded": False, "stale": True,
                    "reason": "the session that declared this place has ended"}
        return result
