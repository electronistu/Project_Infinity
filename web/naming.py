"""Filename helpers for web-created worlds/characters.

Convention (web-only): `output/{slug}.wwf` + `output/{slug}.player`, no `_weave`.
The CLI Forge keeps its own naming; this module is never used by it.
"""

import re
from pathlib import Path

MAX_SLUG_LENGTH = 40
FALLBACK_SLUG = "adventurer"


def slugify(name: str) -> str:
    """Turn a character name into a safe filename stem.

    Lowercase; any run of non [a-z0-9] becomes a single underscore; trimmed.
    Guarantees a non-empty, path-traversal-safe result.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if len(slug) > MAX_SLUG_LENGTH:
        slug = slug[:MAX_SLUG_LENGTH].strip("_")
    return slug or FALLBACK_SLUG


def unique_paths(output_dir, slug: str):
    """Return (stem, wwf_path, player_path), auto-suffixing _2, _3, ... on collision."""
    output_dir = Path(output_dir)
    base = slugify(slug)
    n = 1
    while True:
        stem = base if n == 1 else f"{base}_{n}"
        wwf = output_dir / f"{stem}.wwf"
        player = output_dir / f"{stem}.player"
        if not wwf.exists() and not player.exists():
            return stem, wwf, player
        n += 1


# ── user-chosen save names (web save/load) ────────────────────────────────

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_SAVE_STEM = 80


def sanitize_save_name(name: str, extension: str = ".wwf"):
    """Return (stem, filename) for a player-chosen save name.

    Preserves case and spaces; replaces only filesystem-invalid characters;
    forces the extension; guards Windows reserved device names.
    """
    raw = (name or "").strip()
    if raw.lower().endswith(extension.lower()):
        raw = raw[: -len(extension)]
    raw = _INVALID_FILENAME.sub("_", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" .")
    if not raw:
        raw = "save"
    if raw.lower() in _WINDOWS_RESERVED:
        raw = f"{raw}_save"
    if len(raw) > MAX_SAVE_STEM:
        raw = raw[:MAX_SAVE_STEM].strip(" .")
    return raw, f"{raw}{extension}"
