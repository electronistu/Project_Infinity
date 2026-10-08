"""The SRD 5.1 conformance gate.

Every *name* this repository ships must be a name that exists in SRD 5.1. This gate
proves it mechanically and fails the build when it stops being true.

What it checks -- names, never prose:

  * ``config/spells.yml`` and ``config/components.yml`` -- every spell name
  * ``config/classes.yml``   -- class names, class/fighting-style feature names
  * ``config/races.yml``     -- race, subrace and trait names
  * ``config/backgrounds.yml`` -- background names and their features
  * ``config/weapons.yml`` / ``config/armor.yml`` / ``config/items.yml``
  * ``config/alignments.yml``
  * ``forge/class_spells.py`` -- every spell literal the Forge offers
  * ``forge/character_creator.py`` -- the instrument / gaming-set / artisan-tool lists
  * ``assets/<family>/manifest.json`` -- every committed icon's ``name``, per kind

What it cannot check: the prose inside a description, and whatever the running GM
invents. Those are disclosed in the README; this gate is about what the repository
*ships*.

The whitelist is ``tools/srd/srd-5.1.json`` -- names only, with its provenance in
``_meta``. ``spell_aliases`` holds the names SRD 5.1 deliberately renames to avoid
Product Identity (``Bigby's Hand`` -> ``Arcane Hand``); those PHB spellings are
accepted **only** as aliases, never as the shipped name. ``original`` is the explicit
allow-list of names that are neither SRD nor third-party -- a reviewed decision.

Run:

    venv\\Scripts\\python.exe tools\\srd_gate.py
    venv\\Scripts\\python.exe tools\\srd_gate.py --json

Exit codes: 0 clean, 1 a non-SRD name is shipped, 2 the whitelist is missing/invalid.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WHITELIST = REPO / "tools" / "srd" / "srd-5.1.json"

_PARENS = re.compile(r"\([^)]*\)")
_POSSESSIVE = re.compile(r"['\u2019]s\b")
_NUMBER = re.compile(r"\b[+-]?\d+\b")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def norm(value) -> str:
    """Normalise a name the way the icon service does (mirrors web/icons.py::_norm)."""
    text = _PARENS.sub(" ", str(value or "").lower())
    text = _POSSESSIVE.sub(" ", text)
    text = _NUMBER.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


class Lists:
    """The whitelist, as normalised sets."""

    def __init__(self, path: Path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "_meta" not in raw:
            raise ValueError("whitelist has no _meta block")
        self.meta = raw["_meta"]
        self._raw = {k: v for k, v in raw.items() if k != "_meta"}
        self.sets: dict[str, set[str]] = {
            k: {norm(x) for x in v} for k, v in self._raw.items() if isinstance(v, list)
        }
        self.aliases = {norm(k): norm(v) for k, v in (raw.get("spell_aliases") or {}).items()}

    def spells_ok(self) -> set[str]:
        return self.sets["spells"] | set(self.aliases)

    def union(self, *names: str) -> set[str]:
        out: set[str] = set()
        for n in names:
            out |= self.sets.get(n, set())
        return out

    def sizes(self) -> dict[str, int]:
        return {k: len(v) for k, v in sorted(self.sets.items())}


def _yaml(path: Path):
    import yaml  # imported lazily so --help works without the dep

    return yaml.safe_load(path.read_text(encoding="utf-8"))


class Report:
    def __init__(self) -> None:
        self.violations: list[tuple[str, str, str]] = []
        self.checked = 0

    def check(self, where: str, name: str, allowed: set[str]) -> None:
        self.checked += 1
        if norm(name) not in allowed:
            self.violations.append((where, "name", str(name)))

    def check_all(self, where: str, names, allowed: set[str]) -> None:
        for n in names or []:
            self.check(where, n, allowed)


def run(lists: Lists) -> Report:
    rep = Report()
    spells = lists.spells_ok()

    # ── config ────────────────────────────────────────────────────────────────
    for row in _yaml(REPO / "config" / "spells.yml"):
        rep.check("config/spells.yml", row["name"], spells)
    for row in _yaml(REPO / "config" / "components.yml"):
        rep.check("config/components.yml", row["name"], spells)

    for c in _yaml(REPO / "config" / "classes.yml"):
        rep.check("config/classes.yml", c["name"], lists.sets["classes"])
        for key in ("features", "fighting_styles"):
            rep.check_all("config/classes.yml", [f["name"] for f in c.get(key) or []],
                          lists.sets["class_features"])

    for r in _yaml(REPO / "config" / "races.yml"):
        rep.check("config/races.yml", r["name"], lists.sets["races"])
        rep.check_all("config/races.yml", [t["name"] for t in r.get("traits") or []],
                      lists.sets["race_traits"])
        for s in r.get("subraces") or []:
            rep.check("config/races.yml", s["name"], lists.sets["subraces"])
            rep.check_all("config/races.yml", [t["name"] for t in s.get("traits") or []],
                          lists.sets["race_traits"])

    for b in _yaml(REPO / "config" / "backgrounds.yml"):
        rep.check("config/backgrounds.yml", b["name"], lists.sets["backgrounds"])
        feat = b.get("feature") or {}
        if isinstance(feat, dict) and feat.get("name"):
            rep.check("config/backgrounds.yml", feat["name"], lists.sets["background_features"])

    rep.check_all("config/weapons.yml", [w["name"] for w in _yaml(REPO / "config" / "weapons.yml")],
                  lists.sets["weapons"])
    rep.check_all("config/armor.yml", [a["name"] for a in _yaml(REPO / "config" / "armor.yml")],
                  lists.sets["armor"])
    gear = lists.union("gear", "magic_items", "tools", "weapons", "armor", "weapon_categories",
                       "armor_categories")
    rep.check_all("config/items.yml", [i["name"] for i in _yaml(REPO / "config" / "items.yml")], gear)
    rep.check_all("config/alignments.yml", _yaml(REPO / "config" / "alignments.yml"),
                  lists.sets["alignments"])

    # ── the Forge's own literals ───────────────────────────────────────────────
    # (assignment, whitelist list, how to read it) -- the Forge holds its own copies of
    # SRD names, so it is checked as source too, not just the YAML catalogs.
    forge = {
        "forge/class_spells.py": [
            ("CLASS_CANTRIPS", "spells", "dict_values"),
            ("LEVEL_1_SPELLS", "spells", "dict_values"),
        ],
        "forge/character_creator.py": [
            ("MUSICAL_INSTRUMENTS", "tools", "list"),
            ("GAMING_SETS", "tools", "list"),
            ("ARTISAN_TOOLS", "tools", "list"),
            ("ALL_SKILLS", "skills", "dict_keys"),
            ("ALL_SAVES", "abilities", "dict_keys"),
        ],
    }
    for path, specs in forge.items():
        src = (REPO / path).read_text(encoding="utf-8")
        for name, listname, mode in specs:
            rep.check_all(path, _assignment_strings(src, name, mode), lists.sets[listname])

    # ── the committed icon library, per kind ────────────────────────────────────
    per_kind = {
        "spell": spells,
        "weapon": lists.union("weapons", "weapon_categories", "magic_items"),
        "armor": lists.union("armor", "armor_categories", "magic_items"),
        "tool": lists.sets["tools"],
        "race": lists.sets["races"],
        "class": lists.sets["classes"],
        "background": lists.sets["backgrounds"],
        "language": lists.sets["languages"],
        "condition": lists.sets["conditions"],
        "damage": lists.sets["damage_types"],
        "school": lists.sets["schools"],
        "skill": lists.sets["skills"],
        "ability": lists.sets["abilities"],
        "save": lists.sets["abilities"],
        "alignment": lists.sets["alignments"],
        "feature": lists.union("class_features", "race_traits", "background_features"),
        "item": gear | lists.sets["original"],
    }
    skipped = {"kingdom", "faction", "stat"}
    for manifest in sorted((REPO / "assets").glob("*/manifest.json")):
        icons = (json.loads(manifest.read_text(encoding="utf-8")) or {}).get("icons") or {}
        for key, rec in icons.items():
            kind = key.split("/", 1)[0]
            if kind in skipped:
                continue
            where = f"assets/{manifest.parent.name}/manifest.json"
            if kind not in per_kind:
                rep.violations.append((where, "kind", kind))
                continue
            rep.check(where, rec.get("name") or key.split("/", 1)[1], per_kind[kind])
    return rep


def _assignment_strings(src: str, name: str, mode: str) -> list[str]:
    """The string literals of one module-level assignment.

    ``mode`` is ``list`` (the list's items), ``dict_keys`` or ``dict_values`` (the
    keys / the items of every list value of a dict of lists).
    """
    import ast

    for node in ast.parse(src).body:
        if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != name:
            continue
        if mode == "dict_keys" and isinstance(node.value, ast.Dict):
            return [k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if mode == "dict_values" and isinstance(node.value, ast.Dict):
            out: list[str] = []
            for v in node.value.values:
                out += _assignment_strings_values(v)
            return out
        return _assignment_strings_values(node.value)
    return []


def _assignment_strings_values(node) -> list[str]:
    import ast

    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out: list[str] = []
        for e in node.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                out.append(e.value)
            else:
                out += _assignment_strings_values(e)
        return out
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="SRD 5.1 conformance gate")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    args = ap.parse_args()

    if not WHITELIST.exists():
        print(f"srd gate: whitelist not found: {WHITELIST}", file=sys.stderr)
        return 2
    try:
        lists = Lists(WHITELIST)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"srd gate: cannot read the whitelist: {exc}", file=sys.stderr)
        return 2

    rep = run(lists)
    ok = not rep.violations
    if args.json:
        print(json.dumps({"ok": ok, "checked": rep.checked,
                          "violations": [{"where": w, "issue": i, "name": n}
                                         for w, i, n in rep.violations],
                          "whitelist": lists.sizes()}, indent=2))
        return 0 if ok else 1

    print(f"SRD 5.1 whitelist: tools/srd/srd-5.1.json "
          f"(audited {lists.meta.get('audited')}, {lists.meta.get('document')})")
    print("  " + "  ".join(f"{k}={v}" for k, v in lists.sizes().items()))
    print(f"  names checked: {rep.checked}")
    if ok:
        print("SRD GATE: GREEN -- every shipped name is an SRD 5.1 name")
        return 0
    print(f"SRD GATE: RED -- {len(rep.violations)} name(s) are not in SRD 5.1 or the allow-list:")
    for where, issue, name in rep.violations:
        print(f"  {where}: {issue} {name!r}")
    print("\nFix the name, or add it to tools/srd/srd-5.1.json deliberately "
          "(`spell_aliases` for an SRD rename, `original` for original content).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
