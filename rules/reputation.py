"""Reputation scopes: the faction bucket a reputation change lands in.

Reputation is stored per era, so a scope created here is indistinguishable from one the era
seeding wrote (`web.eras.era_reputation_seed`).
"""

from .consts import _DEFAULT_REPUTATION_FACTION, _LEGACY_REPUTATION_FACTIONS


# The faction slot the engine used before 2026-10-08; still accepted, folded to `others`.

def _era_reputation_scope(era):
    """The empty, era-scoped reputation shape a fresh record gets: `{polity: {faction: []}}`.

    `web.eras` is the one source, so a scope created here is indistinguishable from one
    created at character creation. An import failure is not fatal: the era still gets its
    `others` bucket and the GM's first write lands there.
    """
    name = str(era or "").strip()
    try:
        from web.eras import era_reputation_seed
    except Exception:  # pragma: no cover - the engine always ships web/eras.py
        return {"others": {}}
    seed = era_reputation_seed(name)
    scope = seed.get(name)
    return scope if isinstance(scope, dict) else {"others": {}}


def _ensure_reputation_scope(data, era):
    """The era's writable scope, created on demand from the same seed a new record gets.

    A save that has an era but no scope for it is exactly what a **jump** used to leave
    behind -- the scope was only ever created at character creation -- so every reputation
    write in every era after the first was refused, for good. The scope is now created where
    it is *required*.

    This is not a migration: nothing is rewritten and an unfamiliar top-level key is left
    alone. What the save already holds wins over the seed; a scope that exists but looks
    empty (nothing but `others`, or only keys that are not this era's polities) is merged
    **under** the seed, so a half-written scope heals instead of shadowing its own era.
    """
    name = str(era or "").strip()
    existing = data.get(name)
    seed = _era_reputation_scope(name)
    scope = existing if isinstance(existing, dict) else {}
    if seed and not (set(seed) & set(scope)):
        merged = dict(seed)
        merged.update(scope)
        scope = merged
    data[name] = scope
    return scope


def _reputation_error(key, data, era):
    """A refusal the GM can act on: the era, its real polities, and the shape to write.

    The old refusal listed the era keys of the whole map -- which is not what the GM writes
    against. It now names the era's own polities and their factions.
    """
    known: dict = {}
    scope = data.get(str(era or "").strip()) if isinstance(data, dict) else None
    if isinstance(scope, dict):
        for name, bucket in scope.items():
            known[str(name)] = sorted(bucket) if isinstance(bucket, dict) else []
    return {
        "success": False,
        "error": f"Key '{key}' not found or is not a list.",
        "key": key,
        "era": str(era or ""),
        "known_polities": known,
        "hint": ("Reputation is era-scoped and the engine adds the era: write "
                 "reputation.<polity>[.<faction>]. Pick a polity from known_polities (or "
                 "'others' for people who belong to no polity), then a faction under it."),
    }


def _resolve_reputation_bucket(data, path_in_obj, era, create=True):
    """Resolve a `reputation.<polity>[.<faction>]` path to a writable list.

    Reputation is stored **era-scoped** -- `{era: {polity: {faction: [entries]}}}` -- so
    standing earned in one era is never offered in another, and never lost when the
    Traveler leaves. The GM still writes the short path: the era is the one the save is in,
    and the caller passes it in.

    The era's scope is created on demand (`_ensure_reputation_scope`): a jump, or any path
    that forgets to seed, must not leave an era unwritable. A missing faction under an
    *existing* polity is auto-created when `create` (an `add`); targeting a polity alone, or
    the legacy `misc`, uses the `others` bucket. An unknown polity is never created, so a
    typo still fails.

    Returns `(list_or_None, path_in_obj)`, and the path is relative to **`data`** -- the era
    is part of it -- so the caller writes at the level the resolver actually resolved.
    Returning a scope-relative path here used to make every add create a duplicate bucket at
    the top of the map.
    """
    if not isinstance(data, dict):
        return None, path_in_obj
    name = str(era or "").strip()
    if not name:
        return None, path_in_obj
    scope = _ensure_reputation_scope(data, name)
    parts = path_in_obj.split('.')
    kingdom = parts[0] if parts else ''
    if not kingdom or kingdom not in scope or not isinstance(scope[kingdom], dict):
        return None, path_in_obj
    if len(parts) == 1:
        parts = [kingdom, _DEFAULT_REPUTATION_FACTION]
    if len(parts) != 2 or not parts[1]:
        return None, path_in_obj
    faction = parts[1]
    if faction in _LEGACY_REPUTATION_FACTIONS:
        faction = _DEFAULT_REPUTATION_FACTION
    bucket = scope[kingdom].get(faction)
    if bucket is None:
        if not create:
            return None, path_in_obj
        bucket = []
        scope[kingdom][faction] = bucket
    elif not isinstance(bucket, list):
        return None, path_in_obj
    return bucket, f"{name}.{kingdom}.{faction}"
