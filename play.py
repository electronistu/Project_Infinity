import os
import sys
import argparse
import asyncio
import ollama
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from rich.markdown import Markdown
from rich.live import Live
from rich.layout import Layout
from rich.theme import Theme
from rich.padding import Padding
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dice_server import init_player_db

# Configuration
AVAILABLE_MODELS = [
    "qwen3.5:cloud",
    "qwen3.5:397b-cloud",
    "deepseek-v3.2:cloud",
    "glm-5.1:cloud",
]
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

def render_gm_text(text):
    """Custom renderer to force styles on LLM output since Markdown theme is unreliable."""
    import re
    
    processed = text
    # Bold
    processed = re.sub(r'\*\*(.*?)\*\*', r'[bold white]\1[/]', processed)
    # Italics
    processed = re.sub(r'\*(.*?)\*', r'[italic cyan]\1[/]', processed)
    # Headers (simple # check at start of line)
    lines = processed.split('\n')
    for i in range(len(lines)):
        if lines[i].startswith('#'):
            level = len([c for c in lines[i] if c == '#'])
            content = lines[i].strip('#').strip()
            lines[i] = f"[bold magenta]{content}[/]"
    
    return "\n".join(lines)

def select_model():
    """TUI for selecting the LLM model."""
    console.print(Panel("[bold magenta] Infinity Project: LLM Selection [/bold magenta]", expand=False))
    for i, model in enumerate(AVAILABLE_MODELS):
        console.print(f"[cyan]{i+1}[/cyan] {model}")
    
    choice = Prompt.ask("\n[bold white]Select an LLM (number)[/bold white]", default="1")
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

def select_wwf():
    """TUI for selecting the world file."""
    files = get_wwf_files()
    if not files:
        console.print("[bold red]Error:[/bold red] No .wwf files found in the output directory.")
        sys.exit(1)

    console.print(Panel("[bold magenta] Infinity Project: World Selection [/bold magenta]", expand=False))
    for i, f in enumerate(files):
        console.print(f"[cyan]{i+1}[/cyan] {f}")
    
    choice = Prompt.ask("\n[bold white]Select a world file (number)[/bold white]", default="1")
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
    
    # 1. LLM Model Selection
    model = select_model()
    
    # 2. World File Selection
    wwf_path = select_wwf()
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
            
            async def chat_with_tools(role_content):
                nonlocal messages
                if isinstance(role_content, str):
                    messages.append({"role": "user", "content": role_content})
                else:
                    messages.append(role_content)

                while True:
                    response = await ollama.AsyncClient().chat(
                        model=model,
                        messages=messages,
                        tools=ollama_tools,
                        options={"temperature": TEMP}
                    )
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
                    if "{{_NEED_AN_OTHER_PROMPT}}" in (content or ""):
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
                            "content": str(result.content),
                            "name": tool_name
                        })

            # BOOT SEQUENCE: Provide the Key
            console.print("\n[yellow]Injecting World Data (The Key)...[/yellow]")
            response_text = await chat_with_tools(key_content)
            
            console.print(Panel(
                Padding(render_gm_text(response_text), (1, 1)), 
                title="[bold magenta]The Game Master Awakens[/bold magenta]", 
                border_style="magenta"
            ))

            # 4. Game Loop
            console.print("\n[bold cyan]--- Game Started. Type 'quit' or 'exit' to leave. ---[/bold cyan]\n")
            
            while True:
                user_input = Prompt.ask("[bold white]Your Action[/bold white]")
                
                if user_input.lower() in ["quit", "exit"]:
                    console.print("[yellow]Closing connection to the void... Goodbye.[/yellow]")
                    break
                
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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Exiting...[/yellow]")
        sys.exit(0)
    except SystemExit as e:
        sys.exit(e.code)
