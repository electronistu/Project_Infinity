"""In-memory registry of live game sessions (single-user local server)."""

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

from .engine import GameSession
from .models import DEFAULT_MODEL, DEFAULT_TEMPERATURE, gemini_thinking_level, resolve_model

log = logging.getLogger("uvicorn.error")


def _env_seconds(name: str, default: float) -> float:
    """A non-negative seconds knob; a bad value keeps the default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


# A session is reaped only after it has been DISCONNECTED for this long. A connected
# session is never reaped: a player thinking (or away from the keyboard) is still at the
# table. `0` disables reaping entirely; the process then lives until the server stops or
# the player ends it.
SESSION_TTL_SECONDS = _env_seconds("PI_SESSION_TTL_SECONDS", 3600.0)
SWEEP_INTERVAL_SECONDS = _env_seconds("PI_SESSION_SWEEP_SECONDS", 60.0)
# A disconnected session is normally kept while a turn runs. A model call has no read
# timeout, so a hung stream could pin it forever; past this cap the turn is treated as
# dead. `0` disables the cap (like TTL=0, never reap).
MAX_BUSY_SECONDS = _env_seconds("PI_SESSION_MAX_BUSY_SECONDS", 6 * 3600.0)


class SessionBusyError(RuntimeError):
    """The same save is already open in a live session."""


class SessionManager:
    def __init__(self, base_dir, ttl: float | None = None,
                 sweep_interval: float | None = None, max_busy: float | None = None):
        self.base_dir = Path(base_dir)
        self._sessions: dict[str, GameSession] = {}
        # `disconnected_at` is when the last client detached (creation time until the
        # first attach). `last` stays the last inbound activity, for the state endpoint.
        self._meta: dict[str, dict] = {}
        # Live websocket count per session; >0 means somebody is at the table.
        self._conns: dict[str, int] = {}
        self._sweeper: asyncio.Task | None = None
        self._ttl = SESSION_TTL_SECONDS if ttl is None else max(0.0, float(ttl))
        self._sweep_interval = (SWEEP_INTERVAL_SECONDS if sweep_interval is None
                                else max(0.1, float(sweep_interval)))
        self._max_busy = MAX_BUSY_SECONDS if max_busy is None else max(0.0, float(max_busy))

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def shutdown(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._sweeper = None
        for sid in list(self._sessions):
            await self.remove(sid)

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def create(self, save: str, model: str | None = None,
                     temperature: float | None = None, think=None,
                     scene_images: bool = False, dev: bool = False):
        await self._release_save(save)
        model_id = model or DEFAULT_MODEL
        spec = resolve_model(model_id)
        context = spec["context"] if spec else 1_048_576
        provider = (spec.get("provider") if spec else None) or "ollama"
        temp = DEFAULT_TEMPERATURE if temperature is None else float(temperature)

        session = GameSession(
            base_dir=self.base_dir,
            model=model_id,
            context_window=context,
            temperature=temp,
            think=think,
            thinking_level=gemini_thinking_level(spec),
            scene_images=scene_images,
            provider=provider,
            dev=dev,
        )
        sid = uuid.uuid4().hex
        now = time.time()
        self._sessions[sid] = session
        self._meta[sid] = {
            "created": now,
            "last": now,
            "disconnected_at": now,
            "starting": True,
            "world": save,
            "model": model_id,
            "dev": bool(dev),
        }
        try:
            await session.start(save)
        except Exception:
            await self.remove(sid)
            raise
        meta = self._meta.get(sid)
        if meta is not None:
            meta["starting"] = False
        return sid, session

    async def _release_save(self, save: str) -> None:
        """One live session per save: refuse a connected twin, close a disconnected one.

        Without this, a stale session can be resumed after the save has moved on and
        overwrite newer progress; the disconnected TTL is cleanup, not the safety rule."""
        for sid, meta in list(self._meta.items()):
            if meta.get("world") != save:
                continue
            if self._conns.get(sid) or meta.get("starting"):
                raise SessionBusyError(
                    f"{save} is already open in another window; close it before starting again")
            await self.remove(sid)

    def get(self, sid: str) -> GameSession | None:
        return self._sessions.get(sid)

    def count(self) -> int:
        return len(self._sessions)

    def attach(self, sid: str) -> bool:
        """Register a live websocket. False if the session no longer exists."""
        if sid not in self._meta:
            return False
        self._conns[sid] = self._conns.get(sid, 0) + 1
        self._meta[sid].pop("disconnected_at", None)
        self.touch(sid)
        return True

    def detach(self, sid: str) -> None:
        """Drop a live websocket; the disconnect clock starts when the last one leaves."""
        count = self._conns.get(sid, 0) - 1
        if count > 0:
            self._conns[sid] = count
            return
        self._conns.pop(sid, None)
        meta = self._meta.get(sid)
        if meta is None:
            return
        now = time.time()
        meta["last"] = now
        meta["disconnected_at"] = now

    def touch(self, sid: str) -> None:
        if sid in self._meta:
            self._meta[sid]["last"] = time.time()

    def state(self, sid: str) -> dict | None:
        session = self._sessions.get(sid)
        if session is None:
            return None
        meta = self._meta.get(sid, {})
        return {
            "session_id": sid,
            "world": meta.get("world"),
            "era": session.era,
            "model": session.model,
            "provider": session.provider,
            "context_window": session.context_window,
            "context_tokens": session.current_context_tokens,
            "turn_counter": session.turn_counter,
            "temperature": session.temperature,
            "think": session.think,
            "thinking_level": session.thinking_level,
            "dev": bool(getattr(session, "dev", False)),
            "created": meta.get("created"),
            "last_activity": meta.get("last"),
            "disconnected_at": meta.get("disconnected_at"),
            "alive": bool(session.alive),
            "connected": self._conns.get(sid, 0) > 0,
            "busy": bool(getattr(session, "busy", False)),
        }

    async def remove(self, sid: str) -> None:
        session = self._sessions.pop(sid, None)
        self._meta.pop(sid, None)
        self._conns.pop(sid, None)
        if session is not None:
            await session.close()

    # ── idle sweep ────────────────────────────────────────────────────────

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval)
            await self._sweep_once()

    async def _sweep_once(self, now: float | None = None) -> list[str]:
        """Reap sessions nobody is connected to; return the ids removed."""
        now = time.time() if now is None else now
        removed: list[str] = []
        for sid, meta in list(self._meta.items()):
            session = self._sessions.get(sid)
            if session is None:
                self._meta.pop(sid, None)
                self._conns.pop(sid, None)
                continue
            if meta.get("starting"):
                continue
            if not session.alive:
                await self.remove(sid)
                removed.append(sid)
                continue
            if self._conns.get(sid, 0):
                continue
            if self._ttl <= 0:
                continue
            if getattr(session, "busy", False):
                since_busy = getattr(session, "busy_since", None)
                hung = (self._max_busy > 0 and since_busy is not None
                        and now - since_busy > self._max_busy)
                if not hung:
                    continue
                log.info("session %s reaped: turn busy for %.0fs", sid, now - since_busy)
                await self.remove(sid)
                removed.append(sid)
                continue
            since = meta.get("disconnected_at")
            if since is None:
                since = meta.get("last", meta.get("created", now))
            if now - since > self._ttl:
                log.info("session %s reaped after %.0fs disconnected", sid, now - since)
                await self.remove(sid)
                removed.append(sid)
        return removed
