"""Streaming wrapper around the Gemini chat API (google-genai).

Normalizes `client.aio.models.generate_content_stream` into the same internal
events as `web/ollama_stream.stream_chat`, so the engine stays provider-agnostic:

    {"type": "thinking_delta", "text": str}
    {"type": "narrative_delta", "text": str}
    {"type": "tool_calls", "calls": [{"function": {"name": str, "arguments": dict}}]}
    {"type": "done", "prompt_eval_count": int}
    {"type": "error", "message": str}

The adapter is **stateful**: it keeps its own Gemini `contents` list and appends
the raw model parts after each call. Rebuilding the history statelessly would
drop Gemini 3 `thought_signature` metadata (attached to the parts) and break
multi-turn reasoning + tool use. Pass `persist=False` for one-off, tool-free
calls (timeline summaries) that must not touch the session history.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - surfaced as "unavailable"
    genai = None
    genai_types = None


# JSON-schema keywords Gemini's Developer API rejects (MCP/Pydantic emits some).
_UNSUPPORTED_SCHEMA_KEYS = (
    "additionalProperties", "$schema", "$id", "$defs", "definitions",
    "examples", "title", "discriminator",
)

_MALFORMED_MSG = (
    "Your previous function call was rejected as MALFORMED_FUNCTION_CALL: its "
    "arguments did not match the tool schema. Re-issue the call with EXACTLY the "
    "declared parameter names, types and required fields, and no extra fields."
)


def _clean_schema(value):
    """Drop schema keywords the Gemini Developer API rejects, in place."""
    if isinstance(value, dict):
        for key in list(value.keys()):
            if key in _UNSUPPORTED_SCHEMA_KEYS:
                value.pop(key, None)
            else:
                _clean_schema(value[key])
    elif isinstance(value, list):
        for item in value:
            _clean_schema(item)
    return value


def _is_tool_response(content) -> bool:
    """True when every part of a Content is a function response."""
    parts = list(getattr(content, "parts", None) or [])
    return bool(parts) and all(
        getattr(part, "function_response", None) is not None for part in parts)


def _append_content(contents: list, content) -> None:
    """Append a turn, merging it into a preceding tool-response turn.

    Gemini expects one `user` turn per batch of function responses; the engine
    appends one message per tool call, so consecutive results must be merged
    rather than sent as consecutive user turns.
    """
    if (contents and getattr(content, "role", "") == "user"
            and contents[-1].role == "user" and _is_tool_response(content)
            and _is_tool_response(contents[-1])):
        contents[-1].parts.extend(content.parts or [])
        return
    contents.append(content)


def _sanitize_contents(contents):
    """Belt-and-braces: the Gemini API only knows `user` and `model` roles.

    Anything else (e.g. an OpenAI-style `tool`) becomes a `user` turn; the Part
    objects are passed through untouched so thought signatures survive.
    """
    if genai_types is None:
        return contents
    clean = []
    for content in contents or []:
        if getattr(content, "role", "") in ("user", "model"):
            clean.append(content)
        else:
            clean.append(genai_types.Content(
                role="user", parts=list(getattr(content, "parts", None) or [])))
    return clean


def convert_tools_to_gemini(tools_schema):
    """OpenAI-style tool schemas -> one Gemini `Tool`, or None."""
    if genai_types is None:
        return None
    declarations = []
    for tool in tools_schema or []:
        fn = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        params = fn.get("parameters")
        if isinstance(params, dict):
            params = _clean_schema(copy.deepcopy(params))
        else:
            params = None
        kwargs = {"name": name, "description": str(fn.get("description") or "")}
        if params is not None:
            kwargs["parameters"] = params
        declarations.append(genai_types.FunctionDeclaration(**kwargs))
    return genai_types.Tool(function_declarations=declarations) if declarations else None


def _is_retryable(exc: Exception) -> bool:
    """Only transient backend failures are worth retrying."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in (
        "429", "500", "502", "503", "504", "rate", "quota", "overloaded",
        "unavailable", "deadline", "timeout", "temporar",
    ))


class GeminiChat:
    """Stateful streaming Gemini backend for one game session."""

    def __init__(self, api_key: str):
        self.api_key = api_key or ""
        self._client = None
        self._contents: list = []
        self._system_instruction: str | None = None
        self._system_parts: list[str] = []
        self._consumed = 0

    # ── availability ──────────────────────────────────────────────────────

    @staticmethod
    def available() -> bool:
        return bool(genai is not None and genai_types is not None) and bool(
            os.environ.get("GEMINI_API_KEY")
        )

    def _client_or_raise(self):
        if genai is None or genai_types is None:
            raise RuntimeError("google-genai is not installed on the server.")
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set on the server.")
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    # ── history conversion ─────────────────────────────────────────────────

    def _msg_to_content(self, msg: dict):
        """Return ('system', text) | Content | None for one engine message."""
        role = str(msg.get("role") or "")
        text = str(msg.get("content") or "")
        if role == "system":
            return ("system", text)
        if role == "user":
            return genai_types.Content(role="user", parts=[genai_types.Part(text=text)])
        if role == "assistant":
            parts = []
            if text:
                parts.append(genai_types.Part(text=text))
            for tc in msg.get("tool_calls") or []:
                fn = (tc or {}).get("function", {}) or {}
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                parts.append(genai_types.Part(function_call=genai_types.FunctionCall(
                    name=str(fn.get("name") or ""),
                    args=args if isinstance(args, dict) else {},
                )))
            return genai_types.Content(role="model", parts=parts) if parts else None
        if role == "tool":
            payload = str(msg.get("content") or "")
            try:
                response = json.loads(payload)
                if not isinstance(response, dict):
                    response = {"result": response}
            except (json.JSONDecodeError, TypeError):
                response = {"result": payload}
            # Gemini has no `tool` role: a function response is user input. The
            # `id` must echo the matching function_call (strict call/response
            # matching on Gemini 3.x); older models omit it.
            function_response = genai_types.FunctionResponse(
                name=str(msg.get("name") or ""), response=response)
            call_id = str(msg.get("id") or "")
            if call_id:
                function_response.id = call_id
            return genai_types.Content(role="user", parts=[
                genai_types.Part(function_response=function_response)])
        return None

    def _sync(self, messages: list[dict]) -> None:
        """Append the engine's new user/system/tool messages to `contents`.

        Assistant messages are skipped: the raw model parts were appended by
        `stream()` instead, preserving thought signatures.
        """
        for msg in messages[self._consumed:]:
            if str(msg.get("role") or "") == "assistant":
                continue
            item = self._msg_to_content(msg)
            if isinstance(item, tuple):
                # Every system message matters (protocol + timeline + locations):
                # joining keeps them all, where overwriting kept only the last.
                self._system_parts.append(item[1])
                self._system_instruction = "\n\n".join(self._system_parts)
            elif item is not None:
                _append_content(self._contents, item)
        self._consumed = len(messages)

    def _convert_all(self, messages: list[dict]):
        """Stateless conversion (used for one-off, tool-free calls)."""
        system_parts: list[str] = []
        contents = []
        for msg in messages or []:
            item = self._msg_to_content(msg)
            if isinstance(item, tuple):
                system_parts.append(item[1])
            elif item is not None:
                _append_content(contents, item)
        return ("\n\n".join(system_parts) or None), contents

    # ── request config ─────────────────────────────────────────────────────

    def _config(self, system, tools, think, thinking_level):
        # Gemini 3.x: `temperature`/`top_p`/`top_k` are deprecated (fixed to the
        # model's optimal defaults) and the numeric `thinking_budget` is retired in
        # favour of `thinking_level`. None = the model's own default.
        kwargs = {
            "automatic_function_calling": genai_types.AutomaticFunctionCallingConfig(disable=True),
        }
        tool = convert_tools_to_gemini(tools) if tools else None
        if tool is not None:
            kwargs["tools"] = [tool]
        if system:
            kwargs["system_instruction"] = system
        thinking = {}
        if thinking_level:
            thinking["thinking_level"] = thinking_level
        if think:
            thinking["include_thoughts"] = True
        if thinking:
            kwargs["thinking_config"] = genai_types.ThinkingConfig(**thinking)
        return genai_types.GenerateContentConfig(**kwargs)

    # ── one API round-trip ─────────────────────────────────────────────────

    async def _stream_once(self, model, contents, system, tools, think, thinking_level):
        """Yield normalized events; the final `__round` event carries the result.

        On the Gemini Developer API a streamed `function_call` arrives complete
        in a single chunk (`partial_args`/`will_continue` are Vertex-only), so a
        simple collect is safe.
        """
        info = {
            "parts": [], "tool_calls": [], "text": "", "thinking": "",
            "prompt": 0, "malformed": False, "started": False,
            "error": False, "error_message": "", "retryable": False,
        }
        seen: set = set()
        try:
            config = self._config(system, tools, think, thinking_level)
            client = self._client_or_raise()
            stream = await client.aio.models.generate_content_stream(
                model=model, contents=_sanitize_contents(contents), config=config,
            )
            async for chunk in stream:
                info["started"] = True
                usage = getattr(chunk, "usage_metadata", None)
                if usage is not None:
                    prompt_tokens = getattr(usage, "prompt_token_count", None)
                    if prompt_tokens:
                        info["prompt"] = prompt_tokens
                candidates = getattr(chunk, "candidates", None) or []
                if not candidates:
                    continue
                candidate = candidates[0]
                finish = getattr(candidate, "finish_reason", None)
                if finish is not None and "MALFORMED_FUNCTION_CALL" in str(finish).upper():
                    info["malformed"] = True
                content = getattr(candidate, "content", None)
                parts = getattr(content, "parts", None) if content is not None else None
                if not parts:
                    continue
                for part in parts:
                    info["parts"].append(part)
                    call = getattr(part, "function_call", None)
                    if call is not None and getattr(call, "name", None):
                        args = getattr(call, "args", None) or {}
                        if not isinstance(args, dict):
                            args = {}
                        call_id = getattr(call, "id", None)
                        signature = (call.name, call_id,
                                     json.dumps(args, sort_keys=True, default=str))
                        if signature not in seen:
                            seen.add(signature)
                            info["tool_calls"].append(
                                {"function": {"name": call.name,
                                              "arguments": args, "id": call_id}})
                    text = getattr(part, "text", None)
                    if text:
                        if getattr(part, "thought", False):
                            info["thinking"] += text
                            yield {"type": "thinking_delta", "text": text}
                        else:
                            info["text"] += text
                            yield {"type": "narrative_delta", "text": text}
        except Exception as exc:  # noqa: BLE001 - surface every backend failure
            info["error"] = True
            info["error_message"] = f"{type(exc).__name__}: {exc}"
            info["retryable"] = _is_retryable(exc)
            if info["started"]:
                # Partial output was already streamed; report and stop.
                yield {"type": "error", "message": info["error_message"]}
        if info["tool_calls"]:
            yield {"type": "tool_calls", "calls": info["tool_calls"]}
        yield {"type": "__round", "info": info}

    # ── public interface (mirrors ollama_stream.stream_chat) ──────────────

    async def stream(self, messages, model, tools=None, think=None, thinking_level=None,
                     persist: bool = True, max_retries: int = 3):
        if genai is None or genai_types is None:
            yield {"type": "error", "message": "google-genai is not installed."}
            return

        if not persist:
            system, contents = self._convert_all(messages)
            async for evt in self._stream_once(model, contents, system, None,
                                              think, thinking_level):
                if evt.get("type") != "__round":
                    yield evt
            return

        self._sync(messages)
        attempt = 0
        corrective = 0
        while True:
            attempt += 1
            info = None
            async for evt in self._stream_once(model, self._contents, self._system_instruction,
                                               tools, think, thinking_level):
                if evt.get("type") == "__round":
                    info = evt["info"]
                else:
                    yield evt
            if info is None:
                return

            # MALFORMED_FUNCTION_CALL: nudge the model, then retry (up to twice).
            if info.get("malformed") and corrective < 2:
                corrective += 1
                self._contents.append(genai_types.Content(
                    role="user", parts=[genai_types.Part(text=_MALFORMED_MSG)]))
                continue

            # Transient failure before any output: back off and retry.
            if (info.get("error") and not info.get("started")
                    and info.get("retryable") and attempt <= max_retries):
                await asyncio.sleep(2 * attempt)
                continue

            if info.get("error") and not info.get("started"):
                yield {"type": "error",
                       "message": info.get("error_message") or "Gemini request failed."}
                yield {"type": "done", "prompt_eval_count": 0}
                return

            if info.get("parts"):
                self._contents.append(genai_types.Content(role="model", parts=info["parts"]))
            yield {"type": "done", "prompt_eval_count": info.get("prompt", 0)}
            return
