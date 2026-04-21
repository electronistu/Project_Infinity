import os
import sys
import json
import argparse
import asyncio
from collections import deque
from rich.panel import Panel
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from openai import AsyncOpenAI, APIStatusError
from game_engine import run_game, console

AVAILABLE_MODELS = [
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
]
MODEL_CONTEXT_LENGTHS = {
    "gpt-5.4": 1048576,
    "gpt-5.4-mini": 1048576,
    "gpt-5.4-nano": 1048576,
}
DEFAULT_TEMP = 0.0


def parse_args():
    parser = argparse.ArgumentParser(description="Project Infinity: A Dynamic, Text-Based RPG World Engine (OpenAI)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed MCP tool calls and responses")
    parser.add_argument("--debug", "-d", action="store_true", help="Show raw LLM responses and tool calls")
    parser.add_argument("--temperature", "-t", type=float, default=DEFAULT_TEMP, help=f"Sampling temperature (default: {DEFAULT_TEMP})")
    parser.add_argument("--thinking-level", choices=["LOW", "MEDIUM", "HIGH"], default=None, help="Enable reasoning effort with the specified level")
    return parser.parse_args()


async def select_model(input_session):
    console.print(Panel("[bold magenta] Infinity Project: LLM Selection (OpenAI) [/bold magenta]", expand=False))
    for i, model in enumerate(AVAILABLE_MODELS):
        console.print(f"[cyan]{i+1}[/cyan] {model}")

    choice = await input_session.prompt_async(HTML('<ansicyan><b>Select a model (number)</b></ansicyan> '))
    try:
        idx = int(choice) - 1
        selected_model = AVAILABLE_MODELS[idx]
        console.print(Panel(f"[bold green]Model selected:[/bold green] {selected_model}", border_style="green"))
        return selected_model
    except (ValueError, IndexError):
        console.print("[red]Invalid selection. Defaulting to first model.[/red]")
        return AVAILABLE_MODELS[0]


def create_gpt_chat_fn(api_key, debug=False, thinking_level=None, temperature=DEFAULT_TEMP):
    client = AsyncOpenAI(api_key=api_key)
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
                        fn_name = fn.get("name", "")
                        fn_args = fn.get("arguments", {})
                        if isinstance(fn_args, dict):
                            fn_args = json.dumps(fn_args)
                        oai_tool_calls.append({
                            "id": tc_id,
                            "type": "function",
                            "function": {
                                "name": fn_name,
                                "arguments": fn_args,
                            }
                        })
                        pending_tool_call_ids.append(tc_id)
                    openai_messages.append({
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": oai_tool_calls,
                    })
                else:
                    openai_messages.append({
                        "role": "assistant",
                        "content": content or "",
                    })

            elif role == "tool":
                tool_name = msg.get("name", "")
                tool_content = msg.get("content", "")
                tc_id = pending_tool_call_ids.popleft() if pending_tool_call_ids else _next_tool_call_id()
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_content,
                })

        last_processed = len(messages)

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

        kwargs = {
            "model": model,
            "messages": openai_messages,
            "tools": openai_tools if openai_tools else None,
            "temperature": temperature if thinking_level is None else None,
        }

        if thinking_level is not None:
            kwargs["reasoning_effort"] = thinking_level.lower()

        if system_instruction:
            has_system = any(m.get("role") == "system" for m in openai_messages)
            if not has_system:
                openai_messages.insert(0, {"role": "system", "content": system_instruction})
            kwargs["messages"] = openai_messages

        max_retries = 3
        MAX_EMPTY_RETRIES = 3
        empty_retries = 0
        for attempt in range(max_retries + MAX_EMPTY_RETRIES):
            try:
                response = await client.chat.completions.create(**kwargs)

                prompt_eval_count = 0
                if response.usage:
                    prompt_eval_count = response.usage.prompt_tokens or 0

                choice = response.choices[0] if response.choices else None
                if not choice:
                    return {
                        'prompt_eval_count': prompt_eval_count,
                        'message': {
                            'content': "[No response generated.]",
                            'tool_calls': None,
                        }
                    }

                msg = choice.message
                content = msg.content or ""

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
                            'function': {
                                'name': tc.function.name,
                                'arguments': args,
                            }
                        })

                if not content.strip() and tool_calls is None:
                    empty_retries += 1
                    if empty_retries <= MAX_EMPTY_RETRIES:
                        if debug:
                            console.print(f"[bold yellow]DEBUG: Empty response from OpenAI. Re-injecting last user action... ({empty_retries}/{MAX_EMPTY_RETRIES})[/bold yellow]")
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
                        console.print("[bold red]DEBUG: Empty response persists after retries.[/bold red]")

                return {
                    'prompt_eval_count': prompt_eval_count,
                    'message': {
                        'content': content,
                        'tool_calls': tool_calls,
                    }
                }

            except APIStatusError as e:
                if e.status_code in (429, 500, 502, 503) and attempt < max_retries - 1:
                    if debug:
                        console.print(f"[bold yellow]DEBUG: OpenAI API error ({e.status_code}). Retrying... ({attempt+1}/{max_retries})[/bold yellow]")
                    await asyncio.sleep(2)
                    continue
                raise e

    return chat_fn


async def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        console.print("[bold red]Error:[/bold red] OPENAI_API_KEY environment variable not set.")
        console.print("[yellow]Set it with: export OPENAI_API_KEY=your-api-key[/yellow]")
        sys.exit(1)

    args = parse_args()
    debug = args.debug
    verbose = args.verbose or args.debug

    input_session = PromptSession()

    model = await select_model(input_session)

    context_window = MODEL_CONTEXT_LENGTHS.get(model, 1048576)
    if verbose:
        console.print(f"[dim]Context window: {context_window:,} tokens[/dim]")

    chat_fn = create_gpt_chat_fn(api_key, debug=debug, thinking_level=args.thinking_level, temperature=args.temperature)

    await run_game(chat_fn, model, context_window, verbose=verbose, debug=debug)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Exiting...[/yellow]")
        sys.exit(0)
    except SystemExit as e:
        sys.exit(e.code)