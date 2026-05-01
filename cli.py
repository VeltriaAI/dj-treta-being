#!/usr/bin/env python3
"""
DJ Treta CLI — Talk to the DJ, control the decks, feel the music.

Usage:
    python cli.py              # Interactive mode
    python cli.py status       # Quick status check
    python cli.py talk "msg"   # One-shot talk to brain
"""

import json
import os
import readline
import signal
import sys
import threading
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.layout import Layout
from rich.columns import Columns
from rich.markdown import Markdown
from rich.rule import Rule
from agent.runtime_paths import runtime_path

# ── Config ────────────────────────────────────────────────────────────

MIXXX_URL = "http://localhost:7778"
STATE_FILE = runtime_path("state.json")
COMMAND_FILE = runtime_path("command.json")
DAEMON_LOG = runtime_path("daemon.log")
MUSIC_DIR = Path.home() / "Music" / "DJTreta"

console = Console()

# ── Mixxx Client ──────────────────────────────────────────────────────

def mixxx_get(path: str) -> dict | None:
    try:
        r = httpx.get(f"{MIXXX_URL}{path}", timeout=2)
        return r.json()
    except Exception:
        return None

def mixxx_post(path: str, data: dict) -> dict | None:
    try:
        r = httpx.post(f"{MIXXX_URL}{path}", json=data, timeout=2)
        return r.json()
    except Exception:
        return None

def read_daemon_state() -> dict | None:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return None

# ── Display Helpers ───────────────────────────────────────────────────

KEY_MAP = {
    1: "C", 2: "Db", 3: "D", 4: "Eb", 5: "E", 6: "F",
    7: "F#", 8: "G", 9: "Ab", 10: "A", 11: "Bb", 12: "B",
    13: "Cm", 14: "C#m", 15: "Dm", 16: "Ebm", 17: "Em", 18: "Fm",
    19: "F#m", 20: "Gm", 21: "G#m", 22: "Am", 23: "Bbm", 24: "Bm",
}

def format_time(seconds) -> str:
    if isinstance(seconds, str):
        return seconds  # "infinite" etc
    if seconds <= 0:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"

def format_key(key_num: int) -> str:
    return KEY_MAP.get(key_num, "?")

def energy_bar(energy: float, width: int = 20) -> str:
    filled = int(energy / 10 * width)
    return "█" * filled + "░" * (width - filled)

def make_deck_panel(deck: dict, deck_num: int, is_active: bool) -> Panel:
    if not deck.get("track_loaded"):
        return Panel("[dim]empty[/dim]", title=f"Deck {deck_num}", border_style="dim")

    bpm = deck.get("bpm", 0)
    key = format_key(deck.get("key", 0))
    pos = deck.get("position_seconds", 0)
    dur = deck.get("duration", 0)
    remaining = deck.get("remaining_seconds", 0)
    playing = deck.get("playing", False)
    synced = deck.get("sync_enabled", False)
    vol = deck.get("volume", 0)

    # Progress bar
    progress_pct = pos / dur if dur > 0 else 0
    bar_width = 30
    filled = int(progress_pct * bar_width)
    bar = "━" * filled + "●" + "─" * (bar_width - filled - 1)

    status_icon = "▶" if playing else "⏸"
    sync_icon = " SYNC" if synced else ""
    active_marker = " ★" if is_active else ""

    lines = [
        f"  {status_icon} {format_time(pos)} [{bar}] {format_time(dur)}",
        f"  BPM: [bold]{bpm:.0f}[/bold]  Key: [bold]{key}[/bold]  Vol: {vol:.0f}%{sync_icon}",
        f"  Remaining: [bold]{format_time(remaining)}[/bold]",
    ]

    color = "cyan" if deck_num == 1 else "magenta"
    if is_active:
        color = "bold " + color

    return Panel(
        "\n".join(lines),
        title=f"Deck {deck_num}{active_marker}",
        border_style=color,
    )

def make_crossfader(xf: float) -> str:
    """Visual crossfader. -1 = Deck 1, +1 = Deck 2."""
    width = 30
    pos = int((xf + 1) / 2 * width)
    pos = max(0, min(width, pos))
    bar = "─" * pos + "◆" + "─" * (width - pos)
    return f"  D1 [{bar}] D2"

def make_status_display() -> Panel:
    status = mixxx_get("/api/status")
    if not status:
        return Panel("[red]Mixxx not responding[/red]", title="DJ Treta", border_style="red")

    state = read_daemon_state()
    d1 = status.get("deck1", {})
    d2 = status.get("deck2", {})
    xf = status.get("crossfader", 0)

    # Determine active deck
    if d1.get("playing") and not d2.get("playing"):
        active = 1
    elif d2.get("playing") and not d1.get("playing"):
        active = 2
    elif xf < -0.3:
        active = 1
    else:
        active = 2

    deck1_panel = make_deck_panel(d1, 1, active == 1)
    deck2_panel = make_deck_panel(d2, 2, active == 2)
    crossfader = make_crossfader(xf)

    # Brain status
    brain_lines = []
    if state:
        phase = state.get("phase", "?")
        mood = state.get("mood", "?")
        played = state.get("tracks_played", 0)
        elapsed = state.get("set_elapsed", 0)
        remaining = state.get("set_remaining", 0)

        phase_colors = {
            "playing": "green", "preparing": "yellow",
            "transitioning": "blue", "recovery": "red",
            "starting": "yellow", "stopped": "dim",
        }
        phase_color = phase_colors.get(phase, "white")

        brain_lines.append(f"  Brain: [{phase_color}]{phase.upper()}[/{phase_color}]  Mood: [bold]{mood}[/bold]")
        brain_lines.append(f"  Set: {format_time(elapsed)} elapsed, {format_time(remaining)} remaining  Tracks: {played}")

        if state.get("next_track"):
            nt = state["next_track"]
            brain_lines.append(f"  Next: [italic]{nt.get('title', '?')}[/italic]")

        last_result = state.get("last_command_result", "")
        if last_result and last_result != "processing...":
            # Truncate long results
            if len(last_result) > 80:
                last_result = last_result[:80] + "..."
            brain_lines.append(f"  Last: [dim]{last_result}[/dim]")
    else:
        brain_lines.append("  [dim]Brain not running. Start with: /start[/dim]")

    brain_text = "\n".join(brain_lines)

    # Compose
    content = Table.grid(padding=0)
    content.add_row(Columns([deck1_panel, deck2_panel], equal=True))
    content.add_row(Text(crossfader))
    content.add_row(Text(""))
    content.add_row(Text.from_markup(brain_text))

    return Panel(content, title="[bold]DJ Treta[/bold]", border_style="bright_white")

# ── Commands ──────────────────────────────────────────────────────────

def send_brain_command(command: str, args: dict = {}) -> str:
    """Send command to brain daemon and wait for response."""
    payload = {"command": command, "args": args}
    COMMAND_FILE.write_text(json.dumps(payload, indent=2))

    # Poll for response
    for _ in range(120):  # 60 seconds
        time.sleep(0.5)
        state = read_daemon_state()
        if state:
            result = state.get("last_command_result", "")
            if state.get("last_command") == command and result and result != "processing...":
                return result
    return "No response from brain (timeout)"

def cmd_status():
    """Show current status."""
    console.print(make_status_display())

def cmd_talk(message: str, readonly: bool = False):
    """Talk to the brain. readonly=True for live web listeners (no control)."""
    console.print(f"\n[bold cyan]You:[/bold cyan] {message}")
    console.print("[dim]thinking...[/dim]", end="\r")
    response = send_brain_command("talk", {"message": message, "readonly": readonly})
    # Clear "thinking..."
    console.print(" " * 40, end="\r")
    console.print(f"[bold magenta]DJ Treta:[/bold magenta] {response}\n")

def cmd_mood(mood: str):
    """Change mood."""
    response = send_brain_command("change_mood", {"mood": mood})
    console.print(f"[green]{response}[/green]")

def cmd_skip():
    """Skip current track."""
    response = send_brain_command("skip", {})
    console.print(f"[yellow]{response}[/yellow]")

def cmd_play(deck: int = 1):
    mixxx_post("/api/play", {"deck": deck})
    console.print(f"[green]▶ Deck {deck}[/green]")

def cmd_pause(deck: int = 1):
    mixxx_post("/api/pause", {"deck": deck})
    console.print(f"[yellow]⏸ Deck {deck}[/yellow]")

def cmd_tracks():
    """List library tracks."""
    table = Table(title="Track Library", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Genre", style="cyan", width=15)
    table.add_column("Track", style="white")

    i = 1
    for genre_dir in sorted(MUSIC_DIR.iterdir()):
        if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
            continue
        for f in sorted(genre_dir.iterdir()):
            if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                table.add_row(str(i), genre_dir.name, f.stem)
                i += 1

    console.print(table)

def cmd_load(deck: int, query: str):
    """Load a track by partial name match."""
    query_lower = query.lower()
    for genre_dir in sorted(MUSIC_DIR.iterdir()):
        if not genre_dir.is_dir():
            continue
        for f in sorted(genre_dir.iterdir()):
            if query_lower in f.stem.lower():
                result = mixxx_post("/api/load", {"deck": deck, "track": str(f)})
                console.print(f"[green]Loaded on Deck {deck}:[/green] {f.stem}")
                return
    console.print(f"[red]No track matching '{query}'[/red]")

def cmd_transition(deck: int = 2, technique: str = "blend", duration: int = 60):
    response = send_brain_command("transition_now", {
        "technique": technique, "duration": duration
    })
    console.print(f"[blue]{response}[/blue]")

def cmd_start_brain(mood: str = "melodic-techno", duration: int = 60):
    """Start the brain daemon."""
    import subprocess, shutil
    # Clear old log + bytecache before starting
    runtime_path("daemon.log").write_text("")
    for cache_dir in [Path(__file__).parent / "agent" / "__pycache__",
                      Path(__file__).parent / "agent" / "tools" / "__pycache__",
                      Path(__file__).parent / "__pycache__"]:
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
    venv_python = Path(__file__).parent / ".venv" / "bin" / "python3"
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    subprocess.Popen(
        [str(venv_python), "-m", "agent", "--mood", mood, "--duration", str(duration)],
        cwd=str(Path(__file__).parent),
        stdout=open(runtime_path("daemon.log"), "w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    console.print(f"[green]Brain started — mood: {mood}, duration: {duration}m[/green]")

def cmd_stop_brain():
    """Stop the brain daemon."""
    import subprocess
    subprocess.run(["pkill", "-f", "python.*-m agent"], capture_output=True)
    console.print("[yellow]Brain stopped[/yellow]")

def cmd_logs(n: int = 20):
    """Show recent daemon logs."""
    if DAEMON_LOG.exists():
        lines = DAEMON_LOG.read_text().strip().split("\n")
        # Filter to key events
        skip = ["LiteLLM", "Wrapper:", "completion() model", "─────", "│", "╭", "╰", "Observations:", "Step ", "Calling tool"]
        key_lines = []
        for l in lines:
            if not any(k in l for k in ["INFO", "WARNING", "ERROR"]):
                continue
            if any(s in l for s in skip):
                continue
            # Strip ANSI and check if there's actual content after timestamp
            clean = l.strip()
            # Skip lines that are just "HH:MM:SS [INFO] " with nothing after
            parts = clean.split("] ", 1)
            if len(parts) < 2 or not parts[1].strip():
                continue
            key_lines.append(l)
        for line in key_lines[-n:]:
            if "ERROR" in line or "WARNING" in line:
                console.print(f"[red]{line}[/red]")
            elif "Playing:" in line or "Next:" in line:
                console.print(f"[green]{line}[/green]")
            elif "Transition" in line:
                console.print(f"[blue]{line}[/blue]")
            else:
                console.print(f"[dim]{line}[/dim]")
    else:
        console.print("[dim]No daemon log found[/dim]")

def cmd_live():
    """Live updating status display."""
    console.print("[dim]Live mode — press Ctrl+C to exit[/dim]\n")
    try:
        while True:
            console.clear()
            console.print(make_status_display())
            console.print("\n[dim]Live mode — Ctrl+C to exit | refreshing every 2s[/dim]")
            time.sleep(2)
    except KeyboardInterrupt:
        console.print("\n[dim]Exited live mode[/dim]")

def cmd_help():
    """Show help."""
    help_text = """
[bold]DJ Treta CLI[/bold]

[bold cyan]Talk to the DJ:[/bold cyan]
  [bold]<message>[/bold]              Talk to DJ Treta brain (anything goes)

[bold cyan]Slash Commands:[/bold cyan]
  [bold]/status[/bold]                Show current deck status
  [bold]/live[/bold]                  Live updating display (2s refresh)
  [bold]/mood[/bold] <mood>           Change mood (dark-techno, melodic-techno, deep, progressive, etc.)
  [bold]/skip[/bold]                  Skip to next track
  [bold]/tracks[/bold]               List all tracks in library
  [bold]/load[/bold] <deck> <query>   Load track by name search onto deck
  [bold]/play[/bold] [deck]           Play deck (default: 1)
  [bold]/pause[/bold] [deck]          Pause deck (default: 1)
  [bold]/transition[/bold] [tech] [s] Start transition (blend/bass_swap/filter_sweep, duration)
  [bold]/start[/bold] [mood] [min]    Start brain daemon
  [bold]/stop[/bold]                  Stop brain daemon
  [bold]/logs[/bold] [n]              Show last n daemon log lines
  [bold]/help[/bold]                  This help
  [bold]/quit[/bold]                  Exit

[dim]Anything without / is sent to the brain as conversation.[/dim]
"""
    console.print(help_text)

# ── Main Loop ─────────────────────────────────────────────────────────

def _daemon_cmd(action):
    """Start/stop/restart the Being daemon."""
    import subprocess
    DJ_HOME = Path(__file__).parent
    PYTHON = DJ_HOME / ".venv" / "bin" / "python3"
    PID_FILE = runtime_path("dj-treta.pid")
    LOG = runtime_path("daemon.log")

    if action in ("stop", "restart"):
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, 15)  # SIGTERM
                # Wait for process to actually die before continuing
                import time
                for _ in range(30):  # 3s max
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)  # check if alive
                    except ProcessLookupError:
                        break  # dead
                console.print(f"[yellow]Stopped (PID {pid})[/yellow]")
                PID_FILE.unlink(missing_ok=True)
            except (ProcessLookupError, ValueError):
                PID_FILE.unlink(missing_ok=True)
        else:
            if action == "stop":
                console.print("[dim]Not running[/dim]")
                return
        if action == "restart":
            import time
            time.sleep(1)

    if action in ("start", "restart"):
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, 0)
                console.print(f"[yellow]Already running (PID {pid})[/yellow]")
                return
            except (ProcessLookupError, ValueError):
                PID_FILE.unlink(missing_ok=True)

        PID_FILE.unlink(missing_ok=True)
        # Truncate log AFTER old daemon is dead — clean slate for TUI
        LOG.write_text("")
        # PYTHONUNBUFFERED=1 — without it, stdout block-buffers when redirected
        # to a regular file, so daemon.log stays empty for hours despite the
        # agent logging actively. Killed our visibility on 2026-04-30.
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        subprocess.Popen(
            [str(PYTHON), "-m", "agent"],
            cwd=str(DJ_HOME),
            stdout=open(str(LOG), "a"),  # append — don't clobber our truncation
            stderr=subprocess.STDOUT,
            env=env,
        )
        import time
        time.sleep(2)
        if PID_FILE.exists():
            console.print(f"[green]Started (PID {PID_FILE.read_text().strip()})[/green]")
        else:
            console.print("[green]Starting...[/green]")
        console.print("[dim]Talk to her: djtreta talk 'play something melodic'[/dim]")


def _kill_all():
    """Kill everything — daemon, Mixxx, LiteLLM."""
    import subprocess
    subprocess.run(["pkill", "-f", "python.*agent"], capture_output=True)
    subprocess.run(["pkill", "-f", "mixxx"], capture_output=True)
    subprocess.run(["pkill", "-f", "litellm"], capture_output=True)
    runtime_path("dj-treta.pid").unlink(missing_ok=True)
    console.print("[yellow]Killed: daemon, Mixxx, LiteLLM[/yellow]")


def _reset(hard=False):
    """Reset state. Soft = keep library + DB. Hard = delete everything."""
    import shutil
    import subprocess

    mode = "hard" if hard else "soft"
    console.print(f"[yellow]Resetting ({mode})...[/yellow]")

    # Kill daemon + Mixxx
    subprocess.run(["pkill", "-f", "python.*agent"], capture_output=True)
    subprocess.run(["pkill", "-f", "mixxx"], capture_output=True)

    # Clean state files (always). Includes all in-flight signal files —
    # otherwise a fresh daemon can pick up stale scheduled transitions,
    # directives, or mood-changes from the prior run.
    for name in ["state.json", "command.json", "thinking.log", "daemon.log",
                 "dj-treta.pid", "billing.json", "playlist.json",
                 "scheduled-transition.json", "transition-pending.lock",
                 "directives.json", "mood-change.json", "mood.txt",
                 "being-heartbeat.json"]:
        runtime_path(name).unlink(missing_ok=True)

    # Clean session + bytecache (always)
    DJ_HOME = Path.home() / "beings" / "dj-treta"
    (DJ_HOME / ".beings" / "session.json").unlink(missing_ok=True)
    import shutil as _shutil
    for cache_dir in [DJ_HOME / "agent" / "__pycache__", DJ_HOME / "agent" / "tools" / "__pycache__", DJ_HOME / "__pycache__"]:
        if cache_dir.exists():
            _shutil.rmtree(cache_dir)

    music_dir = Path.home() / "Music" / "DJTreta"

    if hard:
        # Nuclear — delete library + DB, but PRESERVE the knowledge dir
        # (~/Music/DJTreta/knowledge/ holds the 5 GB v4 parquet + 4.5 GB
        # LanceDB index — re-downloading + rebuilding takes ~5 min and
        # bandwidth, so we only delete the per-genre MP3 subdirs.)
        if music_dir.exists():
            for child in music_dir.iterdir():
                if child.name == "knowledge":
                    continue  # preserve knowledge cache + LanceDB
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
        for genre in ["melodic-techno", "dark-techno", "deep", "progressive",
                      "minimal", "vocal", "psychill", "psytrance", "techno"]:
            (music_dir / genre).mkdir(parents=True, exist_ok=True)
        # Nuke BOTH possible DB locations: repo-local (dev) and XDG (installer).
        # agent/db.py prefers repo-local but falls back to XDG, so leaving XDG
        # behind = stale tracks that point to deleted files = stuck planner.
        (DJ_HOME / "djtreta.db").unlink(missing_ok=True)
        xdg_db = Path.home() / ".local" / "share" / "djclaw" / "db" / "djtreta.db"
        for sidecar in (xdg_db, xdg_db.with_suffix(".db-shm"), xdg_db.with_suffix(".db-wal")):
            sidecar.unlink(missing_ok=True)
        console.print("[green]Hard reset complete.[/green]")
        console.print(f"  Library: deleted")
        console.print(f"  Database: deleted (repo + XDG)")
    else:
        # Soft — keep library + DB
        track_count = sum(1 for _ in music_dir.rglob("*.mp3")) if music_dir.exists() else 0
        console.print("[green]Soft reset complete.[/green]")
        console.print(f"  Library: {track_count} tracks (kept)")
        console.print(f"  Database: kept")

    console.print(f"  State: cleared")
    console.print(f"  Billing: cleared")
    console.print(f"[dim]  djtreta start to begin fresh[/dim]")


def cmd_logs_follow():
    """Tail -f the daemon log — full raw output, no filtering."""
    log_file = runtime_path("daemon.log")
    if not log_file.exists():
        console.print("[dim]No daemon log found[/dim]")
        return

    console.print("[dim]Following daemon log (Ctrl+C to stop)...[/dim]\n")
    try:
        import subprocess
        subprocess.run(["tail", "-f", str(log_file)])
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped[/dim]")


BANNER = """[bold bright_white]
  ╔══════════════════════════════════════╗
  ║          [bold cyan]DJ Treta[/bold cyan] [dim]v1.0[/dim]              ║
  ║    [dim]An AI Being that DJs.[/dim]           ║
  ╚══════════════════════════════════════╝
[/bold bright_white]"""

def _parse_remote_args(argv: list[str]) -> tuple[bool, str | None, str | None, list[str]]:
    """Strip --remote / --token from argv. Returns (remote_on, url, token, leftover).

    Accepts:
        --remote                 → default URL
        --remote URL             → explicit URL
        --token TOK              → bearer token
    The flags may appear anywhere; leftover preserves order of unrelated args.
    """
    remote_on = False
    url: str | None = None
    token: str | None = None
    rest: list[str] = []

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--remote":
            remote_on = True
            # Peek: if next token looks like a URL, consume it.
            # Accept http/https (legacy MCP SSE) and ws/wss (current
            # /ws/state WebSocket transport).
            if i + 1 < len(argv) and argv[i + 1].startswith(
                ("http://", "https://", "ws://", "wss://")
            ):
                url = argv[i + 1]
                i += 2
            else:
                i += 1
        elif a.startswith("--remote="):
            remote_on = True
            url = a.split("=", 1)[1] or None
            i += 1
        elif a == "--token":
            if i + 1 < len(argv):
                token = argv[i + 1]
                i += 2
            else:
                i += 1
        elif a.startswith("--token="):
            token = a.split("=", 1)[1] or None
            i += 1
        else:
            rest.append(a)
            i += 1
    return remote_on, url, token, rest


def main():
    # Extract global --remote / --token flags first (can appear anywhere).
    remote_on, remote_url, remote_token, remaining = _parse_remote_args(sys.argv[1:])
    sys.argv = [sys.argv[0]] + remaining

    # If --remote is set without any subcommand, launch the TUI in remote mode.
    if remote_on and not remaining:
        from tui import main as tui_main
        tui_main(remote=True, remote_url=remote_url, remote_token=remote_token)
        return

    # One-shot commands
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "status":
            cmd_status()
            return
        elif cmd in ("ui", "tui"):
            from tui import main as tui_main
            tui_main(remote=remote_on, remote_url=remote_url, remote_token=remote_token)
            return
        elif cmd == "live":
            cmd_live()
            return
        elif cmd == "talk" and len(sys.argv) > 2:
            cmd_talk(" ".join(sys.argv[2:]))
            return
        elif cmd == "logs":
            args = sys.argv[2:]
            if args and args[0] in ("-f", "--follow", "follow", "tail"):
                cmd_logs_follow()
            else:
                n = int(args[0]) if args else 20
                cmd_logs(n)
            return
        elif cmd == "start":
            # djtreta start [mood] — e.g., djtreta start "dark melodic techno"
            mood_args = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
            if mood_args:
                runtime_path("mood.txt").write_text(mood_args)
            _daemon_cmd("start")
            return
        elif cmd == "stop":
            _daemon_cmd("stop")
            return
        elif cmd == "restart":
            _daemon_cmd("restart")
            return
        elif cmd == "reset":
            hard = "--hard" in sys.argv[2:] or "hard" in sys.argv[2:]
            _reset(hard=hard)
            return
        elif cmd == "kill":
            _kill_all()
            return
        elif cmd == "init":
            from agent.init import run_init
            run_init()
            return
        elif cmd in ("doctor", "validate-config", "validate", "check"):
            from agent.validate import main as validate_main
            sys.exit(validate_main())
        elif cmd == "setup":
            from agent.setup_wizard import main as setup_main
            sys.exit(setup_main())

    # Interactive mode
    console.print(BANNER)
    cmd_status()
    console.print()
    console.print("[dim]Type a message to talk to DJ Treta, or /help for commands[/dim]\n")

    while True:
        try:
            user_input = input("\033[1;36m❯ \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Later.[/dim]")
            break

        if not user_input:
            continue

        # Slash commands
        if user_input.startswith("/"):
            parts = user_input[1:].split()
            cmd = parts[0].lower() if parts else ""
            args = parts[1:]

            if cmd in ("quit", "exit", "q"):
                console.print("[dim]Later.[/dim]")
                break
            elif cmd == "help":
                cmd_help()
            elif cmd == "status":
                cmd_status()
            elif cmd == "live":
                cmd_live()
            elif cmd == "mood" and args:
                cmd_mood(args[0])
            elif cmd == "skip":
                cmd_skip()
            elif cmd == "tracks":
                cmd_tracks()
            elif cmd == "load" and len(args) >= 2:
                cmd_load(int(args[0]), " ".join(args[1:]))
            elif cmd == "play":
                cmd_play(int(args[0]) if args else 1)
            elif cmd == "pause":
                cmd_pause(int(args[0]) if args else 1)
            elif cmd == "transition":
                tech = args[0] if args else "blend"
                dur = int(args[1]) if len(args) > 1 else 60
                cmd_transition(technique=tech, duration=dur)
            elif cmd == "start":
                mood = args[0] if args else "melodic-techno"
                dur = int(args[1]) if len(args) > 1 else 60
                cmd_start_brain(mood, dur)
            elif cmd == "stop":
                cmd_stop_brain()
            elif cmd == "logs":
                n = int(args[0]) if args else 20
                cmd_logs(n)
            else:
                console.print(f"[red]Unknown command: /{cmd}[/red] — try /help")
        else:
            # Everything else is a message to the brain
            cmd_talk(user_input)


if __name__ == "__main__":
    main()
