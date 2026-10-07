"""Gemini backend adapter contract: event normalization, tool calls, thought
signatures, history sync, malformed retry, and the tool-schema conversion.

No network — the SDK client is replaced with a fake. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_gemini.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from google.genai import types as gt  # noqa: E402

from web.gemini_stream import (  # noqa: E402
    GeminiChat, _is_tool_response, convert_tools_to_gemini,
)

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


# ── fake SDK surface ───────────────────────────────────────────────────────

class _Usage:
    def __init__(self, n):
        self.prompt_token_count = n


class _Candidate:
    def __init__(self, parts, finish=None):
        self.content = gt.Content(role="model", parts=parts) if parts else None
        self.finish_reason = finish


class _Chunk:
    def __init__(self, parts, finish=None, tokens=None):
        self.candidates = [_Candidate(parts, finish)]
        self.usage_metadata = _Usage(tokens) if tokens else None


class _FakeModels:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    async def generate_content_stream(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": list(contents), "config": config})
        chunks = self.rounds.pop(0) if self.rounds else []

        async def gen():
            for chunk in chunks:
                yield chunk
        return gen()


class _FakeAio:
    def __init__(self, rounds):
        self.models = _FakeModels(rounds)


class _FakeClient:
    def __init__(self, rounds):
        self.aio = _FakeAio(rounds)


def text_part(text, thought=False):
    return gt.Part(text=text, thought=True) if thought else gt.Part(text=text)


def call_part(name, args, call_id=None):
    return gt.Part(function_call=gt.FunctionCall(name=name, args=args, id=call_id))


def some_tool_turn(contents):
    """The last user turn that carries function responses."""
    for content in reversed(contents):
        if content.role == "user" and _is_tool_response(content):
            return content
    return None


def make_chat(rounds):
    chat = GeminiChat("test-key")
    chat._client = _FakeClient(rounds)  # bypass the real genai.Client
    return chat


async def collect(chat, messages, tools=None, model="gemini-3.8-flash", **kwargs):
    events = []
    async for evt in chat.stream(messages, model, tools=tools, **kwargs):
        events.append(evt)
    return events


# ── tests ──────────────────────────────────────────────────────────────────

async def test_text_and_thought():
    chat = make_chat([[
        _Chunk([text_part("hmm", thought=True)]),
        _Chunk([text_part("Hello ")]),
        _Chunk([text_part("world")], tokens=42),
    ]])
    events = await collect(chat, [{"role": "system", "content": "S"},
                                  {"role": "user", "content": "U"}])
    narr = "".join(e["text"] for e in events if e["type"] == "narrative_delta")
    think = "".join(e["text"] for e in events if e["type"] == "thinking_delta")
    done = [e for e in events if e["type"] == "done"]
    rec("text chunks become narrative deltas", narr == "Hello world", repr(narr))
    rec("thought parts become thinking deltas", think == "hmm", repr(think))
    rec("done carries the prompt token count", done and done[-1]["prompt_eval_count"] == 42, str(done))
    rec("system -> system_instruction", chat._system_instruction == "S")
    rec("history got user + raw model content",
        len(chat._contents) == 2 and chat._contents[0].role == "user"
        and chat._contents[1].role == "model", str([c.role for c in chat._contents]))


async def test_tool_call_and_sync():
    tool_call = [call_part("roll_dice", {"sides": 20})]
    chat = make_chat([[_Chunk(tool_call, tokens=10)], [_Chunk([text_part("It hits.")])]])
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "attack"}]
    events = await collect(chat, messages, tools=TOOLS)
    calls = [e for e in events if e["type"] == "tool_calls"]
    rec("function_call becomes a tool_calls event",
        len(calls) == 1 and calls[0]["calls"][0]["function"]["name"] == "roll_dice"
        and calls[0]["calls"][0]["function"]["arguments"] == {"sides": 20}, str(calls))
    rec("tool schema is sent as a Gemini Tool",
        chat._client.aio.models.calls[0]["config"].tools is not None)

    # The engine now appends the assistant turn + the tool result; the adapter
    # must skip the assistant (already stored raw) and convert the tool result.
    messages = messages + [
        {"role": "assistant", "content": "", "tool_calls": calls[0]["calls"]},
        {"role": "tool", "content": "18", "name": "roll_dice"},
    ]
    events2 = await collect(chat, messages, tools=TOOLS)
    narr = "".join(e["text"] for e in events2 if e["type"] == "narrative_delta")
    roles = [c.role for c in chat._contents]
    rec("second round streams the narrative", narr == "It hits.", repr(narr))
    rec("assistant turn is not duplicated; tool result sent as a user turn",
        roles == ["user", "model", "user", "model"], str(roles))
    sent = chat._client.aio.models.calls[1]["contents"]
    rec("only user/model roles reach the API",
        all(c.role in ("user", "model") for c in sent), str([c.role for c in sent]))
    rec("tool result is a function response in that user turn",
        _is_tool_response(some_tool_turn(sent)), str(sent[-1]))
    rec("no duplicate function_call parts",
        sum(1 for c in chat._contents for p in (c.parts or []) if p.function_call) == 1)


async def test_consecutive_tool_results_merge():
    chat = make_chat([[_Chunk([call_part("a", {})]), _Chunk([call_part("b", {})])],
                      [_Chunk([text_part("done")])]])
    messages = [{"role": "user", "content": "go"}]
    events = await collect(chat, messages, tools=TOOLS)
    calls = [e for e in events if e["type"] == "tool_calls"][0]["calls"]
    messages = messages + [
        {"role": "assistant", "content": "", "tool_calls": calls},
        {"role": "tool", "content": "1", "name": "a"},
        {"role": "tool", "content": "2", "name": "b"},
    ]
    await collect(chat, messages, tools=TOOLS)
    sent = chat._client.aio.models.calls[1]["contents"]
    roles = [c.role for c in sent]
    rec("consecutive tool results merge into one user turn",
        roles == ["user", "model", "user"], str(roles))
    responses = [p for p in sent[-1].parts if p.function_response]
    rec("both function responses survive the merge",
        len(responses) == 2, str([p.function_response.name for p in responses]))


async def test_system_messages_join():
    chat = make_chat([[_Chunk([text_part("ok")])]])
    await collect(chat, [{"role": "system", "content": "PROTOCOL"},
                         {"role": "system", "content": "TIMELINE"},
                         {"role": "system", "content": "LOCATIONS"},
                         {"role": "user", "content": "go"}])
    expected = "PROTOCOL\n\nTIMELINE\n\nLOCATIONS"
    rec("every system message is kept (joined, in order), not overwritten",
        chat._system_instruction == expected, repr(chat._system_instruction))
    rec("joined system instruction reaches the API",
        chat._client.aio.models.calls[0]["config"].system_instruction == expected,
        repr(chat._client.aio.models.calls[0]["config"].system_instruction))
    rec("system messages never leak into contents",
        all(c.role in ("user", "model") for c in chat._contents),
        str([c.role for c in chat._contents]))


async def test_persist_false_over_tool_history():
    chat = make_chat([[_Chunk([text_part("once")])]])
    messages = [{"role": "user", "content": "a"}]
    await collect(chat, messages)
    chat._client = _FakeClient([[_Chunk([text_part("summary")])]])
    messages = messages + [
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "roll_dice", "arguments": {}}}]},
        {"role": "tool", "content": "18", "name": "roll_dice"},
        {"role": "user", "content": "summarise"},
    ]
    events = await collect(chat, messages, persist=False)
    sent = chat._client.aio.models.calls[0]["contents"]
    rec("one-off summary over a tool history sends only user/model roles",
        all(c.role in ("user", "model") for c in sent), str([c.role for c in sent]))
    rec("one-off summary over a tool history still streams",
        "".join(e["text"] for e in events if e["type"] == "narrative_delta") == "summary",
        repr(events))


async def test_malformed_retry():
    malformed = _Chunk([], finish="MALFORMED_FUNCTION_CALL")
    chat = make_chat([[malformed], [_Chunk([text_part("Recovered.")])]])
    events = await collect(chat, [{"role": "user", "content": "go"}], tools=TOOLS)
    narr = "".join(e["text"] for e in events if e["type"] == "narrative_delta")
    rec("malformed call is corrected and retried", narr == "Recovered.", repr(narr))
    rec("corrective message was injected",
        any(c.role == "user" for c in chat._contents))


async def test_persist_false():
    chat = make_chat([[_Chunk([text_part("once")])]])
    await collect(chat, [{"role": "user", "content": "a"}])
    before = list(chat._contents)
    consumed = chat._consumed
    chat._client = _FakeClient([[_Chunk([text_part("summary")])]])
    events = await collect(chat, [{"role": "user", "content": "b"}], persist=False)
    narr = "".join(e["text"] for e in events if e["type"] == "narrative_delta")
    rec("one-off call streams its text", narr == "summary", repr(narr))
    rec("one-off call leaves the session history untouched",
        chat._contents == before and chat._consumed == consumed)


async def test_config_sampling_and_thinking():
    chat = make_chat([[_Chunk([text_part("ok")])]])
    await collect(chat, [{"role": "user", "content": "u"}])
    cfg = chat._client.aio.models.calls[0]["config"]
    rec("no temperature reaches the Gemini API",
        getattr(cfg, "temperature", None) is None, repr(getattr(cfg, "temperature", None)))
    rec("no top_p/top_k reach the Gemini API",
        getattr(cfg, "top_p", None) is None and getattr(cfg, "top_k", None) is None)
    rec("no thinking_level without one", getattr(cfg, "thinking_config", None) is None,
        str(getattr(cfg, "thinking_config", None)))

    chat2 = make_chat([[_Chunk([text_part("ok")])]])
    await collect(chat2, [{"role": "user", "content": "u"}], think=True, thinking_level="medium")
    tc = chat2._client.aio.models.calls[0]["config"].thinking_config
    level = getattr(tc.thinking_level, "value", tc.thinking_level) if tc else None
    rec("thinking_level is forwarded", str(level).lower() == "medium", repr(level))
    rec("include_thoughts is forwarded with the level", bool(tc) and tc.include_thoughts is True,
        str(tc))


async def test_function_response_id():
    chat = make_chat([[_Chunk([call_part("roll_dice", {"sides": 20}, "call_1")]),
                       _Chunk([text_part("It hits.")])]])
    messages = [{"role": "user", "content": "attack"}]
    events = await collect(chat, messages, tools=TOOLS)
    calls = [e for e in events if e["type"] == "tool_calls"][0]["calls"]
    rec("the function_call id is surfaced", calls[0]["function"].get("id") == "call_1",
        str(calls[0]))
    messages = messages + [
        {"role": "assistant", "content": "", "tool_calls": calls},
        {"role": "tool", "content": "18", "name": "roll_dice", "id": "call_1"},
    ]
    await collect(chat, messages, tools=TOOLS)
    sent = chat._client.aio.models.calls[1]["contents"]
    responses = [p.function_response for c in sent for p in (c.parts or [])
                 if p.function_response]
    rec("the function response echoes the call id", bool(responses) and responses[0].id == "call_1",
        str([r.id for r in responses]))


async def test_error_paths():
    chat = GeminiChat("")
    chat._client = _FakeClient([])
    events = await collect(chat, [{"role": "user", "content": "x"}], model="gemini-3.8-flash",
                           max_retries=0)
    rec("missing key surfaces as an error event, not a crash",
        any(e["type"] == "error" for e in events) and events[-1]["type"] == "done",
        str([e["type"] for e in events]))

    # available() tracks the environment key.
    saved = os.environ.pop("GEMINI_API_KEY", None)
    rec("available() is False without GEMINI_API_KEY", GeminiChat.available() is False)
    os.environ["GEMINI_API_KEY"] = "test-key"
    rec("available() is True with GEMINI_API_KEY", GeminiChat.available() is True)
    if saved is None:
        os.environ.pop("GEMINI_API_KEY", None)
    else:
        os.environ["GEMINI_API_KEY"] = saved


def test_tool_conversion():
    tool = convert_tools_to_gemini(TOOLS)
    decls = tool.function_declarations
    rec("one declaration per tool", len(decls) == 1 and decls[0].name == "roll_dice")
    schema_json = decls[0].parameters.model_dump_json()
    rec("additionalProperties stripped", "additionalProperties" not in schema_json)
    rec("required/properties survive", "sides" in schema_json)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "roll_dice",
            "description": "Roll dice.",
            "parameters": {
                "type": "object",
                "properties": {"sides": {"type": "integer", "title": "Sides"}},
                "required": ["sides"],
                "additionalProperties": False,
            },
        },
    },
]


async def main() -> int:
    await test_text_and_thought()
    await test_tool_call_and_sync()
    await test_consecutive_tool_results_merge()
    await test_system_messages_join()
    await test_persist_false_over_tool_history()
    await test_malformed_retry()
    await test_persist_false()
    await test_config_sampling_and_thinking()
    await test_function_response_id()
    await test_error_paths()
    test_tool_conversion()
    print(f"\n  RESULT: {'PASS' if all(RESULTS) else 'FAIL'}  ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
