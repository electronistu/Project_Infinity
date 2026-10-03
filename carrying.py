"""SRD 5.1 carrying capacity, encumbrance and item weights.

Shared by the dice engine (`dice_server.py`, the rules authority) and the web
client's sheet builder (`web/stats.py`), so the two can never disagree.

Rules implemented (SRD 5.1, "Lifting and Carrying" + "Variant: Encumbrance"):

* carrying capacity = Strength score x 15 lb; each size category above Medium
  doubles it and a Tiny creature halves it;
* push, drag or lift = twice the carrying capacity;
* variant encumbrance: over 5 x STR -> *encumbered* (speed -10 ft); over 10 x STR
  -> *heavily encumbered* (speed -20 ft and disadvantage on ability checks,
  attack rolls and saving throws using STR, DEX or CON);
* a standard coin weighs a third of an ounce, so 50 coins weigh a pound.

Table rulings this module makes explicitly:

* The 5x / 10x bands follow the character's actual capacity, so they scale with
  the size multiplier and with an active ``capacity_multiplier`` (Bull's
  Strength doubles capacity *and* therefore the bands).
* A GM-declared weight on an inventory entry always beats the catalog, which is
  how homebrew loot gets weighed (the catalog is exact-match only, never fuzzy).
* The catalog is per *catalog line*: a bundle line such as "Arrows (20)" weighs 1 lb.
  A count in the player's `consumables` map is a number of units, so those lines are
  divided by their bundle size there (`per:` in config/weights.yml).
"""

from __future__ import annotations

import os
import re

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced as "no weights known"
    yaml = None

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")

# SRD "Size and Strength": double per category above Medium, half for Tiny.
SIZE_MULTIPLIERS = {"tiny": 0.5, "small": 1.0, "medium": 1.0, "large": 2.0,
                    "huge": 4.0, "gargantuan": 8.0}
COINS_PER_POUND = 50

_PARENS = re.compile(r"\([^)]*\)")
_POSSESSIVE = re.compile(r"'s\b")
_NUMBER = re.compile(r"\b\d+\b")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_SPACES = re.compile(r"\s+")
_LEADING_ARTICLE = re.compile(r"^(?:a|an|the)\s+")
_MAGIC_PREFIX = re.compile(r"^magic\s+")

_WEIGHTS: dict | None = None
_BUNDLES: dict | None = None
_RACES: dict | None = None


def normalize(name) -> str:
    """Fold case, quantities, parentheses and punctuation for catalog matching."""
    text = _PARENS.sub(" ", str(name or "").lower())
    text = _POSSESSIVE.sub(" ", text)
    text = _NUMBER.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    return _LEADING_ARTICLE.sub("", text)


def _config(filename: str):
    path = os.path.join(CONFIG_DIR, filename)
    if yaml is None or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or []


def _catalog() -> tuple[dict, dict]:
    """(weights, bundle sizes) keyed by normalized name/alias, cached.

    A bundle size (`per:`) says how many units one catalog weight covers — the SRD
    lists "Arrows (20)" as one 1 lb line, while the player's `consumables` map counts
    units. Defaults to 1 for everything that is not a bundle.
    """
    global _WEIGHTS, _BUNDLES
    if _WEIGHTS is None:
        weights: dict[str, float] = {}
        bundles: dict[str, float] = {}
        for entry in _config("weights.yml"):
            if not isinstance(entry, dict) or entry.get("weight") is None:
                continue
            try:
                weight = float(entry["weight"])
            except (TypeError, ValueError):
                continue
            try:
                per = float(entry.get("per", 1))
            except (TypeError, ValueError):
                per = 1.0
            if per <= 0:
                per = 1.0
            for key in [entry.get("name"), *(entry.get("aliases") or [])]:
                norm = normalize(key)
                if norm:
                    weights[norm] = weight
                    bundles[norm] = per
        _WEIGHTS, _BUNDLES = weights, bundles
    return _WEIGHTS, _BUNDLES


def load_weights() -> dict:
    """{normalized name/alias: weight in lb} from config/weights.yml (cached)."""
    return _catalog()[0]


def _lookup_norm(name, base=None) -> str | None:
    """Normalized catalog key for a name: its declared `base`, magic variant, singular."""
    table = load_weights()
    for candidate in (base, name):
        if not candidate:
            continue
        norm = normalize(candidate)
        if norm in table:
            return norm
        stripped = _MAGIC_PREFIX.sub("", norm)
        if stripped != norm and stripped in table:
            return stripped
        # The Forge routes quantities to `consumables` with plural names ("Two Daggers"
        # -> consumables.Daggers); fold a trailing plural onto the singular catalog key.
        if norm.endswith("s") and norm[:-1] in table:
            return norm[:-1]
    return None


def weight_for(name, declared=None, base=None) -> float | None:
    """Item weight in lb, or None when unknown. A declared weight always wins.

    Magic variants ("Magic Dagger (+1)") fall back to the base item's weight — a magic
    dagger still weighs a pound — as does a GM-declared `base` archetype; nothing else
    is guessed.
    """
    if declared is not None:
        try:
            return float(declared)
        except (TypeError, ValueError):
            pass
    norm = _lookup_norm(name, base)
    if norm is None:
        return None
    return load_weights()[norm]


def bundle_size_for(name) -> float:
    """Units one catalog weight covers (SRD "Arrows (20)" -> 20); 1 when unknown."""
    norm = _lookup_norm(name)
    if norm is None:
        return 1.0
    return _catalog()[1].get(norm, 1.0)


def unit_weight_for(name) -> float | None:
    """Weight of ONE unit; a catalog bundle line is divided by its bundle size.

    Used for the `consumables` map, whose count is a number of units (the Forge
    stores "20 Arrows" as {"Arrows": 20}), never a number of bundles.
    """
    weight = weight_for(name)
    if weight is None:
        return None
    return weight / bundle_size_for(name)


def size_for_race(race) -> str:
    """Size category from config/races.yml (Medium when unknown)."""
    global _RACES
    if _RACES is None:
        table: dict[str, str] = {}
        for entry in _config("races.yml"):
            if isinstance(entry, dict) and entry.get("name"):
                table[normalize(entry["name"])] = str(entry.get("size") or "Medium")
        _RACES = table
    return _RACES.get(normalize(race), "Medium")


def size_multiplier(size, extra=1) -> float:
    """Capacity multiplier from the size category times a temporary multiplier."""
    base = SIZE_MULTIPLIERS.get(str(size or "").strip().lower(), 1.0)
    try:
        extra = float(extra)
    except (TypeError, ValueError):
        extra = 1.0
    return base * (extra if extra > 0 else 1.0)


def _entry(entry):
    """(name, declared weight, declared base) for one inventory entry."""
    if isinstance(entry, dict):
        return (str(entry.get("name") or entry.get("item") or ""), entry.get("weight"),
                entry.get("base"))
    return str(entry or ""), None, None


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def carry_state(get) -> dict:
    """The carry block for a character.

    ``get(key, default)`` reads one player-DB value (already JSON-decoded), which
    lets both the engine's ``_db_val`` and the sheet's reader share this function.
    """
    stats = get("stats", {}) or {}
    if not isinstance(stats, dict):
        stats = {}
    strength = _int(stats.get("str", 10), 10)

    size = size_for_race(get("race", ""))
    multiplier = size_multiplier(size, get("capacity_multiplier", 1))

    items: list[dict] = []
    unweighed: list[str] = []
    carried = 0.0

    for entry in (get("inventory", []) or []):
        name, declared, base = _entry(entry)
        if not name:
            continue
        weight = weight_for(name, declared, base)
        if weight is None:
            unweighed.append(name)
            items.append({"name": name, "weight": None, "count": 1, "source": "unknown"})
            continue
        carried += weight
        items.append({"name": name, "weight": weight, "count": 1,
                      "source": "declared" if declared is not None else "catalog"})

    consumables = get("consumables", {}) or {}
    if isinstance(consumables, dict):
        for name, count in consumables.items():
            count = _int(count, 0)
            if count <= 0:
                continue
            weight = unit_weight_for(name)
            if weight is None:
                unweighed.append(str(name))
                items.append({"name": str(name), "weight": None, "count": count, "source": "unknown"})
                continue
            carried += weight * count
            items.append({"name": str(name), "weight": weight, "count": count, "source": "catalog"})

    gold = _int(get("gold", 0), 0)
    coin_weight = round(gold / COINS_PER_POUND, 2)
    carried += coin_weight

    capacity = round(strength * 15 * multiplier, 2)
    thresholds = {"encumbered": round(strength * 5 * multiplier, 2),
                  "heavily_encumbered": round(strength * 10 * multiplier, 2)}

    status, penalty = "unencumbered", 0
    if carried > thresholds["heavily_encumbered"]:
        status, penalty = "heavily_encumbered", 20
    elif carried > thresholds["encumbered"]:
        status, penalty = "encumbered", 10

    speed = get("speed", None)
    effective_speed = None
    if isinstance(speed, (int, float)) or (isinstance(speed, str) and speed.strip().isdigit()):
        effective_speed = max(0, _int(speed, 0) - penalty)

    return {
        "carried": round(carried, 2),
        "capacity": capacity,
        "push_drag_lift": round(capacity * 2, 2),
        "thresholds": thresholds,
        "status": status,
        "speed_penalty": penalty,
        "speed": effective_speed,
        "size": size,
        "capacity_multiplier": multiplier,
        "gold": gold,
        "coin_weight": coin_weight,
        "items": items,
        "unweighed": unweighed,
        "heavy_encumbered": status == "heavily_encumbered",
    }


def heavy_encumbrance(get, ability=None) -> tuple[bool, list[str]]:
    """(disadvantage?, sources) for an actor's ability check / save.

    ``ability`` is an optional ability name ("str", "strength", ...). The
    SRD variant only hits Strength, Dexterity and Constitution; pass None for
    attack rolls (always affected when heavily encumbered).
    """
    state = carry_state(get)
    if not state["heavy_encumbered"]:
        return False, []
    if ability:
        key = str(ability).strip().lower()[:3]
        if key not in ("str", "dex", "con"):
            return False, []
    return True, ["heavily encumbered"]
