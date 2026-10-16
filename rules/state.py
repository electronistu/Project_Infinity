"""The engine's only mutable module state.

`dice_server` used to keep these as module-level globals and rebind them with `global`
statements; they live here instead and every reader/writer in the codebase touches them
through `rules.state.<name>` attribute access.

    DB_CONNECTION    the in-memory SQLite connection (or None before init)
    DEBUG_MODE       whether the `debug_*` tools are enabled
    _COMBAT_REGISTRY the active battle registry
    _COMBAT_INDEX    forgiving-name lookup index into it (see registry_get)
    _SPELLS_DB       cached spell catalogue (None until first load)
    _SPELL_COMPONENTS cached spell component table (None until first load)
    _COMBAT_POS/_COMBAT_ROUND/_COMBAT_ACTOR/_COMBAT_SKIPPED  the initiative cursor
    _WOKE/_SHEET_WAKE_CONDITIONS  conditions a spell marks to end on damage
"""


DB_CONNECTION = None


# Developer mode: the `debug_*` tools exist only to let the engine edit live state from
# the UI. They are off unless the server was started with `--debug` (the engine passes it
# when the session was created with the dev flag). They are never offered to the GM.
DEBUG_MODE = False


_COMBAT_REGISTRY: dict[str, dict] = {}


# The registry is keyed by the GM's exact spelling, because the roster and the combatant
# tooltips display that spelling straight back (see `_registry_summary_list`). This additive
# index maps the forgiving form of every registered name -- trimmed, inner runs of whitespace
# collapsed, casefolded -- to that canonical key, so a case or spacing typo still finds its
# combatant instead of falling through to a fabricated default. It is maintained whenever a
# name enters or leaves the registry, and cleared with it.
_COMBAT_INDEX: dict[str, str] = {}


def normalize_combat_name(name) -> str:
    """The lookup form of a combatant name: trimmed, inner whitespace collapsed, casefolded."""
    return " ".join(str(name or "").split()).casefold()


def registry_key(name) -> str | None:
    """The registry key that matches `name` (case/whitespace-forgiving), else None.

    The index is the fast path; a linear scan catches a registry written directly (the debug
    tools, the test fixtures) that the index never saw, so a stale index can never shadow a
    live entry and an unindexed one is still found.
    """
    key = normalize_combat_name(name)
    if not key:
        return None
    canonical = _COMBAT_INDEX.get(key)
    if canonical is not None and canonical in _COMBAT_REGISTRY:
        return canonical
    for existing in _COMBAT_REGISTRY:
        if normalize_combat_name(existing) == key:
            return existing
    return None


def registry_get(name) -> dict | None:
    """The combatant entry stored under `name` (case/whitespace-forgiving), or None."""
    key = registry_key(name)
    return _COMBAT_REGISTRY.get(key) if key is not None else None


def registry_set(name, entry) -> None:
    """Store `entry` under the declared `name` and index that name for forgiving lookup."""
    _COMBAT_REGISTRY[name] = entry
    key = normalize_combat_name(name)
    if key:
        _COMBAT_INDEX[key] = name


def registry_clear() -> None:
    """Empty the combat registry and its name index together."""
    global _COMBAT_REGISTRY
    _COMBAT_REGISTRY = {}
    _COMBAT_INDEX.clear()


_SPELLS_DB = None


_SPELL_COMPONENTS = None


_COMBAT_POS = -1
_COMBAT_ROUND = 0
_COMBAT_ACTOR = ""                 # last actor the engine resolved an action for
_COMBAT_SKIPPED: list[str] = []    # turns the cursor crossed without acting


# A condition a spell marks `ends_on_damage` (Sleep's Unconscious, Eyebite's Asleep) ends the
# moment its holder takes damage. `_WOKE` collects what this tool call woke so the wrapper can
# tell the GM. A registered combatant carries the mark on its registry entry; a spell can also
# put an unregistered player to sleep (Sleep's AoE), whose condition lives only on the sheet, so
# those marks are held here.
_WOKE: list[dict] = []
_SHEET_WAKE_CONDITIONS: dict[str, set] = {}


def reset(db=None):
    """Restore every mutable global to a clean baseline (`db` seeds DB_CONNECTION)."""
    global DB_CONNECTION, DEBUG_MODE, _COMBAT_REGISTRY, _SPELLS_DB, _SPELL_COMPONENTS
    global _COMBAT_POS, _COMBAT_ROUND, _COMBAT_ACTOR, _COMBAT_SKIPPED
    global _WOKE, _SHEET_WAKE_CONDITIONS
    DB_CONNECTION = db
    DEBUG_MODE = False
    _COMBAT_REGISTRY = {}
    _COMBAT_INDEX.clear()
    _SPELLS_DB = None
    _SPELL_COMPONENTS = None
    _COMBAT_POS = -1
    _COMBAT_ROUND = 0
    _COMBAT_ACTOR = ""
    _COMBAT_SKIPPED = []
    _WOKE = []
    _SHEET_WAKE_CONDITIONS = {}
