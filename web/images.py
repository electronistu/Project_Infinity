"""Cached portrait generation for the web client.

Images are one-off assets reused forever -- a character portrait today, stat and
inventory icons later. Each save owns a folder `output/images/{stem}/` holding
`{kind}.png` plus a `manifest.json` that records how each asset was made, so we
can skip the API when nothing relevant changed and regenerate on demand.

The image models themselves live in `web.backends`; this module is the portrait
side (prompt building, caching and the manifest) on top of that seam, and the
shared config/prompt helpers its siblings (`web.scenes`, `web.icons`) import.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None

from .backends import (
    ImageBackend,
    ImageBackendHolder,
    _backend_from_config,
)

from .imageassets import (
    _PARENS,
    downscale_image,
    drop_stale,
    first_existing,
    image_mime,
    read_json_dict,
    sniff_image,
    write_bytes_atomic,
    write_json_atomic,
)


_DEFAULT_MODEL = "gemini-3.1-flash-lite-image"
_DEFAULT_PORTRAIT = (
    "{reference_line} Head-and-shoulders character portrait of a {alignment} {gender} "
    "{race} {class}. {level_line} {age_line} {gear} {background_line} Single subject, centred, "
    "facing the viewer, plain dark background, no other people."
)
_REFERENCE_LINE = (
    "Use the attached portrait as this character's identity reference — keep the same "
    "face, hair colour, build and distinguishing features; this is the same person later "
    "in their career."
)
_PORTRAIT_GUARD = (
    "The subject wears and wields exactly the equipment listed, nothing more — do not add or "
    "invent any garment, armour, weapon, hood, cloak, hat, helmet or accessory, and never depict "
    "gear that is not described."
)
_STYLE_GUARD = (
    "Absolutely no text, letters, numbers, runes, watermarks, logos, borders or user-interface "
    "elements."
)


def _load_config(config_path: Path | None) -> dict:
    path = Path(config_path) if config_path else Path(__file__).resolve().parent.parent / "config" / "images.yml"
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _thinking_level(cfg) -> str | None:
    """The configured Gemini image `thinking_level` (None = model default).

    NB2.1 defaults to `medium`; pinning it keeps the effort explicit and lets
    the seed/portrait and action calls diverge later.
    """
    level = str(cfg.get("image_thinking_level") or "").strip().lower()
    return level or None


def _veterancy_line(level, has_age: bool = False) -> str:
    """A visual experience descriptor from the character level (affects the look).

    Deliberately describes only experience, scars and bearing — never gear, which comes solely
    from the character's equipped set (`_gear_line`). When an explicit age is supplied, the
    hardcoded age cues ("young", "greying", "ageless") are dropped so the two never fight.
    """
    try:
        lvl = int(level)
    except (TypeError, ValueError):
        return ""
    if lvl <= 1:
        if has_age:
            return (f"This is a level-{lvl} novice: untested and unscarred, with an "
                    "uncertain stance.")
        return (f"This is a level-{lvl} novice: young, untested and unscarred, with an "
                "uncertain stance.")
    if lvl <= 4:
        return (f"This is a level-{lvl} adventurer: a few small scars, weathered by the road, "
                "and the first hints of confidence.")
    if lvl <= 10:
        return (f"This is a level-{lvl} seasoned adventurer: visible scars, a weathered face "
                "and an assured, capable bearing.")
    if lvl <= 16:
        if has_age:
            return (f"This is a level-{lvl} renowned adventurer: deep scars and a "
                    "commanding presence.")
        return (f"This is a level-{lvl} renowned adventurer: deep scars, greying at the "
                "temples, and a commanding presence.")
    if has_age:
        return (f"This is a level-{lvl} legendary adventurer: old wounds and a near-mythic "
                "mien.")
    return (f"This is a level-{lvl} legendary adventurer: old wounds and an ageless "
            "near-mythic mien.")


_RACE_AGE_TABLE: dict[str, tuple[int, int]] | None = None


def _race_age_table() -> dict[str, tuple[int, int]]:
    """{race name (lower): (adulthood, max)} from config/races.yml (loaded once)."""
    global _RACE_AGE_TABLE
    if _RACE_AGE_TABLE is None:
        table: dict[str, tuple[int, int]] = {}
        try:
            path = Path(__file__).resolve().parent.parent / "config" / "races.yml"
            data = (yaml.safe_load(path.read_text(encoding="utf-8")) or []) if yaml else []
            for race in data:
                if not isinstance(race, dict):
                    continue
                name = str(race.get("name") or "").strip().lower()
                age = race.get("age")
                if name and isinstance(age, dict):
                    table[name] = (int(age.get("adulthood") or 18), int(age.get("max") or 90))
        except Exception:  # noqa: BLE001 - a missing/broken config just loses the race nuance
            table = {}
        _RACE_AGE_TABLE = table
    return _RACE_AGE_TABLE


def _age_line(age, race) -> str:
    """A race-relative age sentence for the portrait ('' when no usable age)."""
    try:
        value = int(age)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    adulthood, maximum = _race_age_table().get(str(race or "").strip().lower(), (None, None))
    if adulthood is None:
        # Match a subrace to its parent race (e.g. "High Elf" -> "elf").
        key = str(race or "").strip().lower()
        for name, bounds in _race_age_table().items():
            if name and name in key:
                adulthood, maximum = bounds
                break
    if adulthood is None:
        adulthood, maximum = 18, 90
    frac = (value - adulthood) / max(1, maximum - adulthood)
    if frac < 0.25:
        band = "a young adult"
    elif frac < 0.5:
        band = "in their prime"
    elif frac < 0.75:
        band = "middle-aged"
    else:
        band = "venerable"
    who = f" for {('an' if str(race or '')[:1].lower() in 'aeiou' else 'a')} {race}" if race else ""
    return (f"The subject is {value} years old — {band}{who}; render a face and bearing "
            "true to that age.")


def _clean_item_name(name) -> str:
    return _PARENS.sub("", str(name or "")).strip()


def _join_names(names: list) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _gear_line(player) -> str:
    """Exactly what the character is wearing and wielding, from their `equipped` set.

    Reads `equipped` (`armor` + `worn` + `hands`) and nothing else: an item merely carried in the
    inventory is never described, and no gear is invented. Returns "" when nothing is equipped.
    """
    player = player if isinstance(player, dict) else {}
    equipped = player.get("equipped") if isinstance(player.get("equipped"), dict) else {}
    wearing = []
    armor = _clean_item_name(equipped.get("armor"))
    if armor:
        wearing.append(armor)
    wearing += [_clean_item_name(w) for w in (equipped.get("worn") or []) if w]
    hands = [_clean_item_name(h) for h in (equipped.get("hands") or []) if h]
    clauses = []
    if wearing:
        clauses.append(f"Wearing {_join_names(wearing)}.")
    if hands:
        clauses.append(f"Wielding {_join_names(hands)}.")
    return " ".join(clauses)


# Effects that change how the protagonist LOOKS. The value says whether the illusion also covers
# clothing/armour/weapons: the SRD says Disguise Self and Seeming do, while Alter Self changes only
# the physical form. `appearance_override` returns `{look, covers_gear}` for the image prompt.
_APPEARANCE_EFFECTS_DEFAULT = {
    "disguise self": True,
    "seeming": True,
    "alter self": False,
}


def _normalise_effect_name(name) -> str:
    """An effect key without a trailing '(active)' and with collapsed whitespace."""
    text = re.sub(r"\(.*?\)", " ", str(name or ""))
    return re.sub(r"\s+", " ", text).strip().lower()


def _strip_duration(text) -> str:
    """Drop a trailing 'Lasts …' / 'Duration: …' clause from an effect description."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    cleaned = re.sub(r"\s*(?:Lasts?\b|Duration:?)\s*[^.]*\.?\s*$", "", cleaned,
                     flags=re.IGNORECASE)
    return cleaned.strip()


def appearance_override(player, known=None) -> dict | None:
    """`{look, covers_gear}` when an active effect changes the protagonist's appearance.

    An explicit `appearance` field on the effect wins; otherwise the effect's description is
    used when its name matches `known` (or the built-in table). Returns `None` when nothing applies,
    in which case the portrait stays the identity reference.
    """
    effects = player.get("active_effects") if isinstance(player, dict) else None
    if not isinstance(effects, list):
        return None
    table = _APPEARANCE_EFFECTS_DEFAULT if known is None else known
    for entry in effects:
        if not isinstance(entry, dict):
            continue
        name = _normalise_effect_name(entry.get("name"))
        declared = re.sub(r"\s+", " ", str(entry.get("appearance") or "")).strip()
        if declared:
            return {"look": declared, "covers_gear": bool(table.get(name, True))}
        if name in table:
            look = _strip_duration(entry.get("description"))
            if look:
                return {"look": look, "covers_gear": bool(table[name])}
    return None


def _appearance_effects_config(cfg) -> dict | None:
    """`appearance_effects` from config as `{normalised name: covers_gear}` (None = built-in)."""
    raw = cfg.get("appearance_effects")
    if isinstance(raw, dict):
        return {_normalise_effect_name(k): bool(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return {_normalise_effect_name(name): True for name in raw}
    return None


class ImageService(ImageBackendHolder):
    """Generates and caches per-save image assets (portraits) with the configured backend."""

    def __init__(self, output_dir, config_path: Path | None = None,
                 backend: ImageBackend | None = None):
        self.output_dir = Path(output_dir)
        cfg = _load_config(config_path)
        self.backend = backend or _backend_from_config(
            cfg,
            model=str(cfg.get("model") or _DEFAULT_MODEL),
            aspect_ratio=str(cfg.get("aspect_ratio") or "3:4"),
            thinking_level=_thinking_level(cfg),
        )
        self.style = str(cfg.get("style") or "").strip()
        self.style_guard = str(cfg.get("style_guard") or _STYLE_GUARD).strip()
        self.portrait_template = str(cfg.get("portrait") or "").strip()
        self.portrait_guard = str(cfg.get("portrait_guard") or _PORTRAIT_GUARD).strip()
        self.ref_max_side = _int_or(cfg.get("ref_max_side"), 512)
        self.ref_media_resolution = str(cfg.get("ref_media_resolution") or "MEDIA_RESOLUTION_MEDIUM").strip()
        try:
            self.portrait_max_side = int(cfg.get("portrait_max_side") or 0)
        except (TypeError, ValueError):
            self.portrait_max_side = 0

    # ── paths ─────────────────────────────────────────────────────────────

    def image_dir(self, stem: str) -> Path:
        return self.output_dir / "images" / stem

    def portrait_path(self, stem: str) -> Path:
        """Existing portrait file (any supported format), else the default .png path."""
        directory = self.image_dir(stem)
        return first_existing(directory, "portrait") or directory / "portrait.png"

    def portrait_file(self, stem: str) -> tuple[Path, str] | None:
        path = self.portrait_path(stem)
        return (path, image_mime(path)) if path.exists() else None

    def manifest_path(self, stem: str) -> Path:
        return self.image_dir(stem) / "manifest.json"

    def has_portrait(self, stem: str) -> bool:
        return self.portrait_path(stem).exists()

    # ── prompt / cache key ─────────────────────────────────────────────────

    @staticmethod
    def _player_payload(player: dict) -> dict:
        payload = {
            "race": str(player.get("race") or ""),
            "character_class": str(player.get("character_class") or ""),
            "background": str(player.get("background") or ""),
            "alignment": str(player.get("alignment") or ""),
            "gender": str(player.get("gender") or ""),
            "level": player.get("level"),
            "gear": _gear_line(player),
        }
        age = player.get("age")
        if age not in (None, ""):
            payload["age"] = age
        return payload

    def source_hash(self, kind: str, payload: dict, model: str | None = None,
                    style: str | None = None) -> str:
        blob = json.dumps(
            {"kind": kind, "model": model or self.model, "aspect": self.aspect_ratio,
             "style": (style or self.style), "thinking_level": self.thinking_level,
             "payload": payload},
            sort_keys=True, default=str,
        )
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    def portrait_prompt(self, player: dict, has_ref: bool = False,
                        style: str | None = None) -> str:
        payload = self._player_payload(player)
        fields = {
            "alignment": payload["alignment"] or "wandering",
            "gender": payload["gender"] or "figure",
            "race": payload["race"] or "adventurer",
            "class": payload["character_class"] or "adventurer",
            "gear": payload["gear"],
            "level_line": _veterancy_line(payload["level"], has_age=payload.get("age") not in (None, "")),
            "age_line": _age_line(payload.get("age"), payload["race"]),
            "reference_line": _REFERENCE_LINE if has_ref else "",
            "background_line": (f"Background: {payload['background']}." if payload["background"] else ""),
        }
        template = self.portrait_template or _DEFAULT_PORTRAIT
        body = re.sub(r"\s+", " ", template.format(**fields)).strip()
        effective_style = (style or self.style).strip()
        parts = [body]
        if effective_style:
            parts.append(effective_style)
        if self.style_guard:
            parts.append(self.style_guard)
        if self.portrait_guard:
            parts.append(self.portrait_guard)
        return " ".join(parts).strip()

    # ── manifest helpers ───────────────────────────────────────────────────

    def _read_manifest(self, stem: str) -> dict:
        return read_json_dict(self.manifest_path(stem))

    def _write_manifest(self, stem: str, manifest: dict) -> None:
        write_json_atomic(self.manifest_path(stem), manifest)

    # ── generation ────────────────────────────────────────────────────────

    def ensure_portrait(self, stem: str, player: dict, force: bool = False,
                        model: str | None = None, style: str | None = None) -> dict:
        """Generate the portrait if missing (or forced); reuse the cache otherwise."""
        effective = model or self.model
        payload = self._player_payload(player)
        digest = self.source_hash("portrait", payload, model=effective, style=style)
        manifest = self._read_manifest(stem)
        cached = manifest.get("portrait") or {}
        if not force and self.has_portrait(stem) and cached.get("source_hash") == digest:
            return {"kind": "portrait", "generated": False, "cached": True, "model": effective}

        # Use the existing portrait as an identity reference so a regeneration
        # continues the same character instead of inventing a new face.
        refs = None
        existing = self.portrait_file(stem)
        if existing:
            try:
                refs = [(downscale_image(existing[0].read_bytes(), self.ref_max_side), existing[1])]
            except OSError:
                refs = None

        prompt = self.portrait_prompt(player, has_ref=bool(refs), style=style)
        raw = downscale_image(
            self.generate(prompt, refs=refs, ref_media_resolution=self.ref_media_resolution,
                                 model=effective, thinking_level=self.thinking_level),
            self.portrait_max_side,
        )
        # The Developer API returns JPEG for the Lite model (PNG isn't guaranteed),
        # so store whatever came back under the matching extension and drop stale
        # files of other formats.
        ext, mime = sniff_image(raw) or ("png", "image/png")
        target = self.image_dir(stem) / f"portrait.{ext}"
        drop_stale(self.image_dir(stem), "portrait", ext)
        write_bytes_atomic(target, raw)
        manifest["portrait"] = {
            "kind": "portrait",
            "file": target.name,
            "mime": mime,
            "model": effective,
            "aspect_ratio": self.aspect_ratio,
            "image_size": self.image_size,
            "thinking_level": self.thinking_level,
            "prompt": prompt,
            "source_hash": digest,
            "style": (style or self.style),
            "created": int(time.time()),
            "used_reference": bool(refs),
        }
        self._write_manifest(stem, manifest)
        return {"kind": "portrait", "generated": True, "cached": False, "model": effective}
