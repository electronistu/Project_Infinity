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
from display import format_stats, render_gm_text, render_image

LOCK_FILE = "GameMaster_MCP.md"
OUTPUT_DIR = "output"
TIMELINE_INTERVAL = 5  # rounds between timeline snapshots

TIMELINE_PROMPT = """SYSTEM INSTRUCTION: You have just completed several rounds of gameplay.
Write a session timeline entry in the following EXACT format. Replace bracketed
text with actual content. Keep it concise. Output ONLY the entry — no extra narration.

## Rounds X-Y | [current location] | [in-game time]
**Key Events**:
- [event 1 in one sentence]
- [event 2 in one sentence]
**NPCs**: [names and roles of new NPCs encountered, or "none"]
**Mechanical Changes**:
- Gold: [old]→[new] (reason)
- Items: [gained/used key items]
- Reputation: [changed factions, if any]
**Active Hooks**: [all unresolved plot threads, one per line]"""


def load_timeline(timeline_path):
    """Load existing timeline if available."""
    if os.path.exists(timeline_path):
        with open(timeline_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def append_timeline_file(timeline_path, entry):
    """Append a new timeline entry to the file."""
    # Ensure output dir exists
    os.makedirs(os.path.dirname(timeline_path) or ".", exist_ok=True)
    # Write header on first entry
    if not os.path.exists(timeline_path):
        header = "# Session Timeline\n\n"
    else:
        header = ""
    with open(timeline_path, "a", encoding="utf-8") as f:
        if header:
            f.write(header)
        f.write(entry.strip() + "\n\n")


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


async def run_game(chat_fn, model, context_window, verbose=False, debug=False,
                   image_gen_fn=None, image_frequency=0):
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
    timeline_path = os.path.splitext(wwf_path)[0] + ".timeline.md"

    with open(LOCK_FILE, "r", encoding="utf-8") as f:
        lock_content = f.read()
    with open(wwf_path, "r", encoding="utf-8") as f:
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

                # ── Load session timeline ─────────────────────────────
                existing_timeline = load_timeline(timeline_path)
                round_counter = 0
                narrative_counter = 0

                messages = [
                    {"role": "system", "content": lock_content}
                ]
                if existing_timeline:
                    messages.append({
                        "role": "system",
                        "content": f"""SESSION_TIMELINE — these are events that happened earlier this session.
Refer to them when the player asks about past events. Do not replay or re-describe them.

{existing_timeline}"""
                    })
                    if VERBOSE:
                        console.print(f"[dim]Timeline loaded: {timeline_path} ({len(existing_timeline)} chars)[/dim]")

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

                        msg_entry = {
                            "role": "assistant",
                            "content": content or "",
                        }
                        if response_msg.get('tool_calls'):
                            msg_entry["tool_calls"] = response_msg['tool_calls']
                        if response.get('thinking'):
                            msg_entry["thinking"] = response['thinking']
                        messages.append(msg_entry)

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
                        if tool_calls_list:
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
                            if DEBUG:
                                console.print("[bold yellow]DEBUG: Tool calls executed alongside sync token. Ignoring token and continuing loop.[/bold yellow]")
                            continue

                        if any(token in (content or "") for token in ["{{_NEED_AN_OTHER_PROMPT}}", "{{_NEED_ANOTHER_PROMPT}}"]):
                            if DEBUG:
                                console.print("[bold yellow]DEBUG: Checkpoint token detected. Pausing...[/bold yellow]")
                            return "__SYSTEM_PAUSE__"

                        return content

                async def _auto_generate_image(narrative_text):
                    if not narrative_text:
                        return
                    try:
                        result = await session.call_tool("dump_player_db", {})
                        db_text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
                        db_data = json.loads(db_text)
                    except Exception:
                        db_data = {}
                    name = db_data.get("name", "the protagonist")
                    gender = db_data.get("gender", "")
                    race = db_data.get("race", "")
                    cls = db_data.get("character_class", "")
                    level = db_data.get("level", "")
                    hp = db_data.get("hit_points", "unknown")
                    max_hp = db_data.get("max_hp", "unknown")
                    stats = db_data.get("stats", {})
                    bg = db_data.get("background", "")
                    alignment = db_data.get("alignment", "")
                    stat_parts = []
                    for k, v in stats.items():
                        stat_parts.append(f"{k.upper()} {v}")
                    stat_str = ", ".join(stat_parts)
                    char_anchor = f"Character: {name}, a {gender} {race} {cls} (level {level}). {stat_str}. Background: {bg}, Alignment: {alignment}."
                    hp_info = f"{hp}/{max_hp}"
                    
                    image_path = os.path.join(OUTPUT_DIR, "current_scene.png")
                    os.makedirs(OUTPUT_DIR, exist_ok=True)
                    try:
                        with console.status("[bold magenta]Generating image...[/bold magenta]"):
                            image = await image_gen_fn(narrative_text, char_anchor=char_anchor, hp_info=hp_info)
                        if image:
                            image.save(image_path)
                            if VERBOSE:
                                console.print(f"[dim]✅ Image saved to {image_path}[/dim]")
                            render_image(image_path)
                        else:
                            if VERBOSE:
                                console.print("[dim]⚠️ Image generation returned no image. Continuing without image.[/dim]")
                    except Exception as e:
                        if VERBOSE:
                            console.print(f"[dim]⚠️ Image generation failed: {e}. Continuing without image.[/dim]")

                async def handle_slash_command(cmd):
                    cmd = cmd.strip().lower()
                    if cmd == '/help':
                        help_text = (
                            "[bold white]Available Commands:[/bold white]\n\n"
                            "  [cyan]/help[/cyan]  - Show this help message\n"
                            "  [cyan]/stats[/cyan] - Display current player stats\n"
                            "  [cyan]/save[/cyan]  - Overwrite your .player file with your current character sheet (active effects are cleared/reverted)\n"
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
                    elif cmd == '/save':
                        result = await session.call_tool("dump_player_db", arguments={})
                        if hasattr(result, 'content') and result.content:
                            text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
                            try:
                                db_data = json.loads(text)
                            except (json.JSONDecodeError, TypeError):
                                db_data = {}
                            buff_data = db_data.get("_active_buff_data", {})
                            if isinstance(buff_data, str):
                                try:
                                    buff_data = json.loads(buff_data)
                                except (json.JSONDecodeError, TypeError):
                                    buff_data = {}
                            cleared = []
                            for spell_name, entries in buff_data.items():
                                for entry in entries:
                                    field = entry["field"]
                                    delta = entry["delta"]
                                    if field == "temporary_hit_points":
                                        db_data[field] = 0
                                    else:
                                        current_val = db_data.get(field, 0)
                                        if isinstance(current_val, str):
                                            try:
                                                current_val = int(current_val)
                                            except (ValueError, TypeError):
                                                continue
                                        db_data[field] = current_val - delta
                                cleared.append(spell_name)
                            db_data["active_effects"] = []
                            db_data["_active_buff_data"] = {}
                            with open(player_path, "w", encoding="utf-8") as f:
                                json.dump(db_data, f, indent=2)
                            msg = f"[green]Character sheet saved to {player_path}[/green]"
                            if cleared:
                                msg += f"\n[dim]Reverted effects for save: {', '.join(cleared)}[/dim]"
                            console.print(Panel(msg, border_style="green", expand=False))
                        else:
                            console.print("[red]Save failed — could not read database.[/red]")
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

                if image_gen_fn and image_frequency > 0 and response_text and response_text != "__SYSTEM_PAUSE__":
                    clean = response_text.replace("{{_NEED_AN_OTHER_PROMPT}}", "").replace("{{_NEED_ANOTHER_PROMPT}}", "").strip()
                    if clean:
                        narrative_counter += 1
                        if narrative_counter % image_frequency == 0:
                            await _auto_generate_image(clean)

                console.print(Panel(
                    Padding(render_gm_text(response_text), (1, 1)),
                    title="[bold magenta]The Game Master Awakens[/bold magenta]",
                    border_style="magenta"
                ))

                console.print("\n[bold cyan]--- Game Started. Type /help for commands. ---[/bold cyan]\n")

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

                        if image_gen_fn and image_frequency > 0 and clean_response:
                            narrative_counter += 1
                            if narrative_counter % image_frequency == 0:
                                await _auto_generate_image(clean_response)

                        if clean_response:
                            console.print(Panel(
                                Padding(render_gm_text(clean_response), (1, 1)),
                                title="[bold magenta]Game Master[/bold magenta]",
                                border_style="magenta"
                            ))
                            console.print("\n")

                        # ── Timeline checkpoint ────────────────────────
                        round_counter += 1
                        if round_counter % TIMELINE_INTERVAL == 0:
                            if VERBOSE:
                                console.print(f"\n[dim]⏳ Timeline checkpoint (round {round_counter})...[/dim]")
                            try:
                                # Calculate round range for this entry
                                start_round = round_counter - TIMELINE_INTERVAL + 1
                                prompt = TIMELINE_PROMPT.replace("X-Y", f"{start_round}-{round_counter}")
                                tl_response = await chat_with_tools(prompt)
                                if tl_response and tl_response != "__SYSTEM_PAUSE__":
                                    # Clean up sync tokens from timeline entry
                                    entry = tl_response.replace("{{_NEED_AN_OTHER_PROMPT}}", "").replace("{{_NEED_ANOTHER_PROMPT}}", "").strip()
                                    if entry and "**Key Events**" in entry:
                                        append_timeline_file(timeline_path, entry)
                                        if VERBOSE:
                                            console.print(f"[dim]✅ Timeline saved ({len(entry)} chars)[/dim]")
                                    elif VERBOSE:
                                        console.print(f"[dim]⚠️ Timeline entry missing Key Events — skipped[/dim]")
                            except Exception as e:
                                if VERBOSE:
                                    console.print(f"[dim]⚠️ Timeline checkpoint failed: {e}[/dim]")


    except KeyboardInterrupt:
        console.print("\n[yellow]Game interrupted. Goodbye.[/yellow]")
    except Exception as e:
        import traceback
        traceback.print_exc()
        console.print(f"\n[bold red]Fatal error: {e}[/bold red]")
        console.print("[dim]The game session has ended unexpectedly.[/dim]")