"""In-memory registry of live game sessions (single-user local server)."""

import asyncio
import time
import uuid
from pathlib import Path

from .engine import GameSession
from .models import DEFAULT_MODEL, DEFAULT_TEMPERATURE, gemini_thinking_level, resolve_model

IDLE_TTL_SECONDS = 3600      # close sessions idle for an hour
SWEEP_INTERVAL_SECONDS = 60


class SessionManager:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self._sessions: dict[str, GameSession] = {}
        self._meta: dict[str, dict] = {}
        self._sweeper: asyncio.Task | None = None

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
                     scene_images: bool = False):
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
        )
        sid = uuid.uuid4().hex
        self._sessions[sid] = session
        self._meta[sid] = {
            "created": time.time(),
            "last": time.time(),
            "world": save,
            "model": model_id,
        }
        await session.start(save)
        return sid, session

    def get(self, sid: str) -> GameSession | None:
        return self._sessions.get(sid)

    def count(self) -> int:
        return len(self._sessions)

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
            "created": meta.get("created"),
            "last_activity": meta.get("last"),
        }

    async def remove(self, sid: str) -> None:
        session = self._sessions.pop(sid, None)
        self._meta.pop(sid, None)
        if session is not None:
            await session.close()

    # ── idle sweep ────────────────────────────────────────────────────────

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            now = time.time()
            for sid, meta in list(self._meta.items()):
                if now - meta.get("last", now) > IDLE_TTL_SECONDS:
                    await self.remove(sid)
