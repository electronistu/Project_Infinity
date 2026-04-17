import os
import sys
import json
import argparse
import asyncio
import ollama
from rich.console import Console
from rich.panel import Panel
from rich.padding import Padding
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dice_server import init_player_db
from display import format_stats, render_gm_text

# Configuration
AVAILABLE_MODELS = [
    "qwen3.5:cloud",
    "qwen3.5:397b-cloud",
    "deepseek-v3.2:cloud",
    "glm-5.1:cloud",
    "gemma4:31b-cloud",
]
MODEL_CONTEXT_LENGTHS = {
    "qwen3.5:cloud": 262144,
    "qwen3.5:397b-cloud": 262144,
    "deepseek-v3.2:cloud": 163840,
    "glm-5.1:cloud": 202752,
    "gemma4:31b-cloud": 262144,
}
LOCK_FILE = "GameMaster_MCP.md"
OUTPUT_DIR = "output"
TEMP = 0.0
SERVER_PARAMS = StdioServerParameters(
    command="python3",
    args=["dice_server.py"],
)

console = Console()
VERBOSE = False
DEBUG = False

def get_wwf_files():
    """List all .wwf files in the output directory."""
    if not os.path.exists(OUTPUT_DIR):
        return []
    return [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".wwf")]

async def select_model(input_session):
    """TUI for selecting the LLM model."""
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

async def select_wwf(input_session):
    """TUI for selecting the world file."""
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

def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Project Infinity: A Dynamic, Text-Based RPG World Engine")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed MCP tool calls and responses")
    parser.add_argument("--debug", "-d", action="store_true", help="Show raw LLM responses and tool calls")
    return parser.parse_args()

async def main():
    global VERBOSE, DEBUG
    args = parse_args()
    DEBUG = args.debug
    VERBOSE = args.verbose or args.debug

    if VERBOSE:
        console.print("[dim]Verbose mode enabled[/dim]")
    if DEBUG:
        console.print("[dim]Debug mode enabled[/dim]")

    input_session = PromptSession()

    # 1. LLM Model Selection
    model = await select_model(input_session)

    # 1b. Fetch model context window
    context_window = MODEL_CONTEXT_LENGTHS.get(model)
    if context_window is None:
        model_info = ollama.show(model)
        context_window = next(
            (v for k, v in model_info.get('model_info', {}).items() if k.endswith('.context_length')),
            4096
        )
    if VERBOSE:
        console.print(f"[dim]Context window: {context_window:,} tokens[/dim]")

    # 2. World File Selection
    wwf_path = await select_wwf(input_session)
    console.print(f"\n[green]Selected world:[/green] {wwf_path}")

    player_path = wwf_path.replace(".wwf", ".player")

    # 3. Load Files
    with open(LOCK_FILE, "r") as f:
        lock_content = f.read()
    with open(wwf_path, "r") as f:
        key_content = f.read()

    # 4. Setup MCP Client and Ollama Tools
    async with stdio_client(StdioServerParameters(
        command="python3",
        args=["dice_server.py", player_path],
    )) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Fetch tools from MCP server to present to Ollama
            mcp_tools = await session.list_tools()
            ollama_tools = []
            for tool in mcp_tools.tools:
                ollama_tools.append({
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
                    # 1. LLM Request with Retry Logic
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            response = await ollama.AsyncClient().chat(
                                model=model,
                                messages=messages,
                                tools=ollama_tools,
                                options={"temperature": TEMP, "num_ctx": context_window}
                            )
                            break
                        except ollama._types.ResponseError as e:
                            if e.status_code in [500, 502, 503] and attempt < max_retries - 1:
                                if DEBUG:
                                    console.print(f"[bold yellow]DEBUG: Ollama overloaded ({e.status_code}). Retrying... ({attempt+1}/{max_retries})[/bold yellow]")
                                await asyncio.sleep(2)
                                continue
                            raise e

                    current_context_tokens = response.get('prompt_eval_count', current_context_tokens)

                    if DEBUG:
                        console.print(f"[dim]DEBUG RESPONSE: {response}[/dim]")

                    response_msg = response['message']

                    # Extract content safely from Message object or dict
                    content = ""
                    if hasattr(response_msg, 'content'):
                        content = response_msg.content
                    elif isinstance(response_msg, dict):
                        content = response_msg.get('content', "")

                    messages.append(response_msg)

                    # 1. Check for pause token FIRST, before returning or processing tools
                    if any(token in (content or "") for token in ["{{_NEED_AN_OTHER_PROMPT}}", "{{_NEED_ANOTHER_PROMPT}}"]):
                        if DEBUG:
                            console.print("[bold yellow]DEBUG: Checkpoint token detected. Pausing...[/bold yellow]")
                        return "__SYSTEM_PAUSE__"

                    # 2. If no pause token, check if we should return content now
                    tool_calls_list = response_msg.get('tool_calls') if isinstance(response_msg, dict) else getattr(response_msg, 'tool_calls', None)
                    if not tool_calls_list:
                        return content

                    # 3. Process tool calls
                    for tool_call in tool_calls_list:
                        # Support both dict and object access for tool_call
                        if isinstance(tool_call, dict):
                            tool_name = tool_call['function']['name']
                            tool_args = tool_call['function']['arguments']
                        else:
                            tool_name = tool_call.function.name
                            tool_args = tool_call.function.arguments

                        # Call MCP tool
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

            # BOOT SEQUENCE: Provide the Key
            console.print("\n[yellow]Injecting World Data (The Key)...[/yellow]")
            response_text = await chat_with_tools(key_content)

            while response_text == "__SYSTEM_PAUSE__":
                if DEBUG:
                    console.print("[bold cyan]DEBUG: Injecting Resume Token ({{_CONTINUE_EXECUTION}})[/bold cyan]")
                response_text = await chat_with_tools("{{_CONTINUE_EXECUTION}}")

            console.print(Panel(
                Padding(render_gm_text(response_text), (1, 1)),
                title="[bold magenta]The Game Master Awakens[/bold magenta]",
                border_style="magenta"
            ))

            # 4. Game Loop
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
                if VERBOSE:
                    gm_response = await chat_with_tools(user_input)
                else:
                    with console.status("[bold blue]GM is thinking...[/bold blue]"):
                        gm_response = await chat_with_tools(user_input)

                while gm_response == "__SYSTEM_PAUSE__":
                    if DEBUG:
                        console.print("[bold cyan]DEBUG: Injecting Resume Token ({{_CONTINUE_EXECUTION}})[/bold cyan]")
                    gm_response = await chat_with_tools("{{_CONTINUE_EXECUTION}}")

                if gm_response and gm_response != "__SYSTEM_PAUSE__":
                    # Strip the pause token if it leaked into the final narrative
                    clean_response = gm_response.replace("{{_NEED_AN_OTHER_PROMPT}}", "").strip()

                    if clean_response:
                        console.print(Panel(
                            Padding(render_gm_text(clean_response), (1, 1)),
                            title="[bold magenta]Game Master[/bold magenta]",
                            border_style="magenta"
                        ))
                        console.print("\n")

                    # Sync database every 4 prompts
                    if prompt_count > 0 and prompt_count % 4 == 0:
                        if DEBUG or VERBOSE:
                            console.print("[dim]Synchronizing database...[/dim]")
                        await chat_with_tools("{{_SYNC_DATABASE}}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Exiting...[/yellow]")
        sys.exit(0)
    except SystemExit as e:
        sys.exit(e.code)
