"""Fast API checks: /api/worlds shape, static no-store, world deletion.

No LLM calls. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_worlds.py
"""

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from web.server import app, OUTPUT_DIR  # noqa: E402
from web import server as server_mod  # noqa: E402
from web.images import ImageError  # noqa: E402

PORT = 8130
DEL = OUTPUT_DIR / "_deltest"


async def main() -> int:
    # Stub icon generation so the endpoint contract can be exercised without a
    # Gemini key (no network); restore the real methods when done.
    real_available = server_mod.icon_service.available
    real_ensure = server_mod.icon_service.ensure
    server_mod.icon_service.available = lambda: True
    made = []
    icon_models_seen = []

    def _fake_ensure(kind, slug, name, detail="", force=False, model=None):
        made.append(f"{kind}/{slug}")
        icon_models_seen.append(model)
        return {"key": f"{kind}/{slug}", "url": f"/api/icons/{kind}/{slug}",
                "generated": True, "cached": False}

    server_mod.icon_service.ensure = _fake_ensure

    # Stub scene generation + inject a fake (LLM-free) session so the scene
    # endpoints can be exercised cheaply. Scenes persist under
    # output/images/{stem}/scenes/.
    real_scene_available = server_mod.scene_service.available
    real_scene_generate = server_mod.scene_service.generate_scene
    server_mod.scene_service.available = lambda: True
    scene_refs = []
    scene_models_seen = []

    def _fake_scene(player, world, description, mood="", location="", refs=None,
                    has_location_ref=False, has_portrait_ref=False, model=None):
        scene_refs.append(refs)
        scene_models_seen.append(model)
        return b"\xff\xd8\xff\xe0" + b"j" * 24

    server_mod.scene_service.generate_scene = _fake_scene

    # Stub portrait generation so the live-sheet body can be verified offline.
    real_portrait_available = server_mod.image_service.available
    real_ensure_portrait = server_mod.image_service.ensure_portrait
    server_mod.image_service.available = lambda: True
    portrait_seen = {}

    def _fake_ensure_portrait(stem, player, force=False, model=None):
        portrait_seen.update(stem=stem, player=player, force=force)
        return {"generated": True}

    server_mod.image_service.ensure_portrait = _fake_ensure_portrait
    (OUTPUT_DIR / "_portraittest.wwf").write_text("x", encoding="utf-8")

    class _FakeSession:
        def __init__(self):
            self.active_wwf = str(OUTPUT_DIR / "_scenetest.wwf")
            self.player_path = None
            self.wwf_path = None

    server_mod.manager._sessions["scenetest"] = _FakeSession()
    server_mod.manager._meta["scenetest"] = {}
    # A portrait so scene generation always has the character reference.
    scene_img_dir = OUTPUT_DIR / "images" / "_scenetest"
    scene_img_dir.mkdir(parents=True, exist_ok=True)
    (scene_img_dir / "portrait.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"p" * 32)

    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    srv = uvicorn.Server(cfg)
    task = asyncio.create_task(srv.serve())
    ok = True

    def rec(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}") as c:
            for _ in range(60):
                try:
                    if (await c.get("/api/health")).status_code == 200:
                        break
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.1)

            data = (await c.get("/api/worlds")).json()
            worlds = data.get("worlds")
            saves = data.get("saves")
            rec("worlds = list of strings", isinstance(worlds, list) and all(isinstance(w, str) for w in worlds), str(worlds))
            rec("saves = list of objects", isinstance(saves, list) and (not saves or "file" in saves[0]),
                str(saves[:1]))
            if saves:
                rec("saves entries carry a portrait field", "portrait" in saves[0], str(list(saves[0].keys())))
                rec("saves entries carry portrait_modified",
                    "portrait_modified" in saves[0], str(list(saves[0].keys())))

            st = (await c.get("/api/images/status")).json()
            rec("images status shape", set(st) >= {"available", "model", "aspect_ratio"}, str(st))

            models = (await c.get("/api/models")).json()
            rec("/api/models entries carry a provider",
                all("provider" in m for m in models.get("models", [])),
                str(models.get("models", [])[:1]))
            rec("/api/models includes Gemini entries",
                any(m.get("provider") == "gemini" for m in models.get("models", [])))
            image_ids = [x.get("id") for x in models.get("image_models", [])]
            rec("/api/models returns image models + defaults",
                "gemini-3.1-flash-image" in image_ids and "gemini-3.1-flash-lite-image" in image_ids
                and models.get("default_image_model") in image_ids
                and models.get("default_icon_model") in image_ids,
                str(models.get("default_image_model")) + "/" + str(models.get("default_icon_model")))
            rec("in-story default is Nano Banana 2",
                models.get("default_image_model") == "gemini-3.1-flash-image")
            rec("POST scene unknown image model -> 400",
                (await c.post("/api/scene", json={"session_id": "scenetest", "description": "x",
                                                   "model": "not-a-model"})).status_code == 400)
            rec("POST icons unknown image model -> 400",
                (await c.post("/api/icons", json={"items": [{"key": "item/x", "name": "X"}],
                                                   "model": "not-a-model"})).status_code == 400)
            rec("portrait unknown -> 404", (await c.get("/api/portraits/nope.png")).status_code == 404)
            rec("portrait bad ext -> 400", (await c.get("/api/portraits/nope.txt")).status_code == 400)

            icons = (await c.get("/api/icons/index")).json()
            rec("icons index shape", isinstance(icons.get("icons"), dict) and "available" in icons,
                str(list(icons.keys())))
            rec("icon unknown -> 404", (await c.get("/api/icons/weapon/nonexistent")).status_code == 404)
            rec("icon bad kind -> 400", (await c.get("/api/icons/Weapon/dagger")).status_code == 400)
            # unknown keys generate nothing regardless of whether a key is configured
            r = await c.post("/api/icons", json={"keys": ["not-a-kind/nope"]})
            rec("POST icons unknown key is inert", r.status_code in (200, 503), str(r.status_code))

            # On-the-fly generation: arbitrary per-item keys with display names.
            r = await c.post("/api/icons", json={
                "items": [{"key": "item/crossbow-bolts", "name": "Crossbow Bolts"}],
            })
            body = r.json()
            rec("POST icons accepts per-item items",
                r.status_code == 200 and "item/crossbow-bolts" in body.get("generated", []),
                f"status={r.status_code} generated={body.get('generated')}")
            rec("POST icons returns a failed list", isinstance(body.get("failed"), list))

            r = await c.post("/api/icons", json={
                "items": [{"key": "item/overridden", "name": "Overridden"}],
                "model": "gemini-3-pro-image",
            })
            rec("POST icons forwards the image-model override",
                r.status_code == 200 and icon_models_seen[-1] == "gemini-3-pro-image",
                str(icon_models_seen[-1:]))

            r = await c.post("/api/icons", json={"items": [
                {"key": "../../etc/passwd", "name": "x"},
                {"key": "bogus/x", "name": "y"},
                {"key": "weapon/Dagger", "name": "z"},
            ]})
            rec("POST icons skips bad-kind / non-slug keys", r.json().get("generated") == [])  # noqa: E501

            r = await c.post("/api/icons", json={
                "items": [{"key": f"item/k{i}", "name": f"K{i}"} for i in range(10)],
            })
            rec("POST icons honours the batch cap", len(r.json().get("generated", [])) == 6,
                str(len(r.json().get("generated", []))))

            def _fail_ensure(kind, slug, name, detail="", force=False, model=None):
                raise ImageError("boom")

            server_mod.icon_service.ensure = _fail_ensure
            r = await c.post("/api/icons", json={"items": [{"key": "item/boom", "name": "Boom"}]})
            body = r.json()
            rec("POST icons reports per-item failures",
                r.status_code == 200 and body.get("failed", [{}])[0].get("key") == "item/boom",
                f"status={r.status_code} failed={body.get('failed')}")
            server_mod.icon_service.ensure = _fake_ensure

            # Storyline scenes: generated on demand, persisted per save,
            # one image per location (latest wins) reused as the reference.
            r = await c.post("/api/scene", json={
                "session_id": "scenetest", "description": "a forge at dusk",
                "mood": "tense", "location": "Hask's Smithy",
            })
            body = r.json()
            rec("POST scene returns a save-scoped url",
                r.status_code == 200 and body.get("url", "").startswith("/api/scenes/_scenetest/"),
                f"status={r.status_code} url={body.get('url')}")
            rec("POST scene uses the config story model by default",
                scene_models_seen[-1] == "gemini-3.1-flash-image", str(scene_models_seen[-1:]))
            r2 = await c.post("/api/scene", json={
                "session_id": "scenetest", "description": "a forge at dusk",
                "location": "Hask's Smithy", "model": "gemini-3-pro-image",
            })
            rec("POST scene honours an explicit image model",
                r2.status_code == 200 and scene_models_seen[-1] == "gemini-3-pro-image",
                str(scene_models_seen[-1:]))
            url = body.get("url", "")
            rec("portrait attached as a reference on a fresh location",
                body.get("used_portrait_reference") is True, str(body))
            g = await c.get(url)
            rec("GET scene serves the image",
                g.status_code == 200 and g.headers.get("content-type", "").startswith("image/jpeg"),
                f"{g.status_code} {g.headers.get('content-type')}")
            rec("scene written under the save's image dir",
                (OUTPUT_DIR / "images" / "_scenetest" / "scenes" / "hask-smithy.jpg").exists())
            again = await c.post("/api/scene", json={
                "session_id": "scenetest", "description": "the smithy, embers dying",
                "mood": "tense", "location": "Hask's Smithy",
            })
            rec("revisit reuses the location image as reference",
                again.status_code == 200 and again.json().get("used_location_reference") is True
                and again.json().get("used_portrait_reference") is True,
                f"status={again.status_code} body={again.json()}")
            rec("one file per location",
                len(list((OUTPUT_DIR / "images" / "_scenetest" / "scenes").glob("hask-smithy.*"))) == 1)
            rec("POST scene unknown session -> 404",
                (await c.post("/api/scene", json={"session_id": "nope", "description": "x"})).status_code == 404)
            rec("GET scene unknown key -> 404", (await c.get("/api/scenes/_scenetest/nope")).status_code == 404)
            rec("GET scene bad key -> 400", (await c.get("/api/scenes/_scenetest/bad.name")).status_code == 400)

            # Portrait regenerate accepts a live player sheet (no .player needed).
            r = await c.post("/api/portrait", json={
                "wwf": "_portraittest.wwf", "force": True,
                "player": {"race": "Human", "character_class": "Fighter", "level": 10,
                           "inventory": ["Longsword"]},
            })
            rec("POST portrait accepts a live player body",
                r.status_code == 200 and portrait_seen.get("stem") == "_portraittest"
                and portrait_seen.get("player", {}).get("level") == 10
                and portrait_seen.get("force") is True,
                f"status={r.status_code} seen={portrait_seen}")
            r = await c.post("/api/portrait", json={
                "wwf": "_portraittest.wwf",
                "player": {"race": "Human"},
            })
            rec("POST portrait live body needs no .player file", r.status_code == 200)

            r = await c.get("/app.js")
            rec("static Cache-Control no-store", "no-store" in (r.headers.get("cache-control") or ""),
                r.headers.get("cache-control"))

            for suffix in (".wwf", ".player", ".timeline"):
                Path(f"{DEL}{suffix}").write_text("x", encoding="utf-8")
            img_dir = OUTPUT_DIR / "images" / "_deltest"
            img_dir.mkdir(parents=True, exist_ok=True)
            (img_dir / "portrait.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 8)
            saves2 = (await c.get("/api/worlds")).json()["saves"]
            entry = next((s for s in saves2 if s["file"] == "_deltest.wwf"), None)
            rec("portrait URL uses the real extension",
                entry is not None and entry.get("portrait") == "/api/portraits/_deltest.jpg", str(entry))
            rp = await c.get("/api/portraits/_deltest.jpg")
            rec("GET portrait -> 200 image/jpeg",
                rp.status_code == 200 and rp.headers.get("content-type", "").startswith("image/jpeg"),
                rp.headers.get("content-type"))
            pm1 = entry.get("portrait_modified")
            rec("portrait_modified is a timestamp", isinstance(pm1, (int, float)), str(pm1))
            os.utime(img_dir / "portrait.jpg", (float(pm1) + 5, float(pm1) + 5))
            saves3 = (await c.get("/api/worlds")).json()["saves"]
            entry3 = next((s for s in saves3 if s["file"] == "_deltest.wwf"), None)
            rec("portrait_modified changes when the portrait is rewritten",
                entry3 is not None and entry3.get("portrait_modified") != pm1,
                f"{pm1} -> {(entry3 or {}).get('portrait_modified')}")
            r = await c.delete("/api/worlds/_deltest.wwf")
            body = r.json()
            rec("DELETE temp save", r.status_code == 200 and len(body.get("removed", [])) == 4, str(body))
            rec("files removed", not Path(f"{DEL}.wwf").exists() and not Path(f"{DEL}.player").exists())
            rec("image dir removed", not img_dir.exists())

            rec("DELETE unknown -> 404", (await c.delete("/api/worlds/nope.wwf")).status_code == 404)
            rec("DELETE non-.wwf -> 400", (await c.delete("/api/worlds/nofile")).status_code == 400)
    finally:
        srv.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=20)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            task.cancel()
        for suffix in (".wwf", ".player", ".timeline"):
            Path(f"{DEL}{suffix}").unlink(missing_ok=True)
        import shutil
        shutil.rmtree(OUTPUT_DIR / "images" / "_deltest", ignore_errors=True)
        shutil.rmtree(OUTPUT_DIR / "images" / "_scenetest", ignore_errors=True)
        server_mod.icon_service.available = real_available
        server_mod.icon_service.ensure = real_ensure
        server_mod.scene_service.available = real_scene_available
        server_mod.scene_service.generate_scene = real_scene_generate
        server_mod.image_service.available = real_portrait_available
        server_mod.image_service.ensure_portrait = real_ensure_portrait
        (OUTPUT_DIR / "_portraittest.wwf").unlink(missing_ok=True)
        server_mod.manager._sessions.pop("scenetest", None)
        server_mod.manager._meta.pop("scenetest", None)

    print(f"\n  RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
