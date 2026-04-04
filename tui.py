#!/usr/bin/env python3
"""
DJ Treta TUI — Full DJ console in the terminal.

Usage:
    python tui.py
    djtreta tui
"""

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import websockets
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import (
    Footer, Header, Input, Label, RichLog, Static,
)

# DB access for track timeline + stats
sys.path.insert(0, str(Path(__file__).parent))
from agent.db import get_db, get_current_set, get_set_tracks, get_track_by_path

# ── Config ────────────────────────────────────────────────────────────

MIXXX_URL = "http://localhost:7778"
STATE_FILE = Path("/tmp/dj-treta-state.json")
COMMAND_FILE = Path("/tmp/dj-treta-command.json")
DAEMON_LOG = Path("/tmp/dj-treta-daemon.log")
THINKING_LOG = Path("/tmp/dj-treta-thinking.log")
MUSIC_DIR = Path.home() / "Music" / "DJTreta"
WS_URL = "ws://localhost:7779"

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

def _find_current_section(timeline_json, position: float) -> str:
    """Find which section the track is currently in from timeline JSON."""
    try:
        sections = json.loads(timeline_json) if isinstance(timeline_json, str) else timeline_json
        if not sections:
            return ""
        for s in sections:
            if float(s["start"]) <= position <= float(s["end"]):
                return f"{s['section']} (energy:{s['energy']}, {s['start']:.0f}s-{s['end']}s)"
    except Exception:
        pass
    return ""


def _format_timeline_compact(timeline_json, current_pos: float) -> str:
    """Format full timeline with current section highlighted."""
    try:
        sections = json.loads(timeline_json) if isinstance(timeline_json, str) else timeline_json
        if not sections:
            return ""
        parts = []
        for s in sections:
            label = f"{s['section'].upper()}({s['energy']})"
            if float(s["start"]) <= current_pos <= float(s["end"]):
                parts.append(f"[bold yellow]{label}[/bold yellow]")
            else:
                parts.append(f"[dim]{label}[/dim]")
        return " → ".join(parts)
    except Exception:
        return ""


def _get_scheduled_transition() -> dict | None:
    """Read scheduled transition data from temp file."""
    try:
        f = Path("/tmp/dj-treta-scheduled-transition.json")
        return json.loads(f.read_text()) if f.exists() else None
    except Exception:
        return None


def _detect_config_value(section: str, key: str, default=False):
    """Read a value from config.yaml — fallback when daemon state file is old."""
    try:
        import yaml
        config_file = Path(__file__).parent / "config.yaml"
        if config_file.exists():
            cfg = yaml.safe_load(config_file.read_text()) or {}
            return cfg.get(section, {}).get(key, default)
    except Exception:
        pass
    return default


def send_command(command: str, args: dict = {}) -> str:
    cmd_id = f"{time.time():.6f}"
    payload = {"command": command, "args": args, "id": cmd_id}
    COMMAND_FILE.write_text(json.dumps(payload))
    for _ in range(600):  # 300s timeout (generation can take 2-3 min with Lyria + analysis)
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

        max_name = 60
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
        if file_bpm > 0 and abs(bpm - file_bpm) > 2:
            tags.append("[red]DRIFT[/red]")
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

        # Track section from DB timeline
        section_str = ""
        try:
            file_path = deck.get("file_path", "") or _track_cache.get(self.deck_num, {}).get("file_path", "")
            if file_path:
                track_data = get_track_by_path(file_path)
                if track_data and track_data.get("timeline"):
                    section = _find_current_section(track_data["timeline"], pos)
                    if section:
                        section_str = f"  [dim]▸ {section}[/dim]\n"
        except Exception:
            pass

        self.update(
            f"[bold {color}]DECK {self.deck_num}{active_str}[/bold {color}]  {beat_str}  {tags_str}\n"
            f"  [bold]{track_name}[/bold]\n"
            f"{section_str}"
            f"  {icon} {fmt_time_precise(pos)} [{dim_color}]{bar}[/{dim_color}] -{fmt_time(remaining)}\n"
            f"  [bold]{bpm:.2f}[/bold] BPM {rate_str}  [bold]{key}[/bold]  (file: {file_bpm:.0f})\n"
            f"  EQ {eq_str}  {vol_str}\n"
            f"  {vu_str}"
        )


class PlaylistWidget(Static):
    """Live set playlist — energy curve, set details, tracklist."""

    def _energy_sparkline(self, arc: list, width: int = 38) -> str:
        """Render energy arc as a terminal sparkline."""
        if not arc:
            return "[dim]No energy data yet[/dim]"
        blocks = " ▁▂▃▄▅▆▇█"
        energies = [a.get("e", 0) for a in arc]
        # Resample to fit width
        if len(energies) > width:
            step = len(energies) / width
            sampled = [energies[int(i * step)] for i in range(width)]
        else:
            sampled = energies
        # Build sparkline with color gradient
        chars = []
        for e in sampled:
            idx = min(int(e / 10 * 8), 8)
            ch = blocks[idx]
            if e >= 8:
                chars.append(f"[bold red]{ch}[/bold red]")
            elif e >= 6:
                chars.append(f"[yellow]{ch}[/yellow]")
            elif e >= 4:
                chars.append(f"[green]{ch}[/green]")
            else:
                chars.append(f"[cyan]{ch}[/cyan]")
        return "".join(chars)

    def update_playlist(self, state: dict | None):
        if not state:
            self.update("[dim]No set[/dim]")
            return

        set_data = state.get("set", {})
        set_id = set_data.get("id", "")
        current = state.get("current_track", {})
        next_track = state.get("next_track")
        current_title = current.get("title", "")

        lines = []

        # ── Set details ──
        if set_data:
            title = set_data.get("title", "?")
            genre = set_data.get("genre", set_data.get("mood", ""))
            elapsed = set_data.get("elapsed", 0)
            target = set_data.get("target_minutes", 0)
            peak = set_data.get("peak_energy", 0)
            lines.append(f"[bold underline]{title}[/bold underline]")
            lines.append(f"[dim]{genre} | {fmt_time(elapsed)}/{fmt_time(target*60)}[/dim]")

            # ── Energy curve ──
            arc = set_data.get("energy_arc", [])
            if arc:
                sparkline = self._energy_sparkline(arc)
                peak_str = f"[bold]{peak:.0f}[/bold]" if peak else ""
                lines.append(f"⚡ {sparkline} {peak_str}")
            else:
                lines.append("[dim]⚡ Waiting for energy data…[/dim]")
        else:
            lines.append("[bold underline]SET PLAYLIST[/bold underline]")

        lines.append("")  # spacer

        # ── Tracklist ──
        played = []
        if set_id:
            try:
                played = get_set_tracks(set_id)
            except Exception:
                pass

        # Load feedback for this set
        feedback_map = {}
        try:
            db = get_db()
            for row in db.execute("SELECT track_title, feedback FROM feedback ORDER BY created_at").fetchall():
                feedback_map[row["track_title"]] = row["feedback"]
            db.close()
        except Exception:
            pass

        if not played and not current_title:
            lines.append("[dim]Waiting for first track…[/dim]")
            self.update("\n".join(lines))
            return

        for i, t in enumerate(played, 1):
            title = t.get("title", "?")
            # Feedback icon
            fb = ""
            for fb_title, fb_type in feedback_map.items():
                if fb_title in title or title in fb_title:
                    fb = " 👍" if fb_type == "like" else " 👎"
                    break
            # Get energy from DB
            energy_str = ""
            try:
                track_data = get_track_by_path(t.get("file_path", "")) if t.get("file_path") else None
                if not track_data:
                    # Try by title match
                    db = get_db()
                    row = db.execute("SELECT energy_peak FROM tracks WHERE title=? LIMIT 1", (title,)).fetchone()
                    db.close()
                    if row and row["energy_peak"]:
                        energy_str = f" E:{row['energy_peak']:.0f}"
                elif track_data.get("energy_peak"):
                    energy_str = f" E:{track_data['energy_peak']:.0f}"
            except Exception:
                pass
            display = title[:28] + "…" if len(title) > 29 else title
            if title == current_title:
                lines.append(f"[bold green]▶ {i}. {display}{energy_str}{fb}[/bold green]")
            else:
                lines.append(f"[dim]  {i}. {display}{energy_str}{fb}[/dim]")

        # Current track not in history yet
        if current_title and not any(t.get("title") == current_title for t in played):
            idx = len(played) + 1
            display = current_title[:30] + "…" if len(current_title) > 31 else current_title
            lines.append(f"[bold green]▶ {idx}. {display}[/bold green]")

        # Up next
        if next_track and next_track.get("title"):
            nt = next_track["title"]
            display = nt[:30] + "…" if len(nt) > 31 else nt
            lines.append(f"[bold cyan]↳ {display}[/bold cyan]")

        self.update("\n".join(lines))


class AgentActivityWidget(Static):
    """Real-time agent activity dashboard — who's doing what."""

    def __init__(self, **kwargs):
        super().__init__("[dim]AGENT ACTIVITY[/dim]", **kwargs)
        self._agents = {
            "treta": {"status": "idle", "last": "", "time": 0},
            "dj_treta": {"status": "idle", "last": "", "time": 0},
            "planner": {"status": "idle", "last": "", "time": 0},
            "consciousness": {"status": "idle", "last": "", "time": 0},
            "mixer": {"status": "idle", "last": "", "time": 0},
        }

    def update_agent(self, agent: str, status: str = "", text: str = "", tool: str = ""):
        """Update an agent's activity from WS thinking events."""
        if agent not in self._agents:
            self._agents[agent] = {"status": "idle", "last": "", "time": 0}

        entry = self._agents[agent]
        entry["time"] = time.time()

        if tool:
            entry["status"] = "tool"
            entry["last"] = f"{tool}()"
        elif text:
            entry["status"] = "thinking"
            entry["last"] = text[:60]
        elif status:
            entry["status"] = status

        self._refresh_display()

    def set_idle(self, agent: str):
        """Mark an agent as idle (no activity for a while)."""
        if agent in self._agents:
            self._agents[agent]["status"] = "idle"
            self._refresh_display()

    def _refresh_display(self):
        """Render the activity panel."""
        icons = {
            "treta": "🧠",
            "dj_treta": "🎧",
            "planner": "📋",
            "consciousness": "💭",
            "mixer": "🎛",
        }
        status_colors = {
            "idle": "dim",
            "thinking": "yellow",
            "tool": "cyan",
        }

        lines = ["[bold underline]AGENT ACTIVITY[/bold underline]"]

        now = time.time()
        for name, entry in self._agents.items():
            icon = icons.get(name, "⚙")
            color = status_colors.get(entry["status"], "white")
            age = now - entry["time"] if entry["time"] > 0 else 999

            # Auto-idle if no activity for 30s
            if age > 30 and entry["status"] != "idle":
                entry["status"] = "idle"
                color = "dim"

            if entry["status"] == "idle":
                lines.append(f"[dim]{icon} {name}: idle[/dim]")
            elif entry["status"] == "tool":
                lines.append(f"[{color}]{icon} {name}: [bold]{entry['last']}[/bold][/{color}]")
            else:
                text = entry["last"][:45] + "…" if len(entry["last"]) > 45 else entry["last"]
                lines.append(f"[{color}]{icon} {name}: {text}[/{color}]")

        self.update("\n".join(lines))


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
    """Brain panel — set info, status, DJ decisions, timeline, billing."""

    def update_brain(self, state: dict | None, status: dict | None = None):
        if not state:
            self.update("[dim]Brain offline — /start to launch[/dim]")
            return

        phase = state.get("phase", "?")
        played = state.get("tracks_played", 0)
        phase_colors = {
            "playing": "green", "preparing": "yellow", "transitioning": "blue",
            "recovery": "red", "starting": "yellow", "stopped": "dim", "idle": "dim",
        }
        pc = phase_colors.get(phase, "white")

        lines = []

        # ── Line 1: Set info ──
        set_data = state.get("set", {})
        # Fallback: read from DB if old daemon doesn't write set info
        if not set_data:
            try:
                db_set = get_current_set()
                if db_set:
                    elapsed_secs = time.time() - db_set["started_at"]
                    target_min = db_set.get("target_duration_minutes", 0) or 0
                    set_data = {
                        "number": db_set.get("set_number", "?"),
                        "title": db_set.get("title", ""),
                        "mood": db_set.get("mood", ""),
                        "genre": db_set.get("genre", ""),
                        "elapsed": elapsed_secs,
                        "target_minutes": target_min,
                    }
            except Exception:
                pass
        if set_data:
            set_num = set_data.get("number", "?")
            set_title = set_data.get("title", "")
            set_genre = set_data.get("genre", set_data.get("mood", ""))
            elapsed = set_data.get("elapsed", 0)
            target_min = set_data.get("target_minutes", 0)
            target_secs = target_min * 60 if target_min else 0
            set_time = f"{fmt_time(elapsed)} / {fmt_time(target_secs)}" if target_secs else fmt_time(elapsed)
            # Sources badge
            sources = state.get("sources", {})
            src_parts = []
            if sources.get("youtube"):
                src_parts.append("YT")
            if sources.get("treta_originals"):
                src_parts.append("Originals")
            src_str = "+".join(src_parts) if src_parts else "none"
            lines.append(
                f"SET #{set_num} [bold]\"{set_title}\"[/bold] | "
                f"{set_genre} | {set_time} | {played} tracks | [magenta]{src_str}[/magenta]"
            )
        else:
            mood = state.get("mood", "?")
            lines.append(f"[dim]No set active[/dim]  Mood: [bold]{mood}[/bold]  Tracks: {played}")

        # ── Line 2: Status bar ──
        agent_str = "[yellow]THINKING[/yellow]" if state.get("agent_busy") else "[dim]idle[/dim]"
        planner_status = state.get("planner_status", "idle")
        planner_since = state.get("planner_tracks_since", 0)
        planner_str = (f"[yellow]PLANNING[/yellow]" if planner_status == "busy"
                       else f"[dim]idle ({planner_since} since)[/dim]")

        # Producer status
        producing = state.get("producing", {})
        if producing and producing.get("status"):
            prod_name = producing.get("name", "?")
            planner_str += f"  [bold yellow]♫ PRODUCING: {prod_name}[/bold yellow]"

        # Relay: check state file first, fallback to config.yaml
        if "relay_connected" in state:
            relay_on = state.get("relay_connected", False)
        else:
            relay_on = _detect_config_value("relay", "enabled", False)
        relay_str = "[green]ON[/green]" if relay_on else "[dim]OFF[/dim]"

        # Recording/Broadcast: check state file first, fallback to config defaults
        if "recording" in state:
            rec_on = state.get("recording", False)
        else:
            rec_on = _detect_config_value("sets", "local_recording", False)
        rec_str = "[green]ON[/green]" if rec_on else "[dim]OFF[/dim]"

        if "broadcasting" in state:
            bcast_on = state.get("broadcasting", False)
        else:
            bcast_on = _detect_config_value("broadcast", "auto_start", True)
        bcast_str = "[green]ON[/green]" if bcast_on else "[dim]OFF[/dim]"

        emerg = state.get("emergency_count", 0)
        emerg_str = f"[red]{emerg}[/red]" if emerg > 0 else "[dim]0[/dim]"

        lines.append(
            f"[{pc}]● {phase.upper()}[/{pc}] | "
            f"Agent: {agent_str} | Planner: {planner_str} | "
            f"Relay: {relay_str} | REC: {rec_str} | BCAST: {bcast_str} | Emerg: {emerg_str}"
        )

        # ── Line 3: DJ Decision + Scheduled Transition ──
        dj_decision = getattr(self.app, '_last_dj_decision', '') if hasattr(self, 'app') else ''
        scheduled = _get_scheduled_transition()
        decision_parts = []
        if dj_decision:
            decision_parts.append(f"[italic]{dj_decision}[/italic]")
        if scheduled:
            tech = scheduled.get("technique", "crossfade").upper().replace("_", " ")
            at_pos = scheduled.get("atPosition", 0)
            # Compute live countdown from current deck position
            countdown = 0
            if status:
                ct = state.get("current_track", {})
                current_pos = ct.get("position", 0)
                countdown = max(0, int(at_pos - current_pos))
            decision_parts.append(f"[bold yellow]{tech} in {countdown}s[/bold yellow]")
        if decision_parts:
            lines.append("  DJ: " + " | ".join(decision_parts))
        else:
            ct = state.get("current_track", {})
            nt = state.get("next_track")
            now_next = []
            if ct.get("title"):
                now_next.append(f"Now: [italic]{ct['title']}[/italic]")
            if nt and nt.get("title"):
                now_next.append(f"Next: [italic cyan]{nt['title']}[/italic cyan]")
            if now_next:
                lines.append("  " + "  ".join(now_next))

        # ── Line 4: Track timeline ──
        ct = state.get("current_track", {})
        file_path = ct.get("file_path", "")
        pos = ct.get("position", 0)
        if file_path:
            try:
                track_data = get_track_by_path(file_path)
                if track_data and track_data.get("timeline"):
                    timeline_str = _format_timeline_compact(track_data["timeline"], pos)
                    if timeline_str:
                        lines.append(f"  {timeline_str}")
            except Exception:
                pass

        # ── Line 5: Billing ──
        try:
            billing_file = Path("/tmp/dj-treta-billing.json")
            if billing_file.exists():
                b = json.loads(billing_file.read_text())
                total_tokens = b.get("total_input_tokens", 0) + b.get("total_output_tokens", 0)
                cost = b.get("total_cost_usd", 0)
                calls = b.get("calls", 0)
                session_start = b.get("session_start", time.time())
                mins = (time.time() - session_start) / 60
                cost_hr = cost / mins * 60 if mins > 0 else 0
                if total_tokens > 0:
                    tok_str = f"{total_tokens/1_000_000:.1f}M" if total_tokens > 1_000_000 else f"{total_tokens//1000}K"
                    lines.append(f"  [dim]${cost:.3f} | {tok_str} tokens | {calls} calls | ${cost_hr:.3f}/hr[/dim]")
        except Exception:
            pass

        self.update("\n".join(lines))


# ── Main App ──────────────────────────────────────────────────────────

CSS = """
Screen {
    layout: vertical;
}

#decks {
    height: 11;
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
    height: 7;
    padding: 0 1;
    border-top: solid $accent;
}

#main-area {
    height: 1fr;
    layout: horizontal;
}

#conversation {
    width: 1fr;
    height: 100%;
    border-top: solid $accent;
    padding: 0 1;
}

#right-panel {
    width: 44;
    height: 100%;
    border-left: solid $accent;
}

#agent-activity {
    height: auto;
    max-height: 9;
    padding: 0 1;
    border-top: solid $accent;
    border-bottom: solid $accent;
}

#playlist-scroll {
    height: 1fr;
    scrollbar-size: 1 1;
}

#playlist {
    width: 100%;
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
        Binding("ctrl+l", "like", "👍"),
        Binding("ctrl+d", "dislike", "👎"),
        Binding("f2", "toggle_debug", "Debug"),
        Binding("f4", "show_tracks", "Tracks"),
        Binding("f5", "show_set", "Set"),
    ]

    debug_mode = reactive(False)

    # ── WebSocket real-time connection ───────────────────────────────

    def _start_ws_client(self):
        """Connect to daemon WebSocket for real-time updates."""
        self._ws = None
        self._ws_connected = False
        self._ws_event_loop = None
        threading.Thread(target=self._ws_thread, daemon=True).start()

    def _ws_thread(self):
        """WebSocket client thread — runs its own event loop."""
        self._ws_event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._ws_event_loop)
        self._ws_event_loop.run_until_complete(self._ws_loop())

    async def _ws_loop(self):
        """Connect to daemon WS and receive events. Auto-reconnects."""
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    self._ws_connected = True
                    self.call_from_thread(self._on_ws_connected)
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            self._handle_ws_message(msg)
                        except json.JSONDecodeError:
                            pass
            except Exception:
                pass
            # Disconnected — reset and retry
            self._ws_connected = False
            self._ws = None
            self.call_from_thread(self._on_ws_disconnected)
            await asyncio.sleep(3)

    def _on_ws_connected(self):
        """Called on main thread when WS connects."""
        self.log_widget.write("[green]  ⚡ WebSocket connected — real-time mode[/green]")

    def _on_ws_disconnected(self):
        """Called on main thread when WS disconnects."""
        pass  # Silent — file polling takes over as fallback

    def _handle_ws_message(self, msg: dict):
        """Route incoming WebSocket messages to handlers (called from WS thread)."""
        msg_type = msg.get("type", "")

        if msg_type == "event":
            event = msg.get("event", "")
            data = msg.get("data", {})

            if event == "state":
                self.call_from_thread(self._apply_ws_state, data)
            elif event == "log":
                text = data.get("text", "")
                if text:
                    self.call_from_thread(self._apply_ws_log, text)
            elif event == "thinking":
                self.call_from_thread(self._apply_ws_thinking, data)
            elif event == "talk_response":
                # Talk responses come back here when sent via WS
                pass

        elif msg_type == "response":
            # Command responses — currently commands go via file, so this is future-use
            pass

        elif msg_type == "error":
            error = msg.get("error", "Unknown error")
            self.call_from_thread(
                lambda e=error: self.log_widget.write(f"[red]  WS error: {e}[/red]")
            )

    def _apply_ws_state(self, data: dict):
        """Apply state update from WebSocket event."""
        self._ws_state = data
        # Update agent activity from state
        try:
            activity = self.query_one("#agent-activity", AgentActivityWidget)
            if data.get("agent_busy"):
                activity.update_agent("dj_treta", status="thinking")
            if data.get("planner_status") == "busy":
                activity.update_agent("planner", status="thinking")
        except Exception:
            pass

    def _apply_ws_log(self, text: str):
        """Apply a log line from WebSocket — agent-prefixed for clarity."""
        # Extract message (strip timestamp + level)
        msg = text.strip()
        if "] " in msg:
            parts = msg.split("] ", 1)
            if len(parts) >= 2:
                msg = parts[1].strip()
        if not msg:
            return

        # Classify by agent and add prefix
        w = self.log_widget
        if "DJ decision" in msg:
            # Strip "DJ decision: " prefix, show clean
            decision = msg.replace("DJ decision: ", "").strip()
            if decision:
                w.write(f"[bold bright_blue]  DJ  [/bold bright_blue] {decision}")
            else:
                w.write(f"[bold bright_blue]  DJ  [/bold bright_blue] [dim](tool call)[/dim]")
        elif "Executing" in msg or "Transition result" in msg:
            w.write(f"[bold cyan]  MIX [/bold cyan] {msg}")
        elif "Transition scheduled" in msg or "schedule_transition" in msg:
            w.write(f"[bold cyan]  MIX [/bold cyan] {msg}")
        elif "Auto-transition" in msg:
            w.write(f"[yellow]  AUTO[/yellow] {msg}")
        elif "Loaded deck" in msg:
            w.write(f"[green]  LOAD[/green] {msg.replace('Loaded deck ', 'D')}")
        elif "Planner done" in msg:
            w.write(f"[magenta]  PLAN[/magenta] done")
            self._show_planner_plan()
        elif "Planner running" in msg:
            w.write(f"[magenta]  PLAN[/magenta] {msg.replace('Planner running — ', '')}")
        elif "Enriched:" in msg:
            w.write(f"[dim]  SCAN[/dim] {msg.replace('Enriched: ', '')[:70]}")
        elif "Evolution" in msg or "evolve" in msg.lower():
            w.write(f"[bold yellow]  EVO [/bold yellow] {msg}")
        elif "Emergency" in msg:
            w.write(f"[bold red]  SOS [/bold red] {msg}")
        elif "Backup" in msg:
            w.write(f"[red]  BKUP[/red] {msg}")
        elif "Set started" in msg or "Set ended" in msg:
            w.write(f"[bold white]  SET [/bold white] {msg}")
        elif "Being thought" in msg or "Being reflect" in msg:
            w.write(f"[bright_cyan]  SELF[/bright_cyan] {msg.replace('Being thought: ', '').replace('Being reflect: ', '')[:70]}")
        elif "ERROR" in text or "WARNING" in text:
            w.write(f"[red]  ERR [/red] {msg}")
        elif "Generated" in msg or "generate_track" in msg:
            w.write(f"[bold bright_magenta]  PROD[/bold bright_magenta] {msg}")
        else:
            w.write(f"[dim]  ··· [/dim] {msg[:80]}")

    def _apply_ws_thinking(self, data: dict):
        """Apply thinking event from WebSocket — updates agent activity + debug panel."""
        agent = data.get("agent", "?")
        think_type = data.get("type", "")
        text = data.get("text", "")
        tool = data.get("tool", "")

        # Update agent activity panel
        try:
            activity = self.query_one("#agent-activity", AgentActivityWidget)
            if think_type == "call":
                activity.update_agent(agent, tool=tool or text)
            elif think_type == "think" and text:
                activity.update_agent(agent, text=text)
        except Exception:
            pass

        debug_visible = self.query_one("#debug-log").has_class("visible")

        if think_type == "think":
            # Surface DJ decisions to BrainWidget
            if "dj_treta" in agent:
                self._last_dj_decision = text[:300]
                self._last_dj_decision_time = time.time()
            if debug_visible and text:
                display = text[:300] + "..." if len(text) > 300 else text
                self.debug_widget.write(
                    f"[bold bright_white]  {agent}:[/bold bright_white] [italic]{display}[/italic]"
                )

        elif think_type == "call":
            if debug_visible and text:
                self.debug_widget.write(f"[cyan]  {agent} -> {text}[/cyan]")

        elif think_type == "observation":
            if debug_visible and text:
                display = text[:150] + "..." if len(text) > 150 else text
                self.debug_widget.write(f"[dim green]  {agent} <- {display}[/dim green]")

        elif think_type == "tokens":
            if debug_visible and text:
                self.debug_widget.write(f"[dim yellow]  {text}[/dim yellow]")

    # ── End WebSocket ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="decks"):
            yield DeckWidget(1, id="deck1")
            yield DeckWidget(2, id="deck2")
        yield MixerWidget(id="mixer")
        yield BrainWidget(id="brain")
        with Horizontal(id="main-area"):
            yield RichLog(id="conversation", highlight=True, markup=True, wrap=True)
            with Vertical(id="right-panel"):
                yield AgentActivityWidget(id="agent-activity")
                with ScrollableContainer(id="playlist-scroll"):
                    yield PlaylistWidget(id="playlist")
        yield RichLog(id="debug-log", highlight=True, markup=True, wrap=True)
        yield Input(placeholder="Talk to DJ Treta... (or /help)", id="prompt-input")
        yield Footer()

    def on_mount(self) -> None:
        self.log_widget = self.query_one("#conversation", RichLog)
        self.debug_widget = self.query_one("#debug-log", RichLog)
        self.log_widget.write("[dim]DJ Treta Console. Type anything to talk, /help for commands.[/dim]\n")
        # Init WS state BEFORE any timers
        self._ws_state = None
        self._ws_connected = False
        self._ws = None
        self._log_pos = 0
        self._log_mtime = 0.0
        self._debug_log_pos = 0
        self._thinking_pos = 0
        self._last_dj_decision = ""
        self._last_dj_decision_time = 0.0
        # Start WebSocket client for real-time updates
        self._start_ws_client()
        # File-based polling as fallback
        self.set_interval(1.0, self.refresh_status)
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
        """Raw unfiltered daemon log for debug panel."""
        if not self.query_one("#debug-log").has_class("visible"):
            return
        if not DAEMON_LOG.exists():
            return
        try:
            content = DAEMON_LOG.read_text()
            lines = content.split("\n")
            new_lines = lines[self._debug_log_pos:]
            self._debug_log_pos = len(lines)

            for line in new_lines:
                if not line.strip():
                    continue
                clean = line.strip()

                # Color by type
                if "Calling tool:" in clean:
                    # Extract tool name and args
                    self.debug_widget.write(f"[bold cyan]  {clean}[/bold cyan]")
                elif "Observations:" in clean:
                    # Truncate observations
                    obs = clean[:150] + "..." if len(clean) > 150 else clean
                    self.debug_widget.write(f"[dim green]  {obs}[/dim green]")
                elif "Step " in clean and "Duration" in clean:
                    self.debug_widget.write(f"[yellow]  {clean}[/yellow]")
                elif "New run" in clean:
                    self.debug_widget.write(f"[bold magenta]  {clean}[/bold magenta]")
                elif "Final answer:" in clean:
                    ans = clean[:200] + "..." if len(clean) > 200 else clean
                    self.debug_widget.write(f"[bold green]  {ans}[/bold green]")
                elif "Initial plan" in clean or "plan" in clean.lower():
                    self.debug_widget.write(f"[bright_yellow]  {clean}[/bright_yellow]")
                elif "ERROR" in clean or "error" in clean.lower():
                    self.debug_widget.write(f"[bold red]  {clean}[/bold red]")
                elif "WARNING" in clean:
                    self.debug_widget.write(f"[red]  {clean}[/red]")
                elif "LiteLLM" in clean or "completion()" in clean:
                    self.debug_widget.write(f"[dim]  {clean}[/dim]")
                elif "INFO" in clean:
                    parts = clean.split("] ", 1)
                    if len(parts) >= 2:
                        self.debug_widget.write(f"[dim white]  {parts[1]}[/dim white]")
                elif "─" in clean or "│" in clean or "╭" in clean or "╰" in clean:
                    self.debug_widget.write(f"[dim]{clean}[/dim]")
                else:
                    if len(clean) > 200:
                        clean = clean[:200] + "..."
                    self.debug_widget.write(f"[dim]  {clean}[/dim]")
        except Exception:
            pass

    def refresh_status(self) -> None:
        status = mixxx_get("/api/status")
        live = mixxx_get("/api/live")
        # Prefer WebSocket state when connected, fall back to file
        if self._ws_connected and self._ws_state:
            state = self._ws_state
        else:
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

        brain_w.update_brain(state, status)

        # Update playlist sidebar
        playlist_w = self.query_one("#playlist", PlaylistWidget)
        playlist_w.update_playlist(state)

    def poll_daemon_log(self) -> None:
        if not DAEMON_LOG.exists():
            if self._log_pos > 0:
                # Log file was deleted (clean slate) — reset
                self._log_pos = 0
                self._log_mtime = 0.0
            return
        try:
            content = DAEMON_LOG.read_text()
            lines = content.split("\n")

            # Detect new daemon session: file shorter than our position
            if self._log_pos > len(lines):
                self._log_pos = 0
                self.log_widget.write("[yellow]— New daemon session —[/yellow]")

            new_lines = lines[self._log_pos:]
            self._log_pos = len(lines)

            skip = ["LiteLLM", "Wrapper:", "completion() model", "─", "│", "╭", "╰",
                    "Observations:", "Step ", "Calling tool", "TRACK:", "TECHNIQUE:", "ENERGY:",
                    "Talk result", "Talk done", "Talk ack", "processing...", "Result: processing",
                    "Unmapped finish_reason", "malformed_function_call", "TOKENS:", "TokenUsage",
                    "mixer ←", "dj_treta →", "dj_treta ←", "library ←", "library →", "mixer →",
                    "HTTP Request:"]

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
                elif "DJ decision:" in msg:
                    self.log_widget.write(f"[bold bright_blue]  {msg}[/bold bright_blue]")
                elif "Final answer:" in msg:
                    self.log_widget.write(f"[bold blue]  {msg}[/bold blue]")
                elif "Playing:" in msg or "now playing" in msg.lower():
                    self.log_widget.write(f"[green]  {msg}[/green]")
                elif "Next:" in msg:
                    self.log_widget.write(f"[cyan]  {msg}[/cyan]")
                elif "Transition" in msg or "transition" in msg or "Bass-swap" in msg or "bass_swap" in msg:
                    self.log_widget.write(f"[blue]  {msg}[/blue]")
                elif "Set started:" in msg or "Set ended:" in msg:
                    self.log_widget.write(f"[bold yellow]  {msg}[/bold yellow]")
                elif "Emergency" in msg or "emergency" in msg:
                    self.log_widget.write(f"[bold red]  {msg}[/bold red]")
                elif "Generated track:" in msg or "Lyria" in msg or "generate_track" in msg:
                    self.log_widget.write(f"[bold bright_magenta]  {msg}[/bold bright_magenta]")
                elif "Planner done:" in msg:
                    # Show full planner plan from playlist file
                    self.log_widget.write(f"[bold yellow]  Planner done — new plan:[/bold yellow]")
                    self._show_planner_plan()
                elif "Planner" in msg or "planner" in msg.lower():
                    self.log_widget.write(f"[yellow]  {msg}[/yellow]")
                elif "preparing" in msg.lower() or "brain" in msg.lower():
                    self.log_widget.write(f"[yellow]  {msg}[/yellow]")
                elif "sync" in msg.lower() or "Loaded" in msg:
                    self.log_widget.write(f"[magenta]  {msg}[/magenta]")
                elif "schedule_transition" in msg.lower():
                    self.log_widget.write(f"[bold cyan]  {msg}[/bold cyan]")
                elif "hear" in msg.lower() or "listen" in msg.lower():
                    self.log_widget.write(f"[bright_green]  {msg}[/bright_green]")
                else:
                    self.log_widget.write(f"[dim]  {msg}[/dim]")
        except Exception:
            pass

    def poll_thinking_log(self) -> None:
        """Agent internal thinking — DJ decisions go to BrainWidget, details to debug panel."""
        debug_visible = self.query_one("#debug-log").has_class("visible")

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
                    # Surface DJ decisions to BrainWidget
                    if "dj_treta" in agent:
                        self._last_dj_decision = thought[:300]
                        self._last_dj_decision_time = time.time()
                    if debug_visible:
                        if len(thought) > 300:
                            thought = thought[:300] + "..."
                        self.debug_widget.write(f"[bold bright_white]  {agent}:[/bold bright_white] [italic]{thought}[/italic]")

                elif line.startswith("[CALL:"):
                    if debug_visible:
                        agent = line.split("]")[0].split(":")[1]
                        call = line.split("] ", 1)[1] if "] " in line else line
                        self.debug_widget.write(f"[cyan]  {agent} -> {call}[/cyan]")

                elif line.startswith("[OBS:"):
                    if debug_visible:
                        agent = line.split("]")[0].split(":")[1]
                        obs = line.split("] ", 1)[1] if "] " in line else line
                        if len(obs) > 150:
                            obs = obs[:150] + "..."
                        self.debug_widget.write(f"[dim green]  {agent} <- {obs}[/dim green]")

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
                "  [cyan]/set[/cyan]               Show current set info\n"
                "  [cyan]/set history[/cyan]       Show all sets from DB\n"
                "  [cyan]/queue[/cyan]             Show next track + planner plan\n"
                "  [cyan]/brain[/cyan]             Show last DJ decisions\n"
                "  [cyan]/stats[/cyan]             Show library stats from DB\n"
                "  [cyan]/relay[/cyan]             Show relay status\n"
                "  [cyan]/tracks[/cyan]            List library\n"
                "  [cyan]/like[/cyan]              👍 Like current track\n"
                "  [cyan]/dislike[/cyan]           👎 Dislike current track\n"
                "  [cyan]/sources[/cyan] <src> on|off  Toggle music sources (yt, originals)\n"
                "  [cyan]/cost[/cyan]              Show billing details\n"
                "  [cyan]/start[/cyan]             Start Being daemon\n"
                "  [cyan]/kill[/cyan]              Kill Being daemon\n"
                "  [cyan]/help[/cyan]              This help\n"
                "  [dim]Ctrl+Q quit | F2 debug | Ctrl+S skip[/dim]\n"
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
            if args and args[0] == "history":
                self.show_set_history_db()
            else:
                self.show_current_set()
        elif cmd == "queue":
            self.show_queue()
        elif cmd == "brain":
            self.show_brain_decisions()
        elif cmd == "stats":
            self.show_db_stats()
        elif cmd == "relay":
            self.show_relay_status()
        elif cmd == "like":
            self.run_brain_command("feedback", {"type": "like"})
            self.log_widget.write("[green]  👍 Liked![/green]")
        elif cmd == "dislike":
            self.run_brain_command("feedback", {"type": "dislike"})
            self.log_widget.write("[red]  👎 Disliked — will avoid similar[/red]")
        elif cmd == "sources" and args:
            source = args[0].lower()
            enabled = args[1].lower() in ("on", "true", "1") if len(args) > 1 else True
            # Map shorthand
            if source == "yt":
                source = "youtube"
            self.run_brain_command("change_sources", {"source": source, "enabled": enabled})
            self.log_widget.write(f"[green]  Source {source} → {'on' if enabled else 'off'}[/green]")
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
        """Show full set journey — played tracks from DB, now playing, next."""
        state = read_state()
        status = mixxx_get("/api/status")

        lines = ["\n[bold]SET JOURNEY[/bold]"]

        # Set info from state
        if state:
            sd = state.get("set", {})
            if sd:
                elapsed = sd.get("elapsed", 0)
                target_min = sd.get("target_minutes", 0)
                target_secs = target_min * 60 if target_min else 0
                dur_str = f"{fmt_time(elapsed)} / {fmt_time(target_secs)}" if target_secs else fmt_time(elapsed)
                lines.append(f"  Set #{sd.get('number', '?')}: [bold]{sd.get('title', '')}[/bold]")
                lines.append(f"  Mood: [bold]{sd.get('mood', '?')}[/bold]  Duration: {dur_str}")
            else:
                lines.append(f"  Mood: [bold]{state.get('mood', '?')}[/bold]")
            lines.append("")

        # ── PLAYED (from DB set_history) ──
        set_id = state.get("set", {}).get("id", "") if state else ""
        tracks = []
        if set_id:
            try:
                tracks = get_set_tracks(set_id)
            except Exception:
                pass

        if not tracks:
            # Fallback to session.json
            session_file = Path.home() / "beings" / "dj-treta" / ".beings" / "session.json"
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
                played_at = t.get("played_at") or t.get("time", 0)
                if played_at:
                    import datetime
                    ts = datetime.datetime.fromtimestamp(played_at).strftime("%H:%M")
                else:
                    ts = "?"
                transition = t.get("transition_type", "")
                trans_str = f" [dim]({transition})[/dim]" if transition else ""
                original = " [magenta]★[/magenta]" if title.startswith("Treta-") else ""
                lines.append(f"  [dim]{i:2d}. {ts}[/dim]  {title}{original}{trans_str}")
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
                    original_badge = " [magenta]★ ORIGINAL[/magenta]" if title.startswith("Treta-") else ""
                    lines.append(f"  [bold green]▶ NOW PLAYING[/bold green]{original_badge}")
                    lines.append(f"      {title}")
                    lines.append(f"      {bpm:.0f} BPM  {key}  {fmt_time(rem)} remaining")
                    lines.append("")

        # ── NEXT ──
        if state:
            nt = state.get("next_track")
            if nt and nt.get("title"):
                lines.append(f"  [bold cyan]── Next (deck {nt.get('deck', '?')}) ──[/bold cyan]")
                lines.append(f"  [cyan]{nt['title']}[/cyan]")
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

    def show_current_set(self):
        """Show current set info from state file."""
        state = read_state()
        lines = ["\n[bold]CURRENT SET[/bold]"]
        if state:
            sd = state.get("set", {})
            if sd:
                lines.append(f"  Set #{sd.get('number', '?')}: [bold]{sd.get('title', '?')}[/bold]")
                lines.append(f"  Mood: {sd.get('mood', '?')}  Genre: {sd.get('genre', '?')}")
                elapsed = sd.get("elapsed", 0)
                target_min = sd.get("target_minutes", 0)
                target_secs = target_min * 60 if target_min else 0
                lines.append(f"  Duration: {fmt_time(elapsed)} / {fmt_time(target_secs)}")
                lines.append(f"  Tracks played: {state.get('tracks_played', 0)}")
            else:
                lines.append("  [dim]No set active[/dim]")

            # Show currently playing + next
            ct = state.get("current_track", {})
            if ct.get("title"):
                lines.append(f"\n  [green]Now:[/green] {ct['title']}")
            nt = state.get("next_track")
            if nt and nt.get("title"):
                lines.append(f"  [cyan]Next:[/cyan] {nt['title']} (deck {nt.get('deck', '?')})")
        else:
            lines.append("  [dim]Brain offline[/dim]")
        lines.append("")
        self.log_widget.write("\n".join(lines))

    def show_set_history_db(self):
        """Show all sets from SQLite DB."""
        lines = ["\n[bold]SET HISTORY[/bold]"]
        try:
            db = get_db()
            rows = db.execute("SELECT * FROM sets ORDER BY started_at DESC LIMIT 10").fetchall()
            db.close()
            if rows:
                for r in rows:
                    r = dict(r)
                    status_color = "green" if r.get("status") == "live" else "dim"
                    started = time.strftime("%H:%M", time.localtime(r.get("started_at", 0)))
                    dur = ""
                    if r.get("actual_duration_minutes"):
                        dur = f" ({r['actual_duration_minutes']:.0f}m)"
                    elif r.get("started_at"):
                        elapsed = (r.get("ended_at") or time.time()) - r["started_at"]
                        dur = f" ({elapsed/60:.0f}m)"
                    lines.append(
                        f"  [{status_color}]#{r.get('set_number', '?')}[/{status_color}] "
                        f"[bold]{r.get('title', '?')}[/bold] "
                        f"| {r.get('mood', '')} | {started}{dur} | "
                        f"{r.get('track_count', 0)} tracks | [{status_color}]{r.get('status', '?')}[/{status_color}]"
                    )
            else:
                lines.append("  [dim]No sets yet[/dim]")
        except Exception as e:
            lines.append(f"  [red]DB error: {e}[/red]")
        lines.append("")
        self.log_widget.write("\n".join(lines))

    def show_queue(self):
        """Show next track on idle deck + planner plan."""
        state = read_state()
        lines = ["\n[bold]QUEUE[/bold]"]

        # Next track from state
        if state:
            nt = state.get("next_track")
            if nt and nt.get("title"):
                lines.append(f"  [cyan]Next (deck {nt.get('deck', '?')}):[/cyan] {nt['title']}")
            else:
                lines.append("  [dim]No track on idle deck[/dim]")

        # Planner output from playlist file
        playlist_file = Path("/tmp/dj-treta-playlist.json")
        if playlist_file.exists():
            try:
                playlist = json.loads(playlist_file.read_text())
                output = playlist.get("planner_output", "")
                if output:
                    lines.append(f"\n  [bold]Planner Plan:[/bold]")
                    for pline in output.split("\n")[:15]:
                        if pline.strip():
                            lines.append(f"  [dim]{pline.strip()[:80]}[/dim]")
            except Exception:
                pass
        else:
            lines.append("  [dim]No planner output yet[/dim]")
        lines.append("")
        self.log_widget.write("\n".join(lines))

    def show_brain_decisions(self):
        """Show last DJ decisions from thinking log."""
        lines = ["\n[bold]DJ DECISIONS[/bold]"]
        if THINKING_LOG.exists():
            try:
                content = THINKING_LOG.read_text()
                think_lines = [l for l in content.split("\n") if l.startswith("[THINK:dj_treta]")]
                for tl in think_lines[-10:]:
                    thought = tl.split("] ", 1)[1] if "] " in tl else tl
                    if len(thought) > 120:
                        thought = thought[:120] + "..."
                    lines.append(f"  [italic]{thought}[/italic]")
                if not think_lines:
                    lines.append("  [dim]No decisions yet[/dim]")
            except Exception:
                lines.append("  [dim]Cannot read thinking log[/dim]")
        else:
            lines.append("  [dim]No thinking log[/dim]")
        lines.append("")
        self.log_widget.write("\n".join(lines))

    def show_db_stats(self):
        """Show library stats from SQLite DB."""
        lines = ["\n[bold]LIBRARY STATS[/bold]"]
        try:
            db = get_db()
            # Total + analyzed counts
            row = db.execute(
                "SELECT COUNT(*) as total, "
                "COUNT(CASE WHEN analyzed_at IS NOT NULL THEN 1 END) as analyzed, "
                "MIN(bpm) as min_bpm, MAX(bpm) as max_bpm, "
                "AVG(bpm) as avg_bpm "
                "FROM tracks"
            ).fetchone()
            if row:
                lines.append(f"  Total tracks: [bold]{row['total']}[/bold]  Analyzed: [bold]{row['analyzed']}[/bold]")
                if row['min_bpm']:
                    lines.append(f"  BPM range: {row['min_bpm']:.0f} - {row['max_bpm']:.0f} (avg {row['avg_bpm']:.0f})")

            # Genre counts
            genres = db.execute(
                "SELECT genre, COUNT(*) as cnt FROM tracks WHERE genre IS NOT NULL "
                "GROUP BY genre ORDER BY cnt DESC"
            ).fetchall()
            if genres:
                lines.append(f"\n  [bold]By genre:[/bold]")
                for g in genres:
                    lines.append(f"    {g['genre']}: {g['cnt']}")

            # Key distribution (top 5)
            keys = db.execute(
                "SELECT key_camelot, COUNT(*) as cnt FROM tracks "
                "WHERE key_camelot IS NOT NULL AND analyzed_at IS NOT NULL "
                "GROUP BY key_camelot ORDER BY cnt DESC LIMIT 8"
            ).fetchall()
            if keys:
                key_str = ", ".join(f"{k['key_camelot']}({k['cnt']})" for k in keys)
                lines.append(f"\n  [bold]Top keys:[/bold] {key_str}")

            # Sets count
            sets_row = db.execute("SELECT COUNT(*) as cnt FROM sets").fetchone()
            if sets_row:
                lines.append(f"\n  Total sets: [bold]{sets_row['cnt']}[/bold]")

            db.close()
        except Exception as e:
            lines.append(f"  [red]DB error: {e}[/red]")
        lines.append("")
        self.log_widget.write("\n".join(lines))

    def _show_planner_plan(self):
        """Read and display the full planner plan from playlist file."""
        playlist_file = Path("/tmp/dj-treta-playlist.json")
        if not playlist_file.exists():
            return
        try:
            playlist = json.loads(playlist_file.read_text())
            output = playlist.get("planner_output", "")
            if not output:
                return
            for pline in output.strip().split("\n"):
                pline = pline.strip()
                if not pline:
                    continue
                # Highlight track numbers and key info
                if pline.startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
                    self.log_widget.write(f"[cyan]    {pline}[/cyan]")
                elif pline.startswith(("- ", "* ", "•")):
                    self.log_widget.write(f"[dim]      {pline}[/dim]")
                elif "BPM" in pline or "Key" in pline or "Energy" in pline:
                    self.log_widget.write(f"[dim]      {pline}[/dim]")
                elif pline.startswith("**") or "path:" in pline.lower():
                    self.log_widget.write(f"[dim yellow]    {pline}[/dim yellow]")
                else:
                    self.log_widget.write(f"[yellow]    {pline}[/yellow]")
        except Exception:
            pass

    def show_relay_status(self):
        """Show relay connection status."""
        state = read_state()
        lines = ["\n[bold]RELAY STATUS[/bold]"]
        if state:
            enabled = state.get("relay_enabled", False)
            connected = state.get("relay_connected", False)
            lines.append(f"  Enabled: {'[green]Yes[/green]' if enabled else '[red]No[/red]'}")
            lines.append(f"  Connected: {'[green]Yes[/green]' if connected else '[red]No[/red]'}")
            # Read config for URL
            try:
                config_file = Path(__file__).parent / "config.yaml"
                if config_file.exists():
                    import yaml
                    cfg = yaml.safe_load(config_file.read_text()) or {}
                    relay_cfg = cfg.get("relay", {})
                    lines.append(f"  Server: {relay_cfg.get('server_url', '?')}")
                    lines.append(f"  Push rate: {relay_cfg.get('push_hz', '?')} Hz")
            except Exception:
                pass
        else:
            lines.append("  [dim]Brain offline[/dim]")
        lines.append("")
        self.log_widget.write("\n".join(lines))

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

    def action_like(self):
        self.run_brain_command("feedback", {"type": "like"})
        self.log_widget.write("[green]  👍 Liked![/green]")

    def action_dislike(self):
        self.run_brain_command("feedback", {"type": "dislike"})
        self.log_widget.write("[red]  👎 Disliked[/red]")

    def action_show_tracks(self):
        self.show_tracks()

    def action_show_set(self):
        self.show_set_history()


def main():
    app = DJTretaApp()
    app.run()


if __name__ == "__main__":
    main()
