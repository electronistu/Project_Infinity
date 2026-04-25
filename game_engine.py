import os
import sys
import json
import asyncio
from rich.console import Console
from rich.panel import Panel
from rich.padding import Padding
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from display import format_stats, render_gm_text

LOCK_FILE = "GameMaster_MCP.md"
OUTPUT_DIR = "output"

console = Console()
VERBOSE = False
DEBUG = False


def get_wwf_files():
    if not os.path.exists(OUTPUT_DIR):
        return []
    return [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".wwf")]


async def select_wwf(input_session):
    files = get_wwf_files()
    if not files:
        console.print("[bold red]Error:[/bold red] No .wwf files found in the output directory.")
        sys.exit(1)

    console.print(Panel("[bold magenta] Infinity Project: World Selection [/bold magenta]", expand=False))
    for i, f in enumerate(files):
        console.print(f"[cyan]{i+1}[/cyan] {f}")

    choice = await input_session.prompt_async(HTML('<ansicyan><b>Select a world file (number)</b></ansicyan> '))
    try:
        idx = int(choice) - 1
        return os.path.join(OUTPUT_DIR, files[idx])
    except (ValueError, IndexError):
        console.print("[red]Invalid selection. Defaulting to first file.[/red]")
        return os.path.join(OUTPUT_DIR, files[0])


async def run_game(chat_fn, model, context_window, verbose=False, debug=False):
    """
    Run the game loop.

    chat_fn must be an async callable with signature:
        async def chat_fn(messages, tools, model, context_window) -> dict

    The returned dict must have the structure:
        {
            'prompt_eval_count': int,
            'message': {
                'content': str,
                'tool_calls': list[dict] | None
            }
        }

    Where each tool_calls entry is:
        {'function': {'name': str, 'arguments': dict}}
    """
    global VERBOSE, DEBUG
    VERBOSE = verbose
    DEBUG = debug

    if VERBOSE:
        console.print("[dim]Verbose mode enabled[/dim]")
    if DEBUG:
        console.print("[dim]Debug mode enabled[/dim]")

    input_session = PromptSession()

    wwf_path = await select_wwf(input_session)
    console.print(f"\n[green]Selected world:[/green] {wwf_path}")

    player_path = os.path.splitext(wwf_path)[0] + ".player"

    with open(LOCK_FILE, "r") as f:
        lock_content = f.read()
    with open(wwf_path, "r") as f:
        key_content = f.read()

    try:
        async with stdio_client(StdioServerParameters(
            command=sys.executable,
            args=["dice_server.py", player_path],
        )) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                mcp_tools = await session.list_tools()
                tools_schema = []
                for tool in mcp_tools.tools:
                    tools_schema.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.inputSchema
                        }
                    })

                messages = [
                    {"role": "system", "content": lock_content}
                ]
                current_context_tokens = 0

                async def chat_with_tools(role_content):
                    nonlocal messages, current_context_tokens
                    if isinstance(role_content, str):
                        messages.append({"role": "user", "content": role_content})
                    else:
                        messages.append(role_content)

                    while True:
                        response = await chat_fn(
                            messages=messages,
                            tools=tools_schema,
                            model=model,
                            context_window=context_window,
                        )

                        current_context_tokens = response.get('prompt_eval_count', current_context_tokens)

                        if DEBUG:
                            console.print(f"[dim]DEBUG RESPONSE: {response}[/dim]")
                            if response.get('thinking'):
                                console.print(Panel(
                                    response['thinking'],
                                    title="[bold yellow]DEBUG: Thinking (structured)[/bold yellow]",
                                    border_style="yellow",
                                ))

                        if response.get('malformed_function_call'):
                            return "The GM stumbles over their words... (malformed response)"

                        response_msg = response['message']
                        content = response_msg['content'] if response_msg else ""

                        messages.append({
                            "role": "assistant",
                            "content": content or "",
                            "tool_calls": response_msg.get('tool_calls') or None,
                        } if response_msg.get('tool_calls') else {
                            "role": "assistant",
                            "content": content or "",
                        })

                        if any(token in (content or "") for token in ["{{_NEED_AN_OTHER_PROMPT}}", "{{_NEED_ANOTHER_PROMPT}}"]):
                            if DEBUG:
                                console.print("[bold yellow]DEBUG: Checkpoint token detected. Pausing...[/bold yellow]")
                            return "__SYSTEM_PAUSE__"

                        thinking_retries = 0
                        MAX_THINKING_RETRIES = 3
                        while response.get('thinking_only') and thinking_retries < MAX_THINKING_RETRIES:
                            thinking_retries += 1
                            if DEBUG:
                                console.print(f"[bold yellow]DEBUG: Thinking-only response. Injecting 'Continue'... ({thinking_retries}/{MAX_THINKING_RETRIES})[/bold yellow]")
                            messages.append({"role": "user", "content": "Continue"})
                            response = await chat_fn(
                                messages=messages,
                                tools=tools_schema,
                                model=model,
                                context_window=context_window,
                            )
                            current_context_tokens = response.get('prompt_eval_count', current_context_tokens)
                            if DEBUG:
                                console.print(f"[dim]DEBUG RESPONSE: {response}[/dim]")
                                if response.get('thinking'):
                                    console.print(Panel(
                                        response['thinking'],
                                        title="[bold yellow]DEBUG: Thinking (structured)[/bold yellow]",
                                        border_style="yellow",
                                    ))
                            response_msg = response['message']
                            content = response_msg['content'] if response_msg else ""
                            messages.append({
                                "role": "assistant",
                                "content": content or "",
                                "tool_calls": response_msg.get('tool_calls') or None,
                            } if response_msg.get('tool_calls') else {
                                "role": "assistant",
                                "content": content or "",
                            })

                        if response.get('thinking_only') and thinking_retries >= MAX_THINKING_RETRIES:
                            return "The GM pauses, deep in thought..."

                        tool_calls_list = response_msg.get('tool_calls')
                        if not tool_calls_list:
                            return content

                        for tool_call in tool_calls_list:
                            tool_name = tool_call['function']['name']
                            tool_args = tool_call['function']['arguments']

                            if VERBOSE:
                                console.print(f"[dim]🔧 Tool: {tool_name}({tool_args})[/dim]")

                            result = await session.call_tool(tool_name, arguments=tool_args)

                            if VERBOSE:
                                console.print(f"[dim]   → {result.content}[/dim]")

                            messages.append({
                                "role": "tool",
                                "content": "\n".join(block.text for block in result.content if hasattr(block, "text")),
                                "name": tool_name
                            })

                async def handle_slash_command(cmd):
                    cmd = cmd.strip().lower()
                    if cmd == '/help':
                        help_text = (
                            "[bold white]Available Commands:[/bold white]\n\n"
                            "  [cyan]/help[/cyan]  - Show this help message\n"
                            "  [cyan]/stats[/cyan] - Display current player stats\n"
                            "  [cyan]/sync[/cyan]  - Force a database sync with the GM\n"
                            "  [cyan]/quit[/cyan]  - Exit the game\n\n"
                            "[dim]Type anything else to send as an action to the Game Master.[/dim]"
                        )
                        console.print(Panel(help_text, title="[bold magenta]Help[/bold magenta]", border_style="magenta", expand=False))
                    elif cmd == '/stats':
                        result = await session.call_tool("dump_player_db", arguments={})
                        if hasattr(result, 'content') and result.content:
                            text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
                            try:
                                db_data = json.loads(text)
                            except (json.JSONDecodeError, TypeError):
                                db_data = text
                            if isinstance(db_data, dict):
                                for panel in format_stats(db_data):
                                    console.print(panel)
                            else:
                                console.print(Panel(str(db_data), title="[bold green]Player Stats[/bold green]", border_style="green", expand=False))
                        else:
                            console.print("[yellow]Could not retrieve player stats.[/yellow]")
                    elif cmd == '/sync':
                        console.print("[dim]Synchronizing database...[/dim]")
                        await chat_with_tools("{{_SYNC_DATABASE}}")
                        console.print(Panel("[green]Database synchronized.[/green]", border_style="green", expand=False))
                    elif cmd == '/quit':
                        return 'quit'
                    else:
                        console.print(f"[yellow]Unknown command: {cmd}[/yellow]")
                        console.print("[dim]Type /help for available commands.[/dim]")
                    return None

                console.print("\n[yellow]Injecting World Data (The Key)...[/yellow]")
                if VERBOSE:
                    response_text = await chat_with_tools(key_content)
                else:
                    with console.status("[bold blue]GM is thinking...[/bold blue]"):
                        response_text = await chat_with_tools(key_content)

                while response_text == "__SYSTEM_PAUSE__":
                    if DEBUG:
                        console.print("[bold cyan]DEBUG: Injecting Resume Token ({{_CONTINUE_EXECUTION}})[/bold cyan]")
                    if VERBOSE:
                        response_text = await chat_with_tools("{{_CONTINUE_EXECUTION}}")
                    else:
                        with console.status("[bold blue]GM is thinking...[/bold blue]"):
                            response_text = await chat_with_tools("{{_CONTINUE_EXECUTION}}")

                console.print(Panel(
                    Padding(render_gm_text(response_text), (1, 1)),
                    title="[bold magenta]The Game Master Awakens[/bold magenta]",
                    border_style="magenta"
                ))

                console.print("\n[bold cyan]--- Game Started. Type /help for commands. ---[/bold cyan]\n")

                prompt_count = 0
                while True:
                    if VERBOSE or DEBUG:
                        console.print(f"[dim]Context: {current_context_tokens:,} / {context_window:,} tokens[/dim]")
                    user_input = await input_session.prompt_async(HTML('<ansicyan><b>Your Action:</b></ansicyan> '))
                    user_input = user_input.strip()

                    if not user_input:
                        continue

                    if user_input.startswith('/'):
                        result = await handle_slash_command(user_input)
                        if result == 'quit':
                            console.print("[yellow]Closing connection to the void... Goodbye.[/yellow]")
                            break
                        continue

                    prompt_count += 1
                    try:
                        if VERBOSE:
                            gm_response = await chat_with_tools(user_input)
                        else:
                            with console.status("[bold blue]GM is thinking...[/bold blue]"):
                                gm_response = await chat_with_tools(user_input)
                    except KeyboardInterrupt:
                        console.print("\n[yellow]Interrupted. Type /quit to exit.[/yellow]")
                        continue
                    except Exception as e:
                        console.print(f"[bold red]Error communicating with GM: {e}[/bold red]")
                        continue

                    while gm_response == "__SYSTEM_PAUSE__":
                        if DEBUG:
                            console.print("[bold cyan]DEBUG: Injecting Resume Token ({{_CONTINUE_EXECUTION}})[/bold cyan]")
                        if VERBOSE:
                            gm_response = await chat_with_tools("{{_CONTINUE_EXECUTION}}")
                        else:
                            with console.status("[bold blue]GM is thinking...[/bold blue]"):
                                gm_response = await chat_with_tools("{{_CONTINUE_EXECUTION}}")

                    if gm_response and gm_response != "__SYSTEM_PAUSE__":
                        clean_response = gm_response.replace("{{_NEED_AN_OTHER_PROMPT}}", "").replace("{{_NEED_ANOTHER_PROMPT}}", "").strip()

                        if clean_response:
                            console.print(Panel(
                                Padding(render_gm_text(clean_response), (1, 1)),
                                title="[bold magenta]Game Master[/bold magenta]",
                                border_style="magenta"
                            ))
                            console.print("\n")

                        if prompt_count > 0 and prompt_count % 4 == 0:
                            if VERBOSE:
                                await chat_with_tools("{{_SYNC_DATABASE}}")
                            else:
                                with console.status("[bold blue]Synchronizing database...[/bold blue]"):
                                    await chat_with_tools("{{_SYNC_DATABASE}}")
    except KeyboardInterrupt:
        console.print("\n[yellow]Game interrupted. Goodbye.[/yellow]")
    except Exception as e:
        import traceback
        traceback.print_exc()
        console.print(f"\n[bold red]Fatal error: {e}[/bold red]")
        console.print("[dim]The game session has ended unexpectedly.[/dim]")