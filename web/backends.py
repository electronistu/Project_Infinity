"""Image-generation backends: the one seam between a service and an image model.

`ImageError`, the `ImageBackend` interface and its two implementations -- Google
Gemini (hosted) and a local ComfyUI engine -- plus the holder/config plumbing the
services use. Nothing here knows about portraits or scenes: a service hands a
backend a prompt and gets bytes back. `web.images` and `web.scenes` import from
this module; it imports nothing above `web.imageassets`.
"""

from __future__ import annotations

import json
import os
import time
import zlib
from pathlib import Path

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

from .imageassets import sniff_image


class ImageError(RuntimeError):
    """A user-facing image-generation failure (carries an HTTP status hint)."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


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
