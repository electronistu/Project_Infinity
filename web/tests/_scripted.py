"""A scripted GM model for the server/transport suites -- never a real provider.

`SessionBase._stream(messages, tools)` (`web/session_base.py`) is the single dispatch to a
provider. The server suite needs the ENGINE around a model, not a model: the protocol, the
tool calls over the real `dice_server.py`, the event stream and the WS pump. So the stream is
replaced with a queue of scripted responses, one per model call.

A response is the list of normalized stream events the engine consumes:

    response("the door opens")                      -> prose
    response("", [("roll_dice", {"notation": "1d20"})])   -> a mechanical batch, no prose
    response("{{pause}}")                           -> the pause round
    response(prose, [("request_scene_image", {...})])     -> prose carrying the image call

`turn()` composes a whole turn (batch -> pause -> prose). The script is consumed in order;
once exhausted every further call gets `DEFAULT_PROSE`, so the engine's recovery rounds always
terminate. While the script is installed, `stream_chat` (the Ollama adapter) is replaced with
a raiser: any real provider call fails the suite loudly, which is how "the gate needs no
model" is proved rather than asserted.

    from _scripted import scripted, response, turn

    with scripted([response("the opening prose"), *turn("you look around",
                                                       batch=[("roll_dice", {"notation": "1d20"})])]):
        ...drive the app...
"""

from __future__ import annotations

import contextlib

from web.session_base import SessionBase

DEFAULT_PROSE = "The Game Master considers for a moment, and the world holds its breath."


def response(prose: str = "", calls=None) -> list[dict]:
    """One scripted model response: prose, tool calls, or both in the same response."""
    events: list[dict] = []
    if calls:
        events.append({"type": "tool_calls",
                       "calls": [{"function": {"name": name, "arguments": args or {}}}
                                 for name, args in calls]})
    if prose:
        events.append({"type": "narrative_delta", "text": prose})
    return events or [{"type": "narrative_delta", "text": DEFAULT_PROSE}]


def turn(prose: str, batch=None, prose_calls=None, pause: bool = True) -> list[list[dict]]:
    """A whole turn, as the model calls the engine: the batch, the token, then the prose."""
    out: list[list[dict]] = []
    if batch:
        out.append(response("", batch))
        if pause:
            out.append(response("{{pause}}"))
    out.append(response(prose, prose_calls))
    return out


class ScriptedModel:
    """The scripted stand-in: a queue of responses, then the default prose."""

    def __init__(self, responses: list[list[dict]]):
        self.responses = [list(r) for r in responses]
        self.calls = 0
        self.seen: list[int] = []  # the message counts of the prompts it was handed

    async def stream(self, messages, tools):
        self.calls += 1
        self.seen.append(len(messages or ()))
        events = self.responses.pop(0) if self.responses else response(DEFAULT_PROSE)
        for evt in events:
            yield evt
        yield {"type": "done", "prompt_eval_count": 1234}


def _raise_provider(*_a, **_k):
    raise AssertionError("a scripted suite must never call a real provider")


@contextlib.contextmanager
def scripted(responses: list[list[dict]]):
    """Install the script (and the provider raiser) for the block; restore on exit."""
    import web.session_base as base

    model = ScriptedModel(responses)
    original_stream = SessionBase._stream
    original_chat = base.stream_chat

    def _patched(self, messages, tools):
        return model.stream(messages, tools)

    SessionBase._stream = _patched  # type: ignore[method-assign]
    base.stream_chat = _raise_provider  # type: ignore[assignment]
    try:
        yield model
    finally:
        SessionBase._stream = original_stream  # type: ignore[method-assign]
        base.stream_chat = original_chat  # type: ignore[assignment]
