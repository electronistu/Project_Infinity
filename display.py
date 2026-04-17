import json
import re
from rich.panel import Panel
from rich.console import Console

console = Console()


def _stat_modifier(score):
    mod = (score - 10) // 2
    if mod >= 0:
        return f"+{mod}"
    return str(mod)


def render_gm_text(text):
    processed = text
    processed = re.sub(r'\*\*(.*?)\*\*', r'[bold white]\1[/]', processed)
    processed = re.sub(r'\*(.*?)\*', r'[italic cyan]\1[/]', processed)
    lines = processed.split('\n')
    for i in range(len(lines)):
        if lines[i].startswith('#'):
            content = lines[i].strip('#').strip()
            lines[i] = f"[bold magenta]{content}[/]"
    return "\n".join(lines)


def _render_inventory_item(item):
    if isinstance(item, dict):
        name = item.get('name', str(item))
        desc = item.get('description', '')
        if desc:
            return f"  [bold white]{name}[/]\n    [dim]{desc}[/]"
        return f"  [bold white]{name}[/]"
    return f"  [bold white]{item}[/]"


def format_stats(db_data):
    def get(key, default=""):
        val = db_data.get(key, default)
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                return parsed
            except (json.JSONDecodeError, TypeError):
                return val
        return val

    hp_cur = get('current_hit_points', '?')
    hp_max = get('total_hit_points', '?')
    ac = get('armor_class', '?')
    speed = get('speed', '?')
    prof = get('proficiency_bonus', '?')
    hd_count = get('hit_dice_count', '?')
    hd_size = get('hit_dice_size', '?')

    # Character panel
    char_lines = []
    char_lines.append(f"🧙 [bold white]{get('name')}[/]  🧝 {get('race')}  📖 {get('character_class')}")
    char_lines.append(f"⭐ Level {get('level')}  💰 Gold {get('gold')}  ✨ XP {get('xp')}")
    char_lines.append(f"🎭 {get('background')}  ⚖️ {get('alignment')}")
    char_stats = Panel("\n".join(char_lines), title="⚔️ Character", border_style="red", expand=False)

    # Combat panel
    combat_lines = []
    combat_lines.append(f"❤️ {hp_cur}/{hp_max} HP  🛡️ AC {ac}  🏃 Speed {speed}")
    combat_lines.append(f"⭐ Proficiency +{prof}  🎲 Hit Dice {hd_count}d{hd_size}")
    combat_panel = Panel("\n".join(combat_lines), title="🛡️ Combat", border_style="blue", expand=False)

    # Stats panel
    stats_data = get('stats')
    stats_lines = []
    if isinstance(stats_data, dict):
        stats_lines.append(f"💪 STR {stats_data.get('str', '?')} ({_stat_modifier(stats_data.get('str', 10))})    🏹 DEX {stats_data.get('dex', '?')} ({_stat_modifier(stats_data.get('dex', 10))})")
        stats_lines.append(f"🫀 CON {stats_data.get('con', '?')} ({_stat_modifier(stats_data.get('con', 10))})    🧠 INT {stats_data.get('int', '?')} ({_stat_modifier(stats_data.get('int', 10))})")
        stats_lines.append(f"👁️ WIS {stats_data.get('wis', '?')} ({_stat_modifier(stats_data.get('wis', 10))})    ✨ CHA {stats_data.get('cha', '?')} ({_stat_modifier(stats_data.get('cha', 10))})")
    else:
        stats_lines.append(str(stats_data))
    stats_panel = Panel("\n".join(stats_lines), title="📊 Stats", border_style="green", expand=False)

    panels = [char_stats, combat_panel, stats_panel]

    # Spellcasting panel (conditional)
    spell_data = get('spellcasting')
    if isinstance(spell_data, dict):
        spell_lines = []
        spell_lines.append(f"🔮 {spell_data.get('ability', '?').capitalize()}  📿 DC {spell_data.get('dc', '?')}  🪄 Attack +{spell_data.get('attack_modifier', '?')}")
        cantrips = spell_data.get('cantrips', [])
        if cantrips:
            spell_lines.append(f"✋ {', '.join(str(c) for c in cantrips)}")
        spells = spell_data.get('spells', [])
        if spells:
            spell_lines.append(f"📜 {', '.join(str(s) for s in spells)}")
        slots = spell_data.get('slots', {})
        if slots:
            slot_parts = [f"Lv{k}: {v}" for k, v in slots.items()]
            spell_lines.append(f"🔥 {' | '.join(slot_parts)}")
        panels.append(Panel("\n".join(spell_lines), title="🔮 Spellcasting", border_style="magenta", expand=False))

    # Skills & Proficiencies panel
    prof_lines = []
    skills = get('skills')
    if isinstance(skills, list) and skills:
        prof_lines.append(f"🎯 Skills: {', '.join(str(s) for s in skills)}")
    saves = get('saves')
    if isinstance(saves, list) and saves:
        prof_lines.append(f"💫 Saves: {', '.join(str(s) for s in saves)}")
    armor_prof = get('armor_proficiencies')
    if isinstance(armor_prof, list) and armor_prof:
        prof_lines.append(f"🛡️ Armor: {', '.join(str(a) for a in armor_prof)}")
    weapon_prof = get('weapon_proficiencies')
    if isinstance(weapon_prof, list) and weapon_prof:
        prof_lines.append(f"⚔️ Weapons: {', '.join(str(w) for w in weapon_prof)}")
    tool_prof = get('tool_proficiencies')
    if isinstance(tool_prof, list) and tool_prof:
        prof_lines.append(f"🔧 Tools: {', '.join(str(t) for t in tool_prof)}")
    features = get('features')
    if isinstance(features, list) and features:
        prof_lines.append(f"🌟 Features: {', '.join(str(f) for f in features)}")
    languages = get('languages')
    if isinstance(languages, list) and languages:
        prof_lines.append(f"🗣️ Languages: {', '.join(str(l) for l in languages)}")

    if prof_lines:
        panels.append(Panel("\n".join(prof_lines), title="🎯 Skills & Proficiencies", border_style="yellow", expand=False))

    # Inventory & Consumables panel
    inv_lines = []
    inventory = get('inventory')
    if isinstance(inventory, list) and inventory:
        for item in inventory:
            inv_lines.append(_render_inventory_item(item))
    consumables = get('consumables')
    if isinstance(consumables, dict) and consumables:
        for name, qty in consumables.items():
            inv_lines.append(f"  🧪 [bold white]{name}[/]: {qty}")

    if inv_lines:
        panels.append(Panel("\n".join(inv_lines), title="🎒 Inventory & Consumables", border_style="cyan", expand=False))

    return panels