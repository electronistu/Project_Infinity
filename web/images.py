"""Cached image generation for the web client (Gemini / "Nano Banana").

Images are one-off assets reused forever — a character portrait today, stat and
inventory icons later. Each save owns a folder `output/images/{stem}/` holding
`{kind}.png` plus a `manifest.json` that records how each asset was made, so we
can skip the API when nothing relevant changed and regenerate on demand.

The provider is deliberately thin: `ensure_portrait` / `_generate_bytes` are the
only places that talk to Gemini, so swapping providers later is a small change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - surfaced as "unavailable" instead
    genai = None
    genai_types = None


_DEFAULT_MODEL = "gemini-3.1-flash-lite-image"
_DEFAULT_PORTRAIT = (
    "{reference_line} Head-and-shoulders character portrait of a {alignment} {gender} "
    "{race} {class}. {level_line} {gear} {background_line} Single subject, centred, "
    "facing the viewer, plain dark background, no other people."
)
_REFERENCE_LINE = (
    "Use the attached portrait as this character's identity reference — keep the same "
    "face, hair colour, build and distinguishing features; this is the same person later "
    "in their career."
)
_DEFAULT_SCENE = (
    "{location_line} A cinematic widescreen illustration of this moment: {description} "
    "The protagonist is a {race} {class}. {gear} Location: {location}. World setting: {world}. "
    "Mood: {mood}. Wide establishing composition, dramatic atmospheric lighting. "
    "No text, letters, numbers, runes, watermarks, logos or borders."
)
_SCENE_REFERENCE_LINE = (
    "Use the attached image as the established look of {location}: keep its architecture, "
    "colours, layout and atmosphere consistent unless the description says otherwise."
)
_SCENE_PORTRAIT_LINE = (
    "The attached portrait is the protagonist — place that same character in the scene as the "
    "central figure, in the described action and pose; keep the same face, hair colour and build."
)
_SCENE_BOTH_LINE = (
    "Attached references: (1) the established look of {location} — keep its architecture, colours "
    "and layout consistent unless the description says otherwise; (2) the protagonist's portrait — "
    "place that same character in the scene as the central figure."
)
_SCENE_PROTAGONIST_GUARD = (
    "The protagonist's appearance is fixed by the attached portrait — do not restate or "
    "alter their face, hair, build or clothing from the text; depict the scene around them."
)
_PORTRAIT_GUARD = (
    "The subject wears and wields exactly the equipment listed, nothing more — do not add or "
    "invent any garment, armour, weapon, hood, cloak, hat, helmet or accessory, and never depict "
    "gear that is not described."
)
_PARENS = re.compile(r"\([^)]*\)")
_PORTRAIT_EXTS = ("png", "jpg", "webp")
_MIME_BY_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """Best-effort (extension, media type) for image bytes, or None if unknown."""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def image_mime(path) -> str:
    return _MIME_BY_EXT.get(Path(path).suffix.lower().lstrip("."), "application/octet-stream")


def downscale_image(data: bytes, max_side: int) -> bytes:
    """Shrink image bytes so the longest side is <= max_side (best effort).

    Returns the original bytes unchanged if Pillow is missing or decoding fails,
    so a broken resize never costs us an image.
    """
    if not max_side or max_side <= 0 or not data:
        return data
    try:
        from io import BytesIO

        from PIL import Image

        im = Image.open(BytesIO(data))
        fmt = (im.format or "PNG").upper()
        if fmt == "JPEG" and im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        out = BytesIO()
        if fmt == "JPEG":
            im.save(out, format="JPEG", quality=88, optimize=True)
        else:
            im.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - never fail generation because of a resize
        return data


def _background_color(im):
    """Median of the four corner pixels (the flat backdrop)."""
    w, h = im.size
    pts = [im.getpixel((x, y)) for x in (1, w - 2) for y in (1, h - 2)]
    return tuple(sorted(p[c] for p in pts)[len(pts) // 2] for c in range(3))


def _hex_rgb(value):
    """'#F0E9DA' -> (240, 233, 218); None if unparseable."""
    s = str(value or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return None
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def fit_to_frame(data: bytes, fill: float = 0.92, threshold: float = 30.0,
                 max_side: int = 0, background=None, out_format: str | None = None) -> bytes:
    """Crop the subject to its bounding box and scale it to fill `fill` of the
    frame, centred on the background colour; optionally flatten the background
    to an exact colour and downscale to `max_side`.

    Generated art tends to sit small in a sea of flat background; this makes the
    subject use the space. Detection runs on a small proxy so paper-grain noise
    averages out. When `background` is given, every background pixel is replaced
    with that exact RGB so the result matches the character sheet precisely.
    Best effort: returns `data` unchanged on any failure.
    """
    if not data or fill <= 0:
        return data
    try:
        from io import BytesIO

        from PIL import Image, ImageChops

        src = Image.open(BytesIO(data))
        fmt = (out_format or src.format or "PNG").upper()
        im = src.convert("RGB")
        w, h = im.size
        if w < 8 or h < 8:
            return data

        proxy = im.copy()
        proxy.thumbnail((160, 160))
        pw, ph = proxy.size
        bg = _background_color(proxy)
        proxy_mask = ImageChops.difference(proxy, Image.new("RGB", (pw, ph), bg)).convert("L")
        bbox = proxy_mask.point(lambda v: 255 if v > threshold else 0).getbbox()
        if not bbox:
            return data
        sx, sy = w / pw, h / ph
        x0, y0 = int(bbox[0] * sx), int(bbox[1] * sy)
        x1, y1 = int(bbox[2] * sx), int(bbox[3] * sy)
        bw, bh = x1 - x0, y1 - y0
        if bw < 3 or bh < 3:
            return data

        bg_rgb = _hex_rgb(background) or bg
        if bg_rgb != bg:
            full_diff = ImageChops.difference(im, Image.new("RGB", (w, h), bg)).convert("L")
            subject_mask = full_diff.point(lambda v: 255 if v > threshold else 0)
            im.paste(Image.new("RGB", (w, h), bg_rgb), mask=ImageChops.invert(subject_mask))

        subject = im.crop((x0, y0, x1, y1))
        target = max(1, int(round(min(w, h) * fill)))
        scale = target / max(bw, bh)
        nw, nh = max(1, int(round(bw * scale))), max(1, int(round(bh * scale)))
        if nw > w or nh > h:  # never exceed the frame
            shrink = min(w / nw, h / nh)
            nw, nh = max(1, int(nw * shrink)), max(1, int(nh * shrink))
        subject = subject.resize((nw, nh), Image.LANCZOS)
        canvas = Image.new("RGB", (w, h), bg_rgb)
        canvas.paste(subject, ((w - nw) // 2, (h - nh) // 2))
        out = BytesIO()
        if fmt == "JPEG":
            canvas.save(out, format="JPEG", quality=92, optimize=True)
        else:
            canvas.save(out, format="PNG", optimize=True)
        result = out.getvalue()
    except Exception:  # noqa: BLE001 - never fail generation because of framing
        return data
    return downscale_image(result, max_side) if max_side else result


class ImageError(RuntimeError):
    """A user-facing image-generation failure (carries an HTTP status hint)."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


def _load_config(config_path: Path | None) -> dict:
    path = Path(config_path) if config_path else Path(__file__).resolve().parent.parent / "config" / "images.yml"
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _gender_word(value) -> str:
    word = str(value or "").strip().lower()
    return word if word in ("male", "female") else ""


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _veterancy_line(level) -> str:
    """A visual experience descriptor from the character level (affects the look).

    Deliberately describes only experience, scars and bearing — never gear, which comes solely
    from the character's equipped set (`_gear_line`).
    """
    try:
        lvl = int(level)
    except (TypeError, ValueError):
        return ""
    if lvl <= 1:
        return (f"This is a level-{lvl} novice: young, untested and unscarred, with an "
                "uncertain stance.")
    if lvl <= 4:
        return (f"This is a level-{lvl} adventurer: a few small scars, weathered by the road, "
                "and the first hints of confidence.")
    if lvl <= 10:
        return (f"This is a level-{lvl} seasoned adventurer: visible scars, a weathered face "
                "and an assured, capable bearing.")
    if lvl <= 16:
        return (f"This is a level-{lvl} renowned adventurer: deep scars, greying at the "
                "temples, and a commanding presence.")
    return (f"This is a level-{lvl} legendary adventurer: old wounds and an ageless "
            "near-mythic mien.")


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


def _friendly_error(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if "api key" in low or "api_key" in low or "unauthor" in low or "permission" in low:
        return "The Gemini API key was rejected."
    if "429" in text or "quota" in low or "rate" in low:
        return "The image service is rate-limited — try again shortly."
    if "safety" in low or "blocked" in low:
        return "The image model declined to render this character."
    return f"Image generation failed: {text[:200]}"


class GeminiImageBackend:
    """The one place that talks to Gemini for images (shared by all services)."""

    def __init__(self, model: str, aspect_ratio: str, image_size: str):
        self.model = model
        self.aspect_ratio = aspect_ratio
        self.image_size = image_size
        self._api_key = os.environ.get("GEMINI_API_KEY") or ""
        self._client = None

    def available(self) -> bool:
        return bool(self._api_key) and genai is not None and genai_types is not None

    def status(self) -> dict:
        return {
            "available": self.available(),
            "model": self.model,
            "aspect_ratio": self.aspect_ratio,
        }

    def _client_or_raise(self):
        if not self.available():
            raise ImageError("GEMINI_API_KEY is not set on the server.", status=503)
        if self._client is None:
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _generate_bytes(self, prompt: str, aspect_ratio: str | None = None,
                        refs=None, ref_media_resolution=None, model: str | None = None) -> bytes:
        client = self._client_or_raise()
        effective_model = model or self.model
        config = genai_types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            # NOTE: output_mime_type is Enterprise-only and is rejected by the
            # Gemini Developer API — never pass it here.
            image_config=genai_types.ImageConfig(
                aspect_ratio=aspect_ratio or self.aspect_ratio,
                image_size=self.image_size,
            ),
            # Image generation uses no tools; disable automatic function calling
            # so the SDK takes its plain path (and stops logging the "use Chat"
            # recommendation).
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
        )
        resolution = ref_media_resolution
        if isinstance(resolution, str):
            resolution = getattr(genai_types.PartMediaResolutionLevel, resolution, resolution)

        def _contents(with_refs: bool):
            parts: list = [prompt]
            for data, mime in (refs or []):
                if with_refs and data:
                    parts.append(genai_types.Part.from_bytes(
                        data=data, mime_type=mime, media_resolution=resolution,
                    ))
            return parts

        try:
            response = client.models.generate_content(
                model=effective_model, contents=_contents(True), config=config,
            )
        except Exception as exc:  # noqa: BLE001
            if refs:
                # The model may reject image input — fall back to text-only so a
                # portrait/scene is still produced (identity may drift).
                try:
                    response = client.models.generate_content(
                        model=effective_model, contents=_contents(False), config=config,
                    )
                except Exception as exc2:  # noqa: BLE001
                    raise ImageError(_friendly_error(exc2)) from exc2
            else:
                raise ImageError(_friendly_error(exc)) from exc

        candidates = getattr(response, "candidates", None) or []
        content = getattr(candidates[0], "content", None) if candidates else None
        if content is None or not getattr(content, "parts", None):
            raise ImageError("The image model returned no image.")
        for part in content.parts:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline is not None else None
            if data:
                return data
        raise ImageError("The image model returned no image.")


class ImageService(GeminiImageBackend):
    """Generates and caches per-save image assets with Gemini."""

    def __init__(self, output_dir, config_path: Path | None = None):
        self.output_dir = Path(output_dir)
        cfg = _load_config(config_path)
        super().__init__(
            str(cfg.get("model") or _DEFAULT_MODEL),
            str(cfg.get("aspect_ratio") or "3:4"),
            str(cfg.get("image_size") or "1K"),
        )
        self.style = str(cfg.get("style") or "").strip()
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
        for ext in _PORTRAIT_EXTS:
            candidate = directory / f"portrait.{ext}"
            if candidate.exists():
                return candidate
        return directory / "portrait.png"

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
        return {
            "race": str(player.get("race") or ""),
            "character_class": str(player.get("character_class") or ""),
            "background": str(player.get("background") or ""),
            "alignment": str(player.get("alignment") or ""),
            "gender": str(player.get("gender") or ""),
            "level": player.get("level"),
            "gear": _gear_line(player),
        }

    def source_hash(self, kind: str, payload: dict, model: str | None = None) -> str:
        blob = json.dumps(
            {"kind": kind, "model": model or self.model, "aspect": self.aspect_ratio,
             "style": self.style, "payload": payload},
            sort_keys=True, default=str,
        )
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    def portrait_prompt(self, player: dict, has_ref: bool = False) -> str:
        payload = self._player_payload(player)
        fields = {
            "alignment": payload["alignment"] or "wandering",
            "gender": payload["gender"] or "figure",
            "race": payload["race"] or "adventurer",
            "class": payload["character_class"] or "adventurer",
            "gear": payload["gear"],
            "level_line": _veterancy_line(payload["level"]),
            "reference_line": _REFERENCE_LINE if has_ref else "",
            "background_line": (f"Background: {payload['background']}." if payload["background"] else ""),
        }
        template = self.portrait_template or _DEFAULT_PORTRAIT
        body = re.sub(r"\s+", " ", template.format(**fields)).strip()
        parts = [body]
        if self.style:
            parts.append(self.style)
        if self.portrait_guard:
            parts.append(self.portrait_guard)
        return " ".join(parts).strip()

    # ── manifest helpers ───────────────────────────────────────────────────

    def _read_manifest(self, stem: str) -> dict:
        path = self.manifest_path(stem)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_manifest(self, stem: str, manifest: dict) -> None:
        path = self.manifest_path(stem)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _write_image(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    # ── generation ────────────────────────────────────────────────────────

    def ensure_portrait(self, stem: str, player: dict, force: bool = False,
                        model: str | None = None) -> dict:
        """Generate the portrait if missing (or forced); reuse the cache otherwise."""
        effective = model or self.model
        payload = self._player_payload(player)
        digest = self.source_hash("portrait", payload, model=effective)
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

        prompt = self.portrait_prompt(player, has_ref=bool(refs))
        raw = downscale_image(
            self._generate_bytes(prompt, refs=refs, ref_media_resolution=self.ref_media_resolution,
                                 model=effective),
            self.portrait_max_side,
        )
        # The Developer API returns JPEG for the Lite model (PNG isn't guaranteed),
        # so store whatever came back under the matching extension and drop stale
        # files of other formats.
        ext, mime = sniff_image(raw) or ("png", "image/png")
        target = self.image_dir(stem) / f"portrait.{ext}"
        for other in _PORTRAIT_EXTS:
            stale = self.image_dir(stem) / f"portrait.{other}"
            if other != ext and stale.exists():
                stale.unlink()
        self._write_image(target, raw)
        manifest["portrait"] = {
            "kind": "portrait",
            "file": target.name,
            "mime": mime,
            "model": effective,
            "aspect_ratio": self.aspect_ratio,
            "image_size": self.image_size,
            "prompt": prompt,
            "source_hash": digest,
            "created": int(time.time()),
            "used_reference": bool(refs),
        }
        self._write_manifest(stem, manifest)
        return {"kind": "portrait", "generated": True, "cached": False, "model": effective}


def _slug(text) -> str:
    """A filesystem/route-safe slug for a location name."""
    text = _PARENS.sub(" ", str(text or "").lower())
    text = re.sub(r"['\u2019]s\b", " ", text)
    text = re.sub(r"\b[+-]?\d+\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def known_scene_locations(output_dir, stem: str) -> list[str]:
    """Location names already used for a save (from the scene manifest)."""
    path = Path(output_dir) / "images" / stem / "scenes" / "manifest.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in (data or {}).values():
        location = str((entry or {}).get("location") or "").strip()
        key = location.lower()
        if location and key not in seen:
            seen.add(key)
            out.append(location)
    return sorted(out, key=str.lower)


class SceneService(GeminiImageBackend):
    """Generates + persists storyline scene images (16:9), one per location.

    Images live under `output/images/{stem}/scenes/{slug}.{ext}` with a
    `manifest.json` keyed by location slug, so a place looks consistent when the
    player returns (each new image is generated from the previous one).
    """

    def __init__(self, output_dir=None, config_path: Path | None = None):
        cfg = _load_config(config_path)
        super().__init__(
            str(cfg.get("model") or _DEFAULT_MODEL),
            str(cfg.get("scene_aspect_ratio") or "16:9"),
            str(cfg.get("image_size") or "1K"),
        )
        self.output_dir = Path(output_dir) if output_dir else None
        # Scenes share the portrait's locked painterly style unless overridden.
        self.style = str(cfg.get("scene_style") or cfg.get("style") or "").strip()
        self.template = str(cfg.get("scene") or _DEFAULT_SCENE).strip()
        self.reference_template = str(
            cfg.get("scene_reference_line") or _SCENE_REFERENCE_LINE
        ).strip()
        self.portrait_reference = str(
            cfg.get("scene_portrait_reference_line") or _SCENE_PORTRAIT_LINE
        ).strip()
        self.both_reference = str(
            cfg.get("scene_reference_both") or _SCENE_BOTH_LINE
        ).strip()
        self.protagonist_guard = str(
            cfg.get("scene_protagonist_guard") or _SCENE_PROTAGONIST_GUARD
        ).strip()
        self.ref_max_side = _int_or(cfg.get("ref_max_side"), 512)
        self.ref_media_resolution = str(cfg.get("ref_media_resolution") or "MEDIA_RESOLUTION_MEDIUM").strip()
        try:
            self.max_side = int(cfg.get("scene_max_side") or 0)
        except (TypeError, ValueError):
            self.max_side = 0

    # ── paths / manifest ───────────────────────────────────────────────────

    def scenes_dir(self, stem: str) -> Path:
        base = self.output_dir or Path(".")
        return base / "images" / stem / "scenes"

    def manifest_path(self, stem: str) -> Path:
        return self.scenes_dir(stem) / "manifest.json"

    def scene_path(self, stem: str, slug: str) -> Path:
        directory = self.scenes_dir(stem)
        for ext in _PORTRAIT_EXTS:
            candidate = directory / f"{slug}.{ext}"
            if candidate.exists():
                return candidate
        return directory / f"{slug}.png"

    def _read_manifest(self, stem: str) -> dict:
        path = self.manifest_path(stem)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_manifest(self, stem: str, data: dict) -> None:
        path = self.manifest_path(stem)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def url_for(self, stem: str, slug: str) -> str | None:
        if not self.scene_path(stem, slug).exists():
            return None
        return f"/api/scenes/{stem}/{slug}"

    def get_scene(self, stem: str, name: str) -> tuple[bytes, str] | None:
        entry = self._read_manifest(stem).get(name)
        if not isinstance(entry, dict):
            return None
        path = self.scenes_dir(stem) / str(entry.get("file") or "")
        if not path.exists():
            return None
        try:
            return path.read_bytes(), str(entry.get("mime") or image_mime(path))
        except OSError:
            return None

    def _read_portrait(self, stem: str) -> tuple[bytes, str] | None:
        directory = (self.output_dir or Path(".")) / "images" / stem
        for ext in _PORTRAIT_EXTS:
            path = directory / f"portrait.{ext}"
            if path.exists():
                try:
                    return (downscale_image(path.read_bytes(), self.ref_max_side), image_mime(path))
                except OSError:
                    return None
        return None

    # ── prompt / generation ────────────────────────────────────────────────

    def scene_prompt(self, player: dict, world: str, description: str, mood: str = "",
                     location: str = "", has_location_ref: bool = False,
                     has_portrait_ref: bool = False) -> str:
        player = player or {}
        loc = re.sub(r"\s+", " ", str(location or "")).strip() or "an unnamed place"
        if has_location_ref and has_portrait_ref:
            ref_line = self.both_reference.format(location=loc)
        elif has_location_ref:
            ref_line = self.reference_template.format(location=loc)
        elif has_portrait_ref:
            ref_line = self.portrait_reference
        else:
            ref_line = ""
        if has_portrait_ref and self.protagonist_guard:
            ref_line = (ref_line + " " + self.protagonist_guard).strip()
        fields = {
            "description": re.sub(r"\s+", " ", str(description or "")).strip(),
            "race": str(player.get("race") or "") or "adventurer",
            "class": str(player.get("character_class") or "") or "adventurer",
            "gear": _gear_line(player),
            "world": re.sub(r"\s+", " ", str(world or "")).strip() or "a fantasy realm",
            "location": loc,
            "mood": str(mood or "").strip() or "tense and atmospheric",
            "location_line": ref_line,
        }
        body = re.sub(r"\s+", " ", self.template.format(**fields)).strip()
        return (body + " " + self.style).strip() if self.style else body

    def generate_scene(self, player: dict, world: str, description: str, mood: str = "",
                       location: str = "", refs=None, has_location_ref: bool = False,
                       has_portrait_ref: bool = False, model: str | None = None) -> bytes:
        prompt = self.scene_prompt(player, world, description, mood, location,
                                   has_location_ref=has_location_ref,
                                   has_portrait_ref=has_portrait_ref)
        raw = self._generate_bytes(prompt, aspect_ratio=self.aspect_ratio, refs=refs,
                                   ref_media_resolution=self.ref_media_resolution,
                                   model=model or self.model)
        return downscale_image(raw, self.max_side) if self.max_side else raw

    def ensure_scene(self, stem: str, player: dict, world: str, description: str,
                     mood: str = "", location: str = "",
                     model: str | None = None) -> dict:
        """Generate (always) and persist; attach the location's previous image
        (if any) and the protagonist's portrait as continuity references. One
        file per location (latest wins)."""
        effective = model or self.model
        slug = _slug(location) or ("scene-" + hashlib.sha1(
            f"{description}|{time.time()}".encode("utf-8")).hexdigest()[:12])
        manifest = self._read_manifest(stem)

        location_ref = None
        if location and slug in manifest:
            existing = self.scene_path(stem, slug)
            if existing.exists():
                try:
                    location_ref = (downscale_image(existing.read_bytes(), self.ref_max_side),
                                     image_mime(existing))
                except OSError:
                    location_ref = None
        portrait_ref = self._read_portrait(stem)
        refs = [r for r in (location_ref, portrait_ref) if r]

        raw = self.generate_scene(player, world, description, mood, location, refs=refs,
                                  has_location_ref=bool(location_ref),
                                  has_portrait_ref=bool(portrait_ref),
                                  model=effective)
        ext, mime = sniff_image(raw) or ("png", "image/png")
        target = self.scenes_dir(stem) / f"{slug}.{ext}"
        for other in _PORTRAIT_EXTS:
            stale = self.scenes_dir(stem) / f"{slug}.{other}"
            if other != ext and stale.exists():
                stale.unlink()
        ImageService._write_image(target, raw)
        created = int(time.time())
        manifest[slug] = {
            "file": target.name, "slug": slug,
            "location": str(location or ""),
            "description": str(description or ""), "mood": str(mood or ""),
            "mime": mime, "model": effective, "created": created,
            "used_location_reference": bool(location_ref),
            "used_portrait_reference": bool(portrait_ref),
        }
        self._write_manifest(stem, manifest)
        return {"url": self.url_for(stem, slug),
                "created": created, "used_reference": bool(refs),
                "used_location_reference": bool(location_ref),
                "used_portrait_reference": bool(portrait_ref)}
