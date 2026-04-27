import os
import sys
import json
import argparse
import asyncio
from collections import deque
from rich.panel import Panel
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from anthropic import AsyncAnthropic, APIStatusError
from game_engine import run_game, console

AVAILABLE_MODELS = [
    "claude-opus-4-7",
    "claude-opus-4-6",
]
MODEL_CONTEXT_LENGTHS = {
    "claude-opus-4-7": 1048576,
    "claude-opus-4-6": 1048576,
}
NO_TEMP_MODELS = {"claude-opus-4-7"}
DEFAULT_TEMP = 0.0
DEFAULT_MAX_OUTPUT_TOKENS = 16384

THINKING_BUDGETS = {
    "LOW": 4096,
    "MEDIUM": 16000,
    "HIGH": 32000,
}


def parse_args():
    parser = argparse.ArgumentParser(description="Project Infinity: A Dynamic, Text-Based RPG World Engine (Claude)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed MCP tool calls and responses")
    parser.add_argument("--debug", "-d", action="store_true", help="Show raw LLM responses and tool calls")
    parser.add_argument("--temperature", "-t", type=float, default=DEFAULT_TEMP, help=f"Sampling temperature (default: {DEFAULT_TEMP})")
    parser.add_argument("--thinking-level", choices=["LOW", "MEDIUM", "HIGH"], default=None, help="Enable structured thinking with the specified level")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS, help=f"Max output tokens (default: {DEFAULT_MAX_OUTPUT_TOKENS}). Automatically increased if thinking is enabled.")
    return parser.parse_args()


async def select_model(input_session):
    console.print(Panel("[bold magenta] Infinity Project: LLM Selection (Claude) [/bold magenta]", expand=False))
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


def convert_tools(tools_schema):
    claude_tools = []
    for tool in tools_schema:
        fn = tool.get("function", {})
        claude_tools.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {}),
        })
    return claude_tools


def create_claude_chat_fn(api_key, debug=False, thinking_level=None, temperature=DEFAULT_TEMP, max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS):
    client = AsyncAnthropic(api_key=api_key)

    claude_messages = []
    system_instruction = None
    last_processed = 0
    tool_call_id_counter = 0
    pending_tool_call_ids = deque()
    pending_tool_results = []
    last_assistant_blocks = None

    def _next_tool_call_id():
        nonlocal tool_call_id_counter
        tool_call_id_counter += 1
        return f"tc_{tool_call_id_counter}"

    async def chat_fn(messages, tools, model, context_window):
        nonlocal claude_messages, system_instruction, last_processed, \
            last_assistant_blocks, pending_tool_call_ids, pending_tool_results

        new_messages = messages[last_processed:]
        # If the next expected message is not an assistant turn, any stale cached
        # assistant blocks from a previously failed call should be discarded.
        if new_messages and new_messages[0].get("role") != "assistant":
            last_assistant_blocks = None

        for msg in new_messages:
            role = msg.get("role", "")

            if role == "system":
                system_instruction = msg.get("content", "")
                continue

            # Flush accumulated tool results before any non-tool message
            if role != "tool" and pending_tool_results:
                claude_messages.append({
                    "role": "user",
                    "content": pending_tool_results,
                })
                pending_tool_results = []
                pending_tool_call_ids.clear()

            if role == "user":
                claude_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": msg.get("content", "")}],
                })

            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                content_text = msg.get("content", "")

                if last_assistant_blocks is not None:
                    # Use the exact Anthropic response blocks from our last turn
                    claude_messages.append({
                        "role": "assistant",
                        "content": last_assistant_blocks,
                    })
                    for block in last_assistant_blocks:
                        if block.get("type") == "tool_use":
                            pending_tool_call_ids.append(block["id"])
                    last_assistant_blocks = None
                elif tool_calls:
                    # Fallback: reconstruct tool_use blocks with generated IDs
                    blocks = []
                    if content_text:
                        blocks.append({"type": "text", "text": content_text})
                    for tc in tool_calls:
                        tc_id = _next_tool_call_id()
                        fn = tc.get("function", {})
                        fn_args = fn.get("arguments", {})
                        if isinstance(fn_args, dict):
                            fn_args = json.dumps(fn_args)
                        blocks.append({
                            "type": "tool_use",
                            "id": tc_id,
                            "name": fn.get("name", ""),
                            "input": json.loads(fn_args) if isinstance(fn_args, str) else fn_args,
                        })
                        pending_tool_call_ids.append(tc_id)
                    claude_messages.append({
                        "role": "assistant",
                        "content": blocks,
                    })
                else:
                    claude_messages.append({
                        "role": "assistant",
                        "content": [{"type": "text", "text": content_text or ""}],
                    })

            elif role == "tool":
                tool_name = msg.get("name", "")
                tool_content = msg.get("content", "")
                tc_id = pending_tool_call_ids.popleft() if pending_tool_call_ids else _next_tool_call_id()
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc_id,
                    "content": tool_content,
                })

        last_processed = len(messages)

        # Flush any remaining tool results
        if pending_tool_results:
            claude_messages.append({
                "role": "user",
                "content": pending_tool_results,
            })
            pending_tool_results = []
            pending_tool_call_ids.clear()

        claude_tools = convert_tools(tools) if tools else []

        thinking_budget = None
        if thinking_level is not None:
            thinking_budget = THINKING_BUDGETS[thinking_level]

        # Anthropic requires max_tokens to be larger than the thinking budget
        effective_max_tokens = max_output_tokens
        if thinking_budget is not None:
            effective_max_tokens = max(max_output_tokens, thinking_budget + 4096)

        max_retries = 3
        max_empty_retries = 3
        empty_retries = 0
        attempt = 0

        while attempt < max_retries + max_empty_retries:
            attempt += 1
            try:
                kwargs = {
                    "model": model,
                    "messages": claude_messages,
                    "max_tokens": effective_max_tokens,
                }
                if model not in NO_TEMP_MODELS:
                    kwargs["temperature"] = temperature
                if claude_tools:
                    kwargs["tools"] = claude_tools
                if system_instruction:
                    kwargs["system"] = system_instruction
                if thinking_budget is not None:
                    kwargs["thinking"] = {
                        "type": "enabled",
                        "budget_tokens": thinking_budget,
                    }

                response = await client.messages.create(**kwargs)

                content_parts = []
                tool_calls = []
                thinking_parts = []
                cached_blocks = []

                for block in response.content:
                    block_type = getattr(block, "type", None)
                    if block_type == "text":
                        content_parts.append(block.text)
                        cached_blocks.append({"type": "text", "text": block.text})
                    elif block_type == "tool_use":
                        tool_calls.append({
                            "function": {
                                "name": block.name,
                                "arguments": block.input,
                            }
                        })
                        cached_blocks.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
                    elif block_type == "thinking":
                        thinking_parts.append(block.thinking)

                if not cached_blocks:
                    cached_blocks = [{"type": "text", "text": ""}]

                last_assistant_blocks = cached_blocks

                content = "".join(content_parts).strip()

                if not content and not tool_calls:
                    empty_retries += 1
                    if empty_retries <= max_empty_retries:
                        if debug:
                            console.print(
                                f"[bold yellow]DEBUG: Empty response from Claude. "
                                f"Silent retry ({empty_retries}/{max_empty_retries})...[/bold yellow]"
                            )
                        await asyncio.sleep(1)
                        continue

                prompt_eval_count = 0
                if response.usage:
                    prompt_eval_count = getattr(response.usage, "input_tokens", 0) or 0

                return {
                    "prompt_eval_count": prompt_eval_count,
                    "message": {
                        "content": content,
                        "tool_calls": tool_calls if tool_calls else None,
                    },
                    "thinking": "\n".join(thinking_parts) if thinking_parts else None,
                    "thinking_only": bool(thinking_parts and not content and not tool_calls),
                    "malformed_function_call": False,
                }

            except APIStatusError as e:
                status_code = getattr(e, "status_code", None)
                if status_code in (429, 500, 502, 503) and attempt < max_retries:
                    if debug:
                        console.print(
                            f"[bold yellow]DEBUG: Claude API error ({status_code}). "
                            f"Retrying... ({attempt}/{max_retries})[/bold yellow]"
                        )
                    await asyncio.sleep(2)
                    continue
                raise e
            except Exception as e:
                if debug:
                    import traceback
                    traceback.print_exc()
                    console.print(f"[bold red]DEBUG: Non-API error in Claude call: {type(e).__name__}: {e}[/bold red]")
                raise e

    return chat_fn


async def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[bold red]Error:[/bold red] ANTHROPIC_API_KEY environment variable not set.")
        console.print("[yellow]Set it with: export ANTHROPIC_API_KEY=your-api-key[/yellow]")
        sys.exit(1)

    args = parse_args()
    debug = args.debug
    verbose = args.verbose or args.debug

    input_session = PromptSession()

    model = await select_model(input_session)

    context_window = MODEL_CONTEXT_LENGTHS.get(model, 1048576)
    if verbose:
        console.print(f"[dim]Context window: {context_window:,} tokens[/dim]")

    chat_fn = create_claude_chat_fn(
        api_key,
        debug=debug,
        thinking_level=args.thinking_level,
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
    )

    await run_game(chat_fn, model, context_window, verbose=verbose, debug=debug)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Exiting...[/yellow]")
        sys.exit(0)
    except SystemExit as e:
        sys.exit(e.code)
