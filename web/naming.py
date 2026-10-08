"""Filename helpers for web-created saves.

Convention (web-only): a save is `output/{slug}.player` (+ `{slug}.timeline`);
there is no `.wwf` file any more. The world is one era (`config/eras/`), injected
by the engine and selected per save.
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
    """Return (stem, player_path), auto-suffixing _2, _3, ... on collision."""
    output_dir = Path(output_dir)
    base = slugify(slug)
    n = 1
    while True:
        stem = base if n == 1 else f"{base}_{n}"
        player = output_dir / f"{stem}.player"
        if not player.exists() and not (output_dir / f"{stem}.timeline").exists():
            return stem, player
        n += 1
