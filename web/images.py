"""Cached image generation for the web client (Gemini, or a local ComfyUI engine).

Images are one-off assets reused forever — a character portrait today, stat and
inventory icons later. Each save owns a folder `output/images/{stem}/` holding
`{kind}.png` plus a `manifest.json` that records how each asset was made, so we
can skip the API when nothing relevant changed and regenerate on demand.

The provider is deliberately thin: `ensure_portrait` / `ImageBackend.generate` are the
only places that talk to an image model, so a service can be pointed at another engine
without touching anything else.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import zlib
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None

try:
    import httpx
except ImportError:  # pragma: no cover - surfaced as "unavailable" instead
    httpx = None

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

from .models import image_thinking_level


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
_DEFAULT_SCENE = (
    "{location_line} A cinematic widescreen illustration of this moment: {description} "
    "{protagonist_line} Location: {location}. World setting: {world}. "
    "Mood: {mood}. Wide establishing composition, dramatic atmospheric lighting. "
    "No text, letters, numbers, runes, watermarks, logos or borders."
)
_PROTAGONIST_LINE = "The protagonist is a {race} {class}. {gear}"
_SCENE_APPEARANCE_LINE = (
    "The protagonist's appearance is currently: {appearance} Draw exactly that appearance; the "
    "protagonist's usual face, hair, build and clothing are hidden by the illusion."
)
_SCENE_APPEARANCE_GEAR = (
    "The illusion covers clothing, armour and weapons: depict only the garments and gear described "
    "in that appearance, never the protagonist's usual equipment."
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
_STYLE_GUARD = (
    "Absolutely no text, letters, numbers, runes, watermarks, logos, borders or user-interface "
    "elements."
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


def _friendly_error(exc: Exception, model: str | None = None) -> str:
    name = f"[{model}] " if model else ""
    text = str(exc)
    low = text.lower()
    if "api key" in low or "api_key" in low or "unauthor" in low or "permission" in low:
        return f"{name}The Gemini API key was rejected."
    if "429" in text or "quota" in low or "rate" in low:
        return f"{name}The image service is rate-limited — try again shortly."
    if "safety" in low or "blocked" in low:
        return f"{name}The image model declined to render this character."
    return f"{name}Image generation failed: {text[:200]}"


class ImageBackend:
    """The one interface a service talks to for image bytes.

    A service HOLDS a backend (`self.backend`) instead of inheriting from one, so the
    engine behind it can be swapped: Google Gemini today, a local ComfyUI pipeline for
    the icon family next (and, later, for the scene seed and the action). `generate` is
    the only method that reaches a model; `status` is what the client is shown.
    """

    model: str = ""
    aspect_ratio: str = ""
    image_size: str = ""
    thinking_level: str | None = None

    def available(self) -> bool:
        raise NotImplementedError

    def status(self) -> dict:
        raise NotImplementedError

    def fingerprint(self) -> str:
        """A string that changes when the ENGINE changes. It enters the asset's
        cache key, so a new checkpoint or sampler regenerates the asset. Empty for
        a hosted model (its id is already in the key), so old hashes do not move."""
        return ""

    def recipe(self, *, prompt: str = "", seed: int | None = None) -> dict:
        """What the asset was made with, for the manifest. Empty for a hosted model."""
        return {}

    def generate(self, prompt: str, aspect_ratio: str | None = None, refs=None,
                 ref_media_resolution=None, model: str | None = None,
                 thinking_level: str | None = None, seed: int | None = None) -> bytes:
        raise NotImplementedError


class GeminiImageBackend(ImageBackend):
    """The one place that talks to Gemini for images (shared by all services)."""

    def __init__(self, model: str, aspect_ratio: str, image_size: str,
                 thinking_level: str | None = None):
        self.model = model
        self.aspect_ratio = aspect_ratio
        self.image_size = image_size
        self.thinking_level = thinking_level
        self._api_key = os.environ.get("GEMINI_API_KEY") or ""
        self._client = None

    def available(self) -> bool:
        return bool(self._api_key) and genai is not None and genai_types is not None

    def status(self) -> dict:
        return {
            "available": self.available(),
            "model": self.model,
            "aspect_ratio": self.aspect_ratio,
            "thinking_level": self.thinking_level,
        }

    def _client_or_raise(self):
        if not self.available():
            raise ImageError("GEMINI_API_KEY is not set on the server.", status=503)
        if self._client is None:
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def generate(self, prompt: str, aspect_ratio: str | None = None,
                 refs=None, ref_media_resolution=None, model: str | None = None,
                 thinking_level: str | None = None, seed: int | None = None) -> bytes:
        client = self._client_or_raise()
        effective_model = model or self.model
        # Gemini 3 image models cannot disable thinking; pin the level so the
        # model default (NB2.1 = "medium") is explicit rather than inherited.
        # The level is model-specific (Lite rejects "medium", Pro takes none), so
        # validate it against the actual model or the request 400s.
        requested = thinking_level if thinking_level is not None else self.thinking_level
        level = image_thinking_level(effective_model, requested)
        kwargs = {
            "response_modalities": ["IMAGE"],
            # NOTE: output_mime_type is Enterprise-only and is rejected by the
            # Gemini Developer API — never pass it here.
            "image_config": genai_types.ImageConfig(
                aspect_ratio=aspect_ratio or self.aspect_ratio,
                image_size=self.image_size,
            ),
            # Image generation uses no tools; disable automatic function calling
            # so the SDK takes its plain path (and stops logging the "use Chat"
            # recommendation).
            "automatic_function_calling": genai_types.AutomaticFunctionCallingConfig(disable=True),
        }
        if level:
            kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_level=level)
        config = genai_types.GenerateContentConfig(**kwargs)
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
                    raise ImageError(_friendly_error(exc2, effective_model)) from exc2
            else:
                raise ImageError(_friendly_error(exc, effective_model)) from exc

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


# ── a local engine (ComfyUI on this machine) ──────────────────────────────────

_LOCAL_FALLBACKS = {
    "endpoint": "http://127.0.0.1:8188",
    "workflow": "config/comfy/icon.json",
    "repair_workflow": "config/comfy/icon_img2img.json",
    "checkpoint": "sdxl_lightning_4step.safetensors",
    "model_id": "local/sdxl-lightning-4step",
    "steps": 8,
    "cfg": 2.0,
    "sampler": "euler",
    "scheduler": "sgm_uniform",
    "canvas": 1024,
    "timeout_seconds": 240.0,
    "warmup_seconds": 900.0,
    "negative": "",
}


def load_workflow(path) -> tuple[dict, dict]:
    """Read a vendored workflow document: `{format, nodes, workflow}`.

    `nodes` names the patch points (positive / negative / sampler / canvas /
    checkpoint) inside the plain API-format graph, so the client never has to guess
    which CLIPTextEncode is which. A missing or wrong-shaped document fails loudly.
    """
    p = Path(path)
    if not p.exists():
        raise ImageError(f"local image workflow not found: {p}", status=500)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImageError(f"local image workflow is unreadable: {exc}", status=500) from exc
    graph, nodes = doc.get("workflow"), doc.get("nodes")
    if not isinstance(graph, dict) or not isinstance(nodes, dict):
        raise ImageError(f"local image workflow has the wrong shape: {p}", status=500)
    for role, node_id in nodes.items():
        node = graph.get(str(node_id))
        if not isinstance(node, dict) or "class_type" not in node:
            raise ImageError(f"local image workflow: node '{role}' ({node_id}) is missing",
                             status=500)
    return graph, {str(k): str(v) for k, v in nodes.items()}


def build_local_graph(graph: dict, nodes: dict, *, prompt: str = "", negative: str = "",
                      seed: int = 0, steps: int = 8, cfg_scale: float = 2.0,
                      sampler: str = "euler", scheduler: str = "sgm_uniform",
                      canvas: int = 1024, checkpoint: str = "", load: str = "",
                      denoise: float = 1.0) -> dict:
    """A copy of the vendored graph with the settings filled in."""
    out = json.loads(json.dumps(graph))  # deep copy

    def _at(role: str) -> dict:
        return out[nodes[role]]["inputs"]

    if "positive" in nodes:
        _at("positive")["text"] = prompt
    if "negative" in nodes:
        _at("negative")["text"] = negative
    if "canvas" in nodes:
        _at("canvas").update({"width": int(canvas), "height": int(canvas)})
    if "checkpoint" in nodes and checkpoint:
        _at("checkpoint")["ckpt_name"] = checkpoint
    if "load" in nodes and load:
        _at("load")["image"] = load
    if "sampler" in nodes:
        _at("sampler").update({"seed": int(seed), "steps": int(steps),
                               "cfg": float(cfg_scale), "sampler_name": sampler,
                               "scheduler": scheduler, "denoise": float(denoise)})
    return out


# ── the contrast check (the only perceiver a diffusion model cannot argue with) ──

def colour_contrast(data: bytes, ground, *, max_side: int = 64, min_distance: int = 60,
                    min_px: int = 500, min_mean: int | None = None) -> dict:
    """How much of the icon differs from its own GROUND, by COLOUR (not lightness).

    The subject is every pixel whose largest single-channel distance from `ground`
    exceeds `min_distance`; the icon passes when at least `min_px` of them are present
    and their mean distance is at least `min_mean` (default: `min_distance`).

    Luminance would be the wrong test now that the palette is free: a saturated blue or
    red icon is in strong contrast yet not light. Calibrated against the committed
    Gemini family as it stood when it was measured -- 699 of its 754 icons passed (93%).
    The SRD conformance pass of 2026-10-08 removed 60 of those icons, so the absolute
    counts are historical; the rule and the ratio are unchanged.
    """
    from io import BytesIO

    from PIL import Image, ImageChops

    im = Image.open(BytesIO(data)).convert("RGB")
    if max_side and max(im.size) != max_side:
        im = im.resize((max_side, max_side), Image.LANCZOS)
    diff = ImageChops.difference(im, Image.new("RGB", im.size, tuple(ground))).convert("L")
    hist = diff.histogram()
    cut = max(0, int(min_distance))
    px = sum(hist[cut + 1:])
    mean = (sum(i * c for i, c in enumerate(hist[cut + 1:], cut + 1) if c) / px) if px else 0.0
    want_mean = cut if min_mean is None else int(min_mean)
    return {"px": px, "mean": round(mean, 1), "distance": cut, "min_px": int(min_px),
            "min_mean": want_mean, "ok": px >= int(min_px) and mean >= want_mean}


def subject_similarity(before: bytes, after: bytes, ground, *,
                       max_side: int = 64, min_distance: int = 60) -> dict:
    """Are these the SAME picture? Intersection-over-union of the two subject masks.

    Reported, never a gate: the eye is the judge of 'the same picture'. A low IoU on a
    repair means the model REDREW rather than relit the icon.
    """
    from io import BytesIO

    from PIL import Image, ImageChops

    def _mask(data: bytes) -> Image.Image:
        im = Image.open(BytesIO(data)).convert("RGB")
        if max(im.size) != max_side:
            im = im.resize((max_side, max_side), Image.LANCZOS)
        diff = ImageChops.difference(im, Image.new("RGB", im.size, tuple(ground))).convert("L")
        return diff.point(lambda v: 255 if v > min_distance else 0)

    a, b = _mask(before), _mask(after)
    inter = ImageChops.multiply(a, b)
    count = lambda m: sum(m.histogram()[255:])  # noqa: E731
    ia, ib, ii = count(a), count(b), count(inter)
    union = ia + ib - ii
    return {"iou": round(ii / union, 3) if union else 0.0, "before_px": ia, "after_px": ib}


def detail_within(data: bytes, *, max_edge: float = 0.217, max_hf: float = 29.7,
                  max_side: int = 256) -> dict:
    """Is the icon SIMPLE enough? Linework and grain, measured at the size it is looked at.

    `edge` is the fraction of pixels sitting on a contour (FIND_EDGES > 40) and `hf` the
    high-frequency energy (the mean absolute difference from a blurred copy) -- engraving and
    texture. The ceilings are the committed family's own p90 (edge 0.217, hf 29.7), so roughly
    nine icons in ten of that family pass. Colour count is deliberately NOT measured: the
    committed family carries MORE colours than the local one; what separates them is linework.
    """
    from io import BytesIO

    from PIL import Image, ImageChops, ImageFilter

    im = Image.open(BytesIO(data)).convert("RGB")
    if max_side and max(im.size) != max_side:
        im = im.resize((max_side, max_side), Image.LANCZOS)
    grey = im.convert("L")
    hist = grey.filter(ImageFilter.FIND_EDGES).histogram()
    edge = sum(hist[40:]) / (sum(hist) or 1)
    diff = ImageChops.difference(grey, grey.filter(ImageFilter.GaussianBlur(2.0))).histogram()
    hf = sum(i * c for i, c in enumerate(diff)) / (sum(diff) or 1)
    return {"edge": round(edge, 4), "hf": round(hf, 2), "max_edge": float(max_edge),
            "max_hf": float(max_hf), "ok": edge <= max_edge and hf <= max_hf}


class LocalImageBackend(ImageBackend):
    """A local diffusion engine behind **ComfyUI's HTTP API**.

    The weights live OUTSIDE the repo (they carry their own licence); only the graph
    is vendored, under `config/comfy/`. Nothing here touches the GPU until `generate`
    runs, and `available()` is a cheap reachability probe, so an unreachable engine
    reads as "not available" rather than as a crash.

    Every image is one-shot: seed, steps, sampler and canvas come from the `local:`
    block of `config/images.yml`, and a bake is reproducible from the manifest.
    """

    def __init__(self, *, endpoint: str, workflow, checkpoint: str, model_id: str,
                 steps: int = 8, cfg_scale: float = 2.0, sampler: str = "euler",
                 scheduler: str = "sgm_uniform", negative: str = "", canvas: int = 1024,
                 timeout: float = 240.0, warmup: float = 900.0,
                 repair_workflow=None, aspect_ratio: str = "1:1"):
        self.endpoint = str(endpoint).rstrip("/")
        self.workflow_path = Path(workflow)
        self.repair_path = Path(repair_workflow) if repair_workflow else self.workflow_path
        self.checkpoint = str(checkpoint)
        self.model = str(model_id)          # `local/...` — what the manifest records
        self.steps = int(steps)
        self.cfg_scale = float(cfg_scale)
        self.sampler = str(sampler)
        self.scheduler = str(scheduler)
        self.negative = str(negative or "")
        self.canvas = int(canvas)
        self.timeout = float(timeout)
        self.warmup = float(warmup)
        self.aspect_ratio = aspect_ratio
        self.image_size = f"{self.canvas}x{self.canvas}"
        self.thinking_level = None
        self._warm = False          # the first image may have to load the checkpoint
        self._available: tuple[float, bool] | None = None

    # ── construction ──────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, cfg: dict, *, model: str | None = None,
                    aspect_ratio: str = "1:1") -> "LocalImageBackend":
        block = cfg.get("local")
        block = block if isinstance(block, dict) else {}
        values = {**_LOCAL_FALLBACKS, **{k: v for k, v in block.items() if v is not None}}
        if model:
            values["model_id"] = str(model)
        return cls(
            endpoint=values["endpoint"],
            workflow=values["workflow"],
            checkpoint=values["checkpoint"],
            model_id=values["model_id"],
            steps=values["steps"],
            cfg_scale=values["cfg"],
            sampler=values["sampler"],
            scheduler=values["scheduler"],
            negative=values["negative"],
            canvas=values["canvas"],
            timeout=values["timeout_seconds"],
            warmup=values["warmup_seconds"],
            repair_workflow=values.get("repair_workflow"),
            aspect_ratio=aspect_ratio,
        )

    # ── the backend surface ───────────────────────────────────────────────

    def available(self) -> bool:
        now = time.time()
        if self._available and now - self._available[0] < 5.0:
            return self._available[1]
        ok = False
        if httpx is not None:
            try:
                r = httpx.get(f"{self.endpoint}/system_stats", timeout=2.0)
                ok = r.status_code == 200
            except Exception:  # noqa: BLE001 - unreachable is simply "not available"
                ok = False
        self._available = (now, ok)
        return ok

    def status(self) -> dict:
        return {
            "available": self.available(),
            "model": self.model,
            "aspect_ratio": self.aspect_ratio,
            "thinking_level": None,
            "endpoint": self.endpoint,
            "checkpoint": self.checkpoint,
            "canvas": self.canvas,
            "steps": self.steps,
            "cfg": self.cfg_scale,
            "sampler": self.sampler,
            "scheduler": self.scheduler,
        }

    def fingerprint(self) -> str:
        return json.dumps({"engine": "local", "checkpoint": self.checkpoint,
                           "steps": self.steps, "cfg": self.cfg_scale,
                           "sampler": self.sampler, "scheduler": self.scheduler,
                           "canvas": self.canvas, "negative": self.negative,
                           "workflow": self.workflow_path.name}, sort_keys=True)

    def recipe(self, *, prompt: str = "", seed: int | None = None) -> dict:
        return {
            "seed": self._seed(prompt, seed),
            "steps": self.steps,
            "cfg": self.cfg_scale,
            "sampler": self.sampler,
            "scheduler": self.scheduler,
            "canvas": self.canvas,
            "checkpoint": self.checkpoint,
        }

    @staticmethod
    def _seed(prompt: str, seed: int | None) -> int:
        if seed is not None:
            return int(seed) & 0xFFFFFFFF
        return zlib.crc32(prompt.encode("utf-8")) & 0xFFFFFFFF

    def generate(self, prompt: str, aspect_ratio: str | None = None, refs=None,
                 ref_media_resolution=None, model: str | None = None,
                 thinking_level: str | None = None, seed: int | None = None) -> bytes:
        if refs:
            raise ImageError("the local image backend cannot use reference images yet",
                             status=502)
        graph, nodes = load_workflow(self.workflow_path)
        built = build_local_graph(
            graph, nodes, prompt=prompt, negative=self.negative,
            seed=self._seed(prompt, seed), steps=self.steps, cfg_scale=self.cfg_scale,
            sampler=self.sampler, scheduler=self.scheduler, canvas=self.canvas,
            checkpoint=self.checkpoint,
        )
        return self._run(built)

    # ── the repair: the same picture again, in colour ───────────────────────

    def repair(self, data: bytes, prompt: str, *, mode: str = "img2img", seed: int | None = 0,
               denoise: float = 0.5, steps: int = 12, cfg_scale: float = 4.0) -> bytes:
        """Regenerate an icon with better contrast, keeping the picture.

        `mode="img2img** loads the previous PNG into ComfyUI and re-samples it at a mid
        denoise, so the composition survives while the colour changes — that is the
        treatment for an icon that is PRESENT but too dark or too small.

        `mode="redraw"` is a plain text-to-image at the SAME seed and stronger guidance,
        for an icon painted in the background colour: there is no structure to preserve,
        so relighting would be a redraw anyway — better to redraw deliberately.
        """
        graph, nodes = load_workflow(self.repair_path if mode == "img2img" else self.workflow_path)
        load = ""
        if mode == "img2img":
            load = self._upload(data)
        built = build_local_graph(
            graph, nodes, prompt=prompt, negative=self.negative,
            seed=self._seed(prompt, seed), steps=steps, cfg_scale=cfg_scale,
            sampler=self.sampler, scheduler=self.scheduler, canvas=self.canvas,
            checkpoint=self.checkpoint, load=load,
            denoise=denoise if mode == "img2img" else 1.0,
        )
        return self._run(built)

    def _upload(self, data: bytes) -> str:
        """Put the previous image where ComfyUI's LoadImage can find it."""
        if httpx is None:
            raise ImageError("httpx is not installed, so the local image engine cannot be "
                             "reached.", status=503)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(f"{self.endpoint}/upload/image",
                                files={"image": ("infinity-icon-repair.png", data,
                                                 "image/png")},
                                data={"overwrite": "true"})
                if r.status_code >= 400:
                    raise ImageError(f"ComfyUI refused the repair image ({r.status_code})",
                                     status=502)
                name = str((r.json() or {}).get("name") or "")
                if not name:
                    raise ImageError("ComfyUI accepted the repair image but returned no "
                                     "name", status=502)
                return name
        except ImageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ImageError(f"could not hand the image to ComfyUI: {exc}", status=502) from exc

    def _run(self, built: dict) -> bytes:
        """Post a graph, wait for it, download the image. The one engine call."""
        if httpx is None:
            raise ImageError("httpx is not installed, so the local image engine cannot be "
                             "reached.", status=503)
        deadline = time.time() + (self.warmup if not self._warm else self.timeout)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(f"{self.endpoint}/prompt",
                                json={"prompt": built, "client_id": "infinity-icons"})
                if r.status_code >= 400:
                    raise ImageError(f"ComfyUI rejected the workflow ({r.status_code}): "
                                     f"{r.text[:300]}", status=502)
                prompt_id = str(r.json().get("prompt_id") or "")
                if not prompt_id:
                    raise ImageError("ComfyUI accepted the workflow but returned no "
                                     "prompt id", status=502)
                self._warm = True
                image = self._wait_for_image(client, prompt_id, deadline)
                return self._download(client, image)
        except ImageError:
            raise
        except Exception as exc:  # noqa: BLE001 - unreachable engine, bad reply, timeout
            raise ImageError(f"the local image engine at {self.endpoint} failed: {exc}",
                             status=502) from exc

    # ── ComfyUI plumbing ───────────────────────────────────────────────────

    def _wait_for_image(self, client, prompt_id: str, deadline: float) -> dict:
        while time.time() < deadline:
            entry = (client.get(f"{self.endpoint}/history/{prompt_id}").json() or {}).get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    messages = [m for m in status.get("messages") or []
                                if m and m[0] == "execution_error"]
                    raise ImageError(f"ComfyUI reported an error: {json.dumps(messages)[:400]}",
                                     status=502)
                for node in (entry.get("outputs") or {}).values():
                    for image in node.get("images") or []:
                        return image
            time.sleep(0.5)
        raise ImageError("ComfyUI did not produce an image in time", status=504)

    def _download(self, client, image: dict) -> bytes:
        r = client.get(f"{self.endpoint}/view", params={
            "filename": image.get("filename", ""),
            "subfolder": image.get("subfolder", ""),
            "type": image.get("type", "output"),
        })
        r.raise_for_status()
        raw = r.content
        if not raw:
            raise ImageError("ComfyUI returned an empty image", status=502)
        if sniff_image(raw) is None:
            raise ImageError(f"ComfyUI returned {len(raw)} bytes that are not a recognised "
                             f"image", status=502)
        return raw


class ImageBackendHolder:
    """Shared plumbing for the services: a service HOLDS a backend rather than
    inheriting from one, so the engine can be swapped per family or per request.
    `self.generate` is a thin passthrough and stays the seam a test replaces with a
    stub (`svc.generate = ...`), exactly as `svc._generate_bytes` used to be.
    """

    backend: ImageBackend

    def backend_for(self, model: str | None = None) -> ImageBackend:
        """Which backend serves this request. One backend for now; a service that
        owns several (one per image-model family) overrides this."""
        return self.backend

    def generate(self, prompt: str, **kwargs) -> bytes:
        return self.backend_for(kwargs.get("model")).generate(prompt, **kwargs)

    # ── the backend's own surface, kept on the service ─────────────────────
    @property
    def model(self) -> str:
        return self.backend.model

    @property
    def aspect_ratio(self) -> str:
        return self.backend.aspect_ratio

    @property
    def image_size(self) -> str:
        return self.backend.image_size

    @property
    def thinking_level(self) -> str | None:
        return self.backend.thinking_level

    def available(self) -> bool:
        return self.backend.available()

    def status(self) -> dict:
        return self.backend.status()


def _backend_from_config(cfg: dict, *, model: str, aspect_ratio: str,
                         thinking_level: str | None = None,
                         family: str | None = None) -> ImageBackend:
    """The image backend for a config and a family. `family` (the icon store being
    served) wins; otherwise `backend:` in config/images.yml decides; otherwise Gemini,
    so a config with no `backend:` key behaves exactly as it always did."""
    name = str(family or cfg.get("backend") or "gemini").strip().lower()
    if name.startswith("local"):
        return LocalImageBackend.from_config(cfg, model=model, aspect_ratio=aspect_ratio)
    return GeminiImageBackend(model, aspect_ratio,
                              str(cfg.get("image_size") or "1K"), thinking_level)


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
            "thinking_level": self.thinking_level,
            "prompt": prompt,
            "source_hash": digest,
            "style": (style or self.style),
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
    """Normalize a declared NPC to {name, description, role, race, class}. A legacy plain
    string is a description. `race`/`class` are required of the GM but default to ''."""
    if isinstance(value, dict):
        return {"name": re.sub(r"\s+", " ", str(value.get("name") or "")).strip(),
                "description": re.sub(r"\s+", " ", str(value.get("description") or "")).strip(),
                "role": re.sub(r"\s+", " ", str(value.get("role") or "")).strip(),
                "race": re.sub(r"\s+", " ", str(value.get("race") or "")).strip(),
                "class": re.sub(r"\s+", " ", str(value.get("class") or "")).strip()}
    return {"name": "", "description": re.sub(r"\s+", " ", str(value or "")).strip(),
            "role": "", "race": "", "class": ""}


def _npc_list(value) -> list[dict]:
    """Normalize a place's main NPCs to a list of {name, description, role, race, class}.

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


def _dead_names(entry) -> list[str]:
    """A seed entry's dead main-NPC names, lowercased.

    The flag lives OUTSIDE the NPC dicts (`entry["dead"]`): `_npc_fields` rebuilds each NPC
    to exactly name/description/role/race/class and would drop a `dead` key."""
    raw = (entry or {}).get("dead")
    return [str(n).strip().lower() for n in (raw if isinstance(raw, list) else [])
            if str(n or "").strip()]


def _prune_dead(entry: dict) -> None:
    """Keep `entry["dead"]` to names still declared as main NPCs of the place."""
    declared = {str(n.get("name") or "").strip().lower() for n in _seed_main_npcs(entry)}
    entry["dead"] = sorted(n for n in _dead_names(entry) if n in declared)


def _place_path(place) -> list[str]:
    """Normalize a place path: trimmed, non-empty string segments (deepest last)."""
    if isinstance(place, str):
        place = [place]
    if not isinstance(place, (list, tuple)):
        return []
    return [str(p).strip() for p in place if str(p or "").strip()]


def _manifest_entries(data) -> list[dict]:
    """Normalize a scene manifest (v1 flat / v2-v7) to a list of seed dicts."""
    if not isinstance(data, dict):
        return []
    if data.get("version") in (2, 3, 4, 5, 6, 7, 8) and isinstance(data.get("seeds"), dict):
        return [e for e in data["seeds"].values() if isinstance(e, dict)]
    # v1: {location_slug: entry}
    return [e for e in data.values() if isinstance(e, dict)
            and (e.get("location") or e.get("place"))]


# ── the live scene registry: in memory until the player saves ─────────────
#
# The scenes manifest (places + declared NPCs) is the save's companion, but it must change
# on disk ONLY when the player saves -- an unsaved session has to leave the previous save
# untouched. Every read/write during a session goes through this module-level registry,
# loaded from disk on first access. `commit_scene_manifest` writes it (and prunes image
# files the registry no longer lists); `discard_scene_manifest` drops it so the next read
# reloads the save. The engine owns the session boundary; the scene service only reads and
# mutates the dict.

_SCENE_MANIFESTS: dict[str, dict] = {}
_SCENE_LOCK = threading.RLock()  # scene writes run in a worker thread


def _scene_key(kingdom, area, place, era="") -> str:
    """A place's manifest key. The era is the root: two eras can share a place path,
    and they are different places with different seeds."""
    parts = [_slug(p) for p in (kingdom, area, *_place_path(place))]
    return "|".join(([_slug(era)] if era else []) + parts)


def _scenes_dir_for(output_dir, stem: str) -> Path:
    return Path(output_dir or ".") / "images" / stem / "scenes"


def _scene_cache_key(scenes: Path) -> str:
    try:
        return str(scenes.resolve())
    except OSError:  # pragma: no cover - only on a broken path
        return str(scenes)


def _empty_scene_manifest() -> dict:
    return {"version": 8, "seeds": {}, "cast": {}, "current": {}}


def _normalise_scene_manifest(data, scenes: Path, era: str = "") -> dict:
    """Normalize a scene manifest (v1 flat / v2-v8) to the v8 shape."""
    if not isinstance(data, dict):
        return _empty_scene_manifest()
    if data.get("version") in (2, 3, 4, 5, 6, 7, 8) and isinstance(data.get("seeds"), dict):
        if data.get("version") not in (4, 5, 6, 7, 8):
            # Action images became ephemeral one-shot files in v4: clean up any left over.
            for entry in (data.get("actions") or []):
                try:
                    (scenes / str((entry or {}).get("file") or "")).unlink()
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
            place = _place_path(entry.get("place"))
            if not place:
                place = _place_path([entry.get("location"), entry.get("sublocation")])
            entry["place"] = place
            entry.pop("location", None)
            entry.pop("sublocation", None)
            # v7 and older carry no era: it defaults to the save's current one.
            entry["era"] = str(entry.get("era") or "").strip() or era
            seeds[_scene_key(entry.get("kingdom"), entry.get("area"), place, entry["era"])] = entry
        cast: dict = {}
        raw_cast = data.get("cast")
        if isinstance(raw_cast, dict):
            for entry in raw_cast.values():
                fields = _npc_fields(entry)
                if fields["name"]:
                    cast[_slug(fields["name"])] = fields
        current = data.get("current")
        if isinstance(current, dict) and current:
            cplace = _place_path(current.get("place"))
            if not cplace:
                cplace = _place_path([current.get("location"), current.get("sublocation")])
            current = {k: v for k, v in current.items() if k not in ("location", "sublocation")}
            current["place"] = cplace
        else:
            current = {}
        return {"version": 8, "seeds": seeds, "cast": cast, "current": current}
    # v1 flat manifest: {location_slug: entry}
    seeds: dict = {}
    now = int(time.time())
    for slug, entry in (data or {}).items():
        if not isinstance(entry, dict):
            continue
        place = _place_path([entry.get("location"), entry.get("sublocation")])
        if not place:
            continue
        kingdom = str(entry.get("kingdom") or "").strip()
        area = str(entry.get("area") or "").strip()
        seeds[_scene_key(kingdom, area, place, era)] = {
            "file": entry.get("file"), "slug": slug, "era": era,
            "kingdom": kingdom, "area": area, "place": place,
            "description": str(entry.get("description") or ""),
            "main_npcs": _npc_list(entry.get("main_npc")),
            "created": entry.get("created") or now,
            "mime": entry.get("mime") or "image/png",
        }
    return {"version": 8, "seeds": seeds, "cast": {}, "current": {}}


def _read_scene_manifest_file(scenes: Path, era: str = "") -> dict:
    path = scenes / "manifest.json"
    if not path.exists():
        return _empty_scene_manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_scene_manifest()
    return _normalise_scene_manifest(data, scenes, era)


def live_scene_manifest(output_dir, stem: str, era: str = "") -> dict:
    """The LIVE manifest for a save: from memory if a session holds it, else from disk.

    Mutate the returned dict in place. Nothing reaches disk until `commit_scene_manifest`.
    """
    scenes = _scenes_dir_for(output_dir, stem)
    key = _scene_cache_key(scenes)
    with _SCENE_LOCK:
        data = _SCENE_MANIFESTS.get(key)
        if data is None:
            data = _read_scene_manifest_file(scenes, era)
            _SCENE_MANIFESTS[key] = data
        return data


def set_scene_manifest(output_dir, stem: str, data: dict) -> None:
    """Keep a manifest dict as the live one for this save (memory only)."""
    scenes = _scenes_dir_for(output_dir, stem)
    with _SCENE_LOCK:
        _SCENE_MANIFESTS[_scene_cache_key(scenes)] = data


def _write_scene_manifest_file(scenes: Path, data: dict) -> None:
    scenes.mkdir(parents=True, exist_ok=True)
    path = scenes / "manifest.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _prune_scene_images(scenes: Path, data: dict) -> list[str]:
    """Delete image files in the scenes dir that the committed registry does not list."""
    referenced = {Path(str((e or {}).get("file") or "")).name
                  for e in (data.get("seeds") or {}).values()
                  if str((e or {}).get("file") or "").strip()}
    removed: list[str] = []
    try:
        entries = list(scenes.iterdir())
    except OSError:
        return removed
    for path in entries:
        if path.is_file() and path.suffix.lower() in _IMAGE_EXTS and path.name not in referenced:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed


def commit_scene_manifest(output_dir, stem: str) -> dict | None:
    """Write the live manifest to disk (the Save) and prune unreferenced scene images."""
    scenes = _scenes_dir_for(output_dir, stem)
    key = _scene_cache_key(scenes)
    with _SCENE_LOCK:
        data = _SCENE_MANIFESTS.get(key)
        if data is None:
            return None
        # A save that never had a place gets no manifest file at all.
        if not (data.get("seeds") or data.get("cast") or data.get("current")) \
                and not (scenes / "manifest.json").exists():
            return {"seeds": 0, "cast": 0, "pruned": []}
        _write_scene_manifest_file(scenes, data)
        pruned = _prune_scene_images(scenes, data)
    return {"seeds": len(data.get("seeds") or {}), "cast": len(data.get("cast") or {}),
            "pruned": pruned}


def discard_scene_manifest(output_dir, stem: str) -> None:
    """Drop the live manifest so the next read reloads the save from disk."""
    scenes = _scenes_dir_for(output_dir, stem)
    with _SCENE_LOCK:
        _SCENE_MANIFESTS.pop(_scene_cache_key(scenes), None)


def current_scene_place(output_dir, stem: str) -> dict | None:
    """The save's current place pointer ({era, kingdom, area, place}), or None.

    Set by `record_place` / `ensure_scene`. The text-mode scene caption reads it: the engine
    knows the age and the place, never the time of day or the weather (only an image call
    carries those).
    """
    data = live_scene_manifest(output_dir, stem)
    current = data.get("current") if isinstance(data, dict) else None
    if not isinstance(current, dict) or not current:
        return None
    place = _place_path(current.get("place"))
    area = str(current.get("area") or "").strip()
    if not place and not area:
        return None
    return {"era": str(current.get("era") or "").strip().lower(),
            "kingdom": str(current.get("kingdom") or "").strip(),
            "area": area, "place": place}


def known_scene_places(output_dir, stem: str, era: str | None = None) -> list[dict]:
    """Seeded places for a save: {era, kingdom, area, place, description, main_npcs, used}.

    `era` scopes the list to one era. A place with no era -- only possible from a
    manifest written before v8, which the next write re-keys -- matches any era.
    """
    data = live_scene_manifest(output_dir, stem, era or "")
    want_era = None if era is None else str(era).strip().lower()
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    out: list[dict] = []
    for entry in _manifest_entries(data):
        entry_era = str(entry.get("era") or "").strip()
        kingdom = str(entry.get("kingdom") or "").strip()
        area = str(entry.get("area") or "").strip()
        if want_era is not None and entry_era.lower() not in (want_era, ""):
            continue
        place = _place_path(entry.get("place"))
        if not place:
            place = _place_path([entry.get("location"), entry.get("sublocation")])
        key = (entry_era.lower(), kingdom.lower(), area.lower(), tuple(p.lower() for p in place))
        if not place or key in seen:
            continue
        seen.add(key)
        out.append({"era": entry_era, "kingdom": kingdom, "area": area, "place": place,
                    "description": str(entry.get("description") or "").strip(),
                    "main_npcs": _seed_main_npcs(entry),
                    "dead": _dead_names(entry),
                    "used": int(entry.get("used") or entry.get("created") or 0)})
    return sorted(out, key=lambda p: (p["era"].lower(), p["kingdom"].lower(), p["area"].lower(),
                                      [s.lower() for s in p["place"]]))


def known_npc_names(output_dir, stem: str) -> set[str]:
    """Declared NPC names for a save (place main NPCs + the storyline cast), lowercased."""
    data = live_scene_manifest(output_dir, stem)
    names: set[str] = set()
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


def mark_place_dead(output_dir, stem: str, era: str, kingdom: str, area: str, place,
                    names) -> list[str]:
    """Permanently mark main NPCs of one place as dead (era- and place-scoped).

    `names` are matched case-insensitively. The flag is the seed entry's `dead` list,
    OUTSIDE the NPC dicts so `_npc_fields` never strips it. Mutates the live manifest;
    nothing reaches disk until `commit_scene_manifest`. Returns the names newly added."""
    wanted = {str(n).strip().lower() for n in (names or []) if str(n or "").strip()}
    if not wanted:
        return []
    data = live_scene_manifest(output_dir, stem, era)
    key = _scene_key(kingdom, area, place, era)
    entry = (data.get("seeds") or {}).get(key)
    if not isinstance(entry, dict):
        return []
    dead = set(_dead_names(entry))
    added = sorted(wanted - dead)
    if added:
        entry["dead"] = sorted(dead | wanted)
    return added


def known_scene_locations(output_dir, stem: str) -> list[str]:
    """Distinct place-path labels already seeded for a save (back-compat helper)."""
    seen: list[str] = []
    for entry in known_scene_places(output_dir, stem):
        label = " — ".join(entry.get("place") or [])
        if label and label.lower() not in {s.lower() for s in seen}:
            seen.append(label)
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
    "the same scene for day or night), the weather (rain, fog, snow, wind — rendered as "
    "atmosphere, staying outdoors), and the action's own transient damage or mess — a table split, "
    "a chair toppled, a hearth scattered, spilled ale, a fire. Objects the action touches may be "
    "broken or displaced; the room is never rearranged. (2) the protagonist's portrait — that "
    "same character, in the described action."
)
_SCENE_ACTION_FROM_SEED_SOLO = (
    "Attached reference: the establishing view of {location} — {sublocation}: this IS the picture "
    "— reproduce it unchanged (same camera, framing, architecture, furniture, props, colours, "
    "arrangement), then place the characters into it. Change nothing about the space itself, "
    "except the three things the moment legitimately changes: the time of day (relight the same "
    "scene for day or night), the weather (rain, fog, snow, wind — rendered as atmosphere, "
    "staying outdoors), and the action's own transient damage or mess — a table split, a chair "
    "toppled, a hearth scattered, spilled ale, a fire. Objects the action touches may be broken "
    "or displaced; the room is never rearranged."
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
_SCENE_WEATHER_GUARD = (
    "Weather is an outdoor phenomenon: rain and snow fall only where the sky is open. If this "
    "place is an interior — the establishing view shows walls, a ceiling or a roof — keep "
    "precipitation outside: show it only through the windows, doorways and other openings "
    "(streaks on the glass, a sky beyond, weather-light falling through the opening) and keep the "
    "inside dry — no rain, snow, puddles, drips or wet interior surfaces. Never draw rain or snow "
    "falling or pooling inside an enclosed room. Fog, mist and haze may soften the interior air, "
    "but never as falling water. If the place is genuinely open to the sky (a courtyard, a ruined "
    "roofless hall, an open-sided shelter), render the weather directly where the sky is open."
)
_SCENE_LAYOUT_LOCK = (
    "The attached establishing view IS the scene: keep the same camera, framing, crop, architecture, "
    "walls, doors, windows, stairs, furniture, props, their positions, scale and proportions. Do "
    "not redesign, add, remove, resize, restyle or rearrange anything, and do not change the "
    "viewpoint, the framing or the composition. Only what the moment legitimately changes may "
    "differ: the time of day (relight this identical scene for day or night), the weather "
    "(atmosphere only — it stays outdoors and never alters the space), and the transient damage or "
    "mess of the action (a table split, a chair toppled, a fire, spilled ale). Then place the "
    "characters into that same space."
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


class SceneService(ImageBackendHolder):
    """Generates + persists a hidden, permanent establishing **seed** per place
    `(kingdom, area, place path)` and a visible **action** image drawn
    fresh from that seed + the portrait. Actions are never referenced by later
    actions (that made the model duplicate the figures already in the frame) and
    are deleted as soon as they have been served once.

    `output/images/{stem}/scenes/manifest.json` is the v7 manifest:
        {version, seeds: {key: entry}, cast: {name_slug: {name, description, role}}, current: {...}}
    Each seed entry carries `main_npcs` = a list of {name, description, role}. The seed is
    never served to the client; it is the only persisted scene asset. The `cast` map keeps
    the declared NPC looks; the GM only ever passes their names.
    """

    def __init__(self, output_dir=None, config_path: Path | None = None,
                 backend: ImageBackend | None = None):
        cfg = _load_config(config_path)
        self.backend = backend or _backend_from_config(
            cfg,
            model=str(cfg.get("model") or _DEFAULT_MODEL),
            aspect_ratio=str(cfg.get("scene_aspect_ratio") or "16:9"),
            thinking_level=_thinking_level(cfg),
        )
        self.output_dir = Path(output_dir) if output_dir else None
        # Scenes share the portrait's locked painterly style unless overridden.
        self.style = str(cfg.get("scene_style") or cfg.get("style") or "").strip()
        self.style_guard = str(cfg.get("style_guard") or _STYLE_GUARD).strip()
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
        self.weather_guard = str(
            cfg.get("scene_weather_guard") or _SCENE_WEATHER_GUARD
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
        self.protagonist_template = str(cfg.get("scene_protagonist") or _PROTAGONIST_LINE).strip()
        self.appearance_line = str(
            cfg.get("scene_appearance_line") or _SCENE_APPEARANCE_LINE
        ).strip()
        self.appearance_gear_guard = str(
            cfg.get("scene_appearance_gear") or _SCENE_APPEARANCE_GEAR
        ).strip()
        self.appearance_effects = _appearance_effects_config(cfg)
        self.action_from_seed_solo = str(
            cfg.get("scene_action_from_seed_solo") or _SCENE_ACTION_FROM_SEED_SOLO
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
    def _key(kingdom, area, place, era="") -> str:
        """A place's manifest key. The era is the root (see `_scene_key`)."""
        return _scene_key(kingdom, area, place, era)

    def _read_manifest(self, stem: str, era: str = "") -> dict:
        """The live manifest for this save: in memory, written to disk only on Save."""
        return live_scene_manifest(self.output_dir or Path("."), stem, era)

    def _write_manifest(self, stem: str, data: dict) -> None:
        """Keep the live manifest in memory; it reaches disk only on `commit_scene_manifest`."""
        set_scene_manifest(self.output_dir or Path("."), stem, data)

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
    def _seed_by_place(seeds: dict, place, current=None, era: str = ""):
        """Resolve a seed from its place path when kingdom/area are omitted."""
        want = [_slug(p) for p in _place_path(place)]
        matches = [(k, v) for k, v in (seeds or {}).items()
                   if [_slug(x) for x in _place_path(v.get("place"))] == want
                   and (not era or str(v.get("era") or "") == era)]
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

    def _address_line(self, kingdom: str, area: str, place) -> str:
        label = " — ".join(_place_path(place))
        parts = [p for p in (label, self._clean(area), self._clean(kingdom)) if p]
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

    def seed_prompt(self, world: str, kingdom: str = "", area: str = "", place=None,
                    establishing: str = "", change: str = "",
                    has_seed_ref: bool = False, style: str | None = None) -> str:
        path = _place_path(place)
        loc = self._clean(path[0]) if path else "an unnamed place"
        sub = self._clean(" — ".join(path[1:]))
        fields = {
            "address_line": self._address_line(kingdom, area, path),
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
        effective_style = (style or self.style).strip()
        parts = [body]
        if effective_style:
            parts.append(effective_style)
        if self.style_guard:
            parts.append(self.style_guard)
        return " ".join(parts).strip()

    def action_prompt(self, player: dict, world: str, description: str, mood: str,
                      kingdom: str = "", area: str = "", place=None,
                      ref_kind: str = "", time_of_day: str = "", weather: str = "",
                      characters=None, appearance=None, style: str | None = None) -> str:
        player = player or {}
        path = _place_path(place)
        loc = self._clean(path[0]) if path else "an unnamed place"
        sub = self._clean(" — ".join(path[1:]))
        if ref_kind == "seed":
            template = self.action_from_seed_solo if appearance else self.action_from_seed
            ref_line = template.format(location=loc, sublocation=sub or loc)
        elif ref_kind == "portrait":
            ref_line = self.portrait_reference
        else:
            ref_line = ""
        if ref_kind and self.protagonist_guard and not appearance:
            ref_line = (ref_line + " " + self.protagonist_guard).strip()
        race = str(player.get("race") or "") or "adventurer"
        char_class = str(player.get("character_class") or "") or "adventurer"
        gear = _gear_line(player)
        if appearance and appearance.get("look"):
            protagonist_line = self.appearance_line.format(appearance=appearance["look"])
            if appearance.get("covers_gear") and self.appearance_gear_guard:
                protagonist_line = (protagonist_line + " " + self.appearance_gear_guard).strip()
            gear = ""
        else:
            protagonist_line = self.protagonist_template.format(
                race=race, **{"class": char_class}, gear=gear).strip()
        fields = {
            "description": self._clean(description),
            "race": race,
            "class": char_class,
            "gear": gear,
            "protagonist_line": protagonist_line,
            "world": self._clean(world) or "a fantasy realm",
            "kingdom": self._clean(kingdom), "area": self._clean(area),
            "location": loc, "sublocation": sub or loc,
            "address_line": self._address_line(kingdom, area, path),
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
        if self._clean(weather) and self.weather_guard:
            body = (body + " " + self.weather_guard).strip()
        if ref_kind == "seed" and self.layout_lock:
            body = (body + " " + self.layout_lock).strip()
        if ref_kind == "seed" and self.absent_element_guard:
            body = (body + " " + self.absent_element_guard).strip()
        effective_style = (style or self.style).strip()
        parts = [body]
        if effective_style:
            parts.append(effective_style)
        if self.style_guard:
            parts.append(self.style_guard)
        return " ".join(parts).strip()

    # ── generation ──────────────────────────────────────────────────────────

    def generate_seed(self, world: str, kingdom: str = "", area: str = "", place=None,
                      establishing: str = "", change: str = "", refs=None,
                      model: str | None = None, style: str | None = None) -> bytes:
        prompt = self.seed_prompt(world, kingdom, area, place, establishing,
                                  change, has_seed_ref=bool(refs), style=style)
        raw = self.generate(prompt, aspect_ratio=self.aspect_ratio, refs=refs,
                                   ref_media_resolution=self.ref_media_resolution,
                                   model=model or self.model,
                                   thinking_level=self.thinking_level)
        return downscale_image(raw, self.max_side) if self.max_side else raw

    def generate_action(self, player: dict, world: str, description: str, mood: str,
                        kingdom: str = "", area: str = "", place=None,
                        ref_kind: str = "", refs=None, model: str | None = None,
                        time_of_day: str = "", weather: str = "", characters=None,
                        appearance=None, style: str | None = None) -> bytes:
        prompt = self.action_prompt(player, world, description, mood, kingdom, area, place,
                                    ref_kind=ref_kind, time_of_day=time_of_day,
                                    weather=weather, characters=characters, appearance=appearance,
                                    style=style)
        raw = self.generate(prompt, aspect_ratio=self.aspect_ratio, refs=refs,
                                   ref_media_resolution=self.ref_media_resolution,
                                   model=model or self.model,
                                   thinking_level=self.thinking_level)
        return downscale_image(raw, self.max_side) if self.max_side else raw

    # ── orchestration ───────────────────────────────────────────────────────

    def _seed_slug(self, kingdom: str, area: str, place, era: str = "") -> str:
        path = _place_path(place)
        h = hashlib.sha1(self._key(kingdom, area, path, era).encode("utf-8")).hexdigest()[:6]
        head = _slug(path[0])[:16] if path else "place"
        tail = _slug(path[-1])[:16] if len(path) > 1 else "main"
        return f"seed-{head}-{tail}-{h}"

    def _action_slug(self, kingdom: str, area: str, place, era: str = "") -> str:
        """A unique name per request — actions are one-shot and never revisited."""
        path = _place_path(place)
        h = hashlib.sha1(self._key(kingdom, area, path, era).encode("utf-8")).hexdigest()[:6]
        token = hashlib.sha1(str(time.time_ns()).encode("ascii")).hexdigest()[:8]
        head = _slug(path[0])[:16] if path else "place"
        tail = _slug(path[-1])[:16] if len(path) > 1 else "main"
        return f"action-{head}-{tail}-{h}-{token}"

    def _store_seed(self, stem: str, manifest: dict, kingdom: str, area: str, place,
                    description: str, main_npcs, raw: bytes,
                    existing=None, style: str | None = None, era: str = "") -> dict:
        path = _place_path(place)
        ext, mime = sniff_image(raw) or ("png", "image/png")
        slug = (existing or {}).get("slug") or self._seed_slug(kingdom, area, path, era)
        file = f"{slug}.{ext}"
        directory = self.scenes_dir(stem)
        for other in _PORTRAIT_EXTS:
            stale = directory / f"{slug}.{other}"
            if other != ext and stale.exists():
                stale.unlink()
        ImageService._write_image(directory / file, raw)
        now = int(time.time())
        entry = {
            "file": file, "slug": slug, "era": str(era or ""),
            "kingdom": str(kingdom or ""), "area": str(area or ""),
            "place": path,
            "description": str(description or ""), "main_npcs": _npc_list(main_npcs),
            "dead": _dead_names(existing),
            "style": (style or self.style),
            "mime": mime, "created": now, "used": now,
        }
        _prune_dead(entry)
        manifest.setdefault("seeds", {})[self._key(kingdom, area, path, era)] = entry
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
            if fields:
                ident = " ".join(p for p in (fields.get("race"), fields.get("class")) if p)
                look = fields.get("description") or fields.get("role") or ""
                label = fields["name"]
                if ident:
                    label += f" ({ident})"
                if look:
                    label += f" — {look}"
            else:
                label = name
            resolved[label] = " ".join(str(value or "").split())
        return resolved

    def ensure_scene(self, stem: str, player: dict, world: str, description: str = "",
                     mood: str = "", kingdom: str = "", area: str = "", place=None,
                     establishing: str = "", main_npcs=None,
                     seed_change: str = "", npcs=None,
                     time_of_day: str = "", weather: str = "", characters=None,
                     model: str | None = None, style: str | None = None,
                     era: str = "") -> dict:
        """Ensure the hidden seed (idempotent; regenerated on `seed_change` or an art-style
        change), upsert any newly declared NPCs, then generate the action fresh from the seed
        + the portrait and persist it as a one-shot file (served once, then deleted).

        `era` roots the place path (manifest v8): the same place in two eras is two seeds.
        `characters` arrives as `{name: action}`; the stored descriptions are injected here."""
        effective = model or self.model
        effective_style = (style or self.style).strip()
        era = str(era or "").strip().lower()
        manifest = self._read_manifest(stem, era)
        cast = manifest.setdefault("cast", {})
        npcs_added: list[str] = []
        if isinstance(npcs, list):
            for entry in npcs:
                fields = _npc_fields(entry)
                if fields["name"]:
                    cast[_slug(fields["name"])] = fields
                    npcs_added.append(fields["name"])
        seeds = manifest.setdefault("seeds", {})
        path = _place_path(place)
        seed = seeds.get(self._key(kingdom, area, path, era))
        if seed is None and not establishing:
            # kingdom/area omitted (an action call, or a seed change) -> resolve from the place.
            key, found = self._seed_by_place(seeds, path, manifest.get("current"), era)
            if found is not None:
                seed = found
                kingdom = str(found.get("kingdom") or "")
                area = str(found.get("area") or "")
                path = _place_path(found.get("place")) or path
        # A text-only (`note_place`) entry is seed-shaped but has no image. Treat it as
        # unseeded so enabling images draws it now -- preserving its declared main NPCs
        # and description -- instead of treating the metadata as an already-drawn seed.
        text_entry = None
        if seed is not None and self._file_bytes(stem, seed) is None:
            text_entry = seed
            seed = None
        seed_created = seed_regenerated = False
        style_changed = seed is not None and seed.get("style") != effective_style
        if seed is None:
            label = " — ".join(path) or "an unnamed place"
            seed_main = main_npcs if main_npcs else _seed_main_npcs(text_entry)
            seed_desc = establishing or str((text_entry or {}).get("description") or "")
            raw = self.generate_seed(world, kingdom, area, path,
                                     seed_desc or f"An atmospheric view of {label}.",
                                     model=effective, style=effective_style)
            seed = self._store_seed(stem, manifest, kingdom, area, path,
                                    seed_desc, seed_main, raw, existing=text_entry,
                                    style=effective_style, era=era)
            seed_created = True
        elif seed_change or style_changed:
            # A permanent change redraws with the old seed as a reference; a pure style change
            # redraws WITHOUT it, so the new art style is not anchored to the old picture.
            old = self._file_bytes(stem, seed) if seed_change else None
            raw = self.generate_seed(world, kingdom, area, path,
                                     establishing or str(seed.get("description") or ""),
                                     change=seed_change, refs=[old] if old else None,
                                     model=effective, style=effective_style)
            seed = self._store_seed(stem, manifest, kingdom, area, path,
                                    establishing or str(seed.get("description") or ""),
                                    main_npcs if main_npcs else _seed_main_npcs(seed), raw,
                                    existing=seed, style=effective_style, era=era)
            seed_regenerated = True

        # Always draw from the place's empty establishing seed + the portrait. Chaining
        # from the previous action made the model duplicate the figures already in it.
        # An appearance-changing effect (Disguise Self, ...) replaces the portrait: the disguise
        # text is authoritative and the real portrait is deliberately NOT attached.
        appearance = appearance_override(player, self.appearance_effects)
        if seed:
            ref_kind, place_ref = "seed", self._file_bytes(stem, seed)
        elif not appearance:
            ref_kind, place_ref = "portrait", None
        else:
            ref_kind, place_ref = "", None
        portrait = None if appearance else self._read_portrait(stem)
        refs = [r for r in (place_ref, portrait) if r]

        resolved = self._resolve_characters(characters, seed, cast)
        raw = self.generate_action(player, world, description, mood, kingdom, area, path,
                                   ref_kind=ref_kind, refs=refs, model=effective,
                                   time_of_day=time_of_day, weather=weather, characters=resolved,
                                   appearance=appearance, style=effective_style)
        ext = (sniff_image(raw) or ("png", "image/png"))[0]
        slug = self._action_slug(kingdom, area, path, era)
        ImageService._write_image(self.scenes_dir(stem) / f"{slug}.{ext}", raw)
        created = int(time.time())
        if seed:
            # LRU for the priming cap: the place you drew from last is the one the GM
            # should be reminded of first. `seed` is the manifest entry itself.
            seed["used"] = created
        manifest["current"] = {"era": era, "kingdom": str(kingdom or ""), "area": str(area or ""),
                               "place": path, "updated": created}
        self._write_manifest(stem, manifest)
        return {
            "action": {"url": f"/api/scenes/{stem}/{slug}", "created": created},
            "seed": ({"created": seed["created"], "regenerated": seed_regenerated,
                      "description": seed.get("description", "")} if seed_created or seed_regenerated else None),
            "used_seed": ref_kind == "seed",
            "used_portrait_reference": bool(portrait),
            "appearance_applied": bool(appearance),
            "npcs_added": npcs_added,
            "kingdom": str(kingdom or ""), "area": str(area or ""),
            "place": path,
            "seed_created": seed_created, "seed_regenerated": seed_regenerated,
        }

    # ── text-only registry (images off; no generation) ──────────────────────

    def record_place(self, stem: str, era: str = "", kingdom: str = "", area: str = "",
                     place=None, main_npcs=None, cast=None, description: str = "") -> dict:
        """Remember a place (and its regulars) with no image: the text-only registry.

        Used when storyline images are off, so the GM keeps the same place/NPC continuity
        the manifest gives the image path. The entry is seed-shaped with `file: None`, and
        `ensure_scene` later treats a file-less entry as unseeded and draws it. Names +
        roles only: `cast` keeps its description empty (looks are image-only).
        """
        path = _place_path(place)
        if not path:
            return {"recorded": False, "reason": "no place path"}
        era = str(era or "").strip().lower()
        kingdom = re.sub(r"\s+", " ", str(kingdom or "")).strip()[:80]
        area = re.sub(r"\s+", " ", str(area or "")).strip()[:80]
        main = _npc_list(main_npcs)[:20]
        declared = _npc_list(cast)[:20]
        manifest = self._read_manifest(stem, era)
        seeds = manifest.setdefault("seeds", {})
        key = self._key(kingdom, area, path, era)
        now = int(time.time())
        entry = seeds.get(key)
        if entry is None:
            entry = {"file": None, "slug": None, "era": era,
                     "kingdom": kingdom, "area": area, "place": path,
                     "description": str(description or ""), "main_npcs": main,
                     "dead": [],
                     "created": now, "used": now}
            seeds[key] = entry
        else:
            # Never clobber an existing image seed's file/slug: update the address and
            # the declared people only.
            entry["kingdom"] = kingdom or str(entry.get("kingdom") or "")
            entry["area"] = area or str(entry.get("area") or "")
            entry["place"] = path
            if description:
                entry["description"] = str(description)
            if main:
                entry["main_npcs"] = main
            entry["used"] = now
        _prune_dead(entry)
        cast_map = manifest.setdefault("cast", {})
        added: list[str] = []
        for npc in declared:
            if not npc["name"]:
                continue
            slug = _slug(npc["name"])
            prior = cast_map.get(slug) or {}
            cast_map[slug] = {"name": npc["name"],
                              "description": str(prior.get("description") or ""),
                              "role": npc["role"] or str(prior.get("role") or "")}
            added.append(npc["name"])
        manifest["current"] = {"era": era, "kingdom": kingdom, "area": area,
                               "place": path, "updated": now}
        self._write_manifest(stem, manifest)
        return {"recorded": True, "place": path, "era": era, "kingdom": kingdom,
                "area": area,
                "main_npcs": [n["name"] for n in _seed_main_npcs(entry) if n["name"]],
                "cast": added, "file": entry.get("file")}
