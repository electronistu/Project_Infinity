# forge/tui.py
# prompt_toolkit-based selection dialogs for the World Forge

from prompt_toolkit.shortcuts import radiolist_dialog, checkboxlist_dialog, input_dialog
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import Validator, ValidationError
from typing import List, Optional, Any, Callable

FORGE_STYLE = Style.from_dict({
    'dialog':             'bg:#1a1a2e',
    'dialog frame.label': 'bg:#16213e #e94560 bold',
    'dialog.body':        'bg:#1a1a2e #c0caf5',
    'dialog shadow':      'bg:#0f0f23',
    'label':              'bg:#1a1a2e #c0caf5',
    'button':             'bg:#0f3460 #c0caf5',
    'button.focused':     'bg:#e94560 #ffffff bold',
    'check':              'bg:#1a1a2e #c0caf5',
    'check selected':     'bg:#0f3460 #e94560',
    'radio':              'bg:#1a1a2e #c0caf5',
    'radio selected':     'bg:#0f3460 #e94560',
    'scrollbar':          'bg:#16213e',
    'scrollbar.background': 'bg:#1a1a2e',
    'scrollbar.button':   'bg:#0f3460 #c0caf5',
    'text-area':          'bg:#0f3460 #c0caf5',
    'frame.label':        'bg:#16213e #e94560 bold',
})


def select_single(prompt: str, options: List[Any], title: str = "World Forge",
                   display_fn: Optional[Callable[[Any], str]] = None) -> Optional[Any]:
    if not options:
        return None

    values = []
    for opt in options:
        label = str(opt) if display_fn is None else display_fn(opt)
        values.append((opt, label))

    return radiolist_dialog(
        title=title,
        text=prompt,
        values=values,
        style=FORGE_STYLE,
    ).run()


def select_multiple(prompt: str, options: List[Any], title: str = "World Forge",
                    min_choices: int = 0, max_choices: Optional[int] = None,
                    default_checked: Optional[List[Any]] = None,
                    display_fn: Optional[Callable[[Any], str]] = None) -> List[Any]:
    if not options:
        return []

    values = []
    default_set = set(default_checked) if default_checked else set()
    default_values_list = list(default_set)
    for opt in options:
        label = str(opt) if display_fn is None else display_fn(opt)
        values.append((opt, label))

    current_defaults = list(default_values_list)

    if min_choices == 0 and max_choices is None:
        result = checkboxlist_dialog(
            title=title, text=prompt, values=values, default_values=current_defaults,
            style=FORGE_STYLE,
        ).run()
        return list(result) if result else []

    min_msg = f"must select at least {min_choices}" if min_choices > 0 else ""
    max_msg = f"at most {max_choices}" if max_choices is not None else ""
    count_hint = " and ".join(filter(None, [min_msg, max_msg]))

    while True:
        full_prompt = f"{prompt}\n({count_hint})"
        result = checkboxlist_dialog(
            title=title, text=full_prompt, values=values, default_values=current_defaults,
            style=FORGE_STYLE,
        ).run()

        if result is None:
            return list(default_set) if default_set else []

        if min_choices > 0 and len(result) < min_choices:
            from rich.console import Console
            Console().print(f"[red]You must select at least {min_choices}. Please try again.[/]")
            current_defaults = list(result)
            continue
        if max_choices is not None and len(result) > max_choices:
            from rich.console import Console
            Console().print(f"[red]You may select at most {max_choices}. Please try again.[/]")
            current_defaults = list(result)
            continue
        return list(result)


def input_dialog_val(prompt: str, title: str = "World Forge", default: str = "",
                     max_length: int = 50) -> Optional[str]:
    while True:
        result = input_dialog(
            title=title, text=prompt, default=default, style=FORGE_STYLE,
        ).run()
        if result is None:
            return None
        if result.strip():
            if len(result) > max_length:
                from rich.console import Console
                Console().print(f"[red]Input must be at most {max_length} characters (got {len(result)}).[/]")
                default = result[:max_length]
                continue
            return result.strip()
        from rich.console import Console
        Console().print("[red]Input cannot be empty.[/]")
        default = result


def input_number(prompt: str, title: str = "World Forge",
                 min_val: int = 0, max_val: int = 99, default: str = "") -> Optional[int]:

    class RangeValidator(Validator):
        def validate(self, document):
            text = document.text.strip()
            if not text:
                raise ValidationError(message="Enter a number")
            try:
                val = int(text)
            except ValueError:
                raise ValidationError(message="Enter a valid integer")
            if val < min_val or val > max_val:
                raise ValidationError(message=f"Value must be {min_val}–{max_val}")

    while True:
        result = input_dialog(
            title=title, text=prompt, default=default, style=FORGE_STYLE,
            validator=RangeValidator(),
        ).run()
        if result is None:
            return None
        try:
            return int(result.strip())
        except ValueError:
            from rich.console import Console
            Console().print("[red]Invalid number. Please try again.[/]")
