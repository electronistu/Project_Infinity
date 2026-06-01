#!/usr/bin/env python3
"""Project Infinity x DeepSeek — direct cloud API adapter, no proxy needed.

Usage:
    $env:DEEPSEEK_API_KEY="sk-xxx"            # PowerShell, set once
    python play_with_deepseek.py               # default: deepseek-v4-flash
    python play_with_deepseek.py --think       # enable thinking mode
    python play_with_deepseek.py --pro         # use v4-pro (more expensive)
"""

import os
import sys
import json
import argparse
import asyncio
from collections import deque
from rich.panel import Panel
from openai import AsyncOpenAI, APIStatusError
from game_engine import run_game, console

# ── DeepSeek configuration ─────────────────────────────────────
# API docs: https://api-docs.deepseek.com/
# Note: base_url without /v1 suffix (official docs: https://api.deepseek.com)
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# deepseek-v4-flash: recommended default (cheapest)
# deepseek-v4-pro:  higher performance, use --pro
DEFAULT_MODEL = "deepseek-v4-flash"
MODEL_CONTEXT_LENGTHS = {
    "deepseek-v4-flash": 1000000,
    "deepseek-v4-pro": 1000000,
}
DEFAULT_TEMP = 0.0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Project Infinity: D&D RPG powered by DeepSeek API"
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed MCP tool calls and responses")
    parser.add_argument("--debug", "-d", action="store_true",
                        help="Show raw LLM responses and tool calls")
    parser.add_argument("--temperature", "-t", type=float, default=DEFAULT_TEMP,
                        help=f"Sampling temperature (default: {DEFAULT_TEMP})")
    parser.add_argument("--think", action="store_true",
                        help="Enable thinking mode (thinking.type=enabled)")
    parser.add_argument("--pro", action="store_true",
                        help="Use deepseek-v4-pro instead of default deepseek-v4-flash")
    return parser.parse_args()


def create_deepseek_chat_fn(api_key, debug=False, think=False, temperature=DEFAULT_TEMP):
    """Create a DeepSeek chat function compatible with Project Infinity engine."""
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL,
    )
    openai_messages = []
    system_instruction = None
    last_processed = 0
    tool_call_id_counter = 0

    def _next_tool_call_id():
        nonlocal tool_call_id_counter
        tool_call_id_counter += 1
        return f"tc_{tool_call_id_counter}"

    async def chat_fn(messages, tools, model, context_window):
        nonlocal openai_messages, system_instruction, last_processed

        pending_tool_call_ids = deque()

        # ── Build OpenAI-format messages incrementally ─────────
        for msg in messages[last_processed:]:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_instruction = content
                continue

            if role == "user":
                openai_messages.append({"role": "user", "content": content})

            elif role == "assistant":
                tool_calls_raw = msg.get("tool_calls")
                if tool_calls_raw:
                    oai_tool_calls = []
                    for tc in tool_calls_raw:
                        fn = tc.get("function", {})
                        tc_id = _next_tool_call_id()
                        fn_args = fn.get("arguments", {})
                        if isinstance(fn_args, dict):
                            fn_args = json.dumps(fn_args)
                        oai_tool_calls.append({
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": fn.get("name", ""), "arguments": fn_args},
                        })
                        pending_tool_call_ids.append(tc_id)
                    oai_msg = {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": oai_tool_calls,
                    }
                    reasoning = msg.get("thinking")
                    if reasoning:
                        oai_msg["reasoning_content"] = reasoning
                    openai_messages.append(oai_msg)
                else:
                    oai_msg = {"role": "assistant", "content": content or ""}
                    # DeepSeek requires reasoning_content to be echoed back in thinking mode
                    reasoning = msg.get("thinking")
                    if reasoning:
                        oai_msg["reasoning_content"] = reasoning
                    openai_messages.append(oai_msg)

            elif role == "tool":
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": (
                        pending_tool_call_ids.popleft() if pending_tool_call_ids
                        else _next_tool_call_id()
                    ),
                    "content": msg.get("content", ""),
                }
                openai_messages.append(tool_msg)

        last_processed = len(messages)

        # ── Build tools ───────────────────────────────────────
        openai_tools = []
        if tools:
            for tool in tools:
                fn = tool.get("function", {})
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    }
                })

        # ── Chat Completions API call ─────────────────────────
        kwargs = {
            "model": model,
            "messages": openai_messages,
            "temperature": temperature,
            "max_tokens": min(context_window, 16384),
        }

        if openai_tools:
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = "auto"

        # DeepSeek thinking mode: pass via extra_body per official docs
        if think:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        # Inject system prompt
        if system_instruction:
            has_system = any(m.get("role") == "system" for m in openai_messages)
            if not has_system:
                openai_messages.insert(0, {"role": "system", "content": system_instruction})
            kwargs["messages"] = openai_messages

        max_retries = 3
        max_empty_retries = 3
        empty_retries = 0

        for attempt in range(max_retries + max_empty_retries):
            try:
                response = await client.chat.completions.create(**kwargs)

                prompt_tokens = 0
                if response.usage:
                    prompt_tokens = response.usage.prompt_tokens or 0

                choice = response.choices[0] if response.choices else None
                if not choice:
                    return {
                        "prompt_eval_count": prompt_tokens,
                        "message": {"content": "[No response generated.]", "tool_calls": None},
                    }

                msg = choice.message
                content = msg.content or ""

                # DeepSeek returns reasoning_content when thinking is enabled
                reasoning = getattr(msg, "reasoning_content", None)

                tool_calls = None
                if msg.tool_calls:
                    tool_calls = []
                    for tc in msg.tool_calls:
                        args = {}
                        if tc.function.arguments:
                            try:
                                args = json.loads(tc.function.arguments)
                            except (json.JSONDecodeError, TypeError):
                                args = {}
                        tool_calls.append({
                            "function": {
                                "name": tc.function.name,
                                "arguments": args,
                            }
                        })

                # Auto-retry on empty response
                if not content.strip() and tool_calls is None:
                    empty_retries += 1
                    if empty_retries <= max_empty_retries:
                        if debug:
                            console.print(
                                f"[bold yellow]DEBUG: Empty response. "
                                f"Retrying... ({empty_retries}/{max_empty_retries})[/bold yellow]"
                            )
                        last_user_msg = None
                        for m in reversed(messages):
                            if m.get("role") == "user":
                                last_user_msg = m.get("content", "")
                                break
                        if last_user_msg:
                            openai_messages.append({"role": "user", "content": last_user_msg})
                            kwargs["messages"] = openai_messages
                        await asyncio.sleep(1)
                        continue
                    if debug:
                        console.print("[bold red]DEBUG: Empty response persists.[/bold red]")

                result = {
                    "prompt_eval_count": prompt_tokens,
                    "message": {
                        "content": content,
                        "tool_calls": tool_calls,
                    },
                }
                if reasoning:
                    result["thinking"] = reasoning
                return result

            except APIStatusError as e:
                if e.status_code in (429, 500, 502, 503) and attempt < max_retries - 1:
                    if debug:
                        console.print(
                            f"[bold yellow]DEBUG: API error {e.status_code}. "
                            f"Retrying... ({attempt+1}/{max_retries})[/bold yellow]"
                        )
                    await asyncio.sleep(2)
                    continue
                raise e
            except Exception as e:
                if debug:
                    import traceback
                    traceback.print_exc()
                    console.print(
                        f"[bold red]DEBUG: {type(e).__name__}: {e}[/bold red]"
                    )
                raise e

        return {
            "prompt_eval_count": 0,
            "message": {
                "content": "[Error: max retries exceeded]",
                "tool_calls": None,
            },
        }

    return chat_fn


async def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        console.print("[bold red]Error:[/bold red] DEEPSEEK_API_KEY not set.")
        console.print("[yellow]PowerShell: $env:DEEPSEEK_API_KEY=\"sk-your-key\"[/yellow]")
        console.print("[yellow]Bash:      export DEEPSEEK_API_KEY=\"sk-your-key\"[/yellow]")
        sys.exit(1)

    args = parse_args()
    debug = args.debug
    verbose = args.verbose or args.debug

    model = "deepseek-v4-pro" if args.pro else DEFAULT_MODEL
    context_window = MODEL_CONTEXT_LENGTHS.get(model, 1000000)

    console.print(Panel(
        f"[bold cyan]Project Infinity × DeepSeek[/bold cyan]\n"
        f"[dim]Model: {model}  |  Context: {context_window:,} tokens  |  "
        f"api.deepseek.com[/dim]",
        expand=False
    ))

    chat_fn = create_deepseek_chat_fn(
        api_key,
        debug=debug,
        think=args.think,
        temperature=args.temperature,
    )

    await run_game(chat_fn, model, context_window, verbose=verbose, debug=debug)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/dim]")
