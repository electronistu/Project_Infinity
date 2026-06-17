import os
import sys
import json
import argparse
import asyncio
from rich.panel import Panel
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from game_engine import run_game, console

GM_MODELS = [
    "gemini-3.5-flash",
]
IMAGE_MODELS = [
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
    "gemini-2.5-flash-image",
]
MODEL_CONTEXT_LENGTHS = {
    "gemini-3.5-flash": 1048576,
}
DEFAULT_TEMP = 1.0


def parse_args():
    parser = argparse.ArgumentParser(description="Project Infinity: A Dynamic, Text-Based RPG World Engine (Nano Banana — Image Enhanced)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed MCP tool calls and responses")
    parser.add_argument("--debug", "-d", action="store_true", help="Show raw LLM responses and tool calls")
    parser.add_argument("--temperature", "-t", type=float, default=DEFAULT_TEMP, help=f"Sampling temperature (default: {DEFAULT_TEMP})")
    parser.add_argument("--thinking-level", choices=["LOW", "MEDIUM", "HIGH"], default=None, help="Enable structured thinking with the specified level")
    parser.add_argument("--image-frequency", "-f", type=int, default=1, help="Generate an image every N prompts (default: 1 = every prompt, 0 = never)")
    return parser.parse_args()


async def select_model(input_session, models, title):
    console.print(Panel(f"[bold magenta]{title}[/bold magenta]", expand=False))
    for i, model in enumerate(models):
        console.print(f"[cyan]{i+1}[/cyan] {model}")

    choice = await input_session.prompt_async(HTML('<ansicyan><b>Select a model (number)</b></ansicyan> '))
    try:
        idx = int(choice) - 1
        selected_model = models[idx]
        console.print(Panel(f"[bold green]Model selected:[/bold green] {selected_model}", border_style="green"))
        return selected_model
    except (ValueError, IndexError):
        console.print("[red]Invalid selection. Defaulting to first model.[/red]")
        return models[0]


def _strip_additional_properties(schema):
    if isinstance(schema, dict):
        schema.pop("additionalProperties", None)
        for key, value in schema.items():
            if isinstance(value, (dict, list)):
                _strip_additional_properties(value)
    elif isinstance(schema, list):
        for item in schema:
            _strip_additional_properties(item)


def convert_tools_to_gemini(tools_schema):
    declarations = []
    for tool in tools_schema:
        fn = tool.get("function", {})
        name = fn.get("name", "")
        description = fn.get("description", "")
        parameters = fn.get("parameters", {})
        _strip_additional_properties(parameters)
        declarations.append(types.FunctionDeclaration(
            name=name,
            description=description,
            parameters=parameters,
        ))
    return types.Tool(function_declarations=declarations)


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

    prompt_eval_count = 0
    if response.usage_metadata:
        prompt_eval_count = getattr(response.usage_metadata, 'prompt_token_count', 0) or 0

    return {
        'prompt_eval_count': prompt_eval_count,
        'malformed_function_call': False,
        'thinking': "\n".join(thinking_parts) if thinking_parts else None,
        'thinking_only': thinking_only,
        'message': {
            'content': content,
            'tool_calls': tool_calls if tool_calls else None,
        }
    }


def _format_tool_schemas(tools_schema):
    lines = []
    for tool in tools_schema:
        fn = tool.get("function", {})
        name = fn.get("name", "")
        params = fn.get("parameters", {})
        props = params.get("properties", {})
        required = params.get("required", [])
        args = []
        for pname, pdef in props.items():
            ptype = pdef.get("type", "any")
            if pname in required:
                args.append(f"{pname}: {ptype}")
            else:
                default = pdef.get("default")
                if default is not None:
                    args.append(f"{pname}: {ptype} [default: {repr(default)}]")
                else:
                    args.append(f"{pname}: {ptype} [optional]")
        lines.append(f"- {name}({', '.join(args)})")
    return "\n".join(lines)


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
                tool_calls_raw = msg.get("tool_calls")
                content_text = msg.get("content", "")
                parts = []
                if content_text:
                    parts.append(types.Part(text=content_text))
                if tool_calls_raw:
                    for tc in tool_calls_raw:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", {})
                        parts.append(types.Part(function_call=types.FunctionCall(
                            name=fn.get("name", ""),
                            args=args if isinstance(args, dict) else {},
                        )))
                if parts:
                    gemini_contents.append(types.Content(role="model", parts=parts))

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

        MAX_GEMINI_ENTRIES = 600
        if len(gemini_contents) > MAX_GEMINI_ENTRIES:
            gemini_contents = gemini_contents[:12] + gemini_contents[-(MAX_GEMINI_ENTRIES - 12):]

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

        tool_schemas_str = _format_tool_schemas(tools) if tools else "No tools available."

        MAX_API_RETRIES = 3
        MALFORMED_MSG_1 = (
            "Your previous response was rejected by the API with finish_reason: MALFORMED_FUNCTION_CALL. "
            "This means your function call's arguments or structure did not match the tool schema. "
            "Please verify that all parameter names, types, and required fields match the tool definitions exactly, then respond again.\n\n"
            f"Available tools:\n{tool_schemas_str}"
        )
        MALFORMED_MSG_2 = (
            "You are still sending malformed function calls. The API is rejecting them. "
            "Stop and carefully review the tool schemas before attempting another function call.\n\n"
            f"Available tools:\n{tool_schemas_str}"
        )

        attempt = 0
        malformed_count = 0
        max_attempts = MAX_API_RETRIES + 6

        while attempt < max_attempts:
            attempt += 1
            try:
                response = await client.aio.models.generate_content(
                    model=model,
                    contents=gemini_contents,
                    config=config,
                )

                converted = convert_response(response)

                if converted.get('malformed_function_call'):
                    malformed_count += 1
                    if malformed_count == 1:
                        msg = MALFORMED_MSG_1
                        gemini_contents.append(types.Content(
                            role="user",
                            parts=[types.Part(text=msg)]
                        ))
                        if debug:
                            console.print(f"[bold yellow]DEBUG: MALFORMED_FUNCTION_CALL (attempt {attempt}). Injecting corrective message #1.[/bold yellow]")
                    elif malformed_count == 2:
                        msg = MALFORMED_MSG_2
                        gemini_contents.append(types.Content(
                            role="user",
                            parts=[types.Part(text=msg)]
                        ))
                        if debug:
                            console.print(f"[bold yellow]DEBUG: MALFORMED_FUNCTION_CALL (attempt {attempt}). Injecting corrective message #2.[/bold yellow]")
                    elif malformed_count <= 5:
                        if debug:
                            console.print(f"[bold yellow]DEBUG: MALFORMED_FUNCTION_CALL (attempt {attempt}). Silent retry ({malformed_count}/5)...[/bold yellow]")
                    else:
                        if debug:
                            console.print(f"[bold red]DEBUG: MALFORMED_FUNCTION_CALL persists after 6 attempts.[/bold red]")
                        return converted
                    await asyncio.sleep(1)
                    continue

                if response.candidates and response.candidates[0].content:
                    gemini_contents.append(response.candidates[0].content)

                return converted
            except genai_errors.ClientError as e:
                status_code = getattr(e, 'status_code', None) or getattr(e, 'code', None)
                if status_code in (429, 500, 503) and attempt < max_attempts - 1:
                    if debug:
                        console.print(f"[bold yellow]DEBUG: Gemini API error ({status_code}). Retrying... ({attempt}/{max_attempts})[/bold yellow]")
                    await asyncio.sleep(2)
                    continue
                raise e
            except genai_errors.ServerError as e:
                if attempt < max_attempts - 1:
                    if debug:
                        console.print(f"[bold yellow]DEBUG: Gemini server error. Retrying... ({attempt}/{max_attempts})[/bold yellow]")
                    await asyncio.sleep(2)
                    continue
                raise e

    return chat_fn


def create_image_gen_fn(api_key, image_model, debug=False):
    client = genai.Client(api_key=api_key)
    # We store text history and count locally instead of using the API's conversation history
    image_history = [] 
    scene_count = 0

    async def image_gen_fn(description, char_anchor=None, hp_info=None):
        nonlocal image_history, scene_count
        
        config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio="16:9"),
        )

        scene_label = "Opening Scene" if scene_count == 0 else f"Scene {scene_count}"
        hp_text = f" Current HP: {hp_info}" if hp_info else ""
        
        # Build a single, comprehensive prompt for this request
        prompt_parts = []
        
        # 1. Anchor
        if char_anchor:
            prompt_parts.append(f"VISUAL ANCHOR (Primary Character Reference): {char_anchor}")
        
        # 2. Brief Continuity (last 3 scenes)
        if image_history:
            history_context = "\n".join(image_history[-3:])
            prompt_parts.append(f"PREVIOUS SCENES FOR VISUAL CONTINUITY:\n{history_context}")
            
        # 3. Current Scene
        current_scene_prompt = (
            f"{scene_label}{hp_text}. Maintain visual consistency with the protagonist and environment. "
            f"Use the HP to influence the character's physical appearance (e.g., blood, fatigue), "
            f"but DO NOT render any text or numbers.\n"
            f"Current Scene Description: {description}"
        )
        prompt_parts.append(current_scene_prompt)
        
        final_prompt = "\n\n".join(prompt_parts)
        
        # Update local history (only store the description part to save tokens)
        image_history.append(f"{scene_label}: {description[:200]}...")
        scene_count += 1

        if debug:
            console.print(f"[dim]DEBUG IMAGE: Prompt length: {len(final_prompt)} chars, model: {image_model}, scene: {scene_label}[/dim]")

        try:
            # Use a single-turn request to avoid API session issues with IMAGE modality
            response = await client.aio.models.generate_content(
                model=image_model,
                contents=[types.Content(role="user", parts=[types.Part(text=final_prompt)])],
                config=config,
            )

            if debug:
                num_candidates = len(response.candidates) if response.candidates else 0
                console.print(f"[dim]DEBUG IMAGE: Candidates: {num_candidates}[/dim]")

            if not response.candidates or not response.candidates[0].content:
                if debug:
                    console.print("[bold yellow]DEBUG: Image generation returned no content.[/bold yellow]")
                return None

            parts = response.candidates[0].content.parts
            if not parts:
                if debug:
                    console.print("[bold yellow]DEBUG: Image generation returned no parts.[/bold yellow]")
                return None

            for part in parts:
                image = part.as_image()
                if image:
                    return image

            if debug:
                console.print("[bold yellow]DEBUG: No image part found in response.[/bold yellow]")
            return None

        except genai_errors.ClientError as e:
            if debug:
                console.print(f"[bold yellow]DEBUG: Image generation API error: {e}[/bold yellow]")
            return None
        except genai_errors.ServerError as e:
            if debug:
                console.print(f"[bold yellow]DEBUG: Image generation server error: {e}[/bold yellow]")
            return None
        except Exception as e:
            if debug:
                console.print(f"[bold yellow]DEBUG: Image generation unexpected error: {e}[/bold yellow]")
            return None

    return image_gen_fn


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

    gm_model = await select_model(input_session, GM_MODELS, "Infinity Project: GameMaster Model Selection (Nano Banana)")
    image_model = await select_model(input_session, IMAGE_MODELS, "Infinity Project: Image Generation Model Selection (Nano Banana)")

    context_window = MODEL_CONTEXT_LENGTHS.get(gm_model, 1048576)
    if verbose:
        console.print(f"[dim]Context window: {context_window:,} tokens[/dim]")

    chat_fn = create_gemini_chat_fn(api_key, debug=debug, thinking_level=args.thinking_level, temperature=args.temperature)
    image_gen_fn = create_image_gen_fn(api_key, image_model, debug=debug)

    await run_game(chat_fn, gm_model, context_window, verbose=verbose, debug=debug,
                   image_gen_fn=image_gen_fn, image_frequency=args.image_frequency)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Exiting...[/yellow]")
        sys.exit(0)
    except SystemExit as e:
        sys.exit(e.code)
