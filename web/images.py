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
_SCENE_PORTRAIT_LINE = (
    "The attached portrait is the protagonist — keep the same face, hair colour and build; place "
    "that character in the described action and pose."
)
_SCENE_PROTAGONIST_GUARD = (
    "The protagonist's appearance is fixed by the attached portrait — do not restate or "
    "alter their face, hair, build or clothing from the text; depict the scene around them. "
    "In the scene the protagonist does not look at or address the camera — turn them into the "
    "action despite the portrait's frontal pose: they face whatever they are doing, not the lens, "
    "and the camera may see their back. Never rotate the protagonist toward the camera to match "
    "the portrait."
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
    """A filesystem/route-safe slug for a place name."""
    text = _PARENS.sub(" ", str(text or "").lower())
    text = re.sub(r"['\u2019]s\b", " ", text)
    text = re.sub(r"\b[+-]?\d+\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _npc_fields(value) -> dict:
    """Normalize a declared NPC to {name, description, role}. A legacy plain string is a description."""
    if isinstance(value, dict):
        return {"name": re.sub(r"\s+", " ", str(value.get("name") or "")).strip(),
                "description": re.sub(r"\s+", " ", str(value.get("description") or "")).strip(),
                "role": re.sub(r"\s+", " ", str(value.get("role") or "")).strip()}
    return {"name": "", "description": re.sub(r"\s+", " ", str(value or "")).strip(), "role": ""}


def _npc_list(value) -> list[dict]:
    """Normalize a place's main NPCs to a list of {name, description, role}.

    A list is the canonical shape (v6); a legacy single dict/string becomes a one-item
    list. Entries with neither a name nor a description are dropped."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[dict] = []
    for item in items:
        fields = _npc_fields(item)
        if fields["name"] or fields["description"]:
            out.append(fields)
    return out


def _seed_main_npcs(entry) -> list[dict]:
    """A seed entry's main NPCs, reading the v6 `main_npcs` key or a legacy `main_npc`."""
    raw = (entry or {}).get("main_npcs")
    if raw is None:
        raw = (entry or {}).get("main_npc")
    return _npc_list(raw)


def _manifest_entries(data) -> list[dict]:
    """Normalize a scene manifest (v1 flat / v2-v6) to a list of seed dicts."""
    if not isinstance(data, dict):
        return []
    if data.get("version") in (2, 3, 4, 5, 6) and isinstance(data.get("seeds"), dict):
        return [e for e in data["seeds"].values() if isinstance(e, dict)]
    # v1: {location_slug: entry}
    return [e for e in data.values() if isinstance(e, dict) and e.get("location")]


def known_scene_places(output_dir, stem: str) -> list[dict]:
    """Seeded places for a save: {kingdom, area, location, sublocation, description}."""
    path = Path(output_dir) / "images" / stem / "scenes" / "manifest.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict] = []
    for entry in _manifest_entries(data):
        kingdom = str(entry.get("kingdom") or "").strip()
        area = str(entry.get("area") or "").strip()
        location = str(entry.get("location") or "").strip()
        sublocation = str(entry.get("sublocation") or "").strip()
        key = (kingdom.lower(), area.lower(), location.lower(), sublocation.lower())
        if not location or key in seen:
            continue
        seen.add(key)
        out.append({"kingdom": kingdom, "area": area, "location": location,
                    "sublocation": sublocation,
                    "description": str(entry.get("description") or "").strip(),
                    "main_npcs": _seed_main_npcs(entry)})
    return sorted(out, key=lambda p: (p["kingdom"].lower(), p["area"].lower(),
                                      p["location"].lower(), p["sublocation"].lower()))


def known_npc_names(output_dir, stem: str) -> set[str]:
    """Declared NPC names for a save (place main NPCs + the storyline cast), lowercased."""
    path = Path(output_dir) / "images" / stem / "scenes" / "manifest.json"
    names: set[str] = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return names
    for entry in _manifest_entries(data):
        for npc in _seed_main_npcs(entry):
            if npc["name"]:
                names.add(npc["name"].lower())
    raw_cast = data.get("cast") if isinstance(data, dict) else None
    if isinstance(raw_cast, dict):
        for entry in raw_cast.values():
            name = _npc_fields(entry)["name"]
            if name:
                names.add(name.lower())
    return names


def known_scene_locations(output_dir, stem: str) -> list[str]:
    """Distinct location names already seeded for a save (back-compat helper)."""
    seen: list[str] = []
    for place in known_scene_places(output_dir, stem):
        if place["location"] and place["location"].lower() not in {s.lower() for s in seen}:
            seen.append(place["location"])
    return sorted(seen, key=str.lower)


_DEFAULT_SCENE_SEED = (
    "A wide establishing view of {address_line}, seen at eye level or a slight three-quarter "
    "angle. {establishing} World setting: {world}. Empty and unpopulated — no people, no "
    "creatures, no animals, no protagonist. Timeless and weather-neutral: no rain, snow, fog, "
    "mist or time-of-day light (no dawn, dusk or night); the light is neutral. Atmospheric, strong "
    "sense of place, painterly detail. No text, letters, numbers, runes, watermarks, logos or "
    "borders."
)
_SCENE_SEED_GUARD = (
    "The scene must contain no person, no creature and no animal of any kind — an empty place."
)
_SCENE_SEED_CHANGE_LINE = (
    "Apply this permanent change to the place: {change}. Keep the architecture, layout, colours "
    "and everything else consistent with the attached image."
)
_SCENE_ACTION_FROM_SEED = (
    "Attached references: (1) the establishing view of {location} — {sublocation}: this IS the "
    "picture — reproduce it unchanged (same camera, framing, architecture, furniture, props, "
    "colours, arrangement), then place the characters into it. Change nothing about the space "
    "itself, except the three things the moment legitimately changes: the time of day (relight "
    "the same scene for day or night), the weather (rain, fog, snow, wind — the same room under a "
    "different sky), and the action's own transient damage or mess — a table split, a chair "
    "toppled, a hearth scattered, spilled ale, a fire. Objects the action touches may be broken "
    "or displaced; the room is never rearranged. (2) the protagonist's portrait — that same "
    "character, in the described action."
)
_SCENE_NO_EXTRA_FIGURES = (
    "Draw only the protagonist and the characters listed above: no other person, face, silhouette, "
    "figure, reflection or background crowd may appear."
)
_SCENE_NO_CAMERA_GAZE = (
    "No one looks at, faces or acknowledges the camera or the viewer — every figure is caught "
    "mid-action, absorbed in what they are doing and facing into the scene; every figure faces "
    "the ACTION at hand, and a figure seen from behind, back to the camera, is correct whenever "
    "the action is in front of them."
)
_SCENE_LIGHTING_GUARD = (
    "Match the declared time of day exactly. At night or deep night draw true night: deep "
    "darkness over everything except the local light sources — a fire, a lamp, a lantern, a "
    "moonlit doorway — each throwing only a small pool of light around itself. No twilight, no "
    "dusk glow, no sunset colours, no ambient daylight, no blue hour."
)
_SCENE_LAYOUT_LOCK = (
    "The attached establishing view IS the scene: keep the same camera, framing, crop, architecture, "
    "walls, doors, windows, stairs, furniture, props, their positions, scale and proportions. Do "
    "not redesign, add, remove, resize, restyle or rearrange anything, and do not change the "
    "viewpoint, the framing or the composition. Only what the moment legitimately changes may "
    "differ: the time of day (relight this identical scene for day or night), the weather "
    "(atmosphere only — it never alters the space), and the transient damage or mess of the action "
    "(a table split, a chair toppled, a fire, spilled ale). Then place the characters into that "
    "same space."
)
_SCENE_ABSENT_ELEMENT_GUARD = (
    "The attached establishing view is the ground truth for the space: never invent an element it "
    "does not show — no new door, doorway, window, opening, stair, beam, fixture or furniture. "
    "Place characters only where that view allows; if a character is described at a spot it does "
    "not contain (standing in a doorway that is not there), move them into the closest visible "
    "place; if that is impossible, leave them out of the picture. This applies to every figure, "
    "the protagonist included: never invent a place to keep them in frame."
)
_SCENE_TIME_LINE = "Time of day: {time_of_day}."
_SCENE_WEATHER_LINE = "Weather: {weather}."
_SCENE_CHARACTERS_LINE = "Characters present (render each exactly as described; add no one else):"
_SCENE_CHARACTERS_GUARD = (
    "Render every listed character exactly as described — a described woman must not be drawn "
    "as a man — and add no character who is not listed."
)
_DEFAULT_SCENE_ACTION = (
    "{location_line} A cinematic widescreen illustration of this moment: {description} "
    "The protagonist is a {race} {class}. {gear} Location: {address_line}. "
    "World setting: {world}. {atmosphere_line} {characters_line} Mood: {mood}. Wide establishing "
    "composition, dramatic atmospheric lighting. No text, letters, numbers, runes, watermarks, "
    "logos or borders."
)


class SceneService(GeminiImageBackend):
    """Generates + persists a hidden, permanent establishing **seed** per place
    `(kingdom, area, location, sublocation)` and a visible **action** image drawn
    fresh from that seed + the portrait. Actions are never referenced by later
    actions (that made the model duplicate the figures already in the frame) and
    are deleted as soon as they have been served once.

    `output/images/{stem}/scenes/manifest.json` is the v6 manifest:
        {version, seeds: {key: entry}, cast: {name_slug: {name, description, role}}, current: {...}}
    Each seed entry carries `main_npcs` = a list of {name, description, role}. The seed is
    never served to the client; it is the only persisted scene asset. The `cast` map keeps
    the declared NPC looks; the GM only ever passes their names.
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
        self.seed_template = str(cfg.get("scene_seed") or _DEFAULT_SCENE_SEED).strip()
        self.seed_guard = str(cfg.get("scene_seed_guard") or _SCENE_SEED_GUARD).strip()
        self.seed_change_line = str(cfg.get("scene_seed_change") or _SCENE_SEED_CHANGE_LINE).strip()
        self.template = str(cfg.get("scene") or _DEFAULT_SCENE_ACTION).strip()
        self.action_from_seed = str(
            cfg.get("scene_action_from_seed") or _SCENE_ACTION_FROM_SEED
        ).strip()
        self.no_extra_figures = str(
            cfg.get("scene_no_extra_figures") or _SCENE_NO_EXTRA_FIGURES
        ).strip()
        self.no_camera_gaze = str(
            cfg.get("scene_no_camera_gaze") or _SCENE_NO_CAMERA_GAZE
        ).strip()
        self.lighting_guard = str(
            cfg.get("scene_lighting_guard") or _SCENE_LIGHTING_GUARD
        ).strip()
        self.layout_lock = str(
            cfg.get("scene_layout_lock") or _SCENE_LAYOUT_LOCK
        ).strip()
        self.absent_element_guard = str(
            cfg.get("scene_absent_element_guard") or _SCENE_ABSENT_ELEMENT_GUARD
        ).strip()
        self.portrait_reference = str(
            cfg.get("scene_portrait_reference_line") or _SCENE_PORTRAIT_LINE
        ).strip()
        self.protagonist_guard = str(
            cfg.get("scene_protagonist_guard") or _SCENE_PROTAGONIST_GUARD
        ).strip()
        self.time_line = str(cfg.get("scene_time_line") or _SCENE_TIME_LINE).strip()
        self.weather_line = str(cfg.get("scene_weather_line") or _SCENE_WEATHER_LINE).strip()
        self.characters_header = str(cfg.get("scene_characters_line") or _SCENE_CHARACTERS_LINE).strip()
        self.characters_guard = str(cfg.get("scene_characters_guard") or _SCENE_CHARACTERS_GUARD).strip()
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

    @staticmethod
    def _key(kingdom, area, location, sublocation) -> str:
        return "|".join(_slug(p) for p in (kingdom, area, location, sublocation))

    def _read_manifest(self, stem: str) -> dict:
        path = self.manifest_path(stem)
        empty = {"version": 6, "seeds": {}, "cast": {}, "current": {}}
        if not path.exists():
            return empty
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        if isinstance(data, dict) and data.get("version") in (2, 3, 4, 5, 6) and isinstance(data.get("seeds"), dict):
            if data.get("version") not in (4, 5, 6):
                # Action images became ephemeral one-shot files in v4: clean up any left over.
                for entry in (data.get("actions") or []):
                    try:
                        (self.scenes_dir(stem) / str((entry or {}).get("file") or "")).unlink()
                    except OSError:
                        pass
            seeds: dict = {}
            for key, entry in data["seeds"].items():
                if not isinstance(entry, dict):
                    continue
                entry = dict(entry)
                entry.pop("last_cast", None)
                entry["main_npcs"] = _seed_main_npcs(entry)
                entry.pop("main_npc", None)
                seeds[key] = entry
            cast: dict = {}
            raw_cast = data.get("cast")
            if isinstance(raw_cast, dict):
                for entry in raw_cast.values():
                    fields = _npc_fields(entry)
                    if fields["name"]:
                        cast[_slug(fields["name"])] = fields
            return {"version": 6, "seeds": seeds, "cast": cast, "current": data.get("current") or {}}
        # v1 flat manifest: {location_slug: entry}
        seeds: dict = {}
        now = int(time.time())
        for slug, entry in (data or {}).items():
            if not isinstance(entry, dict):
                continue
            location = str(entry.get("location") or "").strip()
            if not location:
                continue
            kingdom = str(entry.get("kingdom") or "").strip()
            area = str(entry.get("area") or "").strip()
            sublocation = str(entry.get("sublocation") or "").strip()
            seeds[self._key(kingdom, area, location, sublocation)] = {
                "file": entry.get("file"), "slug": slug, "kingdom": kingdom, "area": area,
                "location": location, "sublocation": sublocation,
                "description": str(entry.get("description") or ""),
                "main_npcs": _npc_list(entry.get("main_npc")),
                "created": entry.get("created") or now,
                "mime": entry.get("mime") or "image/png",
            }
        return {"version": 6, "seeds": seeds, "cast": {}, "current": {}}

    def _write_manifest(self, stem: str, data: dict) -> None:
        path = self.manifest_path(stem)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _file_bytes(self, stem: str, entry) -> tuple[bytes, str] | None:
        path = self.scenes_dir(stem) / str((entry or {}).get("file") or "")
        if not path.exists():
            return None
        try:
            return (downscale_image(path.read_bytes(), self.ref_max_side), image_mime(path))
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

    @staticmethod
    def _seed_by_place(seeds: dict, location: str, sublocation: str, current=None):
        """Resolve a seed from location/sublocation when kingdom/area are omitted."""
        loc, sub = _slug(location), _slug(sublocation)
        matches = [(k, v) for k, v in (seeds or {}).items()
                   if _slug(v.get("location")) == loc and _slug(v.get("sublocation")) == sub]
        if not matches:
            return None, None
        cur = current or {}
        cur_k, cur_a = _slug(cur.get("kingdom")), _slug(cur.get("area"))
        for k, v in matches:
            if _slug(v.get("kingdom")) == cur_k and _slug(v.get("area")) == cur_a:
                return k, v
        if len(matches) == 1:
            return matches[0]
        return max(matches, key=lambda kv: int(kv[1].get("created") or 0))

    # ── serving (action images only; seeds are hidden) ──────────────────────

    def get_scene(self, stem: str, name: str) -> tuple[bytes, str] | None:
        """Serve an action image once, then delete it: actions are ephemeral.
        Seeds are internal and are never served."""
        if not str(name or "").startswith("action-"):
            return None
        directory = self.scenes_dir(stem)
        for ext in _PORTRAIT_EXTS:
            path = directory / f"{name}.{ext}"
            if not path.exists():
                continue
            try:
                data = path.read_bytes()
                mime = image_mime(path)
            except OSError:
                return None
            try:
                path.unlink()
            except OSError:
                pass
            return data, mime
        return None

    # ── prompts ─────────────────────────────────────────────────────────────

    @staticmethod
    def _clean(text) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _address_line(self, kingdom: str, area: str, location: str, sublocation: str) -> str:
        loc = self._clean(location)
        sub = self._clean(sublocation)
        place = f"{loc} — {sub}" if loc and sub else (loc or sub)
        parts = [p for p in (place, self._clean(area), self._clean(kingdom)) if p]
        return ", ".join(parts) or "an unnamed place"

    def _atmosphere_line(self, time_of_day: str, weather: str) -> str:
        """The explicit time/weather sentence, omitting empty parts."""
        parts = []
        tod = self._clean(time_of_day)
        w = self._clean(weather)
        if tod:
            parts.append(self.time_line.format(time_of_day=tod))
        if w:
            parts.append(self.weather_line.format(weather=w))
        return " ".join(parts)

    def _characters_line(self, characters) -> str:
        """One bullet per on-stage character: `<identity look>: <action>`."""
        if not isinstance(characters, dict):
            return ""
        rows = []
        for key, value in characters.items():
            k = self._clean(key)
            if not k:
                continue
            v = self._clean(value)
            rows.append(f"- {k}: {v}" if v else f"- {k}")
        if not rows:
            return ""
        return self.characters_header + "\n" + "\n".join(rows)

    def seed_prompt(self, world: str, kingdom: str = "", area: str = "", location: str = "",
                    sublocation: str = "", establishing: str = "", change: str = "",
                    has_seed_ref: bool = False) -> str:
        loc = self._clean(location) or "an unnamed place"
        sub = self._clean(sublocation)
        fields = {
            "address_line": self._address_line(kingdom, area, location, sublocation),
            "kingdom": self._clean(kingdom), "area": self._clean(area),
            "location": loc, "sublocation": sub or loc,
            "establishing": self._clean(establishing),
            "change": self._clean(change),
            "world": self._clean(world) or "a fantasy realm",
        }
        body = re.sub(r"[^\S\n]+", " ", self.seed_template.format(**fields)).strip()
        if has_seed_ref and fields["change"]:
            body = (body + " " + self.seed_change_line.format(**fields)).strip()
        if self.seed_guard:
            body = (body + " " + self.seed_guard).strip()
        return (body + " " + self.style).strip() if self.style else body

    def action_prompt(self, player: dict, world: str, description: str, mood: str,
                      kingdom: str = "", area: str = "", location: str = "", sublocation: str = "",
                      ref_kind: str = "", time_of_day: str = "", weather: str = "",
                      characters=None) -> str:
        player = player or {}
        loc = self._clean(location) or "an unnamed place"
        sub = self._clean(sublocation)
        if ref_kind == "seed":
            ref_line = self.action_from_seed.format(location=loc, sublocation=sub or loc)
        elif ref_kind == "portrait":
            ref_line = self.portrait_reference
        else:
            ref_line = ""
        if ref_kind and self.protagonist_guard:
            ref_line = (ref_line + " " + self.protagonist_guard).strip()
        fields = {
            "description": self._clean(description),
            "race": str(player.get("race") or "") or "adventurer",
            "class": str(player.get("character_class") or "") or "adventurer",
            "gear": _gear_line(player),
            "world": self._clean(world) or "a fantasy realm",
            "kingdom": self._clean(kingdom), "area": self._clean(area),
            "location": loc, "sublocation": sub or loc,
            "address_line": self._address_line(kingdom, area, location, sublocation),
            "mood": self._clean(mood) or "tense and atmospheric",
            "atmosphere_line": self._atmosphere_line(time_of_day, weather),
            "characters_line": self._characters_line(characters),
            "location_line": ref_line,
        }
        # Collapse horizontal whitespace but keep the character bullets on their own lines.
        body = re.sub(r"[^\S\n]+", " ", self.template.format(**fields)).strip()
        if fields["characters_line"] and self.characters_guard:
            body = (body + " " + self.characters_guard).strip()
        if self.no_extra_figures:
            body = (body + " " + self.no_extra_figures).strip()
        if self.no_camera_gaze:
            body = (body + " " + self.no_camera_gaze).strip()
        if self.lighting_guard:
            body = (body + " " + self.lighting_guard).strip()
        if ref_kind == "seed" and self.layout_lock:
            body = (body + " " + self.layout_lock).strip()
        if ref_kind == "seed" and self.absent_element_guard:
            body = (body + " " + self.absent_element_guard).strip()
        return (body + " " + self.style).strip() if self.style else body

    # ── generation ──────────────────────────────────────────────────────────

    def generate_seed(self, world: str, kingdom: str = "", area: str = "", location: str = "",
                      sublocation: str = "", establishing: str = "", change: str = "", refs=None,
                      model: str | None = None) -> bytes:
        prompt = self.seed_prompt(world, kingdom, area, location, sublocation, establishing,
                                  change, has_seed_ref=bool(refs))
        raw = self._generate_bytes(prompt, aspect_ratio=self.aspect_ratio, refs=refs,
                                   ref_media_resolution=self.ref_media_resolution,
                                   model=model or self.model)
        return downscale_image(raw, self.max_side) if self.max_side else raw

    def generate_action(self, player: dict, world: str, description: str, mood: str,
                        kingdom: str = "", area: str = "", location: str = "", sublocation: str = "",
                        ref_kind: str = "", refs=None, model: str | None = None,
                        time_of_day: str = "", weather: str = "", characters=None) -> bytes:
        prompt = self.action_prompt(player, world, description, mood, kingdom, area, location,
                                    sublocation, ref_kind=ref_kind, time_of_day=time_of_day,
                                    weather=weather, characters=characters)
        raw = self._generate_bytes(prompt, aspect_ratio=self.aspect_ratio, refs=refs,
                                   ref_media_resolution=self.ref_media_resolution,
                                   model=model or self.model)
        return downscale_image(raw, self.max_side) if self.max_side else raw

    # ── orchestration ───────────────────────────────────────────────────────

    def _seed_slug(self, kingdom: str, area: str, location: str, sublocation: str) -> str:
        h = hashlib.sha1(self._key(kingdom, area, location, sublocation).encode("utf-8")).hexdigest()[:6]
        return f"seed-{_slug(location)[:16] or 'place'}-{_slug(sublocation)[:16] or 'main'}-{h}"

    def _action_slug(self, kingdom: str, area: str, location: str, sublocation: str) -> str:
        """A unique name per request — actions are one-shot and never revisited."""
        h = hashlib.sha1(self._key(kingdom, area, location, sublocation).encode("utf-8")).hexdigest()[:6]
        token = hashlib.sha1(str(time.time_ns()).encode("ascii")).hexdigest()[:8]
        return f"action-{_slug(location)[:16] or 'place'}-{_slug(sublocation)[:16] or 'main'}-{h}-{token}"

    def _store_seed(self, stem: str, manifest: dict, kingdom: str, area: str, location: str,
                    sublocation: str, description: str, main_npcs, raw: bytes,
                    existing=None) -> dict:
        ext, mime = sniff_image(raw) or ("png", "image/png")
        slug = (existing or {}).get("slug") or self._seed_slug(kingdom, area, location, sublocation)
        file = f"{slug}.{ext}"
        directory = self.scenes_dir(stem)
        for other in _PORTRAIT_EXTS:
            stale = directory / f"{slug}.{other}"
            if other != ext and stale.exists():
                stale.unlink()
        ImageService._write_image(directory / file, raw)
        entry = {
            "file": file, "slug": slug, "kingdom": str(kingdom or ""), "area": str(area or ""),
            "location": str(location or ""), "sublocation": str(sublocation or ""),
            "description": str(description or ""), "main_npcs": _npc_list(main_npcs),
            "mime": mime, "created": int(time.time()),
        }
        manifest.setdefault("seeds", {})[self._key(kingdom, area, location, sublocation)] = entry
        return entry

    @staticmethod
    def _resolve_characters(characters, seed, cast) -> dict:
        """Map `{name: action}` to `{name — description: action}` for the image prompt.

        Names resolve against the place's main NPCs first, then the save's storyline cast;
        an undeclared name is drawn from its own key text (a one-off extra, not remembered)."""
        if not isinstance(characters, dict):
            return {}
        known: dict[str, dict] = {}
        for place in _seed_main_npcs(seed):
            if place["name"]:
                known[place["name"].lower()] = place
        if isinstance(cast, dict):
            for entry in cast.values():
                fields = _npc_fields(entry)
                if fields["name"]:
                    known.setdefault(fields["name"].lower(), fields)
        resolved: dict[str, str] = {}
        for key, value in characters.items():
            name = re.sub(r"\s+", " ", str(key or "")).strip()
            if not name:
                continue
            fields = known.get(name.lower())
            label = (f"{fields['name']} — {fields['description']}"
                     if fields and fields["description"] else name)
            resolved[label] = " ".join(str(value or "").split())
        return resolved

    def ensure_scene(self, stem: str, player: dict, world: str, description: str = "",
                     mood: str = "", kingdom: str = "", area: str = "", location: str = "",
                     sublocation: str = "", establishing: str = "", main_npcs=None,
                     seed_change: str = "", npcs=None,
                     time_of_day: str = "", weather: str = "", characters=None,
                     model: str | None = None) -> dict:
        """Ensure the hidden seed (idempotent; regenerated on `seed_change`), upsert any
        newly declared NPCs, then generate the action fresh from the seed + the portrait
        and persist it as a one-shot file (served once, then deleted).

        `characters` arrives as `{name: action}`; the stored descriptions are injected here."""
        effective = model or self.model
        manifest = self._read_manifest(stem)
        cast = manifest.setdefault("cast", {})
        npcs_added: list[str] = []
        if isinstance(npcs, list):
            for entry in npcs:
                fields = _npc_fields(entry)
                if fields["name"]:
                    cast[_slug(fields["name"])] = fields
                    npcs_added.append(fields["name"])
        seeds = manifest.setdefault("seeds", {})
        seed = seeds.get(self._key(kingdom, area, location, sublocation))
        if seed is None and not establishing:
            # kingdom/area omitted (an action call, or a seed change) -> resolve from the place.
            key, found = self._seed_by_place(seeds, location, sublocation, manifest.get("current"))
            if found is not None:
                seed = found
                kingdom = str(found.get("kingdom") or "")
                area = str(found.get("area") or "")
        seed_created = seed_regenerated = False
        if seed is None:
            raw = self.generate_seed(world, kingdom, area, location, sublocation,
                                     establishing or f"An atmospheric view of {sublocation or location}.",
                                     model=effective)
            seed = self._store_seed(stem, manifest, kingdom, area, location, sublocation,
                                    establishing, main_npcs, raw)
            seed_created = True
        elif seed_change:
            old = self._file_bytes(stem, seed)
            raw = self.generate_seed(world, kingdom, area, location, sublocation,
                                     establishing or str(seed.get("description") or ""),
                                     change=seed_change, refs=[old] if old else None, model=effective)
            seed = self._store_seed(stem, manifest, kingdom, area, location, sublocation,
                                    establishing or str(seed.get("description") or ""),
                                    main_npcs if main_npcs else _seed_main_npcs(seed), raw,
                                    existing=seed)
            seed_regenerated = True

        # Always draw from the place's empty establishing seed + the portrait. Chaining
        # from the previous action made the model duplicate the figures already in it.
        if seed:
            ref_kind, place_ref = "seed", self._file_bytes(stem, seed)
        else:
            ref_kind, place_ref = "portrait", None
        portrait = self._read_portrait(stem)
        refs = [r for r in (place_ref, portrait) if r]

        resolved = self._resolve_characters(characters, seed, cast)
        raw = self.generate_action(player, world, description, mood, kingdom, area, location,
                                   sublocation, ref_kind=ref_kind, refs=refs, model=effective,
                                   time_of_day=time_of_day, weather=weather, characters=resolved)
        ext = (sniff_image(raw) or ("png", "image/png"))[0]
        slug = self._action_slug(kingdom, area, location, sublocation)
        ImageService._write_image(self.scenes_dir(stem) / f"{slug}.{ext}", raw)
        created = int(time.time())
        manifest["current"] = {"kingdom": str(kingdom or ""), "area": str(area or ""),
                               "location": str(location or ""), "sublocation": str(sublocation or ""),
                               "updated": created}
        self._write_manifest(stem, manifest)
        return {
            "action": {"url": f"/api/scenes/{stem}/{slug}", "created": created},
            "seed": ({"created": seed["created"], "regenerated": seed_regenerated,
                      "description": seed.get("description", "")} if seed_created or seed_regenerated else None),
            "used_seed": ref_kind == "seed",
            "used_portrait_reference": bool(portrait),
            "npcs_added": npcs_added,
            "kingdom": str(kingdom or ""), "area": str(area or ""),
            "location": str(location or ""), "sublocation": str(sublocation or ""),
            "seed_created": seed_created, "seed_regenerated": seed_regenerated,
        }
