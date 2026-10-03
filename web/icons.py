"""Shared icon library for the character sheet (cross-character, committed).

Icons are keyed off a *bounded canonical vocabulary* — fixed SRD lists, the
`config/*.yml` reference data, the fixed kingdom/faction set, and a keyword map
for generic gear (`config/items.yml`). One generated icon therefore serves every
character and every world, and lives under `assets/icons/` (tracked in git) with
a `manifest.json`.

`icon_key_for(category, name)` maps a sheet entry to `"{kind}/{slug}"` and is
the only place matching rules live; `stats.py` calls it, the UI renders whatever
exists. Unmatched entries return `None` and keep the plain text chip — unique
quest items deliberately get no shared icon.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .images import (  # noqa: E402
    GeminiImageBackend,
    ImageError,
    _load_config,
    downscale_image,
    fit_to_frame,
    image_mime,
    sniff_image,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_ASSETS_DIR = REPO_ROOT / "assets"

# ── fixed SRD / world vocabulary (common to every character) ──────────────

SKILLS = (
    "Acrobatics", "Animal Handling", "Arcana", "Athletics", "Deception", "History",
    "Insight", "Intimidation", "Investigation", "Medicine", "Nature", "Perception",
    "Performance", "Persuasion", "Religion", "Sleight of Hand", "Stealth", "Survival",
)
LANGUAGES = (
    "Common", "Dwarvish", "Elvish", "Giant", "Gnomish", "Goblin", "Halfling", "Orc",
    "Abyssal", "Celestial", "Draconic", "Deep Speech", "Infernal", "Primordial",
    "Sylvan", "Undercommon",
)
SCHOOLS = ("abjuration", "conjuration", "divination", "enchantment", "evocation",
           "illusion", "necromancy", "transmutation")
DAMAGE_TYPES = ("acid", "bludgeoning", "cold", "fire", "force", "lightning",
                "necrotic", "piercing", "poison", "psychic", "radiant", "slashing",
                "thunder")
CONDITIONS = ("blinded", "charmed", "deafened", "exhaustion", "frightened", "grappled",
              "incapacitated", "invisible", "paralyzed", "petrified", "poisoned",
              "prone", "restrained", "stunned", "unconscious")
ARMOR = (
    "Padded", "Leather Armor", "Studded Leather Armor", "Hide", "Chain Shirt",
    "Scale Mail", "Breastplate", "Half Plate", "Chain Mail", "Splint", "Plate",
    "Shield", "Wooden Shield",
    # categories (a proficiency says "Light armor", not a specific suit)
    "Light Armor", "Medium Armor", "Heavy Armor", "All Armor",
)
# Non-specific weapon proficiencies.
WEAPON_CATEGORIES = ("Simple Weapons", "Martial Weapons")
# Vehicles are handled explicitly because `_norm`/`slugify_key` strip the
# parenthetical, which would collapse land and water onto one key.
VEHICLE_TOOLS = {"land": "Vehicles (Land)", "water": "Vehicles (Water)"}
# Category keys get a representative subject (the generic per-kind template
# reads awkwardly for a category, e.g. "A single Simple Weapons").
_CATEGORY_PROMPTS = {
    "armor/light-armor": "A codex emblem representing light armor: a layered leather cuirass.",
    "armor/medium-armor": "A codex emblem representing medium armor: a chain shirt over padding.",
    "armor/heavy-armor": "A codex emblem representing heavy armor: a full steel plate harness.",
    "armor/all-armor": "A codex emblem for armor proficiency: a shield overlaying a breastplate.",
    "weapon/simple-weapons": "A codex emblem representing simple weapons: a club, a handaxe and a spear.",
    "weapon/martial-weapons": "A codex emblem representing martial weapons: a longsword crossed with a battleaxe.",
    "tool/gaming-set": "A codex emblem representing a gaming set: dice and playing cards.",
    "tool/artisan-tools": "A codex emblem representing artisan's tools: a hammer, a chisel and pliers.",
    "tool/vehicles-land": "A codex emblem representing land vehicles: a wooden cart wheel.",
    "tool/vehicles-water": "A codex emblem representing water vehicles: a ship's wheel and a sail.",
}
TOOLS = (
    "Thieves' Tools", "Herbalism Kit", "Disguise Kit", "Forgery Kit", "Poisoner's Kit",
    "Alchemist's Supplies", "Brewer's Supplies", "Calligrapher's Supplies",
    "Carpenter's Tools", "Cobbler's Tools", "Cook's Utensils", "Glassblower's Tools",
    "Jeweler's Tools", "Leatherworker's Tools", "Mason's Tools", "Painter's Supplies",
    "Potter's Tools", "Smith's Tools", "Tinker's Tools", "Weaver's Tools",
    "Woodcarver's Tools", "Bagpipes", "Drum", "Dulcimer", "Flute", "Horn", "Lute",
    "Lyre", "Pan Flute", "Shawm", "Viol", "Dice Set", "Dragonchess Set",
    "Playing Card Set", "Three-Dragon Ante Set",
    # generic tools (a proficiency may say "one type of gaming set")
    "Gaming Set", "Artisan's Tools",
)
FACTIONS = ("ruler", "guard", "mage", "assassin", "merchant", "thief",
            "alchemists league", "rangers conclave", "order of scribes")
ABILITIES = ("Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma")
SAVES = ABILITIES  # saving throws use the same six names
# Fixed stat glyphs (numeric UI values shown as icon + badge).
STAT_ICONS = {
    "level": "Level", "gold": "Gold", "xp": "Experience", "hp": "Hit Points",
    "ac": "Armor Class", "speed": "Speed", "proficiency": "Proficiency",
    "hit-dice": "Hit Dice", "temp-hp": "Temporary Hit Points",
    "save-dc": "Spell Save DC", "spell-attack": "Spell Attack",
}

_PARENS = re.compile(r"\([^)]*\)")
_POSSESSIVE = re.compile(r"['\u2019]s\b")
_NUMBER = re.compile(r"\b[+-]?\d+\b")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")

_CONFIG_CACHE: dict[str, object] = {}
_PORTRAIT_EXTS = ("png", "jpg", "webp")


def slugify_key(name) -> str:
    """A stable, lowercase asset slug ('Sleight of Hand' -> 'sleight-of-hand')."""
    text = _PARENS.sub(" ", str(name or "").lower())
    text = _POSSESSIVE.sub(" ", text)
    text = _NUMBER.sub(" ", text)
    return _NON_ALNUM.sub("-", text).strip("-")


def _norm(name) -> str:
    text = _PARENS.sub(" ", str(name or "").lower())
    text = _POSSESSIVE.sub(" ", text)
    text = _NUMBER.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def _config(filename: str):
    if filename not in _CONFIG_CACHE:
        data: object = []
        path = CONFIG_DIR / filename
        if yaml is not None and path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            except Exception:  # noqa: BLE001
                data = []
        _CONFIG_CACHE[filename] = data
    return _CONFIG_CACHE[filename]


def _name_index(filename: str) -> dict:
    cache_key = f"__idx__{filename}"
    if cache_key in _CONFIG_CACHE:
        return _CONFIG_CACHE[cache_key]  # type: ignore[return-value]
    idx: dict[str, str] = {}
    for entry in _config(filename):
        name = entry.get("name") if isinstance(entry, dict) else str(entry)
        if name:
            idx.setdefault(_norm(name), str(name))
    _CONFIG_CACHE[cache_key] = idx
    return idx


def _weapon_index() -> dict:
    return _name_index("weapons.yml")


def _spell_index() -> dict:
    cache_key = "__idx__spells.yml"
    if cache_key in _CONFIG_CACHE:
        return _CONFIG_CACHE[cache_key]  # type: ignore[return-value]
    idx: dict[str, dict] = {}
    for spell in _config("spells.yml"):
        if isinstance(spell, dict) and spell.get("name"):
            idx.setdefault(_norm(spell["name"]), spell)
    _CONFIG_CACHE[cache_key] = idx
    return idx


def _feature_index() -> dict:
    cache_key = "__idx__features"
    if cache_key in _CONFIG_CACHE:
        return _CONFIG_CACHE[cache_key]  # type: ignore[return-value]
    idx: dict[str, str] = {}

    def add(entry):
        if isinstance(entry, dict) and entry.get("name"):
            idx.setdefault(_norm(entry["name"]), str(entry["name"]))

    for cls in _config("classes.yml"):
        for feat in (cls.get("features") or []) if isinstance(cls, dict) else []:
            add(feat)
    for race in _config("races.yml"):
        if not isinstance(race, dict):
            continue
        for trait in race.get("traits") or []:
            add(trait)
        for sub in race.get("subraces") or []:
            for trait in (sub.get("traits") or []) if isinstance(sub, dict) else []:
                add(trait)
    for bg in _config("backgrounds.yml"):
        if isinstance(bg, dict):
            add(bg.get("feature") or {})
    _CONFIG_CACHE[cache_key] = idx
    return idx


def _vocab(names) -> dict:
    return {_norm(n): n for n in names}


_VOCAB = {
    "skill": _vocab(SKILLS),
    "language": _vocab(LANGUAGES),
    "school": _vocab(SCHOOLS),
    "damage": _vocab(DAMAGE_TYPES),
    "condition": _vocab(CONDITIONS),
    "armor": _vocab(ARMOR),
    "tool": _vocab(TOOLS),
    "weapon_category": _vocab(WEAPON_CATEGORIES),
    "faction": _vocab(FACTIONS),
    "ability": _vocab(ABILITIES),
    "save": _vocab(SAVES),
}


def _match_vocab(vocab: dict, name):
    """Match a free-form name against a canonical vocabulary (word-safe)."""
    key = _norm(name)
    if not key:
        return None
    if key in vocab:
        return vocab[key]
    if key.endswith("s") and key[:-1] in vocab:
        return vocab[key[:-1]]
    tokens = set(key.split())
    for candidate, canonical in vocab.items():
        cand = set(candidate.split())
        if cand and cand <= tokens:
            return canonical
    return None


# Choice prose is not a concrete thing to draw; those entries stay text.
_NON_ICON = re.compile(r"(of your choice|your choice|one type of|any type of|\bany\b)", re.I)


def _fallback_key(category: str, label) -> str | None:
    """A per-item key for a concrete-but-unmatched entry, or None for prose.

    Lets homebrew / GM-created entries (custom factions, exotic tools, …) get a
    shared icon generated on the fly instead of staying text forever.
    """
    text = str(label or "").strip()
    if not text or _NON_ICON.search(text):
        return None
    return f"{category}/{slugify_key(text)}"


_QTY_WORDS = {"a", "an", "one", "two", "three", "four", "five", "six", "seven",
              "eight", "nine", "ten", "twenty"}
_PROSE_CUT = re.compile(r"\s+(?:containing|posing|including|taken from|or)\b", re.I)


def _clean_starting_name(text) -> str:
    """Turn a starting-equipment string into a clean item name."""
    s = _PARENS.sub(" ", str(text or "")).strip()
    for _ in range(3):
        if not s:
            break
        parts = s.split(" ", 1)
        head = parts[0].strip().lower().rstrip(".")
        if head.isdigit() or head in _QTY_WORDS:
            s = parts[1].strip() if len(parts) > 1 else ""
        elif head == "set" and len(parts) > 1 and parts[1].lower().startswith("of"):
            s = parts[1][2:].strip()
        else:
            break
    s = _PROSE_CUT.split(s)[0].strip()
    return s.strip(" ,.")


def starting_item_names() -> list[str]:
    """Clean item names for every starting-equipment possibility in the configs."""
    names: list[str] = []

    def collect(options):
        for opt in options or []:
            if not isinstance(opt, dict):
                continue
            for it in opt.get("fixed_items") or []:
                names.append(_clean_starting_name(it))
            for it in opt.get("choose_one_from") or []:
                cleaned = _clean_starting_name(it)
                if cleaned and not cleaned.lower().startswith("any"):
                    names.append(cleaned)

    for cls in _config("classes.yml"):
        if isinstance(cls, dict):
            collect(cls.get("starting_equipment_options"))
    for bg in _config("backgrounds.yml"):
        if not isinstance(bg, dict):
            continue
        collect(bg.get("starting_equipment_options"))
        for it in bg.get("equipment") or []:
            names.append(_clean_starting_name(it))

    out, seen = [], set()
    for name in names:
        name = name.strip()
        key = _norm(name)
        if name and key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _exact_vocab(vocab: dict, name):
    """Exact (or simple plural) match — never a token subset."""
    key = _norm(name)
    if not key:
        return None
    if key in vocab:
        return vocab[key]
    if key.endswith("s"):
        return vocab.get(key[:-1])
    return None


def _is_covered_by_category(name) -> bool:
    return bool(_exact_vocab(_weapon_index(), name)
                or _exact_vocab(_VOCAB["armor"], name)
                or _exact_vocab(_VOCAB["tool"], name))


def item_catalog() -> dict:
    """{item key: display name} for all shared items (curated + starting gear)."""
    cache_key = "__catalog__items"
    if cache_key in _CONFIG_CACHE:
        return _CONFIG_CACHE[cache_key]  # type: ignore[return-value]
    out: dict[str, str] = {}
    for entry in _config("items.yml"):
        if isinstance(entry, dict) and entry.get("key"):
            out[str(entry["key"])] = str(entry.get("name") or entry["key"])
    for name in starting_item_names():
        if _is_covered_by_category(name):
            continue
        out.setdefault(slugify_key(name), name)
    _CONFIG_CACHE[cache_key] = out
    return out


def _item_index() -> dict:
    cache_key = "__idx__items"
    if cache_key in _CONFIG_CACHE:
        return _CONFIG_CACHE[cache_key]  # type: ignore[return-value]
    idx: dict[str, str] = {}
    for entry in _config("items.yml"):
        if not isinstance(entry, dict) or not entry.get("key"):
            continue
        key = str(entry["key"])
        for label in [entry.get("name"), *(entry.get("aliases") or [])]:
            if label:
                idx.setdefault(_norm(label), key)
    for key, name in item_catalog().items():
        idx.setdefault(_norm(name), key)
    _CONFIG_CACHE[cache_key] = idx
    return idx


_CATEGORY_INDEX = {
    "race": "races.yml",
    "class": "classes.yml",
    "background": "backgrounds.yml",
    "alignment": "alignments.yml",
}


def icon_key_for(category: str, name, detail: str = "") -> str | None:
    """Map a sheet entry to a shared icon key `"{kind}/{slug}"`, or None."""
    cat = (category or "").strip().lower()
    label = str(name or "").strip()
    if not label:
        return None

    if cat == "skill":
        canon = _match_vocab(_VOCAB["skill"], label)
        return f"skill/{slugify_key(canon)}" if canon else _fallback_key("skill", label)
    if cat == "language":
        canon = _match_vocab(_VOCAB["language"], label)
        return f"language/{slugify_key(canon)}" if canon else _fallback_key("language", label)
    if cat == "school":
        canon = _match_vocab(_VOCAB["school"], label)
        return f"school/{slugify_key(canon)}" if canon else _fallback_key("school", label)
    if cat == "damage":
        canon = _match_vocab(_VOCAB["damage"], label)
        return f"damage/{slugify_key(canon)}" if canon else _fallback_key("damage", label)
    if cat == "condition":
        canon = _match_vocab(_VOCAB["condition"], label)
        return f"condition/{slugify_key(canon)}" if canon else _fallback_key("condition", label)
    if cat == "armor":
        canon = _match_vocab(_VOCAB["armor"], label)
        return f"armor/{slugify_key(canon)}" if canon else _fallback_key("armor", label)
    if cat == "tool":
        # Vehicles keep their land/water distinction (the normaliser strips it).
        low = label.lower()
        if "vehicle" in low:
            kind = "water" if "water" in low else "land"
            return f"tool/vehicles-{kind}"
        canon = _match_vocab(_VOCAB["tool"], label)
        return f"tool/{slugify_key(canon)}" if canon else _fallback_key("tool", label)
    if cat == "weapon":
        canon = _match_vocab(_weapon_index(), label)
        if not canon:
            canon = _match_vocab(_VOCAB["weapon_category"], label)
        return f"weapon/{slugify_key(canon)}" if canon else _fallback_key("weapon", label)
    if cat == "spell":
        spell = _match_vocab(_spell_index(), label)
        if not spell:
            # Homebrew spell: a per-item key, generated on the fly when the
            # player has icon generation enabled.
            return _fallback_key("spell", label)
        return f"spell/{slugify_key(spell['name'])}"
    if cat == "feature":
        canon = _match_vocab(_feature_index(), label)
        if not canon:
            # Homebrew / GM-added trait: per-item key.
            return _fallback_key("feature", label)
        return f"feature/{slugify_key(canon)}"
    if cat == "faction":
        canon = _match_vocab(_VOCAB["faction"], label)
        return f"faction/{slugify_key(canon)}" if canon else _fallback_key("faction", label)
    if cat == "ability":
        canon = _match_vocab(_VOCAB["ability"], label)
        return f"ability/{slugify_key(canon)}" if canon else None
    if cat == "save":
        canon = _match_vocab(_VOCAB["save"], label)
        return f"save/{slugify_key(canon)}" if canon else None
    if cat == "kingdom":
        return f"kingdom/{slugify_key(label)}"
    if cat in _CATEGORY_INDEX:
        canon = _match_vocab(_name_index(_CATEGORY_INDEX[cat]), label)
        return f"{cat}/{slugify_key(canon)}" if canon else _fallback_key(cat, label)
    if cat in ("inventory", "consumable", "item"):
        for candidate in (label, _clean_starting_name(label)):
            if not candidate:
                continue
            curated = _exact_vocab(_item_index(), candidate)
            if curated:
                return f"item/{curated}"
            weapon = _exact_vocab(_weapon_index(), candidate)
            if weapon:
                return f"weapon/{slugify_key(weapon)}"
            armor = _exact_vocab(_VOCAB["armor"], candidate)
            if armor:
                return f"armor/{slugify_key(armor)}"
            tool = _exact_vocab(_VOCAB["tool"], candidate)
            if tool:
                return f"tool/{slugify_key(tool)}"
        # Not a shared item: a per-item key, generated later at runtime.
        return f"item/{slugify_key(label)}"
    return None


def spell_detail(name) -> str:
    """A short qualifier for a spell icon prompt (school + level)."""
    spell = _match_vocab(_spell_index(), name)
    if not spell:
        return ""
    level = spell.get("level")
    school = spell.get("school") or ""
    if level in (0, "0"):
        return f"{school} cantrip".strip()
    return f"{school} spell, level {level}".strip()


# ── the shared, committed asset store ─────────────────────────────────────

class IconService(GeminiImageBackend):
    """Generates + caches shared icons under `assets/icons/`."""

    def __init__(self, assets_dir=None, config_path: Path | None = None):
        cfg = _load_config(config_path)
        super().__init__(
            str(cfg.get("icon_model") or cfg.get("model") or "gemini-3.1-flash-lite-image"),
            "1:1",
            str(cfg.get("image_size") or "1K"),
        )
        self.assets_dir = Path(assets_dir) if assets_dir else DEFAULT_ASSETS_DIR
        self.icon_style = str(cfg.get("icon_style") or cfg.get("style") or "").strip()
        self.templates = cfg.get("icon") if isinstance(cfg.get("icon"), dict) else {}
        try:
            self.max_side = int(cfg.get("icon_max_side") or 256)
        except (TypeError, ValueError):
            self.max_side = 256
        try:
            self.fill = float(cfg.get("icon_fill") or 0.92)
        except (TypeError, ValueError):
            self.fill = 0.92
        self.background = str(cfg.get("icon_background") or "").strip() or None
        self._manifest: dict | None = None

    def status(self) -> dict:
        return {**super().status(), "aspect_ratio": "1:1", "max_side": self.max_side}

    # ── paths / manifest ───────────────────────────────────────────────────

    def icon_dir(self, kind: str) -> Path:
        return self.assets_dir / kind

    def icon_path(self, kind: str, slug: str) -> Path:
        directory = self.icon_dir(kind)
        for ext in _PORTRAIT_EXTS:
            candidate = directory / f"{slug}.{ext}"
            if candidate.exists():
                return candidate
        return directory / f"{slug}.png"

    def manifest_path(self) -> Path:
        return self.assets_dir / "manifest.json"

    @staticmethod
    def key(kind: str, slug: str) -> str:
        return f"{kind}/{slug}"

    def _read_manifest(self) -> dict:
        if self._manifest is not None:
            return self._manifest
        path = self.manifest_path()
        data: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}
        data.setdefault("icons", {})
        self._manifest = data
        return data

    def _write_manifest(self) -> None:
        path = self.manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._read_manifest(), indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)

    def has(self, kind: str, slug: str) -> bool:
        return self.icon_path(kind, slug).exists()

    def url_for(self, kind: str, slug: str) -> str | None:
        if not self.has(kind, slug):
            return None
        # Extensionless: the route finds the file (any of png/jpg/webp) and sets
        # the real Content-Type, so URLs stay stable regardless of output format.
        return f"/api/icons/{kind}/{slug}"

    def index(self) -> dict:
        """{key: url} for every committed icon that is actually on disk."""
        out: dict[str, str] = {}
        for key in (self._read_manifest().get("icons") or {}):
            kind, _, slug = key.partition("/")
            url = self.url_for(kind, slug)
            if url:
                out[key] = url
        return out

    # ── prompts ────────────────────────────────────────────────────────────

    def prompt_for(self, kind: str, name: str, detail: str = "") -> str:
        low = str(name or "").lower()
        key = self.key(slugify_key(kind), slugify_key(name))
        if "vehicle" in low:
            key = f"tool/vehicles-{'water' if 'water' in low else 'land'}"
        if key in _CATEGORY_PROMPTS:
            body = _CATEGORY_PROMPTS[key]
        else:
            template = (self.templates or {}).get(kind) or "A single {name}. {detail}"
            body = _SPACES.sub(" ", template.format(name=str(name), detail=str(detail or ""))).strip()
        return f"{body} {self.icon_style}".strip()

    def _source_hash(self, key: str, prompt: str, model: str | None = None) -> str:
        blob = json.dumps({"key": key, "model": model or self.model, "style": self.icon_style,
                           "prompt": prompt}, sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    # ── generation ─────────────────────────────────────────────────────────

    def ensure(self, kind: str, slug: str, name: str, detail: str = "",
               force: bool = False, model: str | None = None) -> dict:
        kind = slugify_key(kind)
        slug = slugify_key(slug)
        effective = model or self.model
        key = self.key(kind, slug)
        prompt = self.prompt_for(kind, name, detail)
        digest = self._source_hash(key, prompt, model=effective)
        manifest = self._read_manifest()
        entry = (manifest.get("icons") or {}).get(key) or {}

        if not force and self.has(kind, slug) and entry.get("source_hash") == digest:
            return {"key": key, "url": self.url_for(kind, slug), "generated": False,
                    "cached": True, "model": effective}

        raw = fit_to_frame(self._generate_bytes(prompt, model=effective), self.fill,
                           max_side=self.max_side, background=self.background, out_format="PNG")
        ext, mime = sniff_image(raw) or ("png", "image/png")
        target = self.icon_dir(kind) / f"{slug}.{ext}"
        for other in _PORTRAIT_EXTS:
            stale = self.icon_dir(kind) / f"{slug}.{other}"
            if other != ext and stale.exists():
                stale.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, target)

        manifest["icons"][key] = {
            "kind": kind, "slug": slug, "name": str(name), "file": target.name,
            "mime": mime, "model": effective, "prompt": prompt,
            "source_hash": digest, "created": int(time.time()),
        }
        self._write_manifest()
        return {"key": key, "url": self.url_for(kind, slug), "generated": True,
                "cached": False, "model": effective}


# ── enumeration for the warm CLI + stats ───────────────────────────────────

def all_icon_keys() -> dict[str, str]:
    """{key: display name} for every *known* shared icon, from the vocabulary."""
    out: dict[str, str] = {}

    def add(kind: str, name: str):
        key = f"{kind}/{slugify_key(name)}"
        out.setdefault(key, name)

    for name in SKILLS:
        add("skill", name)
    for name in LANGUAGES:
        add("language", name)
    for name in SCHOOLS:
        add("school", name)
    for name in DAMAGE_TYPES:
        add("damage", name)
    for name in CONDITIONS:
        add("condition", name)
    for name in ARMOR:
        add("armor", name)
    for name in TOOLS:
        add("tool", name)
    for name in FACTIONS:
        add("faction", name)
    for name in ABILITIES:
        add("ability", name)
    for name in SAVES:
        add("save", name)
    for key, name in STAT_ICONS.items():
        out.setdefault(f"stat/{key}", name)
    for name in _weapon_index().values():
        add("weapon", name)
    for name in WEAPON_CATEGORIES:
        add("weapon", name)
    for name in VEHICLE_TOOLS.values():
        # explicit keys: the parenthetical is stripped by the normaliser
        out.setdefault(f"tool/vehicles-{'water' if 'water' in name.lower() else 'land'}", name)
    for spell in _spell_index().values():
        add("spell", spell["name"])
    for name in _feature_index().values():
        add("feature", name)
    for category, filename in _CATEGORY_INDEX.items():
        for name in _name_index(filename).values():
            add(category, name)
    for key, name in item_catalog().items():
        out.setdefault(f"item/{key}", name)
    return out


def _display_detail(kind: str, name: str) -> str:
    if kind == "spell":
        return spell_detail(name)
    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Warm the shared Project Infinity icon library.")
    parser.add_argument("--only", default="", help="comma-separated kinds (e.g. weapon,armor)")
    parser.add_argument("--limit", type=int, default=0, help="max icons this run (0 = all)")
    parser.add_argument("--assets", default=str(DEFAULT_ASSETS_DIR), help="assets directory")
    parser.add_argument("--yes", action="store_true", help="actually generate (default: dry run)")
    parser.add_argument("--force", action="store_true", help="regenerate even if cached")
    parser.add_argument("--existing", action="store_true",
                        help="regenerate only icons already committed (implies --force for those)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds to wait between generations (default: 1.0; 0 = none)")
    args = parser.parse_args(argv)

    service = IconService(args.assets)
    only = {k.strip() for k in args.only.split(",") if k.strip()}
    catalog = all_icon_keys()
    todo = []
    if args.existing:
        # Restyle pass: only touch icons already in the manifest (optionally --only).
        for key in sorted(service.index()):
            kind, _, slug = key.partition("/")
            if only and kind not in only:
                continue
            todo.append((kind, slug, catalog.get(key, slug.replace("-", " ").title())))
    else:
        for key, name in sorted(catalog.items()):
            kind, _, slug = key.partition("/")
            if only and kind not in only:
                continue
            if not args.force and service.has(kind, slug):
                continue
            todo.append((kind, slug, name))
    force = args.force or args.existing
    if args.limit and len(todo) > args.limit:
        todo = todo[: args.limit]

    print(f"model: {service.model} | available: {service.available()}")
    print(f"catalog: {len(catalog)} icons | missing after filter: {len(todo)}")
    print(f"estimated cost: ~${len(todo) * 0.0336:.2f} (at $0.0336/image)")
    print(f"delay: {args.delay:g}s between generations")
    if not args.yes:
        print("dry run — pass --yes to generate.")
        return 0
    if not service.available():
        print("GEMINI_API_KEY is not set; cannot generate.")
        return 2

    ok = 0
    for i, (kind, slug, name) in enumerate(todo, 1):
        try:
            result = service.ensure(kind, slug, name, _display_detail(kind, name), force=force)
            ok += 1
            print(f"  [{i}/{len(todo)}] {result['key']} -> {result.get('url')}")
        except ImageError as exc:
            print(f"  [{i}/{len(todo)}] {kind}/{slug} FAILED: {exc}")
        if args.delay and i < len(todo):
            time.sleep(args.delay)
    print(f"done: {ok}/{len(todo)} generated")
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    raise SystemExit(main())
