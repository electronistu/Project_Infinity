"""SRD 5.1 conformance: the whitelist, the gate, and the 2026-10-08 audit's findings.

Asserts the audit's *result*, not just the gate's opinion: the spell count, the
Product-Identity names, the non-SRD instruments and the icon library are checked
directly, so a green gate cannot be achieved by weakening the whitelist alone.

No network, no GPU.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_srd.py
"""

import importlib.util
import json
import subprocess
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.icons import _norm as icon_norm, icon_key_for  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _load_gate():
    spec = importlib.util.spec_from_file_location("srd_gate", REPO / "tools" / "srd_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE = _load_gate()
WHITELIST = json.loads((REPO / "tools" / "srd" / "srd-5.1.json").read_text(encoding="utf-8"))
TABLES = GATE.Lists(REPO / "tools" / "srd" / "srd-5.1.json")

# Spot-checks of the names the 2026-10-08 audit found. Each must now be absent from
# the shipped catalogs (the non-SRD spells and the Product-Identity spell names).
NON_SRD = ["Hex", "Armor of Agathys", "Arms of Hadar", "Witch Bolt", "Blade Ward", "Friends",
           "Thorn Whip", "Abi-Dalzim's Horrid Wilting", "Bones of the Earth", "Whirlwind",
           "Skywrite", "Aura of Vitality", "Steel Wind Strike", "Psychic Scream",
           "Snilloc's Snowball Swarm", "Investiture of Flame", "Druid Grove", "Arcane Gate"]
PI_NAMES = ["Bigby's Hand", "Mordenkainen's Sword", "Evard's Black Tentacles",
            "Mordenkainen's Faithful Hound", "Otiluke's Freezing Sphere",
            "Tasha's Hideous Laughter", "Drawmij's Instant Summons", "Otto's Irresistible Dance",
            "Mordenkainen's Private Sanctum", "Otiluke's Resilient Sphere",
            "Leomund's Secret Chest", "Leomund's Tiny Hut", "Telepathy"]
NON_SRD_INSTRUMENTS = ["Birdpipes", "Glaur", "Hand Drum", "Longhorn", "Songhorn", "Tantan",
                       "Thelarr", "Tocken", "Warhorn", "Zulkoon"]
RENAMED = {"Hideous Laughter": "hideous-laughter", "Arcane Hand": "arcane-hand",
           "Arcane Sword": "arcane-sword", "Black Tentacles": "black-tentacles",
           "Faithful Hound": "faithful-hound", "Freezing Sphere": "freezing-sphere",
           "Instant Summons": "instant-summons", "Irresistible Dance": "irresistible-dance",
           "Private Sanctum": "private-sanctum", "Resilient Sphere": "resilient-sphere",
           "Secret Chest": "secret-chest", "Tiny Hut": "tiny-hut",
           "Telepathic Bond": "telepathic-bond"}


def _text(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def main() -> bool:
    # ── the whitelist ──────────────────────────────────────────────────────────
    rec("whitelist exists", (REPO / "tools" / "srd" / "srd-5.1.json").exists())
    meta = WHITELIST.get("_meta") or {}
    rec("whitelist carries its provenance", all(
        meta.get(k) for k in ("document", "license", "attribution", "audited", "method")))
    rec("whitelist is per-list attributed", isinstance(meta.get("basis"), dict)
        and set(meta["basis"]) >= {"spells", "classes", "races", "tools", "original"})
    basis = meta.get("basis") or {}
    rec("the load-bearing lists are source-verified", all(
        basis.get(k) == "source" for k in ("spells", "classes", "races", "conditions", "magic_items")))
    sizes = TABLES.sizes()
    rec("whitelist holds the 319 SRD spells", sizes["spells"] == 319, str(sizes["spells"]))
    rec("whitelist holds the 12 SRD classes", sizes["classes"] == 12)
    rec("whitelist holds the 9 SRD races", sizes["races"] == 9)
    rec("whitelist holds the 15 SRD conditions", sizes["conditions"] == 15)
    rec("whitelist holds the 13 SRD damage types", sizes["damage_types"] == 13)
    rec("whitelist holds the 18 SRD skills", sizes["skills"] == 18)
    rec("whitelist holds the 10 SRD instruments", all(
        GATE.norm(i) in TABLES.sets["tools"] for i in
        ["Bagpipes", "Drum", "Dulcimer", "Flute", "Horn", "Lute", "Lyre", "Pan Flute", "Shawm", "Viol"]))
    rec("whitelist refuses the Forgotten Realms instruments",
        not any(GATE.norm(i) in TABLES.sets["tools"] for i in NON_SRD_INSTRUMENTS))
    rec("whitelist records the SRD's spell renames", len(TABLES.aliases) == 13)
    rec("the gate's normaliser matches the icon service",
        all(GATE.norm(s) == icon_norm(s) for s in
            ["Bigby's Hand", "Vellin Cray's Signet Ring (+1)", "Leather Tunic, Longbow, 20 Arrows",
             "Antipathy/Sympathy", "Evocation Savant (SRD)", "Moonstone (2)", "  Dagger "]))

    # ── the gate itself ────────────────────────────────────────────────────────
    rep = GATE.run(TABLES)
    rec("the gate scans the whole surface", rep.checked > 1700, f"{rep.checked} names")
    rec("no shipped name is outside SRD 5.1", not rep.violations,
        "; ".join(f"{w}:{n}" for w, _, n in rep.violations[:5]))
    proc = subprocess.run([sys.executable, str(REPO / "tools" / "srd_gate.py")],
                          cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    rec("`tools/srd_gate.py` exits 0", proc.returncode == 0, proc.stdout.strip().splitlines()[-1])

    # ── the catalogs ───────────────────────────────────────────────────────────
    spells_src = _text("config/spells.yml")
    components_src = _text("config/components.yml")
    import yaml

    spells = yaml.safe_load(spells_src)
    components = yaml.safe_load(components_src)
    rec("spells.yml is exactly the 319 SRD spells", len(spells) == 319, str(len(spells)))
    rec("components.yml covers the same 319 spells", len(components) == 319, str(len(components)))
    rec("no non-SRD spell is shipped", not any(s["name"] in NON_SRD for s in spells))
    rec("no Product-Identity spell name is shipped",
        not any(n in spells_src or n in components_src or n in _text("forge/class_spells.py")
                for n in PI_NAMES))
    rec("the SRD spell names replaced them",
        all(any(s["name"] == n for s in spells) for n in RENAMED))
    rec("the renamed spells kept their components",
        all(any(c["name"] == n for c in components) for n in RENAMED))

    # ── the Forge ──────────────────────────────────────────────────────────────
    import ast

    creator = ast.parse(_text("forge/character_creator.py"))
    instruments = next(n for n in creator.body if isinstance(n, ast.Assign)
                       and getattr(n.targets[0], "id", "") == "MUSICAL_INSTRUMENTS")
    names = [e.value for e in instruments.value.elts]
    rec("the Forge offers only SRD instruments", len(names) == 10 and "Viol" in names, str(names))
    rec("the Forge dropped the Forgotten Realms instruments",
        not any(i in names for i in NON_SRD_INSTRUMENTS))
    class_spells_src = _text("forge/class_spells.py")
    rec("the class lists offer no non-SRD spell",
        not any(f'"{n}"' in class_spells_src for n in NON_SRD))

    # ── the icon library ───────────────────────────────────────────────────────
    manifest = json.loads((REPO / "assets" / "gemini" / "manifest.json").read_text(encoding="utf-8"))
    icons = manifest["icons"]
    rec("every committed spell icon is an SRD spell", all(
        GATE.norm(rec_["name"]) in TABLES.sets["spells"]
        for key, rec_ in icons.items() if key.startswith("spell/")))
    rec("one icon per SRD spell", sum(1 for k in icons if k.startswith("spell/")) == 319)
    rec("the Zulkoon icon is gone", "tool/zulkoon" not in icons
        and not (REPO / "assets" / "gemini" / "tool" / "zulkoon.png").exists())
    rec("the renamed icons exist under their SRD slugs", all(
        f"spell/{slug}" in icons for slug in RENAMED.values()))
    rec("the Product-Identity slugs are gone", not any(
        k.split("/", 1)[1] in {"bigby-hand", "tasha-hideous-laughter", "telepathy",
                               "leomund-tiny-hut", "snilloc-snowball-swarm"}
        for k in icons if k.startswith("spell/")))
    rec("the icon service resolves a renamed spell",
        icon_key_for("spell", "Hideous Laughter") == "spell/hideous-laughter")

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
