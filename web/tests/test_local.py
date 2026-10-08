"""The LOCAL image engine (P11a / D27): ComfyUI client, graph patching, the icon
family's store. No GPU, no network: a stub ComfyUI is served in-process.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_local.py
"""

import io
import json
import sys
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web.icons import IconService, family_for_model  # noqa: E402
from web.images import (  # noqa: E402
    GeminiImageBackend, ImageError, LocalImageBackend, build_local_graph, colour_contrast,
    detail_within, load_workflow, subject_similarity,
)

RESULTS = []
REAL_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
GROUND = (30, 26, 19)


def _icon(colour, *, size=256, box=120):
    """A real PNG: a square subject in `colour` on the icon ground."""
    im = Image.new("RGB", (size, size), GROUND)
    off = (size - box) // 2
    im.paste(Image.new("RGB", (box, box), colour), (off, off))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


DARK = _icon(GROUND)                      # the subject painted in the ground colour
COLOURED = _icon((210, 80, 60))           # the same shape, in colour
_PNG = COLOURED


def _busy() -> bytes:
    """In contrast with the ground, but far too much linework: dense deterministic noise.
    (A fine checkerboard looks busy, but downsampled to 64 px it averages back toward the
    ground and fails the CONTRAST gate instead -- the metric behaving correctly, so the
    fixture is noise, which keeps a strong colour distance at every size.)"""
    import random

    rng = random.Random(7)
    im = Image.new("RGB", (256, 256))
    im.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                for _ in range(256 * 256)])
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


BUSY = _busy()


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


class _ComfyStub(BaseHTTPRequestHandler):
    """The endpoints the client uses: /prompt, /history/{id}, /view, /upload/image."""

    mode = "ok"          # ok | http-error | exec-error | no-image | garbage | empty
    posted: list = []
    uploads: list = []
    serve = None         # bytes | list of bytes (per /prompt call)

    def log_message(self, *args):  # keep the test output clean
        return

    def _send(self, payload, code=200, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "image/png" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b""
        path = self.path.rstrip("/")
        if path == "/upload/image":
            type(self).uploads.append(raw)
            return self._send({"name": "infinity-icon-repair.png", "subfolder": "",
                               "type": "input"})
        if path == "/prompt":
            if type(self).mode == "http-error":
                return self._send({"error": {"message": "bad graph"}}, 400)
            body = json.loads(raw or b"{}")
            if "prompt" not in body:
                return self._send({"error": "no prompt"}, 400)
            type(self).posted.append(body)
            return self._send({"prompt_id": "p1"})
        self._send({"error": "not found"}, 404)

    def do_GET(self):
        mode = type(self).mode
        if self.path.startswith("/system_stats"):
            return self._send({"system": {"comfyui_version": "stub"}})
        if self.path.startswith("/history/"):
            if mode == "no-image":
                return self._send({})
            if mode == "exec-error":
                return self._send({"p1": {"status": {"status_str": "error", "messages": [
                    ["execution_error", {"error": "CUDA out of memory"}]]}, "outputs": {}}})
            return self._send({"p1": {"status": {"status_str": "success"},
                                      "outputs": {"7": {"images": [
                                          {"filename": "icon.png", "subfolder": "",
                                           "type": "output"}]}}}})
        if self.path.startswith("/view"):
            if mode == "garbage":
                return self._send(None, raw=b"definitely not an image")
            if mode == "empty":
                return self._send(None, raw=b"")
            serve = type(self).serve
            if isinstance(serve, list):
                n = max(0, len(type(self).posted) - 1)
                return self._send(None, raw=serve[min(n, len(serve) - 1)])
            return self._send(None, raw=serve if serve else _PNG)
        self._send({"error": "not found"}, 404)


class _Comfy:
    """A stub ComfyUI on a random local port."""

    def __init__(self, mode="ok", serve=None):
        _ComfyStub.mode = mode
        _ComfyStub.posted = []
        _ComfyStub.uploads = []
        _ComfyStub.serve = serve
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ComfyStub)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def posted(self) -> list:
        return _ComfyStub.posted

    @property
    def uploads(self) -> list:
        return _ComfyStub.uploads

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _backend(endpoint: str, **over) -> LocalImageBackend:
    cfg = {"local": {"endpoint": endpoint, "workflow": "config/comfy/icon.json",
                     "checkpoint": "sdxl_lightning_4step.safetensors",
                     "model_id": "local/sdxl-lightning-4step", "steps": 8, "cfg": 2.0,
                     "sampler": "euler", "scheduler": "sgm_uniform", "canvas": 1024,
                     "timeout_seconds": 0.6, "warmup_seconds": 0.6,
                     "negative": "frame, border"}}
    cfg["local"].update(over)
    return LocalImageBackend.from_config(cfg, aspect_ratio="1:1")


def _recipe_seed(backend: LocalImageBackend, key: str, family: str) -> int:
    """The seed the service derives for an icon key."""
    return backend.recipe(prompt="", seed=zlib.crc32(f"{key}|{family}".encode("utf-8"))
                          & 0xFFFFFFFF)["seed"]


def _set_passes_zero(td: str) -> bool:
    """The loop must be switchable off (a Gemini-style family never repairs anyway)."""
    svc = IconService(td, "local", backend=_GeminiStub())
    svc.repair_passes = 0
    svc._check_and_repair = None  # type: ignore[assignment]
    return svc.repair_passes == 0 and not hasattr(_GeminiStub(), "repair")


def main() -> bool:
    # ── the vendored workflow document ────────────────────────────────────
    graph, nodes = load_workflow(REPO / "config" / "comfy" / "icon.json")
    rec("the vendored workflow loads", isinstance(graph, dict) and bool(nodes))
    rec("it names every patch point",
        set(nodes) >= {"positive", "negative", "sampler", "canvas", "checkpoint"},
        str(sorted(nodes)))
    built = build_local_graph(graph, nodes, prompt="a simple flat icon", negative="frame",
                              seed=1234, steps=4, cfg_scale=1.0, sampler="euler",
                              scheduler="sgm_uniform", canvas=768, checkpoint="x.safetensors")
    smp = built[nodes["sampler"]]["inputs"]
    rec("the graph patches the sampler",
        (smp["seed"], smp["steps"], smp["cfg"], smp["sampler_name"]) == (1234, 4, 1.0, "euler"))
    rec("the graph patches the prompts",
        built[nodes["positive"]]["inputs"]["text"] == "a simple flat icon"
        and built[nodes["negative"]]["inputs"]["text"] == "frame")
    rec("the graph patches the canvas and the checkpoint",
        built[nodes["canvas"]]["inputs"]["width"] == 768
        and built[nodes["checkpoint"]]["inputs"]["ckpt_name"] == "x.safetensors")
    rec("the source graph is not mutated",
        graph[nodes["sampler"]]["inputs"]["steps"] == 8
        and graph[nodes["positive"]]["inputs"]["text"] == "")
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.json"
        bad.write_text('{"workflow": {}, "nodes": {"positive": "9"}}', encoding="utf-8")
        try:
            load_workflow(bad)
            rec("a workflow missing a named node fails loudly", False)
        except ImageError:
            rec("a workflow missing a named node fails loudly", True)
        try:
            load_workflow(Path(td) / "absent.json")
            rec("a missing workflow file fails loudly", False)
        except ImageError:
            rec("a missing workflow file fails loudly", True)

    # ── the client, against a stub ComfyUI ─────────────────────────────────
    comfy = _Comfy("ok")
    try:
        be = _backend(comfy.endpoint)
        rec("available() sees a live engine", be.available() is True)
        rec("the model id is the local one", be.model == "local/sdxl-lightning-4step")
        rec("status carries the recipe",
            be.status()["steps"] == 8 and be.status()["sampler"] == "euler")
        raw = be.generate("a simple flat icon of a dagger")
        rec("generate returns the image bytes", raw == _PNG)
        posted = comfy.posted[-1]["prompt"]
        rec("the posted graph is the vendored one",
            set(posted) == set(json.loads((REPO / "config" / "comfy" / "icon.json")
                                          .read_text(encoding="utf-8"))["workflow"]))
        rec("the prompt reaches ComfyUI",
            posted["3"]["inputs"]["text"].startswith("a simple flat icon"))
        rec("the negative reaches ComfyUI", posted["4"]["inputs"]["text"] == "frame, border")
        rec("the settings reach ComfyUI",
            posted["5"]["inputs"]["steps"] == 8 and posted["5"]["inputs"]["cfg"] == 2.0)
        seed_a = posted["5"]["inputs"]["seed"]
        be.generate("a simple flat icon of a dagger")
        rec("the same prompt gives the same seed (reproducible)",
            comfy.posted[-1]["prompt"]["5"]["inputs"]["seed"] == seed_a, str(seed_a))
        be.generate("a simple flat icon of a dagger", seed=777)
        rec("an explicit seed wins", comfy.posted[-1]["prompt"]["5"]["inputs"]["seed"] == 777)
        rec("recipe() reports what will be used",
            be.recipe(prompt="x", seed=777) == {"seed": 777, "steps": 8, "cfg": 2.0,
                                                "sampler": "euler", "scheduler": "sgm_uniform",
                                                "canvas": 1024,
                                                "checkpoint": "sdxl_lightning_4step.safetensors"})
        try:
            be.generate("x", refs=[(b"\xff\xd8\xff\xe0", "image/jpeg")])
            rec("a reference image is refused, not ignored", False)
        except ImageError:
            rec("a reference image is refused, not ignored", True)
        rec("fingerprint covers the engine settings", "euler" in be.fingerprint())
        rec("a different sampler is a different fingerprint",
            be.fingerprint() != _backend(comfy.endpoint, sampler="dpmpp_2m").fingerprint())
    finally:
        comfy.close()

    for mode, label in (("http-error", "a rejected graph"),
                        ("exec-error", "a ComfyUI execution error"),
                        ("no-image", "no image in time"),
                        ("garbage", "a reply that is not an image"),
                        ("empty", "an empty image")):
        stub = _Comfy(mode)
        try:
            be = _backend(stub.endpoint, timeout_seconds=0.6, warmup_seconds=0.6)
            try:
                be.generate("x")
                rec(f"{label} raises ImageError", False)
            except ImageError:
                rec(f"{label} raises ImageError", True)
        finally:
            stub.close()

    # An unreachable engine is "not available", never a crash.
    dead = _backend("http://127.0.0.1:9")
    rec("an unreachable engine is not available", dead.available() is False)
    try:
        dead.generate("x")
        rec("an unreachable engine fails loudly on generate", False)
    except ImageError:
        rec("an unreachable engine fails loudly on generate", True)

    # ── the icon family that uses it ───────────────────────────────────────
    rec("the local family is derived from the local model id",
        family_for_model("local/sdxl-lightning-4step") == "local")
    rec("the gemini family is unaffected",
        family_for_model("gemini-3.1-flash-lite-image") == "gemini")

    with tempfile.TemporaryDirectory() as td:
        stub = _Comfy("ok")
        try:
            svc = IconService(td, "local", backend=_backend(stub.endpoint))
            r = svc.ensure("race", "gnome", "Gnome")
            rec("the local family writes into its own store",
                r["generated"] is True
                and (Path(td) / "local" / "race" / "gnome.png").exists())
            rec("the url names the local family",
                r["url"] == "/api/icons/local/race/gnome")
            manifest = json.loads((Path(td) / "local" / "manifest.json").read_text(encoding="utf-8"))
            entry = (manifest.get("icons") or {}).get("race/gnome") or {}
            rec("the manifest records the family and the model",
                entry.get("family") == "local"
                and entry.get("model") == "local/sdxl-lightning-4step")
            rec("the manifest records the recipe (seed, sampler, steps)",
                isinstance(entry.get("seed"), int) and entry.get("steps") == 8
                and entry.get("sampler") == "euler" and entry.get("canvas") == 1024,
                json.dumps({k: entry.get(k) for k in ("seed", "steps", "sampler")}))
            rec("the seed is derived from the icon key, and is stable",
                entry.get("seed") == _recipe_seed(_backend(stub.endpoint), "race/gnome", "local")
                and entry.get("seed") == _recipe_seed(svc.backend, "race/gnome", "local"),
                str(entry.get("seed")))
            again = svc.ensure("race", "gnome", "Gnome")
            rec("a second ensure is cached", again["generated"] is False and again["cached"] is True)
            rec("the gemini store is untouched by a local bake",
                not (Path(td) / "gemini").exists())
            # A re-tuned engine regenerates INSIDE its own family, and only there.
            retuned = IconService(td, "local", backend=_backend(stub.endpoint, steps=12))
            forced = retuned.ensure("race", "gnome", "Gnome")
            rec("re-tuning the engine regenerates its own family's icon",
                forced["generated"] is True)
            gem = IconService(td, "gemini", backend=_GeminiStub())
            gem.ensure("race", "gnome", "Gnome")
            rec("the same key lives in both families, side by side",
                (Path(td) / "gemini" / "race" / "gnome.png").exists()
                and (Path(td) / "local" / "race" / "gnome.png").exists())
            rec("the gemini family sees only its own icons",
                set(gem.index()) == {"race/gnome"} and "race/gnome" in svc.index())
        finally:
            stub.close()

    # ── the hosted backend's hash must not move ────────────────────────────
    rec("a hosted backend has no engine fingerprint",
        GeminiImageBackend("gemini-3.1-flash-lite-image", "1:1", "1K").fingerprint() == "")
    rec("a hosted backend reports no recipe",
        GeminiImageBackend("gemini-3.1-flash-lite-image", "1:1", "1K").recipe(prompt="x") == {})
    with tempfile.TemporaryDirectory() as td:
        svc = IconService(td, backend=_GeminiStub())
        h1 = svc._source_hash("weapon/dagger", "a prompt", model="gemini-3.1-flash-lite-image")
        h2 = svc._source_hash("weapon/dagger", "a prompt", model="gemini-3.1-flash-lite-image",
                              engine="")
        rec("an empty engine leaves the committed hashes byte-identical", h1 == h2)
        h3 = svc._source_hash("weapon/dagger", "a prompt", model="gemini-3.1-flash-lite-image",
                              engine="engine-v2")
        rec("a non-empty engine changes the hash", h3 != h1)

    # ── the contrast check (colour, not lightness) ─────────────────────────
    fails = colour_contrast(DARK, GROUND)
    rec("a subject painted in the ground colour FAILS",
        fails["ok"] is False and fails["px"] == 0, json.dumps(fails))
    passes = colour_contrast(COLOURED, GROUND)
    rec("a coloured subject passes", passes["ok"] is True and passes["px"] > 500,
        json.dumps(passes))
    rec("a subject only slightly off the ground still fails (contrast, not lightness)",
        colour_contrast(_icon((70, 60, 50)), GROUND)["ok"] is False)
    twins = sorted((REPO / "assets" / "gemini").rglob("*.png"))[::30]
    passed = [p for p in twins
              if colour_contrast(p.read_bytes(), GROUND, min_distance=60,
                                 min_px=500)["ok"]]
    rate = len(passed) / len(twins) if twins else 0
    rec("the rule is calibrated on the committed family (a sample of it passes)",
        len(twins) >= 20 and rate >= 0.75,
        f"{len(passed)}/{len(twins)} of the committed icons pass ({100 * rate:.0f}%)")
    rec("the same shape twice is the same picture",
        subject_similarity(COLOURED, COLOURED, GROUND)["iou"] == 1.0)
    rec("an invisible icon and a redrawn one are not the same picture",
        subject_similarity(DARK, COLOURED, GROUND)["iou"] == 0.0)

    # ── the detail ceiling (C4): linework and grain, at the tooltip size ──
    flat = detail_within(COLOURED, max_edge=0.217, max_hf=29.7)
    rec("a flat emblem is simple enough", flat["ok"] is True, json.dumps(flat))
    busy = detail_within(BUSY, max_edge=0.217, max_hf=29.7)
    rec("a crammed icon is too detailed",
        busy["ok"] is False and busy["edge"] > 0.217 and busy["hf"] > 29.7, json.dumps(busy))
    rec("a crammed icon can still pass the CONTRAST gate (the two gates are independent)",
        colour_contrast(BUSY, GROUND)["ok"] is True, json.dumps(colour_contrast(BUSY, GROUND)))
    root = REPO / "assets" / "gemini"
    sample = [p for d in sorted(root.iterdir()) if d.is_dir()
              for p in sorted(d.glob("*.png"))[:3]]     # stratified across the kinds
    passed_n = sum(1 for p in sample if detail_within(p.read_bytes())["ok"])
    rec("the ceiling is calibrated on the committed family (~9 in 10 pass)",
        len(sample) >= 40 and passed_n >= 0.8 * len(sample),
        f"{passed_n}/{len(sample)} of the committed icons pass")

    # ── the repair graph and the repair call ───────────────────────────────
    rgraph, rnodes = load_workflow(REPO / "config" / "comfy" / "icon_img2img.json")
    rec("the repair workflow loads", isinstance(rgraph, dict) and "load" in rnodes)
    built_r = build_local_graph(rgraph, rnodes, prompt="p", negative="n", seed=5, steps=12,
                               cfg_scale=4.0, denoise=0.5, load="prev.png")
    rec("the repair graph loads the previous image",
        built_r[rnodes["load"]]["inputs"]["image"] == "prev.png")
    rec("the repair graph re-samples at a mid denoise, at the higher guidance",
        built_r[rnodes["sampler"]]["inputs"]["denoise"] == 0.5
        and built_r[rnodes["sampler"]]["inputs"]["cfg"] == 4.0
        and built_r[rnodes["sampler"]]["inputs"]["steps"] == 12)
    rec("a text-to-image graph still uses denoise 1.0",
        build_local_graph(graph, nodes, prompt="p")[nodes["sampler"]]["inputs"]["denoise"] == 1.0)
    stub_r = _Comfy("ok", serve=COLOURED)
    try:
        be_r = _backend(stub_r.endpoint)
        out_r = be_r.repair(COLOURED, "the same icon, in colour", mode="img2img", seed=7,
                            denoise=0.5, steps=12, cfg_scale=4.0)
        rec("img2img repair uploads the previous image and returns the new one",
            out_r == COLOURED and len(stub_r.uploads) == 1)
        rec("the repair posts the img2img graph",
            "LoadImage" in json.dumps(stub_r.posted[-1]["prompt"]))
        out_d = be_r.repair(DARK, "the icon, in colour", mode="redraw", seed=7, steps=12,
                            cfg_scale=4.0)
        rec("a redraw uploads nothing and posts the text-to-image graph",
            out_d == COLOURED and len(stub_r.uploads) == 1
            and "LoadImage" not in json.dumps(stub_r.posted[-1]["prompt"]))
    finally:
        stub_r.close()

    # ── the loop: check, repair, early exit, and the record ────────────────
    with tempfile.TemporaryDirectory() as td2:
        stub = _Comfy("ok", serve=[DARK, COLOURED])
        try:
            svc = IconService(td2, "local", backend=_backend(stub.endpoint))
            rec("the loop is armed from the config for the local family",
                svc.repair_passes == 3 and svc.contrast_min_px == 500)
            r = svc.ensure("race", "tiefling", "Tiefling")
            stored = (Path(td2) / "local" / "race" / "tiefling.png").read_bytes()
            rec("a failing icon is repaired and the REPAIRED file is stored",
                r["generated"] is True and colour_contrast(stored, GROUND)["ok"] is True)
            rec("the record says what happened",
                r["repair"]["before"]["ok"] is False and r["repair"]["ok"] is True
                and r["repair"]["passes"][0]["mode"] == "redraw",
                json.dumps({k: v for k, v in r["repair"].items() if k != "passes"}))
            rec("one draw plus one repair, then it stops (early exit)",
                len(stub.posted) == 2, f"{len(stub.posted)} engine calls")
            rec("a redraw repair hands nothing back to the engine (best-of-N, not img2img)",
                len(stub.uploads) == 0)
            manifest = json.loads((Path(td2) / "local" / "manifest.json").read_text(encoding="utf-8"))
            entry = (manifest.get("icons") or {}).get("race/tiefling") or {}
            rec("the manifest keeps the check and the repair record",
                entry.get("check", {}).get("ok") is True
                and entry.get("check", {}).get("detail", {}).get("ok") is True
                and entry.get("repair", {}).get("before", {}).get("ok") is False
                and entry.get("repair", {}).get("ok") is True
                and entry.get("repair", {}).get("passes", [{}])[0].get("mode") == "redraw")
            rec("a repaired icon is cached like any other",
                svc.ensure("race", "tiefling", "Tiefling")["cached"] is True)
        finally:
            stub.close()
        stub2 = _Comfy("ok", serve=COLOURED)
        try:
            svc2 = IconService(td2, "local", backend=_backend(stub2.endpoint))
            r2 = svc2.ensure("race", "gnome", "Gnome")
            rec("an icon that already passes is never repaired",
                len(stub2.posted) == 1 and "repair" not in r2
                and r2["check"]["ok"] is True)
        finally:
            stub2.close()
        stub3 = _Comfy("ok", serve=[DARK])
        try:
            svc3 = IconService(td2, "local", backend=_backend(stub3.endpoint, timeout_seconds=0.6))
            svc3.repair_passes = 1
            r3 = svc3.ensure("race", "half-elf", "Half-Elf")
            rec("a repair that fails to fix the icon is recorded, not hidden",
                r3["repair"]["ok"] is False and len(r3["repair"]["passes"]) == 1)
        finally:
            stub3.close()
        # Every retry must be a NEW draw: the first version reused the seed, so passes 2 and 3
        # reproduced attempt 1 byte-for-byte and the retry was theatre.
        stub4 = _Comfy("ok", serve=[DARK, DARK, DARK, COLOURED])
        try:
            svc4 = IconService(td2, "local", backend=_backend(stub4.endpoint))
            r4 = svc4.ensure("race", "half-orc", "Half-Orc")
            seeds = [p["seed"] for p in r4["repair"]["passes"]]
            base = zlib.crc32(b"race/half-orc|local") & 0xFFFFFFFF
            rec("each retry draws at a fresh seed",
                len(seeds) == 3 and len(set(seeds)) == 3 and base not in seeds,
                f"seeds={seeds} base={base}")
            rec("the loop keeps trying until the check passes, then stops",
                r4["repair"]["ok"] is True and len(stub4.posted) == 4
                and len(r4["repair"]["passes"]) == 3,
                f"{len(stub4.posted)} engine calls, {len(r4['repair']['passes'])} retries")
        finally:
            stub4.close()
        # Both gates: a draw that is in contrast but crammed must be retried, and the loop
        # must stop on the one that satisfies BOTH.
        stub5 = _Comfy("ok", serve=[BUSY, COLOURED])
        try:
            svc5 = IconService(td2, "local", backend=_backend(stub5.endpoint))
            r5 = svc5.ensure("stat", "ac", "Armor Class")
            before = r5["repair"]["before"]
            rec("a crammed icon passes the contrast gate but fails the detail gate",
                before["contrast"]["ok"] is True and before["detail"]["ok"] is False
                and before["ok"] is False, json.dumps(before))
            rec("the loop retries until BOTH gates pass",
                r5["repair"]["ok"] is True and len(stub5.posted) == 2,
                f"{len(stub5.posted)} engine calls")
        finally:
            stub5.close()
        # Nothing passes: the least-detailed attempt is the one kept.
        stub6 = _Comfy("ok", serve=[BUSY, DARK])
        try:
            svc6 = IconService(td2, "local", backend=_backend(stub6.endpoint))
            svc6.repair_passes = 1
            r6 = svc6.ensure("save", "charisma", "Charisma")
            rec("when nothing passes, the least detailed attempt is kept",
                r6["repair"]["ok"] is False
                and r6["repair"]["after"]["detail"]["hf"] < before["detail"]["hf"],
                json.dumps(r6["repair"]["after"]))
        finally:
            stub6.close()
        rec("the loop is off when the config says zero passes",
            _set_passes_zero(td2) is True)

    print()
    print(f"  RESULT: {'PASS' if all(RESULTS) else 'FAIL'}  ({sum(RESULTS)}/{len(RESULTS)})")
    return all(RESULTS)
class _GeminiStub(GeminiImageBackend):
    """A Gemini backend that never talks to Google (tests the family, not the API)."""

    def __init__(self):
        super().__init__("gemini-3.1-flash-lite-image", "1:1", "1K")

    def available(self) -> bool:
        return True

    def generate(self, prompt, aspect_ratio=None, refs=None, ref_media_resolution=None,
                 model=None, thinking_level=None, seed=None):
        return _PNG


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
