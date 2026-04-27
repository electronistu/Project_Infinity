import sys
import argparse
import asyncio
import ollama
from rich.panel import Panel
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from game_engine import run_game, console

AVAILABLE_MODELS = [
    "kimi-k2.6:cloud",
]
MODEL_CONTEXT_LENGTHS = {
    "kimi-k2.6:cloud": 250000,
}
DEFAULT_TEMP = 0.0


def parse_args():
    parser = argparse.ArgumentParser(description="Project Infinity: A Dynamic, Text-Based RPG World Engine (Ollama)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed MCP tool calls and responses")
    parser.add_argument("--debug", "-d", action="store_true", help="Show raw LLM responses and tool calls")
    parser.add_argument("--temperature", "-t", type=float, default=DEFAULT_TEMP, help=f"Sampling temperature (default: {DEFAULT_TEMP})")
    parser.add_argument("--think", action="store_true", help="Enable thinking/reasoning for the model (boolean toggle)")
    parser.add_argument("--thinking-level", choices=["LOW", "MEDIUM", "HIGH"], default=None, help="Enable thinking with a specific reasoning effort level (overrides --think)")
    return parser.parse_args()


async def select_model(input_session):
    console.print(Panel("[bold magenta] Infinity Project: LLM Selection [/bold magenta]", expand=False))
    for i, model in enumerate(AVAILABLE_MODELS):
        console.print(f"[cyan]{i+1}[/cyan] {model}")

    choice = await input_session.prompt_async(HTML('<ansicyan><b>Select an LLM (number)</b></ansicyan> '))
    try:
        idx = int(choice) - 1
        selected_model = AVAILABLE_MODELS[idx]

        console.print(f"\n[yellow]Validating model availability...[/yellow]")
        try:
            models_response = ollama.list()
            available_model_names = [m.model for m in models_response.models]

            if selected_model not in available_model_names:
                console.print(f"[bold red]Error:[/bold red] Model '{selected_model}' is not available in Ollama.")
                console.print("[yellow]Please ensure Ollama is running and the model is downloaded.[/yellow]")
                sys.exit(1)
        except Exception as e:
            console.print(f"[yellow]Could not validate model: {e}[/yellow]")
            console.print("[yellow]Proceeding anyway...[/yellow]")

        console.print(Panel(f"[bold green]Model validated:[/bold green] {selected_model}", border_style="green"))
        return selected_model
    except (ValueError, IndexError):
        console.print("[red]Invalid selection. Defaulting to first model.[/red]")
        return AVAILABLE_MODELS[0]


def create_ollama_chat_fn(debug=False, think=False, thinking_level=None, temperature=DEFAULT_TEMP):
    client = ollama.AsyncClient()

    think_value = None
    if thinking_level:
        think_value = thinking_level.lower()
    elif think:
        think_value = True

    async def chat_fn(messages, tools, model, context_window):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = await client.chat(
                    model=model,
                    messages=messages,
                    tools=tools,
                    think=think_value,
                    options={"temperature": temperature, "num_ctx": context_window}
                )
                prompt_eval_count = response.get('prompt_eval_count', 0) or 0
                response_msg = response.get('message', {})

                content = ""
                if hasattr(response_msg, 'content'):
                    content = response_msg.content
                elif isinstance(response_msg, dict):
                    content = response_msg.get('content', "")

                thinking_text = None
                if hasattr(response_msg, 'thinking'):
                    thinking_text = response_msg.thinking
                elif isinstance(response_msg, dict):
                    thinking_text = response_msg.get('thinking')

                tool_calls = None
                if hasattr(response_msg, 'tool_calls'):
                    tool_calls = response_msg.tool_calls
                elif isinstance(response_msg, dict):
                    tool_calls = response_msg.get('tool_calls')

                normalized_tool_calls = None
                if tool_calls:
                    normalized_tool_calls = []
                    for tc in tool_calls:
                        if hasattr(tc, 'function'):
                            fn = tc.function
                            fn_name = fn.name if hasattr(fn, 'name') else fn.get('name')
                            fn_args = fn.arguments if hasattr(fn, 'arguments') else fn.get('arguments', {})
                            if isinstance(fn_args, str):
                                import json as _json
                                try:
                                    fn_args = _json.loads(fn_args)
                                except (_json.JSONDecodeError, TypeError):
                                    fn_args = {}
                            normalized_tool_calls.append({
                                'function': {'name': fn_name, 'arguments': fn_args}
                            })
                        elif isinstance(tc, dict):
                            fn = tc.get('function', {})
                            fn_name = fn.get('name', '')
                            fn_args = fn.get('arguments', {})
                            if isinstance(fn_args, str):
                                import json as _json
                                try:
                                    fn_args = _json.loads(fn_args)
                                except (_json.JSONDecodeError, TypeError):
                                    fn_args = {}
                            normalized_tool_calls.append({
                                'function': {'name': fn_name, 'arguments': fn_args}
                            })

                return {
                    'prompt_eval_count': prompt_eval_count,
                    'message': {
                        'content': content,
                        'tool_calls': normalized_tool_calls,
                    },
                    'thinking': thinking_text,
                    'thinking_only': bool(thinking_text and not content and not normalized_tool_calls),
                }
            except ollama._types.ResponseError as e:
                if e.status_code in [500, 502, 503] and attempt < max_retries - 1:
                    if debug:
                        console.print(f"[bold yellow]DEBUG: Ollama overloaded ({e.status_code}). Retrying... ({attempt+1}/{max_retries})[/bold yellow]")
                    await asyncio.sleep(2)
                    continue
                raise e

    return chat_fn


async def main():
    args = parse_args()
    debug = args.debug
    verbose = args.verbose or args.debug

    input_session = PromptSession()

    model = await select_model(input_session)

    context_window = MODEL_CONTEXT_LENGTHS.get(model)
    if context_window is None:
        try:
            model_info = ollama.show(model)
            context_window = next(
                (v for k, v in model_info.get('model_info', {}).items() if k.endswith('.context_length')),
                4096
            )
        except Exception:
            context_window = 4096
    if verbose:
        console.print(f"[dim]Context window: {context_window:,} tokens[/dim]")

    chat_fn = create_ollama_chat_fn(
        debug=debug,
        think=args.think,
        thinking_level=args.thinking_level,
        temperature=args.temperature,
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
