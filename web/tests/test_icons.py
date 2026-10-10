"""Shared icon library contract: key matching, store layout, cache. No network.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_icons.py
"""

import json
import io
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from PIL import Image  # noqa: E402

from web.icons import (  # noqa: E402
    IconService, all_icon_keys, family_for_model, icon_key_for, slugify_key, _config,
)
from web.images import fit_to_frame  # noqa: E402

RESULTS = []
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32  # opaque bytes; Pillow will refuse to resize it


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def main() -> bool:
    rec("slug strips possessives/quantities", slugify_key("Vellin Cray's Signet Ring (+1)") == "vellin-cray-signet-ring")

    rec("weapon proficiency canonicalises", icon_key_for("weapon", "Light crossbows") == "weapon/light-crossbow")

    # Inventory/consumables: exact match to the shared catalog, else per-item.
    rec("plain weapon reuses the weapon icon", icon_key_for("inventory", "Dagger") == "weapon/dagger")
    rec("start-of-name quantity tolerated", icon_key_for("inventory", "Two Daggers") == "weapon/dagger")
    rec("armour reuses the armour icon", icon_key_for("inventory", "Shield") == "armor/shield")
    rec("tool reuses the tool icon", icon_key_for("inventory", "Thieves' Tools") == "tool/thieves-tools")
    rec("shared catalog item", icon_key_for("inventory", "Spellbook") == "item/spellbook")
    rec("alias maps to the catalog key", icon_key_for("inventory", "Crowbar") == "item/crowbar")
    rec("magic weapon gets its own key", icon_key_for("inventory", "Magic Dagger (+1)") == "item/magic-dagger")
    rec("potion of healing is shared", icon_key_for("consumable", "Potion of Healing") == "item/potion-of-healing")
    rec("other potions are per-item", icon_key_for("consumable", "Potion of Invisibility") == "item/potion-of-invisibility")
    rec("unique quest item is per-item", icon_key_for("inventory", "Nab's Knife") == "item/nab-knife")

    rec("spell maps", icon_key_for("spell", "Magic Missile") == "spell/magic-missile")
    rec("unknown spell -> per-item key", icon_key_for("spell", "Homebrew Zap") == "spell/homebrew-zap")
    rec("feature maps", icon_key_for("feature", "Darkvision") == "feature/darkvision")
    rec("unknown feature -> per-item key", icon_key_for("feature", "Nab's Curse") == "feature/nab-curse")
    rec("class maps", icon_key_for("class", "Wizard") == "class/wizard")
    rec("skill maps", icon_key_for("skill", "Sleight of Hand") == "skill/sleight-of-hand")
    rec("tool maps", icon_key_for("tool", "Thieves' tools") == "tool/thieves-tools")
    rec("language maps", icon_key_for("language", "Elvish") == "language/elvish")
    rec("kingdom slug", icon_key_for("kingdom", "Blacksail Archipelago") == "kingdom/blacksail-archipelago")
    rec("faction maps", icon_key_for("faction", "Alchemists League") == "faction/alchemists-league")
    rec("custom faction gets a per-item key",
        icon_key_for("faction", "Cult of the Dragon") == "faction/cult-of-the-dragon")
    rec("ability maps", icon_key_for("ability", "Intelligence") == "ability/intelligence")
    rec("save maps", icon_key_for("save", "Strength") == "save/strength")
    # Categories and generic tools (previously text-only).
    rec("all-armor category maps", icon_key_for("armor", "All armor") == "armor/all-armor")
    rec("light-armor category maps", icon_key_for("armor", "Light armor") == "armor/light-armor")
    rec("medium-armor category maps", icon_key_for("armor", "Medium armor") == "armor/medium-armor")
    rec("simple-weapons category maps", icon_key_for("weapon", "Simple weapons") == "weapon/simple-weapons")
    rec("martial-weapons category maps", icon_key_for("weapon", "Martial weapons") == "weapon/martial-weapons")
    rec("vehicles (land) maps", icon_key_for("tool", "Vehicles (land)") == "tool/vehicles-land")
    rec("vehicles (water) maps", icon_key_for("tool", "Vehicles (water)") == "tool/vehicles-water")
    rec("generic gaming set maps", icon_key_for("tool", "One type of gaming set") == "tool/gaming-set")
    rec("generic artisan's tools maps",
        icon_key_for("tool", "One type of artisan's tools") == "tool/artisan-tools")
    rec("choice prose stays text",
        icon_key_for("language", "Two of your choice") is None
        and icon_key_for("tool", "Any tool") is None)

    # Audit: every concrete config-derived proficiency maps to an icon key.
    unmapped = []
    for cls in _config("classes.yml"):
        if not isinstance(cls, dict):
            continue
        for field, cat in (("armor_proficiencies", "armor"),
                           ("weapon_proficiencies", "weapon"),
                           ("tool_proficiencies", "tool")):
            for value in cls.get(field) or []:
                if icon_key_for(cat, value) is None:
                    unmapped.append((cat, value))
    for bg in _config("backgrounds.yml"):
        if isinstance(bg, dict):
            for value in bg.get("tool_proficiencies") or []:
                if icon_key_for("tool", value) is None:
                    unmapped.append(("tool", value))
    rec("config proficiencies all map (audit)", not unmapped, str(unmapped))
    rec("choice-prose languages are the only intentional blanks",
        icon_key_for("language", "One extra language of your choice") is None)

    catalog = all_icon_keys()
    rec("catalog is a broad shared vocabulary", len(catalog) > 600, f"{len(catalog)} keys")
    rec("catalog excludes per-character art", not any(k.startswith("portrait") for k in catalog))
    rec("catalog includes shared potion of healing", "item/potion-of-healing" in catalog)
    rec("catalog includes starting gear", "item/spellbook" in catalog and "item/crowbar" in catalog)
    rec("catalog includes stat glyphs", "stat/level" in catalog and "stat/hp" in catalog)
    rec("catalog includes ability/save", "ability/strength" in catalog and "save/charisma" in catalog)
    rec("catalog includes the new category icons",
        all(k in catalog for k in ("armor/all-armor", "armor/light-armor",
                                   "weapon/simple-weapons", "weapon/martial-weapons",
                                   "tool/vehicles-land", "tool/vehicles-water",
                                   "tool/gaming-set", "tool/artisan-tools")))

    with tempfile.TemporaryDirectory() as td:
        # One store per IMAGE-MODEL family: assets/{family}/{kind}/{slug}.png (D32).
        svc = IconService(td)  # default family = gemini
        svc.generate = lambda prompt, model=None, **_: _PNG  # type: ignore[assignment]

        r1 = svc.ensure("weapon", "dagger", "Dagger")
        rec("icon generated", r1["generated"] is True and svc.has("weapon", "dagger"))
        rec("icon path convention",
            svc.icon_path("weapon", "dagger") == Path(td) / "gemini" / "weapon" / "dagger.png")
        rec("manifest written", svc.manifest_path() == Path(td) / "gemini" / "manifest.json"
            and svc.manifest_path().exists())
        manifest = json.loads(svc.manifest_path().read_text(encoding="utf-8"))
        rec("manifest records the key", "weapon/dagger" in (manifest.get("icons") or {}))
        rec("manifest records the family",
            (manifest.get("icons") or {}).get("weapon/dagger", {}).get("family") == "gemini")
        rec("index exposes the icon", "weapon/dagger" in svc.index())
        rec("url names the family", r1["url"] == "/api/icons/gemini/weapon/dagger")
        rec("index urls are extensionless",
            all("." not in u.rsplit("/", 1)[-1] for u in svc.index().values()))

        r2 = svc.ensure("weapon", "dagger", "Dagger")
        rec("second ensure is cached", r2["generated"] is False and r2["cached"] is True)

        # The icon-model override participates in the cache key.
        r_m1 = svc.ensure("weapon", "mace", "Mace", model="gemini-3-pro-image")
        rec("icon model override is recorded", r_m1["generated"] is True and r_m1["model"] == "gemini-3-pro-image")
        r_m2 = svc.ensure("weapon", "mace", "Mace")
        rec("switching the icon model regenerates", r_m2["generated"] is True and r_m2["model"] != "gemini-3-pro-image")
        r_m3 = svc.ensure("weapon", "mace", "Mace")
        rec("same icon model is cached again", r_m3["cached"] is True)

        r3 = svc.ensure("Weapon", "Light Crossbow", "Light Crossbow")
        rec("kind/slug are slugified on ensure", r3["key"] == "weapon/light-crossbow")

        r4 = svc.ensure("item", "crossbow-bolts", "Crossbow Bolts")
        rec("per-item (non-catalog) key generates", r4["generated"] is True and r4["key"] == "item/crossbow-bolts")
        rec("per-item icon is reusable across characters", svc.has("item", "crossbow-bolts"))

        # ── D32: per-family stores never mix ──────────────────────────────
        loc = IconService(td, "local")
        loc.generate = lambda prompt, model=None, **_: _PNG  # type: ignore[assignment]
        rec("a family gets its own directory",
            loc.icon_dir("weapon") == Path(td) / "local" / "weapon"
            and loc.manifest_path() == Path(td) / "local" / "manifest.json")
        rec("a family does not see another family's icon",
            loc.has("weapon", "dagger") is False and "weapon/dagger" not in loc.index())
        r_loc = loc.ensure("weapon", "dagger", "Dagger")
        rec("the same key can exist in both families",
            r_loc["generated"] is True and r_loc["url"] == "/api/icons/local/weapon/dagger"
            and svc.has("weapon", "dagger") and loc.has("weapon", "dagger"))
        rec("a local bake cannot overwrite the gemini file",
            (Path(td) / "gemini" / "weapon" / "dagger.png").exists()
            and (Path(td) / "local" / "weapon" / "dagger.png").exists())
        loc_manifest = json.loads(loc.manifest_path().read_text(encoding="utf-8"))
        rec("each family records its own name in the manifest",
            (loc_manifest.get("icons") or {}).get("weapon/dagger", {}).get("family") == "local")
        rec("the index is scoped to one family",
            "weapon/dagger" in svc.index() and "weapon/dagger" in loc.index()
            and set(svc.index()) == {"weapon/dagger", "weapon/mace", "weapon/light-crossbow",
                                    "item/crossbow-bolts"})
        rec("an odd family name is made safe",
            IconService(td, "../ evil").family == "evil"
            and IconService(td, "").family == "gemini")
        rec("the family is derived from the image model",
            family_for_model("gemini-3.1-flash-lite-image") == "gemini"
            and family_for_model("local/sdxl-lightning-4step") == "local"
            and family_for_model(None) == "gemini"
            and family_for_model("flux-schnell") == "flux")

    # Auto-fit: a small dark square on a light ground should fill ~92% of the frame.
    src = Image.new("RGB", (128, 128), (240, 235, 220))
    for x in range(48, 80):
        for y in range(48, 80):
            src.putpixel((x, y), (40, 30, 20))
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    fitted = fit_to_frame(buf.getvalue(), 0.92)
    out = Image.open(io.BytesIO(fitted)).convert("RGB")
    xs, ys = [], []
    for y in range(out.height):
        for x in range(out.width):
            r, g, b = out.getpixel((x, y))
            if r + g + b < 300:
                xs.append(x); ys.append(y)
    side = max(max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) if xs else 0
    rec("fit_to_frame fills the frame", 108 <= side <= 122, f"side={side}/128")
    rec("fit_to_frame passes through empty input", fit_to_frame(b"", 0.92) == b"")
    fitted_bg = fit_to_frame(buf.getvalue(), 0.92, background="#1E1A13", out_format="PNG")
    flat = Image.open(io.BytesIO(fitted_bg)).convert("RGB")
    rec("fit_to_frame flattens background to the exact colour", flat.getpixel((0, 0)) == (30, 26, 19),
        str(flat.getpixel((0, 0))))

    # ── the frozen Gemini recipe must match the committed assets ────────────
    gemini_manifest = REPO / "assets" / "gemini" / "manifest.json"
    if gemini_manifest.exists():
        committed = json.loads(gemini_manifest.read_text(encoding="utf-8")).get("icons") or {}
        gem = IconService(REPO / "assets", "gemini")
        loc = IconService(REPO / "assets", "local")
        rec("the gemini family resolves its own frozen style",
            "Flat codex emblem" in gem.icon_style, gem.icon_style[:60])
        rec("the local family resolves a different style",
            bool(loc.icon_style) and loc.icon_style != gem.icon_style)
        rec("every committed gemini prompt carries the frozen style",
            bool(committed) and all(str(v.get("prompt", "")).endswith(gem.icon_style)
                                    for v in committed.values()))
        reproduced = [k for k, v in committed.items()
                      if gem.prompt_for(v["kind"], v["name"]) == v["prompt"]]
        rec("the frozen recipe reproduces committed prompts byte for byte",
            len(reproduced) >= 100, f"{len(reproduced)}/{len(committed)} detail-free keys")
        v = committed.get("weapon/dagger")
        rec("the frozen recipe reproduces the stored source_hash",
            v is not None and gem._source_hash("weapon/dagger", v["prompt"],
                                               model=v.get("model")) == v.get("source_hash"))

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
