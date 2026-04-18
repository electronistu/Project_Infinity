import os
import sys
import json
import re
import argparse
import asyncio
from rich.panel import Panel
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from game_engine import run_game, console

AVAILABLE_MODELS = [
    "gemini-3.1-pro-preview",
]
MODEL_CONTEXT_LENGTHS = {
    "gemini-3.1-pro-preview": 1048576,
}
DEFAULT_TEMP = 1.0


def parse_args():
    parser = argparse.ArgumentParser(description="Project Infinity: A Dynamic, Text-Based RPG World Engine (Gemini)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed MCP tool calls and responses")
    parser.add_argument("--debug", "-d", action="store_true", help="Show raw LLM responses and tool calls")
    parser.add_argument("--temperature", "-t", type=float, default=DEFAULT_TEMP, help=f"Sampling temperature (default: {DEFAULT_TEMP})")
    parser.add_argument("--thinking-level", choices=["LOW", "MEDIUM", "HIGH"], default=None, help="Enable structured thinking with the specified level")
    return parser.parse_args()


async def select_model(input_session):
    console.print(Panel("[bold magenta] Infinity Project: LLM Selection (Gemini) [/bold magenta]", expand=False))
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


def convert_tools_to_gemini(tools_schema):
    declarations = []
    for tool in tools_schema:
        fn = tool.get("function", {})
        name = fn.get("name", "")
        description = fn.get("description", "")
        parameters = fn.get("parameters", {})
        declarations.append(types.FunctionDeclaration(
            name=name,
            description=description,
            parameters=parameters,
        ))
    return types.Tool(function_declarations=declarations)


def _strip_thinking_from_content(content):
    stripped = None
    if content and re.match(r'^[Tt]hinking\s*\n', content):
        first_double_newline = re.search(r'\n\n', content)
        if first_double_newline:
            stripped = content[:first_double_newline.start()].rstrip()
            content = content[first_double_newline.end():].lstrip('\n')
        else:
            single_newline_after_first = re.search(r'\n(?=.{0,10}\n)', content)
            if single_newline_after_first:
                stripped = content[:single_newline_after_first.start()].rstrip()
                content = content[single_newline_after_first.end():].lstrip('\n')
    return content, stripped


def convert_response(response):
    if not response.candidates:
        return {
            'prompt_eval_count': 0,
            'message': {
                'content': "[Content filtered by safety settings. Please rephrase.]",
                'tool_calls': None,
            }
        }

    candidate = response.candidates[0]

    finish_reason = getattr(candidate, 'finish_reason', None)
    if finish_reason and hasattr(finish_reason, 'name') and finish_reason.name == 'MALFORMED_FUNCTION_CALL':
        prompt_eval_count = 0
        if response.usage_metadata:
            prompt_eval_count = getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
        return {
            'prompt_eval_count': prompt_eval_count,
            'malformed_function_call': True,
            'thinking': None,
            'thinking_scrubbed': None,
            'thinking_only': False,
            'message': {
                'content': '',
                'tool_calls': None,
            }
        }

    if not candidate.content or not candidate.content.parts:
        return {
            'prompt_eval_count': 0,
            'message': {
                'content': "[No response generated.]",
                'tool_calls': None,
            }
        }

    text_parts = []
    thinking_parts = []
    tool_calls = []
    for part in candidate.content.parts:
        if hasattr(part, 'text') and part.text:
            if getattr(part, 'thought', False):
                thinking_parts.append(part.text)
            else:
                text_parts.append(part.text)
        if hasattr(part, 'function_call') and part.function_call:
            fc = part.function_call
            args = {}
            if fc.args:
                args = dict(fc.args)
            tool_calls.append({
                'function': {
                    'name': fc.name,
                    'arguments': args,
                }
            })

    content = "".join(text_parts)
    thinking_only = not text_parts and not tool_calls and bool(thinking_parts)

    thinking_scrubbed = None
    if not thinking_parts and content:
        content, thinking_scrubbed = _strip_thinking_from_content(content)

    prompt_eval_count = 0
    if response.usage_metadata:
        prompt_eval_count = getattr(response.usage_metadata, 'prompt_token_count', 0) or 0

    return {
        'prompt_eval_count': prompt_eval_count,
        'malformed_function_call': False,
        'thinking': "\n".join(thinking_parts) if thinking_parts else None,
        'thinking_scrubbed': thinking_scrubbed,
        'thinking_only': thinking_only,
        'message': {
            'content': content,
            'tool_calls': tool_calls if tool_calls else None,
        }
    }


def create_gemini_chat_fn(api_key, debug=False, thinking_level=None, temperature=DEFAULT_TEMP):
    client = genai.Client(api_key=api_key)
    gemini_contents = []
    system_instruction = None
    last_processed = 0

    async def chat_fn(messages, tools, model, context_window):
        nonlocal gemini_contents, system_instruction, last_processed

        for msg in messages[last_processed:]:
            role = msg.get("role", "")
            content_text = msg.get("content", "")

            if role == "system":
                system_instruction = content_text
                continue

            if role == "user":
                gemini_contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=content_text)]
                ))

            elif role == "assistant":
                pass

            elif role == "tool":
                tool_name = msg.get("name", "")
                tool_content = msg.get("content", "")
                try:
                    response_dict = json.loads(tool_content)
                except (json.JSONDecodeError, TypeError):
                    response_dict = {"result": tool_content}
                gemini_contents.append(types.Content(
                    role="tool",
                    parts=[types.Part.from_function_response(
                        name=tool_name,
                        response=response_dict,
                    )]
                ))

        last_processed = len(messages)

        gemini_tool = convert_tools_to_gemini(tools) if tools else None

        config = types.GenerateContentConfig(
            temperature=temperature,
            tools=[gemini_tool] if gemini_tool else [],
        )
        if thinking_level:
            config.thinking_config = types.ThinkingConfig(
                include_thoughts=True,
                thinking_level=thinking_level,
            )
        if system_instruction:
            config.system_instruction = system_instruction

        MAX_API_RETRIES = 3
        MAX_MALFORMED_RETRIES = 5
        total_attempts = MAX_API_RETRIES + MAX_MALFORMED_RETRIES
        malformed_count = 0

        for attempt in range(total_attempts):
            try:
                response = await client.aio.models.generate_content(
                    model=model,
                    contents=gemini_contents,
                    config=config,
                )

                converted = convert_response(response)

                if converted.get('malformed_function_call'):
                    malformed_count += 1
                    if malformed_count <= MAX_MALFORMED_RETRIES:
                        if debug:
                            console.print(f"[bold yellow]DEBUG: MALFORMED_FUNCTION_CALL detected. Retrying... ({malformed_count}/{MAX_MALFORMED_RETRIES})[/bold yellow]")
                        await asyncio.sleep(1)
                        continue
                    if debug:
                        console.print(f"[bold red]DEBUG: MALFORMED_FUNCTION_CALL persists after {MAX_MALFORMED_RETRIES} retries.[/bold red]")
                    return converted

                if response.candidates and response.candidates[0].content:
                    gemini_contents.append(response.candidates[0].content)

                return converted
            except genai_errors.ClientError as e:
                status_code = getattr(e, 'status_code', None) or getattr(e, 'code', None)
                if status_code in (429, 500, 503) and attempt < total_attempts - 1:
                    if debug:
                        console.print(f"[bold yellow]DEBUG: Gemini API error ({status_code}). Retrying... ({attempt+1}/{total_attempts})[/bold yellow]")
                    await asyncio.sleep(2)
                    continue
                raise e
            except genai_errors.ServerError as e:
                if attempt < total_attempts - 1:
                    if debug:
                        console.print(f"[bold yellow]DEBUG: Gemini server error. Retrying... ({attempt+1}/{total_attempts})[/bold yellow]")
                    await asyncio.sleep(2)
                    continue
                raise e

    return chat_fn


async def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        console.print("[bold red]Error:[/bold red] GEMINI_API_KEY environment variable not set.")
        console.print("[yellow]Set it with: export GEMINI_API_KEY=your-api-key[/yellow]")
        sys.exit(1)

    args = parse_args()
    debug = args.debug
    verbose = args.verbose or args.debug

    input_session = PromptSession()

    model = await select_model(input_session)

    context_window = MODEL_CONTEXT_LENGTHS.get(model, 1048576)
    if verbose:
        console.print(f"[dim]Context window: {context_window:,} tokens[/dim]")

    chat_fn = create_gemini_chat_fn(api_key, debug=debug, thinking_level=args.thinking_level, temperature=args.temperature)

    await run_game(chat_fn, model, context_window, verbose=verbose, debug=debug)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Exiting...[/yellow]")
        sys.exit(0)
    except SystemExit as e:
        sys.exit(e.code)