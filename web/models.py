"""Curated model registry for the web client.

`MODELS` is the Game Master picker; each entry carries a `provider` so the
engine knows which backend adapter to use. `IMAGE_MODELS` backs the two image
pickers (in-story images vs. sheet icons).
"""

DEFAULT_MODEL = "deepseek-v4.1-flash:cloud"
DEFAULT_TEMPERATURE = 1.0

# Ollama Cloud entries; `provider` selects the streaming adapter.
MODELS = [
    {
        "id": "deepseek-v4.1-flash:cloud",
        "label": "DeepSeek V4.1 Flash (Ollama Cloud)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "ollama",
    },
    {
        "id": "deepseek-v4-pro:cloud",
        "label": "DeepSeek V4 Pro (Ollama Cloud)",
        "context": 1_000_000,
        "default_temperature": 1.0,
        "provider": "ollama",
    },
    {
        "id": "kimi-k2.6:cloud",
        "label": "Kimi K2.6 (Ollama Cloud)",
        "context": 250_000,
        "default_temperature": 1.0,
        "provider": "ollama",
    },
    # Google Gemini (needs GEMINI_API_KEY, the same key the images use).
    # All listed models take 1,048,576 input tokens and support function calling.
    # Gemini 3.x uses `thinking_level` (the numeric `thinking_budget` is retired)
    # and ignores custom sampling, so `thinking_level` pins the GM's effort
    # (None = the model's own default) and `thinking_levels` lists what the model
    # accepts, so an unsupported level (e.g. "minimal" on 3.8 Flash) is never sent.
    {
        "id": "gemini-3.8-flash",
        "label": "Gemini 3.8 Flash (Google)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "gemini",
        "thinking_level": None,
        "thinking_levels": ["low", "medium", "high"],
    },
    {
        "id": "gemini-3.6-flash",
        "label": "Gemini 3.6 Flash (Google)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "gemini",
        "thinking_level": None,
        "thinking_levels": ["minimal", "low", "medium", "high"],
    },
    {
        "id": "gemini-3.5-flash",
        "label": "Gemini 3.5 Flash (Google)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "gemini",
        "thinking_level": None,
        "thinking_levels": ["minimal", "low", "medium", "high"],
    },
    {
        "id": "gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash-Lite (Google)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "gemini",
        "thinking_level": None,
        "thinking_levels": ["minimal", "low", "medium", "high"],
    },
    {
        "id": "gemini-3.1-pro-preview",
        "label": "Gemini 3.1 Pro Preview (Google)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "gemini",
        "thinking_level": None,
        "thinking_levels": ["low", "medium", "high"],
    },
]

# Gemini "Nano Banana" image models. `gemini-2.5-flash-image` is omitted (it
# retires 2026-10-02) and `gemini-3.1-flash-image` (Nano Banana 2) is deprecated
# in favour of `gemini-nano-banana-2.1`; Imagen is shut down.
#
# `thinking_levels` is what each model accepts and `thinking_level` its default
# (mirrors MODELS above). They differ per model — NB2.1 takes medium, Lite does not,
# and Pro takes no thinking level at all — so a desired level is validated against
# the chosen model before it is sent (see `image_thinking_level`).
IMAGE_MODELS = [
    {
        "id": "gemini-3.1-flash-lite-image",
        "label": "Nano Banana 2 Lite (fastest, cheapest, 1K)",
        "thinking_levels": ["minimal", "high"],
        "thinking_level": "minimal",
    },
    {
        "id": "gemini-nano-banana-2.1",
        "label": "Nano Banana 2.1 (better quality + references)",
        "thinking_levels": ["minimal", "medium", "high"],
        "thinking_level": "medium",
    },
    {
        "id": "gemini-3-pro-image",
        "label": "Nano Banana Pro (slowest, highest quality)",
        "thinking_levels": [],
        "thinking_level": None,
    },
]

# In-story images (portrait + storyline scenes) feed references to the model,
# so the multi-reference-capable Nano Banana 2.1 is the default. Icons are a
# single subject with no references, so the cheap Lite model stays ideal.
DEFAULT_IMAGE_MODEL = "gemini-nano-banana-2.1"
DEFAULT_ICON_MODEL = "gemini-3.1-flash-lite-image"

# Local image models: the icon family's OWN engine, served by ComfyUI on this machine
# (D27/P11a). They are offered for ICONS ONLY — the story images need a
# reference-consistency story the local path has not got yet (the seed and the action,
# S6) — so `/api/models` publishes them in `icon_models` and only the icon picker lists
# them. The family follows the model id: `local/...` -> the `local` store.
LOCAL_ICON_MODELS = [
    {
        "id": "local/sdxl-lightning-4step",
        "label": "Local SDXL-Lightning (this PC's GPU, free)",
        "thinking_levels": [],
        "thinking_level": None,
        "backend": "local",
    },
]


def list_icon_models() -> list[dict]:
    """Everything the sheet-icon picker offers: the hosted models plus the local one."""
    return list_image_models() + [dict(m) for m in LOCAL_ICON_MODELS]


def resolve_icon_model(model_id: str | None) -> dict | None:
    """A model the sheet-icon picker offers (Gemini or local)."""
    if not model_id:
        return None
    for model in list_icon_models():
        if model["id"] == model_id:
            return model
    return None


def list_models() -> list[dict]:
    return MODELS


def resolve_model(model_id: str | None) -> dict | None:
    if not model_id:
        return None
    for model in MODELS:
        if model["id"] == model_id:
            return model
    return None


def gemini_thinking_level(spec: dict | None) -> str | None:
    """Validated `thinking_level` for a Gemini model (None = model default).

    Returns None for non-Gemini models, for models that pin no level, and for a
    pinned level the model does not support (so the request can never 400).
    """
    if not spec or spec.get("provider") != "gemini":
        return None
    level = spec.get("thinking_level")
    return level if level in (spec.get("thinking_levels") or []) else None


def list_image_models() -> list[dict]:
    return IMAGE_MODELS


def resolve_image_model(model_id: str | None) -> dict | None:
    if not model_id:
        return None
    for model in IMAGE_MODELS:
        if model["id"] == model_id:
            return model
    return None


def image_thinking_level(model_id: str | None, desired: str | None) -> str | None:
    """Validated `thinking_level` for a Gemini image model (None = omit / model default).

    Image models differ: NB2.1 supports minimal/medium/high, Lite only minimal/high,
    and Pro takes no `thinking_level` at all. Sending an unsupported level (e.g.
    `medium` to Lite/Pro) is a 400 INVALID_ARGUMENT, so an unsupported desired level
    is omitted and the model's own default applies.
    """
    spec = resolve_image_model(model_id)
    supported = (spec or {}).get("thinking_levels") or []
    level = str(desired or "").strip().lower()
    return level if level in supported else None
