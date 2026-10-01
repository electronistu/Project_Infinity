"""Fast API checks: /api/worlds shape, static no-store, world deletion.

No LLM calls. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_worlds.py
"""

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from web.server import app, OUTPUT_DIR  # noqa: E402

PORT = 8130
DEL = OUTPUT_DIR / "_deltest"


async def main() -> int:
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

            r = await c.get("/app.js")
            rec("static Cache-Control no-store", "no-store" in (r.headers.get("cache-control") or ""),
                r.headers.get("cache-control"))

            for suffix in (".wwf", ".player", ".timeline"):
                Path(f"{DEL}{suffix}").write_text("x", encoding="utf-8")
            r = await c.delete("/api/worlds/_deltest.wwf")
            body = r.json()
            rec("DELETE temp save", r.status_code == 200 and len(body.get("removed", [])) == 3, str(body))
            rec("files removed", not Path(f"{DEL}.wwf").exists() and not Path(f"{DEL}.player").exists())

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

    print(f"\n  RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
