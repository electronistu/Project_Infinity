"""Streaming wrapper around the Ollama chat API.

Normalizes `ollama.AsyncClient.chat(stream=True)` chunks into simple events that
the headless game engine can consume. Kept separate so the engine stays readable.

Internal event types yielded:
    {"type": "thinking_delta", "text": str}
    {"type": "narrative_delta", "text": str}
    {"type": "tool_calls", "calls": [{"function": {"name": str, "arguments": dict}}]}
    {"type": "done", "prompt_eval_count": int}
    {"type": "error", "message": str}
"""

import asyncio
import json


def _val(obj, attr, default=None):
    if hasattr(obj, attr):
        return getattr(obj, attr)
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return default


def normalize_tool_calls(tool_calls) -> list[dict]:
    """Normalize Ollama tool-call objects into plain dicts with dict arguments."""
    out = []
    for tc in tool_calls or []:
        fn = getattr(tc, "function", None)
        if fn is None and isinstance(tc, dict):
            fn = tc.get("function", {})
        name = _val(fn, "name", "") or ""
        args = _val(fn, "arguments", {}) or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        out.append({"function": {"name": name, "arguments": args}})
    return out


async def stream_chat(client, model, messages, tools=None, options=None,
                      think=None, max_retries=3):
    """Async-generator that streams one assistant turn from Ollama.

    Retries only if the stream fails *before* any chunk has been produced
    (so we never silently duplicate partial output).
    """
    attempt = 0
    while True:
        attempt += 1
        started = False
        kwargs = dict(model=model, messages=messages, stream=True)
        if options:
            kwargs["options"] = options
        if tools:
            kwargs["tools"] = tools
        if think is not None:
            kwargs["think"] = think

        try:
            stream = await client.chat(**kwargs)
            async for chunk in stream:
                started = True
                msg = _val(chunk, "message")
                if msg is None:
                    continue
                thinking = _val(msg, "thinking") or ""
                content = _val(msg, "content") or ""
                tool_calls = _val(msg, "tool_calls") or []

                if thinking:
                    yield {"type": "thinking_delta", "text": thinking}
                if content:
                    yield {"type": "narrative_delta", "text": content}
                if tool_calls:
                    yield {"type": "tool_calls", "calls": normalize_tool_calls(tool_calls)}
                if _val(chunk, "done", False):
                    yield {"type": "done", "prompt_eval_count": _val(chunk, "prompt_eval_count", 0) or 0}
            return
        except Exception as e:  # noqa: BLE001 — surface every backend failure as an event
            status = getattr(e, "status_code", None)
            if not started and status in (429, 500, 502, 503) and attempt <= max_retries:
                await asyncio.sleep(2 * attempt)
                continue
            yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
            return
