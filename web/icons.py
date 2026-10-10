"""Shared icon library for the character sheet (cross-character, committed).

Icons are keyed off a *bounded canonical vocabulary* — fixed SRD lists, the
`config/*.yml` reference data, the fixed kingdom/faction set, and a keyword map
for generic gear (`config/items.yml`). One generated icon therefore serves every
character and every world, and lives under `assets/{family}/{kind}/{slug}.png` (tracked in git)
with one `manifest.json` per family. **Every image-model family keeps its own store** and a
family never serves another family's file.

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
import zlib
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .images import (  # noqa: E402
    ImageBackend,
    ImageBackendHolder,
    ImageError,
    _backend_from_config,
    _hex_rgb,
    _load_config,
    colour_contrast,
    detail_within,
    downscale_image,
    fit_to_frame,
    image_mime,
    sniff_image,
    subject_similarity,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_ASSETS_DIR = REPO_ROOT / "assets"

# ── icon families ────────────────────────────────────────────────────────────
# One store per IMAGE-MODEL family: assets/{family}/{kind}/{slug}.png. The family
# is a property of the image model, never of the game mode or the character, and
# a family never serves another family's file -- so a local bake can never
# overwrite the committed Gemini set.
DEFAULT_FAMILY = "gemini"
KNOWN_FAMILIES = ("gemini", "local")


def safe_family(value) -> str:
    """A directory-safe family name; anything odd falls back to the default."""
    text = "".join(c for c in str(value or "").strip().lower()
                   if c.isalnum() or c in "_-")
    return text or DEFAULT_FAMILY


def family_for_model(model) -> str:
    """The family an image-model id belongs to: `gemini-*` -> gemini,
    `local/*` -> local, anything else -> its own head (so a future
    `azure/...` or `flux-...` gets an honest store of its own)."""
    text = str(model or "").strip().lower()
    if not text:
        return DEFAULT_FAMILY
    head = text.replace("/", "-").split("-", 1)[0]
    return safe_family(head)

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

def _int_setting(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_setting(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _local_model_id(cfg: dict) -> str:
    """The local engine's model id, from the `local:` block (`local/sdxl-lightning-4step`)."""
    block = cfg.get("local")
    block = block if isinstance(block, dict) else {}
    named = str(block.get("model_id") or "").strip()
    if named:
        return named
    stem = str(block.get("checkpoint") or "local-model").rsplit(".", 1)[0]
    return f"local/{stem}"


class IconService(ImageBackendHolder):
    """Generates + caches shared icons in one family's store:
    `assets/{family}/{kind}/{slug}.png` with `assets/{family}/manifest.json`.
    `assets_dir` is the store ROOT that holds every family, and the family also picks
    the ENGINE: the `local` store is filled by the local ComfyUI pipeline, every other
    store by the hosted model."""

    def __init__(self, assets_dir=None, family: str = DEFAULT_FAMILY,
                 config_path: Path | None = None,
                 backend: ImageBackend | None = None):
        cfg = _load_config(config_path)
        self.config = cfg
        self.family = safe_family(family)
        hosted = str(cfg.get("icon_model") or cfg.get("model") or "gemini-3.1-flash-lite-image")
        self.backend = backend or _backend_from_config(
            cfg,
            model=_local_model_id(cfg) if self.family == "local" else hosted,
            aspect_ratio="1:1",
            thinking_level=None,
            family=self.family,
        )
        self.assets_dir = Path(assets_dir) if assets_dir else DEFAULT_ASSETS_DIR
        # A family may carry its own prompt recipe (`cfg[<family>]`); the top-level
        # `icon_style` / `icon:` are the default. The Gemini override is frozen to match
        # the committed assets, so local-model work can never shadow it.
        fam = cfg.get(self.family) if isinstance(cfg.get(self.family), dict) else {}
        self.icon_style = str(fam.get("icon_style") or cfg.get("icon_style")
                              or cfg.get("style") or "").strip()
        self.templates = (fam.get("icon") if isinstance(fam.get("icon"), dict)
                          else (cfg.get("icon") if isinstance(cfg.get("icon"), dict) else {}))
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
        # ── the contrast check and the repair loop (local engine only) ──────
        # The check is code, not a model: a diffusion model cannot see its own output.
        # `contrast_distance` is how far a pixel must differ from the ground colour to
        # count as subject; the icon passes with `contrast_min_px` such pixels whose mean
        # distance is at least `contrast_min_mean` (calibrated: 93% of the committed
        # Gemini family passes this rule).
        local = cfg.get("local") if isinstance(cfg.get("local"), dict) else {}
        self.contrast_distance = _int_setting(local.get("contrast_distance"), 60)
        self.contrast_min_px = _int_setting(local.get("contrast_min_px"), 500)
        self.contrast_min_mean = _int_setting(local.get("contrast_min_mean"), 60)
        self.detail_max_edge = _float_setting(local.get("detail_max_edge"), 0.217)
        self.detail_max_hf = _float_setting(local.get("detail_max_hf"), 29.7)
        self.repair_passes = max(0, _int_setting(local.get("repair_passes"), 0))
        self.repair_denoise = _float_setting(local.get("repair_denoise"), 0.5)
        self.repair_steps = _int_setting(local.get("repair_steps"), 12)
        self.repair_cfg = _float_setting(local.get("repair_cfg"), 4.0)
        self.repair_prompt = " ".join(str(local.get("repair_prompt") or "").split())

    def status(self) -> dict:
        return {**super().status(), "aspect_ratio": "1:1", "max_side": self.max_side}

    # ── paths / manifest ───────────────────────────────────────────────────

    def family_dir(self) -> Path:
        """The one store this service owns. A family never reads another's."""
        return self.assets_dir / self.family

    def icon_dir(self, kind: str) -> Path:
        return self.family_dir() / kind

    def icon_path(self, kind: str, slug: str) -> Path:
        directory = self.icon_dir(kind)
        for ext in _PORTRAIT_EXTS:
            candidate = directory / f"{slug}.{ext}"
            if candidate.exists():
                return candidate
        return directory / f"{slug}.png"

    def manifest_path(self) -> Path:
        return self.family_dir() / "manifest.json"

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
        return f"/api/icons/{self.family}/{kind}/{slug}"

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

    def _source_hash(self, key: str, prompt: str, model: str | None = None,
                     engine: str = "") -> str:
        blob = {"key": key, "model": model or self.model, "style": self.icon_style,
                "prompt": prompt}
        # A hosted model needs nothing more (its id is already in the key), so an empty
        # engine leaves every existing hash byte-identical. A local engine records its
        # checkpoint and sampler here, so re-tuning it regenerates inside its own family.
        if engine:
            blob["engine"] = engine
        return hashlib.sha1(json.dumps(blob, sort_keys=True).encode("utf-8")).hexdigest()

    # ── generation ─────────────────────────────────────────────────────────

    def ensure(self, kind: str, slug: str, name: str, detail: str = "",
               force: bool = False, model: str | None = None) -> dict:
        kind = slugify_key(kind)
        slug = slugify_key(slug)
        effective = model or self.model
        if family_for_model(effective) != self.family:
            effective = self.model  # a model from another family never writes into this store
        key = self.key(kind, slug)
        prompt = self.prompt_for(kind, name, detail)
        digest = self._source_hash(key, prompt, model=effective,
                                   engine=self.backend.fingerprint())
        # A seed derived from the icon KEY (not from the prompt), so a bake is
        # reproducible per icon and a reworded prompt keeps the same picture.
        seed = zlib.crc32(f"{key}|{self.family}".encode("utf-8")) & 0xFFFFFFFF
        recipe = self.backend.recipe(prompt=prompt, seed=seed)
        manifest = self._read_manifest()
        entry = (manifest.get("icons") or {}).get(key) or {}

        if not force and self.has(kind, slug) and entry.get("source_hash") == digest:
            return {"key": key, "url": self.url_for(kind, slug), "generated": False,
                    "cached": True, "model": effective}

        raw = fit_to_frame(self.generate(prompt, model=effective, seed=recipe.get("seed")),
                           self.fill,
                           max_side=self.max_side, background=self.background, out_format="PNG")
        check, repair = None, None
        backend = self.backend_for(effective)
        if self.repair_passes and self.background and hasattr(backend, "repair"):
            raw, check, repair = self._check_and_repair(backend, raw, prompt, recipe)
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
            "mime": mime, "model": effective, "prompt": prompt, "family": self.family,
            **recipe,
            **({"check": check} if check else {}),
            **({"repair": repair} if repair else {}),
            "source_hash": digest, "created": int(time.time()),
        }
        self._write_manifest()
        return {"key": key, "url": self.url_for(kind, slug), "generated": True,
                "cached": False, "model": effective,
                **({"check": check} if check else {}),
                **({"repair": repair} if repair else {})}

    # ── the contrast check + the repair loop ────────────────────────────────

    def _check_and_repair(self, backend, data: bytes, prompt: str, recipe: dict):
        """Measure the icon in code, and repair it up to `repair_passes` times.

        Returns (best_bytes, check, repair_record). An icon PASSES when both gates hold:
        the subject is in contrast with the ground (C1) AND the icon is simple enough -- not
        more linework or grain than the committed family's own p90 (C4).

        The repair is BEST-OF-N: the same prompt again at a FRESH seed (base + attempt), at the
        higher guidance below. Image-to-image was the first design and the measurement killed
        it -- an img2img pass scored only 0.05-0.25 IoU against the original, i.e. it redrew the
        picture anyway, so there is no reason to pretend the composition is preserved. Attempts
        stop at the first icon that passes BOTH gates; otherwise the best is kept, preferring a
        pass, then an icon within the detail ceiling, then the most contrast, then the least
        detail. Every attempt records both measurements.
        """
        ground = _hex_rgb(self.background) or (30, 26, 19)
        base_seed = int(recipe.get("seed") or 0)

        def _check(image: bytes) -> dict | None:
            try:
                contrast = colour_contrast(image, ground,
                                           min_distance=self.contrast_distance,
                                           min_px=self.contrast_min_px,
                                           min_mean=self.contrast_min_mean)
                detail = detail_within(image, max_edge=self.detail_max_edge,
                                       max_hf=self.detail_max_hf)
            except Exception:  # noqa: BLE001 - not a readable image: nothing to measure
                return None
            return {"contrast": contrast, "detail": detail,
                    "ok": contrast["ok"] and detail["ok"]}

        def _score(check: dict) -> tuple:
            return (1 if check["ok"] else 0,
                    1 if check["detail"]["ok"] else 0,
                    check["contrast"]["px"],
                    -check["detail"]["hf"])

        check = _check(data)
        if check is None:
            return data, None, None
        record: dict = {"passes": [], "before": check, "ok": check["ok"], "engine": ""}
        if check["ok"]:
            return data, check, None
        if not backend.available():
            record["engine"] = "not reachable, repair skipped"
            return data, check, record
        for attempt in range(1, self.repair_passes + 1):
            seed = (base_seed + attempt) & 0xFFFFFFFF
            entry: dict = {"pass": attempt, "mode": "redraw", "seed": seed,
                           "steps": self.repair_steps, "cfg": self.repair_cfg, "denoise": 1.0}
            try:
                out = backend.repair(data, prompt, mode="redraw", seed=seed,
                                     denoise=self.repair_denoise, steps=self.repair_steps,
                                     cfg_scale=self.repair_cfg)
            except ImageError as exc:
                entry["error"] = str(exc)
                record["passes"].append(entry)
                break
            candidate = fit_to_frame(out, self.fill, max_side=self.max_side,
                                     background=self.background, out_format="PNG")
            entry["after"] = _check(candidate)
            if not entry["after"]:
                record["passes"].append(entry)
                break
            entry.update(subject_similarity(data, candidate, ground,
                                           min_distance=self.contrast_distance))
            record["passes"].append(entry)
            if _score(entry["after"]) > _score(check):
                data, check = candidate, entry["after"]
            if check["ok"]:
                break
        record["after"] = check
        record["ok"] = check["ok"]
        return data, check, record


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
    parser.add_argument("--family", default=DEFAULT_FAMILY,
                        help=f"icon store to write ({', '.join(KNOWN_FAMILIES)})")
    parser.add_argument("--yes", action="store_true", help="actually generate (default: dry run)")
    parser.add_argument("--force", action="store_true", help="regenerate even if cached")
    parser.add_argument("--existing", action="store_true",
                        help="regenerate only icons already committed (implies --force for those)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds to wait between generations (default: 1.0; 0 = none)")
    parser.add_argument("--keys", default="",
                        help="exact keys to generate, comma-separated (e.g. race/gnome,weapon/dagger)")
    args = parser.parse_args(argv)

    service = IconService(args.assets, args.family)
    only = {k.strip() for k in args.only.split(",") if k.strip()}
    wanted_keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    catalog = all_icon_keys()
    todo = []
    if wanted_keys:
        # An explicit batch: exactly these keys, in the order given (the review batch,
        # and later a targeted top-up), regardless of what each family already holds.
        for key in wanted_keys:
            kind, sep, slug = key.partition("/")
            if not sep or not slug or kind not in {k.partition("/")[0] for k in catalog}:
                print(f"  !! {key} is not a known icon key -- skipped")
                continue
            todo.append((kind, slug, catalog.get(key, slug.replace("-", " ").title())))
    elif args.existing:
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
    if service.repair_passes:
        print(f"check: contrast = subject pixels > {service.contrast_distance} from the ground "
              f"colour (>= {service.contrast_min_px} px, mean >= {service.contrast_min_mean}) "
              f"| detail = edge <= {service.detail_max_edge:g}, high-freq <= {service.detail_max_hf:g} "
              f"at 256 px")
        print(f"repair: up to {service.repair_passes} redraws ({service.repair_steps} steps, "
              f"cfg {service.repair_cfg:g}, a fresh seed each) until BOTH checks pass")
    if service.family == "local":
        print(f"cost: the local engine (free; {len(todo)} images at "
              f"{getattr(service.backend, 'steps', '?')} steps)")
    else:
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
