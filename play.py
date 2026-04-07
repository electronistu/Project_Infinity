import os
import sys
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

# Configuration
MODEL = "gemma4:31b-cloud"
LOCK_FILE = "GameMaster.md"
OUTPUT_DIR = "output"
TEMP = 0.0

console = Console()

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

def main():
    # 1. World File Selection
    wwf_path = select_wwf()
    console.print(f"\n[green]Selected world:[/green] {wwf_path}")
    
    # 2. Load Files
    with open(LOCK_FILE, "r") as f:
        lock_content = f.read()
    with open(wwf_path, "r") as f:
        key_content = f.read()

    # 3. Initialize Ollama Session
    messages = []
    
    # BOOT SEQUENCE - STEP 1: Provide the Lock
    console.print("\n[yellow]Loading Agent Protocol (The Lock)...[/yellow]")
    messages.append({"role": "user", "content": lock_content})
    
    response = ollama.chat(
        model=MODEL,
        messages=messages,
        options={"temperature": TEMP}
    )
    
    response_text = response['message']['content']
    messages.append({"role": "assistant", "content": response_text})
    
    # Check for "Awaiting Key..."
    if "Awaiting Key..." in response_text:
        console.print(Panel("[bold green]SOCIALLY VERIFIED:[/bold green] Model is awaiting the key.", border_style="green"))
    else:
        console.print(Panel("[bold yellow]SOCIALLY UNVERIFIED:[/bold yellow] Model did not respond with 'Awaiting Key...', but proceeding anyway.", border_style="yellow"))

    # BOOT SEQUENCE - STEP 2: Provide the Key
    console.print("\n[yellow]Injecting World Data (The Key)...[/yellow]")
    messages.append({"role": "user", "content": key_content})
    
    response = ollama.chat(
        model=MODEL,
        messages=messages,
        options={"temperature": TEMP}
    )
    
    response_text = response['message']['content']
    messages.append({"role": "assistant", "content": response_text})
    
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
            
        messages.append({"role": "user", "content": user_input})
        
        with console.status("[bold blue]GM is thinking...[/bold blue]"):
            response = ollama.chat(
                model=MODEL,
                messages=messages,
                options={"temperature": TEMP}
            )
            
        gm_response = response['message']['content']
        messages.append({"role": "assistant", "content": gm_response})
        
        console.print(Panel(
            Padding(render_gm_text(gm_response), (1, 1)), 
            title="[bold magenta]Game Master[/bold magenta]", 
            border_style="magenta"
        ))
        console.print("\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Exiting...[/yellow]")
        sys.exit(0)
