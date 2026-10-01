"""Web character creation via a runtime bridge over the World Forge.

Drives the *real* `forge.character_creator.create_character(config)` from the
browser by installing a `forge.ui` handler that maps each prompt to a step sent
to the client; the worker thread blocks until the client answers.

Flow
----
1. `CreationManager.start()` spawns a worker thread running the Forge.
2. Handlers call `bridge.take_steps()` to read steps up to the next prompt.
3. `bridge.submit_answer(value)` feeds the answer back.
4. On completion the worker also generates the world and writes
   `output/{slug}.wwf` + `output/{slug}.player`.
"""

import contextlib
import os
import random
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from .naming import slugify, unique_paths  # noqa: E402

PROMPT_TIMEOUT_SECONDS = 900.0

# Copied verbatim from main.py so web-created worlds match Forge worlds.
WORLD_HISTORY = [
    "The War of the Ashen Crown, a bitter conflict ignited by Zarthus's expansionism, ended a decade ago in a fragile truce. The cities of Eldoria still bear the scars, and its people have long memories.",
    "During the war, the Blacksail Archipelago allied with Zarthus, preying on Eldorian shipping lanes. Though the war is over, their piracy continues, a constant thorn in the side of all civilized kingdoms.",
    "Silverwood's staunch neutrality during the war earned it no friends. Eldoria views them with suspicion for not aiding their cause, while Zarthus holds them in contempt for refusing to bow to their power.",
    "An uneasy peace now holds between Eldoria and Zarthus. It is not a peace of friendship, but a bitter rivalry of two great powers rebuilding their strength, each waiting for the other to show a sign of weakness.",
]

_MISSING = object()


class CreationCancelled(Exception):
    """Raised inside the worker when the client cancels."""


class CreationBridge:
    """Coordinates one creation between a worker thread and async handlers."""

    def __init__(self, config, output_dir):
        self.id = uuid.uuid4().hex
        self.config = config
        self.output_dir = Path(output_dir)
        self.thread: threading.Thread | None = None

        self._cond = threading.Condition()
        self._steps: list[dict] = []
        self._answer = _MISSING
        self._waiting = False
        self._terminal: dict | None = None

    # ── worker side ───────────────────────────────────────────────────────

    def emit(self, step: dict) -> None:
        """Non-blocking step (e.g. a notice)."""
        with self._cond:
            self._steps.append(step)
            self._cond.notify_all()

    def request(self, step: dict):
        """Emit a blocking prompt and wait for the client's answer."""
        with self._cond:
            self._steps.append(step)
            self._waiting = True
            self._cond.notify_all()
            while self._answer is _MISSING and self._terminal is None:
                self._cond.wait()
            if self._terminal is not None:
                raise CreationCancelled()
            answer = self._answer
            self._answer = _MISSING
            self._waiting = False
            if isinstance(answer, dict) and answer.get("cancelled"):
                raise CreationCancelled()
            return answer.get("value") if isinstance(answer, dict) else answer

    def finish_ok(self, result: dict) -> None:
        with self._cond:
            self._terminal = {"type": "done", **result}
            self._cond.notify_all()

    def finish_cancelled(self) -> None:
        with self._cond:
            self._terminal = {"type": "cancelled"}
            self._cond.notify_all()

    def finish_error(self, message: str, tb: str | None = None) -> None:
        with self._cond:
            self._terminal = {"type": "error", "message": message}
            if tb:
                self._terminal["traceback"] = tb
            self._cond.notify_all()

    # ── handler side ──────────────────────────────────────────────────────

    @property
    def terminal(self) -> dict | None:
        with self._cond:
            return self._terminal

    def submit_answer(self, value) -> bool:
        with self._cond:
            if self._terminal is not None:
                return False
            self._answer = {"value": value}
            self._cond.notify_all()
            return True

    def cancel(self) -> None:
        with self._cond:
            if self._terminal is None:
                self._terminal = {"type": "cancelled"}
            self._cond.notify_all()

    def take_steps(self, timeout: float = PROMPT_TIMEOUT_SECONDS):
        """Block until the worker needs an answer (or finishes); return (steps, terminal)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not ((self._waiting and self._answer is _MISSING) or self._terminal is not None):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminal = {"type": "error", "message": "creation timed out"}
                    self._cond.notify_all()
                    break
                self._cond.wait(remaining)
            steps = self._steps
            self._steps = []
            return steps, self._terminal


# ── Forge UI bridge ───────────────────────────────────────────────────────

def _select_single(bridge, prompt, options, display_fn=None):
    if not options:
        return None
    options = list(options)
    step = {
        "type": "prompt",
        "kind": "single",
        "prompt": str(prompt),
        "options": [
            {"id": str(i), "label": (display_fn(opt) if display_fn else str(opt))}
            for i, opt in enumerate(options)
        ],
    }
    while True:
        value = bridge.request(step)
        try:
            idx = int(value)
            if 0 <= idx < len(options):
                return options[idx]
        except (TypeError, ValueError):
            pass
        step = {**step, "error": "Invalid selection — choose again."}


def _select_multiple(bridge, prompt, options, min_choices=0, max_choices=None,
                     default_checked=None, display_fn=None):
    if not options:
        return []
    options = list(options)
    default_ids = []
    if default_checked:
        for d in default_checked:
            try:
                default_ids.append(str(options.index(d)))
            except ValueError:
                pass
    step = {
        "type": "prompt",
        "kind": "multi",
        "prompt": str(prompt),
        "options": [
            {"id": str(i), "label": (display_fn(opt) if display_fn else str(opt))}
            for i, opt in enumerate(options)
        ],
        "min_choices": min_choices,
        "max_choices": max_choices,
        "default_checked": default_ids,
    }
    while True:
        value = bridge.request(step)
        if not isinstance(value, list):
            value = []
        idxs = []
        for v in value:
            try:
                i = int(v)
                if 0 <= i < len(options) and i not in idxs:
                    idxs.append(i)
            except (TypeError, ValueError):
                pass
        if min_choices and len(idxs) < min_choices:
            step = {**step, "error": f"Select at least {min_choices}."}
            continue
        if max_choices is not None and len(idxs) > max_choices:
            step = {**step, "error": f"Select at most {max_choices}."}
            continue
        return [options[i] for i in idxs]


def _input_dialog_val(bridge, prompt, default="", max_length=50):
    step = {"type": "prompt", "kind": "text", "prompt": str(prompt),
            "default": default, "max_length": max_length}
    while True:
        value = bridge.request(step)
        value = "" if value is None else str(value)
        if not value.strip():
            step = {**step, "error": "Input cannot be empty."}
            continue
        if len(value) > max_length:
            step = {**step, "error": f"Must be at most {max_length} characters."}
            continue
        return value.strip()


def _input_number(bridge, prompt, min_val=0, max_val=99, default=""):
    step = {"type": "prompt", "kind": "number", "prompt": str(prompt),
            "min": min_val, "max": max_val, "default": default}
    while True:
        value = bridge.request(step)
        try:
            number = int(value)
        except (TypeError, ValueError):
            step = {**step, "error": "Enter a valid integer."}
            continue
        if number < min_val or number > max_val:
            step = {**step, "error": f"Value must be {min_val}-{max_val}."}
            continue
        return number


def _validate_point_buy(value, abilities, costs, budget, min_val, max_val):
    """Return a normalised allocation dict, or None if it is not a valid spend."""
    if not isinstance(value, dict):
        return None
    out = {}
    for key in abilities:
        try:
            score = int(value.get(key))
        except (TypeError, ValueError):
            return None
        if score < min_val or score > max_val:
            return None
        out[key] = score
    if sum(costs.get(score, 0) for score in out.values()) != budget:
        return None
    return out


def _point_buy(bridge, prompt, costs, budget=27, default=None, min_val=8, max_val=15, bonuses=None):
    default = dict(default or {})
    abilities = list(default.keys()) or [
        "strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma",
    ]
    bonuses = dict(bonuses or {})
    step = {
        "type": "prompt",
        "kind": "pointbuy",
        "prompt": str(prompt),
        "abilities": [
            {"key": a, "label": a[:3].upper(), "bonus": int(bonuses.get(a, 0))}
            for a in abilities
        ],
        "costs": {str(k): v for k, v in costs.items()},
        "budget": budget,
        "min": min_val,
        "max": max_val,
        "default": {a: int(default.get(a, min_val)) for a in abilities},
    }
    while True:
        normalized = _validate_point_buy(bridge.request(step), abilities, costs, budget, min_val, max_val)
        if normalized is not None:
            return normalized
        step = {**step, "error": f"Spend all {budget} points (each score {min_val}-{max_val})."}


def _show_message(bridge, text):
    bridge.emit({"type": "notice", "text": str(text)})


class _BridgeUI:
    """Forge UI handler that turns each prompt into a browser step."""

    def __init__(self, bridge: CreationBridge):
        self.bridge = bridge

    def select_single(self, prompt, options, title="World Forge", display_fn=None):
        return _select_single(self.bridge, prompt, options, display_fn)

    def select_multiple(self, prompt, options, title="World Forge", min_choices=0,
                        max_choices=None, default_checked=None, display_fn=None):
        return _select_multiple(self.bridge, prompt, options, min_choices,
                                max_choices, default_checked, display_fn)

    def input_dialog_val(self, prompt, title="World Forge", default="", max_length=50):
        return _input_dialog_val(self.bridge, prompt, default, max_length)

    def input_number(self, prompt, title="World Forge", min_val=0, max_val=99, default=""):
        return _input_number(self.bridge, prompt, min_val, max_val, default)

    def point_buy(self, prompt, costs, budget=27, default=None, min_val=8, max_val=15, bonuses=None):
        return _point_buy(self.bridge, prompt, costs, budget, default, min_val, max_val, bonuses)

    def show_message(self, text, title="World Forge"):
        return _show_message(self.bridge, text)


@contextlib.contextmanager
def _installed_ui(bridge: CreationBridge):
    from forge import ui
    previous = ui.set_handler(_BridgeUI(bridge))
    try:
        yield
    finally:
        ui.set_handler(previous)


# ── worker ────────────────────────────────────────────────────────────────

def _generate_world(config, player_character, output_dir):
    from forge.population_generator import populate_world
    from forge.guild_generator import create_guilds
    from forge.formatter import format_world_to_wwf
    from forge.models import WorldState

    kingdoms = populate_world(config)
    create_guilds(kingdoms, config)

    all_npcs = []
    for kingdom in kingdoms:
        all_npcs.append(kingdom.ruler)
        for guild in kingdom.guilds:
            all_npcs.append(guild.leader)
            all_npcs.append(guild.right_hand)
    if all_npcs:
        random.choice(all_npcs).is_walker = True

    world_state = WorldState(
        player_character=player_character,
        kingdoms=kingdoms,
        world_history=list(WORLD_HISTORY),
    )

    stem, wwf_path, _player_path = unique_paths(output_dir, slugify(player_character.name))
    format_world_to_wwf(world_state, str(wwf_path))
    return {
        "name": player_character.name,
        "wwf": wwf_path.name,
        "player": f"{stem}.player",
        "slug": stem,
        "character_class": player_character.character_class,
        "race": player_character.race,
        "level": player_character.level,
    }


def _make_streams_safe() -> None:
    """Windows consoles default to cp1252; the Forge prints Unicode banners.
    Prefer replacement over raising UnicodeEncodeError on write."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 — stream may not support reconfigure
            pass


def _run_creation(bridge: CreationBridge) -> None:
    try:
        import forge.character_creator as character_creator
        _make_streams_safe()
        with _installed_ui(bridge):
            player_character = character_creator.create_character(bridge.config)
        result = _generate_world(bridge.config, player_character, bridge.output_dir)
        bridge.finish_ok(result)
    except CreationCancelled:
        bridge.finish_cancelled()
    except BaseException as exc:  # noqa: BLE001 — report everything to the client
        bridge.finish_error(f"{type(exc).__name__}: {exc}", traceback.format_exc())


class CreationManager:
    def __init__(self, base_dir, output_dir):
        self.base_dir = Path(base_dir)
        self.output_dir = Path(output_dir)
        self._active: dict[str, CreationBridge] = {}
        self._current_id: str | None = None
        self._lock = threading.Lock()

    def start(self) -> CreationBridge:
        with self._lock:
            if self._current_id:
                current = self._active.get(self._current_id)
                if current is not None and current.terminal is None:
                    raise RuntimeError("a character creation is already in progress")
            from forge.config_loader import load_config
            config = load_config()
            bridge = CreationBridge(config, self.output_dir)
            self._active[bridge.id] = bridge
            self._current_id = bridge.id
            thread = threading.Thread(target=_run_creation, args=(bridge,), daemon=True)
            bridge.thread = thread
            thread.start()
            return bridge

    def get(self, creation_id: str) -> CreationBridge | None:
        return self._active.get(creation_id)

    def forget(self, creation_id: str) -> None:
        with self._lock:
            self._active.pop(creation_id, None)
            if self._current_id == creation_id:
                self._current_id = None

    def cancel_all(self) -> None:
        for bridge in list(self._active.values()):
            bridge.cancel()
