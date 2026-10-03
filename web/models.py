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
    {
        "id": "gemini-3.8-flash",
        "label": "Gemini 3.8 Flash (Google)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "gemini",
    },
    {
        "id": "gemini-3.6-flash",
        "label": "Gemini 3.6 Flash (Google)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "gemini",
    },
    {
        "id": "gemini-3.5-flash",
        "label": "Gemini 3.5 Flash (Google)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "gemini",
    },
    {
        "id": "gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash-Lite (Google)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "gemini",
    },
    {
        "id": "gemini-3.1-pro-preview",
        "label": "Gemini 3.1 Pro Preview (Google)",
        "context": 1_048_576,
        "default_temperature": 1.0,
        "provider": "gemini",
    },
]

# Gemini "Nano Banana" image models. `gemini-2.5-flash-image` is omitted (it
# retires 2026-10-02); Imagen is deprecated.
IMAGE_MODELS = [
    {
        "id": "gemini-3.1-flash-lite-image",
        "label": "Nano Banana 2 Lite (fastest, cheapest, 1K)",
    },
    {
        "id": "gemini-3.1-flash-image",
        "label": "Nano Banana 2 (better quality + references)",
    },
    {
        "id": "gemini-3-pro-image",
        "label": "Nano Banana Pro (slowest, highest quality)",
    },
]

# In-story images (portrait + storyline scenes) feed references to the model,
# so the multi-reference-capable Nano Banana 2 is the default. Icons are a
# single subject with no references, so the cheap Lite model stays ideal.
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"
DEFAULT_ICON_MODEL = "gemini-3.1-flash-lite-image"


def list_models() -> list[dict]:
    return MODELS


def resolve_model(model_id: str | None) -> dict | None:
    if not model_id:
        return None
    for model in MODELS:
        if model["id"] == model_id:
            return model
    return None


def list_image_models() -> list[dict]:
    return IMAGE_MODELS


def resolve_image_model(model_id: str | None) -> dict | None:
    if not model_id:
        return None
    for model in IMAGE_MODELS:
        if model["id"] == model_id:
            return model
    return None
