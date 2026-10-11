"""The Device and its five parts (Text Time Traveler) — the engine-owned vocabulary.

The Device is seeded with the character at creation; the five parts are the only Device
things the Game Master may add. This module is the single source of truth shared by
`dice_server.py` (the add guard + the age stamp), `web/engine.py` (the cadence and each
part's own power) and `web/stats.py` (the sheet card), so all three agree on names,
descriptions, order and powers.

`config/device.yml` holds the names, aliases and descriptions; `config/eras/*.yml`
supplies the display labels for the age a part was recovered in. **One part = one power**
(see `POWERS`): holding a part grants its power whatever else is held, and the count of
parts never unlocks an ability — it only sets how long the Device waits between the jumps
it chooses to make on its own.
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = REPO_ROOT / "config"
ERAS_DIR = CONFIG_DIR / "eras"

_DEFAULT = {
    "name": "The Device",
    "aliases": ["the Device", "Device", "The Horologe", "Horologe"],
    "description": "",
    "parts": [],
}

_CACHE: dict | None = None
_ERA_LABELS: dict[str, str] | None = None


def _read_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _load() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    data = dict(_DEFAULT)
    data.update(_read_yaml(CONFIG_DIR / "device.yml"))
    parts = []
    for entry in data.get("parts") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        parts.append({
            "name": name,
            "era": str(entry.get("era") or "").strip().lower(),
            "description": str(entry.get("description") or "").strip(),
        })
    data["parts"] = parts
    data["aliases"] = [str(a).strip() for a in (data.get("aliases") or []) if str(a).strip()]
    _CACHE = data
    return data


def _cfg() -> dict:
    return _load()


def device_name() -> str:
    return str(_cfg().get("name") or _DEFAULT["name"])


def device_description() -> str:
    return str(_cfg().get("description") or "")


def aliases() -> list[str]:
    return list(_cfg().get("aliases") or [])


def part_names() -> list[str]:
    """The five canonical part names, in config order."""
    return [p["name"] for p in _cfg().get("parts") or []]


def part_total() -> int:
    return len(part_names())


# ── one part = one power ──────────────────────────────────────────────────────
# The single source of truth for what each part grants. Holding a part grants its power
# whatever else is held; the count never unlocks an ability. Full control -- the era, the
# place AND when -- needs all five: the Mainspring arms the Device, the Escapement releases
# it, the Compass Rose gives the direction, the Regulator names the era, the Vernier the place.
CHARGE = "charge"        # Mainspring: arm the Device; its forced jumps stop
RELEASE = "release"      # Escapement: fire the held jump now
DIRECTION = "direction"  # Compass Rose: aim the previous era / a random era forward
ERA = "era"              # Regulator: name the era ahead ( + previous with the Compass Rose )
PLACE = "place"          # Vernier: the exact place, from the places already visited

POWERS = {
    "Mainspring": CHARGE,
    "Escapement": RELEASE,
    "Compass Rose": DIRECTION,
    "Regulator": ERA,
    "Vernier": PLACE,
}


def powers(inventory) -> dict[str, bool]:
    """Which powers the held parts grant: ``{ability id: held}``."""
    held = set(recovered_parts(inventory))
    out = {ability: False for ability in set(POWERS.values())}
    for part, ability in POWERS.items():
        if part in held:
            out[ability] = True
    return out


def has_part(inventory, part: str) -> bool:
    """True when a canonical part (case-insensitive) is in the inventory."""
    canonical = canonical_part(part) or str(part or "").strip()
    return bool(canonical) and canonical in recovered_parts(inventory)


def part_entry_data(name: str) -> dict | None:
    """The config row for a canonical part name (case-insensitive; a leading "the" is
    ignored, so "the Vernier" matches "Vernier"), or None."""
    wanted = str(name or "").strip().lower()
    if wanted.startswith("the "):
        wanted = wanted[4:].strip()
    for part in _cfg().get("parts") or []:
        if part["name"].lower() == wanted:
            return part
    return None


def part_description(name: str) -> str:
    """The engine description for a canonical part name (empty when unknown)."""
    part = part_entry_data(name)
    return str((part or {}).get("description") or "")


def is_device(name: str) -> bool:
    """True when a name is the Device itself (its canonical name or any alias)."""
    wanted = str(name or "").strip().lower()
    if not wanted:
        return False
    if wanted == device_name().lower():
        return True
    return wanted in {a.lower() for a in aliases()}


def canonical_part(name: str) -> str | None:
    """The canonical part name a name matches (case-insensitive), else None."""
    part = part_entry_data(name)
    return part["name"] if part else None


def is_device_item(name: str) -> bool:
    """The Device itself or one of its parts."""
    return is_device(name) or canonical_part(name) is not None


def era_label(era: str) -> str:
    """The display name of an age id (e.g. 'egypt' -> 'Egypt'), else the id itself."""
    global _ERA_LABELS
    if _ERA_LABELS is None:
        labels: dict[str, str] = {}
        if ERAS_DIR.is_dir():
            for path in sorted(ERAS_DIR.glob("*.yml")):
                meta = (_read_yaml(path) or {}).get("meta") or {}
                era_id = str(meta.get("id") or path.stem).strip().lower()
                labels[era_id] = str(meta.get("name") or era_id).strip()
        _ERA_LABELS = labels
    era_id = str(era or "").strip().lower()
    return _ERA_LABELS.get(era_id, era_id)


# ── inventory entries ─────────────────────────────────────────────────────

def device_entry() -> dict:
    """The Device as an inventory entry (engine-owned, weightless)."""
    return {"name": device_name(), "description": device_description(),
            "device": True, "weight": 0}


def part_entry(part: str, era: str) -> dict:
    """A canonical part as an inventory entry, stamped with the age it was recovered in."""
    canonical = canonical_part(part) or str(part).strip()
    data = part_entry_data(canonical) or {"description": ""}
    entry = {
        "name": canonical,
        "description": str(data.get("description") or "").strip(),
        "device_part": canonical,
        "weight": 0,
    }
    era_id = str(era or "").strip().lower()
    if era_id:
        entry["found_in"] = era_id
    return entry


def _entry_name(entry) -> str:
    return str(entry.get("name") or "") if isinstance(entry, dict) else str(entry)


def has_device(inventory) -> bool:
    """True when the Device itself is in the inventory."""
    return any(is_device(_entry_name(e)) for e in (inventory or []))


def recovered_parts(inventory) -> list[str]:
    """The canonical part names present in the inventory, in config order."""
    present = {_entry_name(e) for e in (inventory or [])}
    return [p for p in part_names() if p in present]


def entry_for_part(inventory, part: str):
    """The inventory entry for a part (canonical name), or None."""
    canonical = canonical_part(part) or str(part or "").strip()
    for entry in inventory or []:
        if isinstance(entry, dict) and str(entry.get("name") or "") == canonical:
            return entry
    return None
