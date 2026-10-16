"""Spell catalogue loading: the YAML spell list and per-spell components.

Both tables are cached on `rules.state` (`_SPELLS_DB`, `_SPELL_COMPONENTS`).
"""

import os

import carrying

from . import state

try:
    import yaml
except ImportError:
    yaml = None

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_spells() -> dict:
    if state._SPELLS_DB is not None:
        return state._SPELLS_DB
    if yaml is None:
        state._SPELLS_DB = {}
        return state._SPELLS_DB
    config_path = os.path.join(_REPO_ROOT, "config", "spells.yml")
    if not os.path.exists(config_path):
        state._SPELLS_DB = {}
        return state._SPELLS_DB
    with open(config_path, "r", encoding="utf-8") as f:
        spells_list = yaml.safe_load(f)
    if spells_list is None:
        state._SPELLS_DB = {}
    else:
        state._SPELLS_DB = {}
        for s in spells_list:
            state._SPELLS_DB[s["name"].lower()] = s
    return state._SPELLS_DB


def _spell_components() -> dict:
    """{normalized spell name: 'VSM'} from config/components.yml (cached).

    The SRD 5.1 components per spell. A spell missing from the table is treated as needing a
    hand (V,S,M) and the Game Master can override per cast with components='...'.
    """
    if state._SPELL_COMPONENTS is None:
        table = {}
        try:
            path = os.path.join(_REPO_ROOT,
                                "config", "components.yml")
            if yaml is not None and os.path.exists(path):
                with open(path, encoding="utf-8") as handle:
                    for entry in yaml.safe_load(handle) or []:
                        if isinstance(entry, dict) and entry.get("name"):
                            table[carrying.normalize(entry["name"])] = \
                                str(entry.get("components") or "").upper()
        except Exception:  # noqa: BLE001 - a missing/!broken catalog means "no components known"
            table = {}
        state._SPELL_COMPONENTS = table
    return state._SPELL_COMPONENTS
