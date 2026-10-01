"""Pluggable prompt interface for the World Forge.

The Forge never talks to a terminal. A caller installs a handler implementing
the five methods below; the web client installs one that drives the browser
wizard (see `web/creation.py`). This replaces the old prompt_toolkit dialogs.
"""

from typing import Any, Callable, List, Optional

_handler = None


class ForgeUI:
    """Interface the Forge calls. Implement all five methods."""

    def select_single(self, prompt, options, title="World Forge", display_fn=None):
        raise NotImplementedError

    def select_multiple(self, prompt, options, title="World Forge", min_choices=0,
                        max_choices=None, default_checked=None, display_fn=None):
        raise NotImplementedError

    def input_dialog_val(self, prompt, title="World Forge", default="", max_length=50):
        raise NotImplementedError

    def input_number(self, prompt, title="World Forge", min_val=0, max_val=99, default=""):
        raise NotImplementedError

    def point_buy(self, prompt, costs, budget=27, default=None, min_val=8, max_val=15,
                  bonuses=None):
        raise NotImplementedError

    def show_message(self, text, title="World Forge"):
        raise NotImplementedError


def set_handler(handler):
    """Install the active handler; return the previous one (for restore)."""
    global _handler
    previous = _handler
    _handler = handler
    return previous


def get_handler():
    if _handler is None:
        raise RuntimeError(
            "No Forge UI handler installed; call forge.ui.set_handler() first."
        )
    return _handler


def select_single(prompt: str, options: List[Any], title: str = "World Forge",
                  display_fn: Optional[Callable[[Any], str]] = None) -> Any:
    return get_handler().select_single(prompt, options, title=title, display_fn=display_fn)


def select_multiple(prompt: str, options: List[Any], title: str = "World Forge",
                    min_choices: int = 0, max_choices: Optional[int] = None,
                    default_checked: Optional[List[Any]] = None,
                    display_fn: Optional[Callable[[Any], str]] = None) -> List[Any]:
    return get_handler().select_multiple(
        prompt, options, title=title, min_choices=min_choices,
        max_choices=max_choices, default_checked=default_checked, display_fn=display_fn,
    )


def input_dialog_val(prompt: str, title: str = "World Forge", default: str = "",
                     max_length: int = 50) -> str:
    return get_handler().input_dialog_val(prompt, title=title, default=default,
                                          max_length=max_length)


def input_number(prompt: str, title: str = "World Forge", min_val: int = 0,
                 max_val: int = 99, default: str = "") -> int:
    return get_handler().input_number(prompt, title=title, min_val=min_val,
                                      max_val=max_val, default=default)


def point_buy(prompt: str, costs: dict, budget: int = 27, default=None,
              min_val: int = 8, max_val: int = 15, bonuses=None) -> dict:
    """Return an allocation of `budget` points across all abilities at once."""
    return get_handler().point_buy(prompt, costs=costs, budget=budget, default=default,
                                   min_val=min_val, max_val=max_val, bonuses=bonuses)


def show_message(text: str, title: str = "World Forge"):
    return get_handler().show_message(text, title=title)
