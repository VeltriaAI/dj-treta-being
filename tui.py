#!/usr/bin/env python3
"""
DJ Treta TUI — Full DJ console in the terminal.

Usage:
    python tui.py
    djtreta tui
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
THINKING_LOG = Path("/tmp/dj-treta-thinking.log")
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

_track_cache: dict[int, dict] = {}
_track_cache_path: dict[int, str] = {}


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

def fmt_time(s) -> str:
    if isinstance(s, str): return s
    if s <= 0: return "0:00"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"

def fmt_time_precise(s) -> str:
    if s <= 0: return "0:00.00"
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:05.2f}"

def fmt_key(k: int) -> str:
    name = KEY_MAP.get(k, "")
    cam = CAMELOT_MAP.get(name, "")
    return f"{name} ({cam})" if cam else name or "—"

def get_track_name(deck: int, file_path: str = "") -> str:
    global _track_cache, _track_cache_path
    if file_path and file_path == _track_cache_path.get(deck, ""):
        cached = _track_cache.get(deck, {})
        title = cached.get("title", "")
        artist = cached.get("artist", "")
        if title:
            if " - " in title and not artist:
                parts = title.split(" - ", 1)
                if len(parts) == 2:
                    artist = parts[0].strip()
                    title = parts[1].strip()
            if artist and artist.lower() not in title.lower():
                return f"{artist} — {title}"
            return title
        return ""

    info = mixxx_get(f"/api/deck/{deck}/track_info")
    if info and not info.get("error"):
        _track_cache[deck] = info
        _track_cache_path[deck] = file_path or info.get("file_path", "")
        return get_track_name(deck, _track_cache_path[deck])

    if file_path:
        name = Path(file_path).stem
        _track_cache_path[deck] = file_path
        _track_cache[deck] = {"title": name}
        return name
    return ""

def vu_bar(level: float, width: int = 12) -> str:
    """Vertical-style VU meter rendered horizontally with colors."""
    filled = int(level * width)
    if filled <= 0:
        return "[dim]" + "░" * width + "[/dim]"

    # Green zone (0-60%), Yellow (60-80%), Red (80-100%)
    green_end = int(width * 0.6)
    yellow_end = int(width * 0.8)

    bar = ""
    for i in range(width):
        if i < filled:
            if i < green_end:
                bar += "[green]█[/green]"
            elif i < yellow_end:
                bar += "[yellow]█[/yellow]"
            else:
                bar += "[red]█[/red]"
        else:
            bar += "[dim]░[/dim]"
    return bar

def knob_display(value: float, label: str, neutral: float = 1.0) -> str:
    """Show a knob value with visual indicator."""
    if abs(value - neutral) < 0.05:
        return f"[dim]{label}[/dim]"
    elif value > neutral:
        return f"[bold yellow]{label}↑{value:.1f}[/bold yellow]"
    else:
        return f"[bold cyan]{label}↓{value:.1f}[/bold cyan]"

def send_command(command: str, args: dict = {}) -> str:
    cmd_id = f"{time.time():.6f}"
    payload = {"command": command, "args": args, "id": cmd_id}
    COMMAND_FILE.write_text(json.dumps(payload))
    for _ in range(240):  # 120s timeout (agent can take 60s+ for tool calls)
        time.sleep(0.5)
        state = read_state()
        if state and state.get("last_command_id") == cmd_id:
            result = state.get("last_command_result", "")
            if result and result != "processing...":
                return result
    return "No response (timeout)"


# ── Widgets ───────────────────────────────────────────────────────────

class DeckWidget(Static):
    """Full DJ deck display — track, waveform, BPM, key, EQ, VU, filter, sync."""

    def __init__(self, deck_num: int, **kwargs):
        super().__init__(**kwargs)
        self.deck_num = deck_num

    def update_deck(self, deck: dict, live: dict, is_active: bool):
        if not deck.get("track_loaded"):
            color = "cyan" if self.deck_num == 1 else "magenta"
            self.update(
                f"[bold {color}]DECK {self.deck_num}[/bold {color}]\n"
                f"[dim]  No track loaded[/dim]\n\n\n\n\n\n"
            )
            return

        playing = deck.get("playing", False)
        bpm = deck.get("bpm", 0)
        file_bpm = deck.get("file_bpm", 0)
        key = fmt_key(deck.get("key", 0))
        pos = deck.get("position_seconds", 0)
        dur = deck.get("duration", 0)
        remaining = deck.get("remaining_seconds", 0)
        synced = deck.get("sync_enabled", False)
        vol = deck.get("volume", 1.0)
        rate = deck.get("rate", 0) * 100  # as percentage
        loop = deck.get("loop_enabled", False)

        # EQ
        eq_hi = deck.get("eq_hi", 1.0)
        eq_mid = deck.get("eq_mid", 1.0)
        eq_lo = deck.get("eq_lo", 1.0)

        # VU from live data
        vu_l = live.get("vu_left", 0)
        vu_r = live.get("vu_right", 0)
        beat_active = live.get("beat_active", False)
        beat_dist = live.get("beat_distance", 0)
        peak = live.get("peak_indicator", False)

        # Track name
        track_name = get_track_name(self.deck_num)
        if not track_name:
            state = read_state()
            if state:
                ct = state.get("current_track", {})
                if ct.get("title"):
                    track_name = ct["title"]

        max_name = 48
        if len(track_name) > max_name:
            track_name = track_name[:max_name - 1] + "…"

        # Progress bar
        pct = pos / dur if dur > 0 else 0
        w = 44
        filled = int(pct * w)
        bar = "█" * filled + "▌" + "░" * max(0, w - filled - 1)

        # Colors
        icon = "▶" if playing else "⏸"
        color = "cyan" if self.deck_num == 1 else "magenta"
        dim_color = "bright_cyan" if self.deck_num == 1 else "bright_magenta"
        active_str = f" [bold yellow]★[/bold yellow]" if is_active else ""
        beat_str = "[bold white]●[/bold white]" if beat_active else "[dim]○[/dim]"

        # Status tags
        tags = []
        if synced:
            tags.append("[green]SYNC[/green]")
        if loop:
            tags.append("[yellow]LOOP[/yellow]")
        if peak:
            tags.append("[red bold]PEAK[/red bold]")
        tags_str = " ".join(tags)

        # Rate display
        rate_str = f"[dim]{rate:+.1f}%[/dim]" if abs(rate) > 0.01 else ""

        # EQ knobs
        eq_str = f"{knob_display(eq_hi, 'H')} {knob_display(eq_mid, 'M')} {knob_display(eq_lo, 'L')}"

        # VU meters
        vu_str = f"L {vu_bar(vu_l, 10)}  R {vu_bar(vu_r, 10)}"

        # Volume fader visual
        vol_pct = int(vol * 10)
        vol_bar = "█" * vol_pct + "░" * (10 - vol_pct)
        vol_str = f"VOL [{dim_color}]{vol_bar}[/{dim_color}] {vol:.0%}"

        self.update(
            f"[bold {color}]DECK {self.deck_num}{active_str}[/bold {color}]  {beat_str}  {tags_str}\n"
            f"  [bold]{track_name}[/bold]\n"
            f"  {icon} {fmt_time_precise(pos)} [{dim_color}]{bar}[/{dim_color}] -{fmt_time(remaining)}\n"
            f"  [bold]{bpm:.2f}[/bold] BPM {rate_str}  [bold]{key}[/bold]  (file: {file_bpm:.0f})\n"
            f"  EQ {eq_str}  {vol_str}\n"
            f"  {vu_str}"
        )


class MixerWidget(Static):
    """Center mixer — crossfader, master VU, master vol, headphone."""

    def update_mixer(self, status: dict, live: dict):
        xf = status.get("crossfader", 0)
        master_vol = status.get("master_volume", 1)
        head_vol = status.get("headphone_volume", 1)
        m_vu_l = live.get("master_vu_left", 0)
        m_vu_r = live.get("master_vu_right", 0)

        # Crossfader
        w = 40
        pos = int((xf + 1) / 2 * w)
        pos = max(0, min(w, pos))
        xf_bar = "─" * pos + "[bold yellow]◆[/bold yellow]" + "─" * (w - pos)

        # Master VU
        master_vu = f"L {vu_bar(m_vu_l, 15)}  R {vu_bar(m_vu_r, 15)}"

        self.update(
            f"  [cyan]D1[/cyan] {xf_bar} [magenta]D2[/magenta]\n"
            f"  MASTER {master_vu}  VOL {master_vol:.0%}  HEAD {head_vol:.0%}"
        )


class BrainWidget(Static):
    """Brain status bar."""

    def update_brain(self, state: dict | None):
        if not state:
            self.update("[dim]Brain offline — /start to launch[/dim]")
            return

        phase = state.get("phase", "?")
        mood = state.get("mood", "?")
        played = state.get("tracks_played", 0)
        elapsed = state.get("set_elapsed", 0)
        remaining = state.get("set_remaining", 0)

        colors = {
            "playing": "green", "preparing": "yellow", "transitioning": "blue",
            "recovery": "red", "starting": "yellow", "stopped": "dim", "idle": "dim",
        }
        pc = colors.get(phase, "white")

        if isinstance(remaining, str):
            set_str = f"{fmt_time(elapsed)} / {remaining}"
        else:
            set_str = f"{fmt_time(elapsed)} / {fmt_time(elapsed + remaining)}"

        # Get billing info
        billing_str = ""
        try:
            billing_file = Path("/tmp/dj-treta-billing.json")
            if billing_file.exists():
                b = json.loads(billing_file.read_text())
                total_tokens = b.get("total_input_tokens", 0) + b.get("total_output_tokens", 0)
                cost = b.get("total_cost_usd", 0)
                if total_tokens > 0:
                    if total_tokens > 1_000_000:
                        billing_str = f"  [dim]{total_tokens/1_000_000:.1f}M tokens ${cost:.3f}[/dim]"
                    else:
                        billing_str = f"  [dim]{total_tokens//1000}K tokens ${cost:.4f}[/dim]"
        except Exception:
            pass

        line1 = (
            f"[{pc}]● {phase.upper()}[/{pc}]  "
            f"Mood: [bold]{mood or 'none'}[/bold]  "
            f"Tracks: [bold]{played}[/bold]  "
            f"Set: {set_str}{billing_str}"
        )

        parts = [line1]
        ct = state.get("current_track", {})
        if ct.get("title"):
            parts.append(f"  Now: [italic]{ct['title']}[/italic]")

        nt = state.get("next_track")
        if nt and nt.get("title"):
            parts.append(f"  Next: [italic cyan]{nt['title']}[/italic cyan]")

        self.update("\n".join(parts))


# ── Main App ──────────────────────────────────────────────────────────

CSS = """
Screen {
    layout: vertical;
}

#decks {
    height: 10;
    layout: horizontal;
}

#deck1, #deck2 {
    width: 1fr;
    height: 100%;
    padding: 0 1;
}

#mixer {
    height: 2;
    text-align: center;
    padding: 0 1;
}

#brain {
    height: 4;
    padding: 0 1;
    border-top: solid $accent;
}

#conversation {
    height: 1fr;
    border-top: solid $accent;
    padding: 0 1;
}

#debug-log {
    height: 1fr;
    border-top: solid $warning;
    padding: 0 1;
    display: none;
}

#debug-log.visible {
    display: block;
}

#prompt-input {
    width: 100%;
    dock: bottom;
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
        Binding("ctrl+s", "skip", "Skip"),
        Binding("f2", "toggle_debug", "Debug"),
        Binding("f4", "show_tracks", "Tracks"),
        Binding("f5", "show_set", "Set"),
    ]

    debug_mode = reactive(False)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="decks"):
            yield DeckWidget(1, id="deck1")
            yield DeckWidget(2, id="deck2")
        yield MixerWidget(id="mixer")
        yield BrainWidget(id="brain")
        yield RichLog(id="conversation", highlight=True, markup=True, wrap=True)
        yield RichLog(id="debug-log", highlight=True, markup=True, wrap=True)
        yield Input(placeholder="Talk to DJ Treta... (or /help)", id="prompt-input")
        yield Footer()

    def on_mount(self) -> None:
        self.log_widget = self.query_one("#conversation", RichLog)
        self.debug_widget = self.query_one("#debug-log", RichLog)
        self.log_widget.write("[dim]DJ Treta Console. Type anything to talk, /help for commands.[/dim]\n")
        self.set_interval(1.0, self.refresh_status)
        self.refresh_status()
        self._log_pos = 0
        self._log_mtime = 0.0
        self._debug_log_pos = 0
        self._thinking_pos = 0
        self.set_interval(3.0, self.poll_daemon_log)
        self.set_interval(1.0, self.poll_debug_log)
        self.set_interval(1.0, self.poll_thinking_log)

    def action_toggle_debug(self) -> None:
        debug_log = self.query_one("#debug-log")
        if debug_log.has_class("visible"):
            debug_log.remove_class("visible")
            self.log_widget.write("[dim]Debug panel OFF[/dim]")
        else:
            debug_log.add_class("visible")
            self.debug_widget.write("[yellow bold]── Debug ──[/yellow bold]")
            self.debug_widget.write("[dim]Tool calls, API responses, timing, tokens[/dim]\n")
            self.log_widget.write("[yellow]Debug panel ON (Ctrl+D to toggle)[/yellow]")

    def poll_debug_log(self) -> None:
        """Filtered debug log — shows agent actions, not framework noise."""
        if not self.query_one("#debug-log").has_class("visible"):
            return
        if not DAEMON_LOG.exists():
            return
        try:
            content = DAEMON_LOG.read_text()
            lines = content.split("\n")
            new_lines = lines[self._debug_log_pos:]
            self._debug_log_pos = len(lines)

            # Skip smolagents framework noise
            noise = [
                "You're a helpful agent", "You have been submitted",
                "your manager", "final_answer WILL HAVE", "Task outcome",
                "extremely detailed", "Additional context", "one-line answer",
                "Put all these", "argument to final_answer", "will be lost",
                "LiteLLM", "completion()", "Wrapper:", "provider = openai",
                "─", "│", "╭", "╰", "━",
                "New run -", "You're helping",
            ]

            for line in new_lines:
                clean = line.strip()
                if not clean or len(clean) < 5:
                    continue

                # Skip noise
                if any(n in clean for n in noise):
                    continue

                # Show useful debug info with colors
                if "Calling tool:" in clean:
                    self.debug_widget.write(f"[bold cyan]  🔧 {clean}[/bold cyan]")
                elif "Step " in clean and "Duration" in clean:
                    self.debug_widget.write(f"[yellow]  ⏱ {clean}[/yellow]")
                elif "Final answer:" in clean:
                    ans = clean[:200] + "..." if len(clean) > 200 else clean
                    self.debug_widget.write(f"[bold green]  ✓ {ans}[/bold green]")
                elif "ERROR" in clean:
                    self.debug_widget.write(f"[bold red]  ✗ {clean}[/bold red]")
                elif "WARNING" in clean:
                    self.debug_widget.write(f"[red]  ⚠ {clean}[/red]")
                elif "Agent acted" in clean or "Talk done" in clean:
                    self.debug_widget.write(f"[green]  {clean}[/green]")
                elif "INFO" in clean:
                    parts = clean.split("] ", 1)
                    if len(parts) >= 2 and parts[1].strip():
                        self.debug_widget.write(f"[dim]  {parts[1]}[/dim]")
        except Exception:
            pass

    def refresh_status(self) -> None:
        status = mixxx_get("/api/status")
        live = mixxx_get("/api/live")
        state = read_state()

        deck1_w = self.query_one("#deck1", DeckWidget)
        deck2_w = self.query_one("#deck2", DeckWidget)
        mixer_w = self.query_one("#mixer", MixerWidget)
        brain_w = self.query_one("#brain", BrainWidget)

        if status and live:
            d1 = status.get("deck1", {})
            d2 = status.get("deck2", {})
            l1 = live.get("deck1", {})
            l2 = live.get("deck2", {})
            xf = status.get("crossfader", 0)

            if d1.get("playing") and not d2.get("playing"):
                active = 1
            elif d2.get("playing") and not d1.get("playing"):
                active = 2
            elif xf < -0.3:
                active = 1
            else:
                active = 2

            deck1_w.update_deck(d1, l1, active == 1)
            deck2_w.update_deck(d2, l2, active == 2)
            mixer_w.update_mixer(status, live)
        else:
            deck1_w.update("[red bold]DECK 1[/red bold]\n  [red]Mixxx offline[/red]\n\n\n\n\n")
            deck2_w.update("[red bold]DECK 2[/red bold]\n  [red]Mixxx offline[/red]\n\n\n\n\n")
            mixer_w.update("[red]No connection to Mixxx[/red]")

        brain_w.update_brain(state)

    def poll_daemon_log(self) -> None:
        if not DAEMON_LOG.exists():
            return
        try:
            mtime = DAEMON_LOG.stat().st_mtime
            if mtime != self._log_mtime and self._log_mtime > 0:
                content_check = DAEMON_LOG.read_text()
                if len(content_check.split("\n")) < self._log_pos:
                    self._log_pos = 0
                    self.log_widget.write("[yellow]— New daemon session —[/yellow]")
            self._log_mtime = mtime

            content = DAEMON_LOG.read_text()
            lines = content.split("\n")
            new_lines = lines[self._log_pos:]
            self._log_pos = len(lines)

            skip = ["LiteLLM", "Wrapper:", "completion() model", "─", "│", "╭", "╰",
                    "Observations:", "Step ", "Calling tool", "Final answer:", "TRACK:", "TECHNIQUE:", "ENERGY:",
                    "Talk result", "Talk done", "Talk ack", "processing...", "Result: processing",
                    "Unmapped finish_reason", "malformed_function_call", "TOKENS:", "TokenUsage",
                    "mixer ←", "dj_treta →", "dj_treta ←", "library ←", "library →", "mixer →"]

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
                    self.log_widget.write(f"[red]  {msg}[/red]")
                elif "Playing:" in msg or "now playing" in msg.lower():
                    self.log_widget.write(f"[green]  {msg}[/green]")
                elif "Next:" in msg:
                    self.log_widget.write(f"[cyan]  {msg}[/cyan]")
                elif "Transition" in msg or "transition" in msg:
                    self.log_widget.write(f"[blue]  {msg}[/blue]")
                elif "preparing" in msg.lower() or "brain" in msg.lower():
                    self.log_widget.write(f"[yellow]  {msg}[/yellow]")
                elif "sync" in msg.lower() or "Loaded" in msg:
                    self.log_widget.write(f"[magenta]  {msg}[/magenta]")
                elif "hear" in msg.lower() or "listen" in msg.lower():
                    self.log_widget.write(f"[bright_green]  {msg}[/bright_green]")
                else:
                    self.log_widget.write(f"[dim]  {msg}[/dim]")
        except Exception:
            pass

    def poll_thinking_log(self) -> None:
        """Agent internal thinking — all goes to debug panel."""
        debug_visible = self.query_one("#debug-log").has_class("visible")

        if not debug_visible:
            return
        if not THINKING_LOG.exists():
            return
        try:
            content = THINKING_LOG.read_text()
            lines = content.split("\n")
            new_lines = lines[self._thinking_pos:]
            self._thinking_pos = len(lines)

            for line in new_lines:
                if not line.strip():
                    continue

                if line.startswith("[THINK:"):
                    agent = line.split("]")[0].split(":")[1]
                    thought = line.split("] ", 1)[1] if "] " in line else line
                    if len(thought) > 300:
                        thought = thought[:300] + "..."
                    self.debug_widget.write(f"[bold bright_white]  💭 {agent}:[/bold bright_white] [italic]{thought}[/italic]")

                elif line.startswith("[CALL:"):
                    # Tool calls → DEBUG panel only
                    if debug_visible:
                        agent = line.split("]")[0].split(":")[1]
                        call = line.split("] ", 1)[1] if "] " in line else line
                        self.debug_widget.write(f"[cyan]  🔧 {agent} → {call}[/cyan]")

                elif line.startswith("[OBS:"):
                    if debug_visible:
                        agent = line.split("]")[0].split(":")[1]
                        obs = line.split("] ", 1)[1] if "] " in line else line
                        if len(obs) > 150:
                            obs = obs[:150] + "..."
                        self.debug_widget.write(f"[dim green]  📋 {agent} ← {obs}[/dim green]")

                elif line.startswith("[TOKENS:"):
                    if debug_visible:
                        self.debug_widget.write(f"[dim yellow]  {line}[/dim yellow]")
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
                "\n[bold]Commands:[/bold]\n"
                "  [cyan]<message>[/cyan]          Talk to DJ Treta\n"
                "  [cyan]/play[/cyan] [mood] [min] Start a set (e.g. /play dark-techno 60)\n"
                "  [cyan]/stop[/cyan]              Stop the set (fade out)\n"
                "  [cyan]/mood[/cyan] <name>       Change mood\n"
                "  [cyan]/skip[/cyan]              Skip (smooth 20s transition)\n"
                "  [cyan]/pause[/cyan] [deck]      Pause\n"
                "  [cyan]/load[/cyan] <d> <name>   Load track by name\n"
                "  [cyan]/set[/cyan]               Show set history (full playlist)\n"
                "  [cyan]/tracks[/cyan]            List library\n"
                "  [cyan]/start[/cyan]             Start Being daemon\n"
                "  [cyan]/kill[/cyan]              Kill Being daemon\n"
                "  [cyan]/help[/cyan]              This help\n"
                "  [dim]Ctrl+Q quit | Ctrl+S skip[/dim]\n"
            )
        elif cmd == "play":
            mood = args[0] if args else "melodic-techno"
            dur = int(args[1]) if len(args) > 1 else 0
            self.log_widget.write(f"[green]  Starting {mood} set...[/green]")
            self.run_brain_command("play", {"mood": mood, "duration": dur})
        elif cmd == "stop":
            self.run_brain_command("stop", {})
        elif cmd == "mood" and args:
            self.run_brain_command("change_mood", {"mood": args[0]})
        elif cmd == "skip":
            self.action_skip()
        elif cmd == "pause":
            deck = int(args[0]) if args else 1
            mixxx_post("/api/pause", {"deck": deck})
            self.log_widget.write(f"[yellow]  Paused Deck {deck}[/yellow]")
        elif cmd == "load" and len(args) >= 2:
            deck = int(args[0])
            query = " ".join(args[1:]).lower()
            self.load_track(deck, query)
        elif cmd == "tracks":
            self.show_tracks()
        elif cmd == "start":
            self.start_brain()
        elif cmd == "kill":
            self.stop_brain()
        elif cmd == "cost":
            self.show_cost()
        elif cmd == "debug":
            self.action_toggle_debug()
        elif cmd == "set":
            self.show_set_history()
        else:
            self.log_widget.write(f"[red]Unknown: {text}[/red] — /help")

    @work(thread=True)
    def handle_talk(self, message: str) -> None:
        self.log_widget.write(f"\n[bold cyan]You:[/bold cyan] {message}")
        self.log_widget.write("[dim]  thinking...[/dim]")
        response = send_command("talk", {"message": message})
        self.log_widget.write(f"[bold magenta]DJ Treta:[/bold magenta] {response}\n")

    @work(thread=True)
    def run_brain_command(self, cmd: str, args: dict) -> None:
        response = send_command(cmd, args)
        self.log_widget.write(f"[green]  {response}[/green]")

    def load_track(self, deck: int, query: str):
        for genre_dir in sorted(MUSIC_DIR.iterdir()):
            if not genre_dir.is_dir():
                continue
            for f in sorted(genre_dir.iterdir()):
                if query in f.stem.lower():
                    mixxx_post("/api/load", {"deck": deck, "track": str(f)})
                    self.log_widget.write(f"[green]  Loaded on Deck {deck}: {f.stem}[/green]")
                    return
        self.log_widget.write(f"[red]  No track matching '{query}'[/red]")

    def show_tracks(self):
        lines = ["[bold]Library:[/bold]"]
        for genre_dir in sorted(MUSIC_DIR.iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
                continue
            tracks = [f.stem for f in sorted(genre_dir.iterdir()) if f.suffix.lower() in ('.mp3', '.wav', '.flac')]
            lines.append(f"\n  [cyan]{genre_dir.name}[/cyan] ({len(tracks)})")
            for t in tracks:
                lines.append(f"    [dim]•[/dim] {t}")
        self.log_widget.write("\n".join(lines))

    def show_set_history(self):
        """Show full set journey — played, now, planned."""
        state = read_state()
        status = mixxx_get("/api/status")

        lines = ["\n[bold]SET JOURNEY[/bold]"]

        if state:
            mood = state.get("mood", "?")
            elapsed = state.get("set_elapsed", 0)
            remaining = state.get("set_remaining", 0)
            if isinstance(remaining, str):
                dur_str = f"{fmt_time(elapsed)} / {remaining}"
            else:
                dur_str = f"{fmt_time(elapsed)} / {fmt_time(elapsed + remaining)}"
            lines.append(f"  Mood: [bold]{mood}[/bold]  Duration: {dur_str}")
            lines.append("")

        # ── PLAYED ──
        session_file = Path.home() / "beings" / "dj-treta" / ".beings" / "session.json"
        tracks = []
        if session_file.exists():
            try:
                session = json.loads(session_file.read_text())
                tracks = session.get("tracks_played", [])
            except Exception:
                pass

        if tracks:
            lines.append("  [dim]── Played ──[/dim]")
            for i, t in enumerate(tracks, 1):
                title = t.get("title", "Unknown")
                played_at = t.get("time", 0)
                if played_at:
                    import datetime
                    ts = datetime.datetime.fromtimestamp(played_at).strftime("%H:%M")
                else:
                    ts = "?"
                lines.append(f"  [dim]{i:2d}. {ts}[/dim]  {title}")
            lines.append("")

        # ── NOW PLAYING ──
        if status:
            d1 = status.get("deck1", {})
            d2 = status.get("deck2", {})
            for deck_num, d in [(1, d1), (2, d2)]:
                if d.get("playing"):
                    tinfo = mixxx_get(f"/api/deck/{deck_num}/track_info")
                    title = tinfo.get("title", "?") if tinfo and not tinfo.get("error") else "?"
                    bpm = d.get("bpm", 0)
                    key = fmt_key(d.get("key", 0))
                    rem = d.get("remaining_seconds", 0)
                    lines.append(f"  [bold green]▶ NOW PLAYING[/bold green]")
                    lines.append(f"      {title}")
                    lines.append(f"      {bpm:.0f} BPM  {key}  {fmt_time(rem)} remaining")
                    lines.append("")

        # ── PLANNED ──
        planned = []
        if state:
            planned = state.get("planned_tracks", [])

        if planned:
            lines.append("  [bold cyan]── Coming Up ──[/bold cyan]")
            for i, t in enumerate(planned, 1):
                title = t.get("title", "?")
                reason = t.get("reason", "")
                if reason:
                    lines.append(f"  [cyan]{i}.[/cyan] {title}")
                    lines.append(f"     [dim italic]{reason}[/dim italic]")
                else:
                    lines.append(f"  [cyan]{i}.[/cyan] {title}")
        else:
            lines.append("  [dim]── Coming Up ──[/dim]")
            lines.append("  [dim]Brain hasn't planned ahead yet[/dim]")

        lines.append("")
        self.log_widget.write("\n".join(lines))

    def show_cost(self):
        """Show billing — tokens used, cost, per-agent breakdown."""
        billing_file = Path("/tmp/dj-treta-billing.json")
        if not billing_file.exists():
            self.log_widget.write("[dim]No billing data yet[/dim]")
            return

        try:
            b = json.loads(billing_file.read_text())
            elapsed = time.time() - b.get("session_start", time.time())
            mins = elapsed / 60

            lines = ["\n[bold]BILLING[/bold]"]
            lines.append(f"  Session: {mins:.0f} minutes")
            lines.append(f"  Total calls: [bold]{b['calls']}[/bold]")
            lines.append(f"  Input tokens:  [bold]{b['total_input_tokens']:,}[/bold]")
            lines.append(f"  Output tokens: [bold]{b['total_output_tokens']:,}[/bold]")
            lines.append(f"  Total cost:    [bold green]${b['total_cost_usd']:.4f}[/bold green]")

            if mins > 0:
                cost_per_hour = b['total_cost_usd'] / mins * 60
                lines.append(f"  Cost/hour:     [bold]${cost_per_hour:.3f}/hr[/bold]")

            if b.get("by_agent"):
                lines.append("\n  [dim]By agent:[/dim]")
                for name, data in b["by_agent"].items():
                    lines.append(
                        f"    {name}: {data['calls']} calls, "
                        f"{data['input']:,}+{data['output']:,} tokens, "
                        f"${data['cost']:.4f}"
                    )

            lines.append("")
            self.log_widget.write("\n".join(lines))
        except Exception as e:
            self.log_widget.write(f"[red]Billing error: {e}[/red]")

    def start_brain(self):
        import subprocess
        venv = Path(__file__).parent / ".venv" / "bin" / "python3"
        subprocess.Popen(
            [str(venv), "-m", "agent"],
            cwd=str(Path(__file__).parent),
            stdout=open("/tmp/dj-treta-daemon.log", "w"),
            stderr=subprocess.STDOUT,
        )
        self._log_pos = 0
        self.log_widget.write("[green]  Being daemon started[/green]")

    def stop_brain(self):
        import subprocess
        subprocess.run(["pkill", "-f", "python.*-m agent"], capture_output=True)
        self.log_widget.write("[yellow]  Being daemon killed[/yellow]")

    def action_skip(self):
        self.log_widget.write("[yellow]  Skipping...[/yellow]")
        self.run_brain_command("skip", {})

    def action_show_tracks(self):
        self.show_tracks()

    def action_show_set(self):
        self.show_set_history()


def main():
    app = DJTretaApp()
    app.run()


if __name__ == "__main__":
    main()
