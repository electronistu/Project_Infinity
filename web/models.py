"""Curated model registry for the web client.

Extend `MODELS` to offer more backends in the picker. Each entry needs an
Ollama tag and its context window.
"""

DEFAULT_MODEL = "deepseek-v4.1-flash:cloud"
DEFAULT_TEMPERATURE = 1.0

MODELS = [
    {
        "id": "deepseek-v4.1-flash:cloud",
        "label": "DeepSeek V4.1 Flash (Ollama Cloud)",
        "context": 1_048_576,
        "default_temperature": 1.0,
    },
    {
        "id": "deepseek-v4-pro:cloud",
        "label": "DeepSeek V4 Pro (Ollama Cloud)",
        "context": 1_000_000,
        "default_temperature": 1.0,
    },
    {
        "id": "kimi-k2.6:cloud",
        "label": "Kimi K2.6 (Ollama Cloud)",
        "context": 250_000,
        "default_temperature": 1.0,
    },
]


def list_models() -> list[dict]:
    return MODELS


def resolve_model(model_id: str | None) -> dict | None:
    if not model_id:
        return None
    for model in MODELS:
        if model["id"] == model_id:
            return model
    return None
