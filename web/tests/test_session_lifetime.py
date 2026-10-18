"""Session lifetime: who gets reaped, and who does not.

The policy under test (no provider, no engine):
  - a session with a live websocket is NEVER reaped, however long the player is silent;
  - the disconnected grace starts when the LAST client detaches, not at the last message;
  - a busy turn is not interrupted;
  - TTL=0 disables reaping;
  - a dead run loop is removed at once;
  - one live session per save: a connected twin is refused, a disconnected one replaced;
  - a stale close cannot clear a newer session's place registry.

Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_session_lifetime.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web import session_manager as sm  # noqa: E402
from web.scenes import discard_scene_manifest  # noqa: E402

RESULTS: list[bool] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


class FakeSession:
    """Only what SessionManager reads; no engine process, no model."""

    def __init__(self):
        self.busy = False
        self.alive = True
        self.closed = False
        self.era = ""
        self.model = "test"
        self.provider = "ollama"
        self.context_window = 1024
        self.current_context_tokens = 0
        self.turn_counter = 0
        self.temperature = 1.0
        self.think = None
        self.thinking_level = None
        self.dev = False

    async def close(self):
        self.closed = True
        self.alive = False


def add_session(mgr: sm.SessionManager, sid: str, now: float = 0.0,
                world: str = "hero.player") -> FakeSession:
    session = FakeSession()
    mgr._sessions[sid] = session
    mgr._meta[sid] = {"created": now, "last": now, "disconnected_at": now, "world": world}
    return session


async def _lifetime() -> None:
    # A connected session is never reaped, even a very long time after the last message.
    mgr = sm.SessionManager(REPO, ttl=10, sweep_interval=3600)
    session = add_session(mgr, "a", now=0)
    mgr.attach("a")
    removed = await mgr._sweep_once(now=10_000)
    rec("a connected session survives any idle time", not removed and mgr.get("a") is session)
    rec("... and counts as connected in the state view",
        mgr.state("a")["connected"] is True and mgr.state("a")["alive"] is True)

    # The clock starts when the last client detaches, not at the last message.
    mgr.attach("a")
    mgr.detach("a")
    rec("one of two clients detaching keeps the session connected",
        mgr._conns.get("a") == 1)
    mgr.detach("a")
    meta = mgr._meta["a"]
    rec("the last detach records the disconnect moment", "disconnected_at" in meta)
    meta["disconnected_at"] = 100.0
    rec("inside the grace the session is kept",
        await mgr._sweep_once(now=105) == [] and mgr.get("a") is not None)
    rec("past the grace the disconnected session is removed",
        await mgr._sweep_once(now=111) == ["a"] and mgr.get("a") is None)
    rec("removal closes the session", session.closed is True)

    # A turn in flight is never interrupted.
    mgr = sm.SessionManager(REPO, ttl=10)
    busy = add_session(mgr, "b", now=0)
    mgr.attach("b")
    mgr.detach("b")
    mgr._meta["b"]["disconnected_at"] = 0.0
    busy.busy = True
    rec("a busy disconnected session is not reaped",
        await mgr._sweep_once(now=10_000) == [] and mgr.get("b") is not None)
    busy.busy = False
    rec("... and is reaped once the turn ends",
        await mgr._sweep_once(now=10_000) == ["b"])

    # A hung model call has no read timeout; a very long busy turn is treated as dead.
    mgr = sm.SessionManager(REPO, ttl=10, max_busy=100)
    hung = add_session(mgr, "hung", now=0)
    mgr.attach("hung")
    mgr.detach("hung")
    mgr._meta["hung"]["disconnected_at"] = 0.0
    hung.busy = True
    hung.busy_since = 0.0
    rec("a turn hung past the cap is reaped",
        await mgr._sweep_once(now=101) == ["hung"] and mgr.get("hung") is None)

    # TTL=0 means "keep until the server stops or the player ends it".
    mgr = sm.SessionManager(REPO, ttl=0)
    add_session(mgr, "c", now=0)
    mgr.attach("c")
    mgr.detach("c")
    mgr._meta["c"]["disconnected_at"] = 0.0
    rec("TTL=0 disables reaping",
        await mgr._sweep_once(now=10_000_000) == [] and mgr.get("c") is not None)

    # A run loop that ended (fatal, close) cannot serve a client; remove it even if attached.
    mgr = sm.SessionManager(REPO, ttl=3600)
    dead = add_session(mgr, "d", now=0)
    mgr.attach("d")
    dead.alive = False
    rec("a dead session is removed even while a socket is attached",
        await mgr._sweep_once(now=1) == ["d"] and mgr.get("d") is None)


async def _one_session_per_save() -> None:
    class FakeGame:
        def __init__(self, **kwargs):
            self.busy = False
            self.alive = True
            self.closed = False

        async def start(self, save):
            self.started = save

        async def close(self):
            self.closed = True
            self.alive = False

    real = sm.GameSession
    sm.GameSession = FakeGame
    try:
        mgr = sm.SessionManager(REPO, ttl=10)
        sid1, first = await mgr.create("hero.player")
        mgr.attach(sid1)
        try:
            await mgr.create("hero.player")
            refused = False
        except sm.SessionBusyError:
            refused = True
        rec("a save open in a live window refuses a second session", refused)
        rec("... and the live session is untouched", mgr.get(sid1) is first and not first.closed)

        mgr.detach(sid1)
        sid2, second = await mgr.create("hero.player")
        rec("a disconnected session on the same save is replaced",
            sid2 != sid1 and first.closed and mgr.get(sid1) is None)
        rec("... leaving exactly one live session", mgr.count() == 1 and mgr.get(sid2) is second)
    finally:
        sm.GameSession = real


def _stale_close() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        first = discard_scene_manifest(tmp, "hero")
        second = discard_scene_manifest(tmp, "hero")
        stale = discard_scene_manifest(tmp, "hero", expected_generation=first)
        rec("a stale close does not bump the registry generation", stale == second,
            f"{stale} == {second}")
        live = discard_scene_manifest(tmp, "hero", expected_generation=second)
        rec("the live close still discards and bumps", live == second + 1,
            f"{live} == {second + 1}")


async def main() -> int:
    await _lifetime()
    await _one_session_per_save()
    _stale_close()
    print("\n  RESULT:", "PASS" if all(RESULTS) else "FAIL")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    try:
        code = asyncio.run(asyncio.wait_for(main(), timeout=60))
    except asyncio.TimeoutError:
        print("\nFAIL: timed out after 60s")
        code = 1
    sys.exit(code)
