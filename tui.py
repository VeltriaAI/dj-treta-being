#!/usr/bin/env python3
"""
DJ Treta TUI — Full terminal UI. Decks always visible, brain activity live, conversation inline.

Usage:
    python tui.py
    djtreta ui
"""

import json
import os
import threading
import time
from pathlib import Path

import httpx
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import (
    Footer, Header, Input, Label, RichLog, Static,
)

# ── Config ────────────────────────────────────────────────────────────

MIXXX_URL = "http://localhost:7778"
STATE_FILE = Path("/tmp/dj-treta-state.json")
COMMAND_FILE = Path("/tmp/dj-treta-command.json")
DAEMON_LOG = Path("/tmp/dj-treta-daemon.log")
MUSIC_DIR = Path.home() / "Music" / "DJTreta"

KEY_MAP = {
    1: "C", 2: "Db", 3: "D", 4: "Eb", 5: "E", 6: "F",
    7: "F#", 8: "G", 9: "Ab", 10: "A", 11: "Bb", 12: "B",
    13: "Cm", 14: "C#m", 15: "Dm", 16: "Ebm", 17: "Em", 18: "Fm",
    19: "F#m", 20: "Gm", 21: "G#m", 22: "Am", 23: "Bbm", 24: "Bm",
}

CAMELOT_MAP = {
    "C": "8B", "Db": "3B", "D": "10B", "Eb": "5B", "E": "12B", "F": "7B",
    "F#": "2B", "G": "9B", "Ab": "4B", "A": "11B", "Bb": "6B", "B": "1B",
    "Cm": "5A", "C#m": "12A", "Dm": "7A", "Ebm": "2A", "Em": "9A", "Fm": "4A",
    "F#m": "11A", "Gm": "6A", "G#m": "1A", "Am": "8A", "Bbm": "3A", "Bm": "10A",
}


# ── Helpers ───────────────────────────────────────────────────────────

def mixxx_get(path: str) -> dict | None:
    try:
        return httpx.get(f"{MIXXX_URL}{path}", timeout=2).json()
    except Exception:
        return None

def mixxx_post(path: str, data: dict) -> dict | None:
    try:
        return httpx.post(f"{MIXXX_URL}{path}", json=data, timeout=2).json()
    except Exception:
        return None

def read_state() -> dict | None:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return None

def fmt_time(s: float) -> str:
    if s <= 0: return "0:00"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"

def fmt_key(k: int) -> str:
    name = KEY_MAP.get(k, "")
    cam = CAMELOT_MAP.get(name, "")
    return f"{name} ({cam})" if cam else name or "—"

def send_command(command: str, args: dict = {}) -> str:
    COMMAND_FILE.write_text(json.dumps({"command": command, "args": args}))
    for _ in range(120):
        time.sleep(0.5)
        state = read_state()
        if state and state.get("last_command") == command:
            result = state.get("last_command_result", "")
            if result and result != "processing...":
                return result
    return "No response (timeout)"


# ── Widgets ───────────────────────────────────────────────────────────

class DeckWidget(Static):
    """Single deck display."""

    def __init__(self, deck_num: int, **kwargs):
        super().__init__(**kwargs)
        self.deck_num = deck_num

    def update_deck(self, deck: dict, is_active: bool):
        if not deck.get("track_loaded"):
            self.update(f"[dim]Deck {self.deck_num} — empty[/dim]")
            return

        playing = deck.get("playing", False)
        bpm = deck.get("bpm", 0)
        key = fmt_key(deck.get("key", 0))
        pos = deck.get("position_seconds", 0)
        dur = deck.get("duration", 0)
        remaining = deck.get("remaining_seconds", 0)
        synced = deck.get("sync_enabled", False)

        # Progress bar
        pct = pos / dur if dur > 0 else 0
        w = 28
        filled = int(pct * w)
        bar = "━" * filled + "●" + "─" * max(0, w - filled - 1)

        icon = "▶" if playing else "⏸"
        sync_tag = " [green]SYNC[/green]" if synced else ""
        active_tag = " [bold yellow]★[/bold yellow]" if is_active else ""
        color = "cyan" if self.deck_num == 1 else "magenta"

        self.update(
            f"[bold {color}]Deck {self.deck_num}{active_tag}[/bold {color}]\n"
            f"  {icon} {fmt_time(pos)} [{bar}] {fmt_time(dur)}\n"
            f"  BPM [bold]{bpm:.0f}[/bold]  Key [bold]{key}[/bold]{sync_tag}\n"
            f"  Remaining [bold]{fmt_time(remaining)}[/bold]"
        )


class CrossfaderWidget(Static):
    """Crossfader visualization."""

    def update_xf(self, xf: float):
        w = 40
        pos = int((xf + 1) / 2 * w)
        pos = max(0, min(w, pos))
        bar = "─" * pos + "◆" + "─" * (w - pos)
        self.update(f"  [dim]D1[/dim] [{bar}] [dim]D2[/dim]")


class BrainWidget(Static):
    """Brain status."""

    def update_brain(self, state: dict | None):
        if not state:
            self.update("[dim]Brain offline — type /start to launch[/dim]")
            return

        phase = state.get("phase", "?")
        mood = state.get("mood", "?")
        played = state.get("tracks_played", 0)
        elapsed = state.get("set_elapsed", 0)
        remaining = state.get("set_remaining", 0)

        colors = {
            "playing": "green", "preparing": "yellow", "transitioning": "blue",
            "recovery": "red", "starting": "yellow", "stopped": "dim",
        }
        pc = colors.get(phase, "white")

        parts = [
            f"[{pc}]● {phase.upper()}[/{pc}]  Mood: [bold]{mood}[/bold]  Tracks: {played}  Set: {fmt_time(elapsed)} / {fmt_time(elapsed + remaining)}"
        ]

        nt = state.get("next_track")
        if nt:
            parts.append(f"  Next: [italic]{nt.get('title', '?')}[/italic]")

        self.update("\n".join(parts))


# ── Main App ──────────────────────────────────────────────────────────

CSS = """
Screen {
    layout: vertical;
}

#decks {
    height: 7;
    layout: horizontal;
}

#deck1, #deck2 {
    width: 1fr;
    height: 100%;
    padding: 0 1;
}

#crossfader {
    height: 1;
    text-align: center;
}

#brain {
    height: 3;
    padding: 0 1;
    border-top: solid $accent;
}

#conversation {
    height: 1fr;
    border-top: solid $accent;
    padding: 0 1;
}

#input-area {
    height: 3;
    dock: bottom;
    padding: 0 1;
}

#prompt-input {
    width: 100%;
}

Footer {
    height: 1;
}
"""


class DJTretaApp(App):
    """DJ Treta Terminal UI."""

    TITLE = "DJ Treta"
    SUB_TITLE = "An AI Being that DJs"
    CSS = CSS

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+l", "live_toggle", "Live"),
        Binding("ctrl+s", "skip", "Skip"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="decks"):
            yield DeckWidget(1, id="deck1")
            yield DeckWidget(2, id="deck2")
        yield CrossfaderWidget(id="crossfader")
        yield BrainWidget(id="brain")
        yield RichLog(id="conversation", highlight=True, markup=True, wrap=True)
        yield Input(placeholder="Talk to DJ Treta... (type message or /help)", id="prompt-input")
        yield Footer()

    def on_mount(self) -> None:
        self.log_widget = self.query_one("#conversation", RichLog)
        self.log_widget.write("[dim]Welcome to DJ Treta. Type anything to talk, or /help for commands.[/dim]\n")
        # Start polling
        self.set_interval(2.0, self.refresh_status)
        # Show initial status
        self.refresh_status()
        # Track daemon log position
        self._log_pos = 0
        self.set_interval(3.0, self.poll_daemon_log)

    def refresh_status(self) -> None:
        status = mixxx_get("/api/status")
        state = read_state()

        deck1_w = self.query_one("#deck1", DeckWidget)
        deck2_w = self.query_one("#deck2", DeckWidget)
        xf_w = self.query_one("#crossfader", CrossfaderWidget)
        brain_w = self.query_one("#brain", BrainWidget)

        if status:
            d1 = status.get("deck1", {})
            d2 = status.get("deck2", {})
            xf = status.get("crossfader", 0)

            if d1.get("playing") and not d2.get("playing"):
                active = 1
            elif d2.get("playing") and not d1.get("playing"):
                active = 2
            elif xf < -0.3:
                active = 1
            else:
                active = 2

            deck1_w.update_deck(d1, active == 1)
            deck2_w.update_deck(d2, active == 2)
            xf_w.update_xf(xf)
        else:
            deck1_w.update("[red]Mixxx offline[/red]")
            deck2_w.update("")
            xf_w.update("[red]No connection[/red]")

        brain_w.update_brain(state)

    def poll_daemon_log(self) -> None:
        """Check for new daemon log entries and show key events."""
        if not DAEMON_LOG.exists():
            return
        try:
            content = DAEMON_LOG.read_text()
            lines = content.split("\n")
            new_lines = lines[self._log_pos:]
            self._log_pos = len(lines)

            skip = ["LiteLLM", "Wrapper:", "completion() model", "─", "│", "╭", "╰",
                    "Observations:", "Step ", "Calling tool", "Final answer:", "TRACK:", "TECHNIQUE:", "ENERGY:"]

            for line in new_lines:
                if not line.strip():
                    continue
                if not any(k in line for k in ["INFO", "WARNING", "ERROR"]):
                    continue
                if any(s in line for s in skip):
                    continue
                parts = line.strip().split("] ", 1)
                if len(parts) < 2 or not parts[1].strip():
                    continue

                msg = parts[1].strip()
                if "ERROR" in line or "WARNING" in line:
                    self.log_widget.write(f"[red]⚠ {msg}[/red]")
                elif "Playing:" in msg:
                    self.log_widget.write(f"[green]🎵 {msg}[/green]")
                elif "Next:" in msg:
                    self.log_widget.write(f"[cyan]⏭ {msg}[/cyan]")
                elif "Transition" in msg:
                    self.log_widget.write(f"[blue]🔄 {msg}[/blue]")
                elif "preparing" in msg.lower():
                    self.log_widget.write(f"[yellow]🧠 {msg}[/yellow]")
                elif "sync" in msg.lower():
                    self.log_widget.write(f"[magenta]🔗 {msg}[/magenta]")
        except Exception:
            pass

    @on(Input.Submitted, "#prompt-input")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()

        if text.startswith("/"):
            self.handle_command(text)
        else:
            self.handle_talk(text)

    def handle_command(self, text: str) -> None:
        parts = text[1:].split()
        cmd = parts[0].lower() if parts else ""
        args = parts[1:]

        if cmd == "help":
            self.log_widget.write(
                "[bold]Commands:[/bold]\n"
                "  [cyan]<message>[/cyan]        Talk to DJ Treta\n"
                "  [cyan]/mood[/cyan] <name>     Change mood (dark-techno, melodic-techno, deep, progressive, psychill, vocal)\n"
                "  [cyan]/skip[/cyan]            Skip to next track\n"
                "  [cyan]/play[/cyan] [deck]     Play deck\n"
                "  [cyan]/pause[/cyan] [deck]    Pause deck\n"
                "  [cyan]/load[/cyan] <d> <name> Load track by name\n"
                "  [cyan]/tracks[/cyan]          List library\n"
                "  [cyan]/start[/cyan] [mood]    Start brain\n"
                "  [cyan]/stop[/cyan]            Stop brain\n"
                "  [cyan]/help[/cyan]            This help\n"
                "  [dim]Ctrl+Q quit | Ctrl+S skip[/dim]\n"
            )
        elif cmd == "mood" and args:
            self.log_widget.write(f"[yellow]Changing mood to {args[0]}...[/yellow]")
            self.run_brain_command("change_mood", {"mood": args[0]})
        elif cmd == "skip":
            self.action_skip()
        elif cmd == "play":
            deck = int(args[0]) if args else 1
            mixxx_post("/api/play", {"deck": deck})
            self.log_widget.write(f"[green]▶ Deck {deck}[/green]")
        elif cmd == "pause":
            deck = int(args[0]) if args else 1
            mixxx_post("/api/pause", {"deck": deck})
            self.log_widget.write(f"[yellow]⏸ Deck {deck}[/yellow]")
        elif cmd == "load" and len(args) >= 2:
            deck = int(args[0])
            query = " ".join(args[1:]).lower()
            self.load_track(deck, query)
        elif cmd == "tracks":
            self.show_tracks()
        elif cmd == "start":
            mood = args[0] if args else "melodic-techno"
            dur = int(args[1]) if len(args) > 1 else 60
            self.start_brain(mood, dur)
        elif cmd == "stop":
            self.stop_brain()
        else:
            self.log_widget.write(f"[red]Unknown: {text}[/red] — try /help")

    @work(thread=True)
    def handle_talk(self, message: str) -> None:
        self.log_widget.write(f"\n[bold cyan]You:[/bold cyan] {message}")
        self.log_widget.write("[dim]thinking...[/dim]")
        response = send_command("talk", {"message": message})
        self.log_widget.write(f"[bold magenta]DJ Treta:[/bold magenta] {response}\n")

    @work(thread=True)
    def run_brain_command(self, cmd: str, args: dict) -> None:
        response = send_command(cmd, args)
        self.log_widget.write(f"[green]{response}[/green]")

    def load_track(self, deck: int, query: str):
        for genre_dir in sorted(MUSIC_DIR.iterdir()):
            if not genre_dir.is_dir():
                continue
            for f in sorted(genre_dir.iterdir()):
                if query in f.stem.lower():
                    mixxx_post("/api/load", {"deck": deck, "track": str(f)})
                    self.log_widget.write(f"[green]Loaded on Deck {deck}:[/green] {f.stem}")
                    return
        self.log_widget.write(f"[red]No track matching '{query}'[/red]")

    def show_tracks(self):
        lines = ["[bold]Library:[/bold]"]
        for genre_dir in sorted(MUSIC_DIR.iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
                continue
            tracks = [f.stem for f in sorted(genre_dir.iterdir()) if f.suffix.lower() in ('.mp3', '.wav', '.flac')]
            lines.append(f"\n[cyan]{genre_dir.name}[/cyan] ({len(tracks)})")
            for t in tracks:
                lines.append(f"  [dim]•[/dim] {t}")
        self.log_widget.write("\n".join(lines))

    def start_brain(self, mood: str, duration: int):
        import subprocess
        venv = Path(__file__).parent / ".venv" / "bin" / "python3"
        subprocess.Popen(
            [str(venv), "-m", "agent", "--mood", mood, "--duration", str(duration)],
            cwd=str(Path(__file__).parent),
            stdout=open("/tmp/dj-treta-daemon.log", "w"),
            stderr=subprocess.STDOUT,
        )
        self._log_pos = 0
        self.log_widget.write(f"[green]Brain started — mood: {mood}, duration: {duration}m[/green]")

    def stop_brain(self):
        import subprocess
        subprocess.run(["pkill", "-f", "python.*-m agent"], capture_output=True)
        self.log_widget.write("[yellow]Brain stopped[/yellow]")

    def action_skip(self):
        self.log_widget.write("[yellow]Skipping...[/yellow]")
        self.run_brain_command("skip", {})

    def action_live_toggle(self):
        self.refresh_status()


def main():
    app = DJTretaApp()
    app.run()


if __name__ == "__main__":
    main()
