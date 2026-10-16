"""Family-agnostic image leaves shared by the web image/icon services.

Nothing here knows about portraits, icons, scenes, families or caches: it is the
byte-level mechanics every image store needs — magic-byte sniffing, resizing,
framing, the local-engine measurement helpers, the one canonical asset slug, and
the atomic JSON/bytes writers. `images.py` and `icons.py` keep their own cache
semantics and call in here for the mechanics.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


_PARENS = re.compile(r"\([^)]*\)")
_POSSESSIVE = re.compile(r"['\u2019]s\b")
_NUMBER = re.compile(r"\b[+-]?\d+\b")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_PORTRAIT_EXTS = ("png", "jpg", "webp")
_MIME_BY_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def slugify(text) -> str:
    """The one canonical asset slug: lower-case, parentheticals/possessives/
    quantities dropped, every non-alphanumeric run folded to '-', then trimmed."""
    text = _PARENS.sub(" ", str(text or "").lower())
    text = _POSSESSIVE.sub(" ", text)
    text = _NUMBER.sub(" ", text)
    return _NON_ALNUM.sub("-", text).strip("-")


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


def first_existing(directory: Path, name: str, exts=_PORTRAIT_EXTS) -> Path | None:
    """The first `<name>.<ext>` that exists under `directory`, or None."""
    for ext in exts:
        candidate = Path(directory) / f"{name}.{ext}"
        if candidate.exists():
            return candidate
    return None


def drop_stale(directory: Path, name: str, keep_ext: str) -> None:
    """Delete `<name>.<ext>` files whose format is not `keep_ext` (old formats)."""
    for ext in _PORTRAIT_EXTS:
        if ext == keep_ext:
            continue
        stale = Path(directory) / f"{name}.{ext}"
        if stale.exists():
            stale.unlink()


def read_json_dict(path) -> dict:
    """Parse a JSON object at `path`; {} on missing/corrupt/non-object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json_atomic(path, data) -> None:
    """`json.dumps(data, indent=2, ensure_ascii=False)` to `<path>.json.tmp`, then replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def write_bytes_atomic(path, data: bytes) -> None:
    """Write `data` to `<path>.tmp`, then atomically replace `path`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
