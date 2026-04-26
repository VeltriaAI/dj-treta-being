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
    Footer, Header, Input, Label, RichLog, Static, TabbedContent, TabPane,
)

# DB access for track timeline + stats
sys.path.insert(0, str(Path(__file__).parent))
from agent.db import get_db, get_current_set, get_set_tracks, get_track_by_path
from agent.runtime_paths import runtime_path
from agent.tui_state_source import (
    StateSource, WebSocketRemoteStateSource,
    DEFAULT_LOCAL_WS_URL, DEFAULT_REMOTE_WS_URL,
)

# ── Config ────────────────────────────────────────────────────────────

MIXXX_URL = "http://localhost:7778"
# All TUI state comes from the daemon's WebSocket — no file IO. The daemon
# still writes state.json/billing.json/etc. for offline debugging, but the
# TUI never reads them. ``runtime_path`` is kept for the few side-effecting
# helpers (e.g. ``start_brain`` rotating the daemon log).
DAEMON_LOG = runtime_path("daemon.log")
MUSIC_DIR = Path.home() / "Music" / "DJTreta"

# Remote mode — human UI goes over WebSocket (public state + token-auth command).
# MCP stays reserved for AI agents (Himani, Claude Desktop), not this TUI.
DEFAULT_REMOTE_URL = DEFAULT_REMOTE_WS_URL  # wss://dj.treta.life/ws/state
REMOTE_TOKEN_FILE = Path.home() / ".djtreta-command-token"


def _load_remote_token() -> str | None:
    """Resolve the /ws/command token from env or ~/.djtreta-command-token.

    Env wins. File is a convenience for launches outside a shell that has
    the env exported (eg via a desktop shortcut or ``djtreta --remote``).

    Read-side (/ws/state) is public and needs no token; the token only
    authenticates the write channel.
    """
    # DJTRETA_MCP_TOKEN is checked as a fallback for users who configured
    # the original MCP-bearer token scheme (from the task spec). On the
    # public WebSocket, the token is used on /ws/command only.
    for envvar in ("DJTRETA_COMMAND_TOKEN", "DJTRETA_RELAY_TOKEN", "DJTRETA_MCP_TOKEN"):
        tok = os.environ.get(envvar, "").strip()
        if tok:
            return tok
    try:
        if REMOTE_TOKEN_FILE.exists():
            text = REMOTE_TOKEN_FILE.read_text().strip()
            return text or None
    except Exception:
        pass
    return None


# Commands with no remote equivalent (the VM already runs a continuous set).
BRAIN_CMD_LOCAL_ONLY = {"play", "stop", "reset"}

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

# WS is the canonical protocol — daemon proxies all Mixxx HTTP calls.
# When the TUI app is up, it registers its state_source here so module-level
# helpers (mixxx_get / mixxx_post / get_track_name) route through WS too.
_active_state_source = None

def set_active_state_source(src) -> None:
    """Called by DJTretaTUI on mount so module-level helpers route via WS."""
    global _active_state_source
    _active_state_source = src

def mixxx_get(path: str) -> dict | None:
    """GET a Mixxx endpoint via the daemon's WS proxy.

    /api/status and /api/live are pushed to the TUI at 5 Hz, so reads of
    those return the cached snapshot — no round-trip. Other paths are
    proxied synchronously through /ws/command (one round-trip per call).
    """
    src = _active_state_source
    if src is not None:
        if path == "/api/status":
            cached = src.read_mixxx_status()
            if cached is not None:
                return cached
        elif path == "/api/live":
            cached = src.read_mixxx_live()
            if cached is not None:
                return cached
        return src.mixxx_proxy(path, "GET")
    # Fallback for tools that import this module before the app is mounted.
    try:
        return httpx.get(f"{MIXXX_URL}{path}", timeout=2).json()
    except Exception:
        return None

def mixxx_post(path: str, data: dict) -> dict | None:
    """POST to a Mixxx endpoint via the daemon's WS proxy."""
    src = _active_state_source
    if src is not None:
        return src.mixxx_proxy(path, "POST", data)
    try:
        return httpx.post(f"{MIXXX_URL}{path}", json=data, timeout=2).json()
    except Exception:
        return None

def read_state() -> dict | None:
    """Deprecated: file-based read kept as a no-op shim for any third-party
    helper still calling it. The TUI itself reads via ``state_source``.
    """
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
    """Deprecated: file-based read kept as a shim. Real reads now go via
    ``app.state_source.read_scheduled_transition()`` which is reachable
    through the WebSocket state stream.
    """
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


def _synthesize_mixxx_from_state(state: dict) -> tuple[dict, dict]:
    """REMOTE mode: fake a Mixxx status/live payload from current_track.

    The VM's Mixxx is not reachable from the Mac. We reconstruct just enough
    for the deck widgets to render track + BPM + key + position + duration.
    """
    ct = state.get("current_track") or {}
    nt = state.get("next_track") or {}

    def _deck_for_track(track: dict, playing: bool) -> dict:
        if not track:
            return {"track_loaded": False}
        dur = track.get("duration") or 0
        pos = track.get("position") or 0
        rem = track.get("remaining")
        if rem is None:
            rem = max(0, dur - pos) if dur else 0
        return {
            "track_loaded": True,
            "playing": playing,
            "bpm": track.get("bpm") or 0,
            "file_bpm": track.get("file_bpm") or track.get("bpm") or 0,
            "key": track.get("key") or 0,
            "position_seconds": pos,
            "duration": dur,
            "remaining_seconds": rem,
            "volume": 1.0,
            "rate": 0.0,
            "sync_enabled": False,
            "loop_enabled": False,
            "eq_hi": 1.0,
            "eq_mid": 1.0,
            "eq_lo": 1.0,
            "file_path": track.get("file_path") or "",
        }

    cur_deck = int(ct.get("deck") or 1)
    nxt_deck = int(nt.get("deck") or (2 if cur_deck == 1 else 1))

    d1 = _deck_for_track(ct, True) if cur_deck == 1 else _deck_for_track(nt if nxt_deck == 1 else {}, False)
    d2 = _deck_for_track(ct, True) if cur_deck == 2 else _deck_for_track(nt if nxt_deck == 2 else {}, False)

    status = {
        "deck1": d1,
        "deck2": d2,
        "crossfader": -1 if cur_deck == 1 else 1,
        "master_volume": 1.0,
        "headphone_volume": 0.0,
    }
    # Minimal live data — no VU in remote mode (would need more tools).
    live = {
        "deck1": {"vu_left": 0, "vu_right": 0, "beat_active": False, "beat_distance": 0, "peak_indicator": False},
        "deck2": {"vu_left": 0, "vu_right": 0, "beat_active": False, "beat_distance": 0, "peak_indicator": False},
        "master_vu_left": 0,
        "master_vu_right": 0,
    }
    return status, live


def send_command(command: str, args: dict = {}) -> str:
    """Deprecated: file-based command dispatch. Use
    ``app.state_source.send_command(command, args)`` instead — works
    identically for local + remote daemons over /ws/command.
    """
    raise RuntimeError(
        "send_command(file-based) removed in feat/tui-websocket-unified. "
        "Use app.state_source.send_command(...) — it dispatches over "
        "/ws/command for both local and remote daemons."
    )


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
        # Derive rate% from bpm vs file_bpm — Mixxx's `rate` field has
        # ambiguous semantics across versions (sometimes slider position,
        # sometimes offset ratio). The bpm/file_bpm delta is always honest.
        if file_bpm > 0 and bpm > 0:
            rate = (bpm - file_bpm) / file_bpm * 100
        else:
            rate = 0.0
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
            # Prefer app state_source if available (LOCAL/REMOTE aware).
            try:
                state = self.app.state_source.read_state()
            except Exception:
                state = None
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

    def _mode_badge(self) -> str:
        """Return a [LOCAL] / [REMOTE host] / [REMOTE host ● DISCONNECTED] prefix."""
        try:
            src = self.app.state_source
        except Exception:
            return ""
        label = getattr(src, "label", "LOCAL")
        connected = getattr(src, "connected", True)
        detail = getattr(src, "status_detail", "")
        if "REMOTE" in label:
            if connected:
                extra = f" {detail}" if detail else ""
                return f"[bold cyan]\\[{label}{extra}][/bold cyan] "
            return f"[bold red]\\[{label} ● DISCONNECTED {detail}][/bold red] "
        # LOCAL
        if not connected:
            return f"[dim]\\[LOCAL daemon off][/dim] "
        return f"[green]\\[{label}][/green] "

    def update_brain(self, state: dict | None, status: dict | None = None):
        badge = self._mode_badge()
        if not state:
            self.update(f"{badge}[dim]Brain offline — /start to launch[/dim]")
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
            b = self.app.state_source.read_billing() if hasattr(self, 'app') else None
            if b:
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

        # Prefix first line with the LOCAL/REMOTE badge so mode is always visible.
        if lines:
            lines[0] = badge + lines[0]
        else:
            lines = [badge]
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

#log-tabs {
    width: 1fr;
    height: 100%;
    border-top: solid $accent;
}

TabPane {
    height: 1fr;
    padding: 0;
}

#log-all, #log-dj, #log-planner, #log-treta {
    height: 1fr;
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
    height: 3;
    margin-bottom: 0;
}

Footer {
    height: 1;
    dock: bottom;
}
"""


class DJTretaApp(App):
    """DJ Treta Terminal UI."""

    TITLE = "DJ Treta"
    SUB_TITLE = "An AI Being that DJs"
    CSS = CSS

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+s", "skip", "Skip", priority=True),
        Binding("ctrl+l", "like", "👍"),
        Binding("ctrl+d", "dislike", "👎"),
        Binding("f6", "tab_all", "All"),
        Binding("f7", "tab_treta", "Treta"),
        Binding("f8", "tab_dj", "DJ"),
        Binding("f9", "tab_planner", "Plan"),
        Binding("f11", "fullscreen", "Full"),
        Binding("f2", "toggle_debug", "Debug"),
        Binding("f4", "show_tracks", "Tracks"),
        Binding("f5", "show_set", "Set"),
        # ctrl+r instead of plain 'r' — prompt Input always has focus and
        # would otherwise swallow bare 'r' keypresses.
        Binding("ctrl+r", "toggle_remote", "Local/Remote", priority=True),
    ]

    debug_mode = reactive(False)

    # ── State source (always WebSocket; URL distinguishes local vs remote) ──
    def __init__(
        self,
        state_source: StateSource | None = None,
        remote_url: str | None = None,
        remote_token: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        # Default to a local-daemon WebSocket subscription. The same code
        # path serves remote — only the URL differs.
        self.state_source: StateSource = state_source or WebSocketRemoteStateSource(
            url=DEFAULT_LOCAL_WS_URL
        )
        # Stored so Ctrl+R can toggle to a remote URL and back.
        self._remote_url = remote_url
        self._remote_token = remote_token

    def _is_remote(self) -> bool:
        """True iff state_source is connected to a non-localhost daemon.

        ``WebSocketRemoteStateSource._is_local`` is True for ws://localhost,
        False for everything else (incl. wss://dj.treta.life).
        """
        ss = self.state_source
        if isinstance(ss, WebSocketRemoteStateSource):
            return not ss._is_local
        # Future-proof: any other StateSource subclass is treated as remote
        # (it isn't talking to localhost over our specific WS protocol).
        return True

    # ── State source consumption (single WebSocket via state_source) ──────
    #
    # The TUI no longer opens its own WebSocket connection. The state_source
    # owns the only socket; we drain its buffers from periodic poll funcs
    # already scheduled in on_mount(). _apply_ws_state / _apply_ws_log /
    # _apply_ws_thinking remain — they're now invoked from drain_* polls.

    async def action_quit(self) -> None:
        """Clean shutdown — close state source before exit."""
        try:
            self.state_source.close()
        except Exception:
            pass
        await super().action_quit()

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
        # Skip empty, noise, or too-short messages
        if not msg or not msg.strip() or len(msg.strip()) < 10:
            return
        msg = msg.strip()
        # Skip ANSI escape, LiteLLM noise, bare timestamps
        if msg.startswith("[") and "m" in msg[:10] and "[INFO]" not in msg:
            return
        if any(n in msg for n in ["utils.py:", "Wrapper:", "completion()", "server listening"]):
            return
        # Skip bare "HH:MM:SS [INFO]" with nothing after
        import re
        if re.match(r'^\d{2}:\d{2}:\d{2}\s*\[INFO\]\s*$', msg):
            return

        # Classify by agent, write to All + agent-specific tab
        all_w = self.log_widget

        if "DJ decision" in msg:
            decision = msg.replace("DJ decision: ", "").strip()
            line = f"[bold bright_blue]  DJ  [/bold bright_blue] {decision or '[dim](tool call)[/dim]'}"
            all_w.write(line)
            self._log_dj.write(line)
        elif "Executing" in msg or "Transition result" in msg:
            line = f"[bold cyan]  MIX [/bold cyan] {msg}"
            all_w.write(line)
            self._log_dj.write(line)
        elif "Transition scheduled" in msg or "schedule_transition" in msg:
            line = f"[bold cyan]  MIX [/bold cyan] {msg}"
            all_w.write(line)
            self._log_dj.write(line)
        elif "Auto-transition" in msg:
            line = f"[yellow]  AUTO[/yellow] {msg}"
            all_w.write(line)
            self._log_dj.write(line)
        elif "Loaded deck" in msg:
            line = f"[green]  LOAD[/green] {msg.replace('Loaded deck ', 'D')}"
            all_w.write(line)
            self._log_planner.write(line)
        elif "Planner done" in msg:
            line = f"[magenta]  PLAN[/magenta] done"
            all_w.write(line)
            self._log_planner.write(line)
            self._show_planner_plan()
        elif "Planner running" in msg:
            line = f"[magenta]  PLAN[/magenta] {msg.replace('Planner running — ', '')}"
            all_w.write(line)
            self._log_planner.write(line)
        elif "Enriched:" in msg:
            line = f"[dim]  SCAN[/dim] {msg.replace('Enriched: ', '')[:70]}"
            all_w.write(line)
            self._log_planner.write(line)
        elif "Evolution" in msg or "evolve" in msg.lower():
            line = f"[bold yellow]  EVO [/bold yellow] {msg}"
            all_w.write(line)
            self._log_treta.write(line)
        elif "Emergency" in msg:
            line = f"[bold red]  SOS [/bold red] {msg}"
            all_w.write(line)
            self._log_dj.write(line)
        elif "Backup" in msg:
            line = f"[red]  BKUP[/red] {msg}"
            all_w.write(line)
            self._log_planner.write(line)
        elif "Set started" in msg or "Set ended" in msg:
            line = f"[bold white]  SET [/bold white] {msg}"
            all_w.write(line)
        elif "Being thought" in msg or "Being reflect" in msg:
            line = f"[bright_cyan]  SELF[/bright_cyan] {msg.replace('Being thought: ', '').replace('Being reflect: ', '')[:70]}"
            all_w.write(line)
            self._log_treta.write(line)
        elif "ERROR" in text or "WARNING" in text:
            line = f"[red]  ERR [/red] {msg}"
            all_w.write(line)
        elif "Generated" in msg or "generate_track" in msg:
            line = f"[bold bright_magenta]  PROD[/bold bright_magenta] {msg}"
            all_w.write(line)
            self._log_planner.write(line)
        else:
            all_w.write(f"[dim]  ··· [/dim] {msg[:80]}")

    def _apply_ws_thinking(self, data: dict):
        """Apply thinking event — route to agent tab + activity panel."""
        agent = data.get("agent", "?")
        think_type = data.get("type", "")
        text = data.get("text", "")
        tool = data.get("tool", "")
        args = data.get("args", "")

        # Update agent activity panel
        try:
            activity = self.query_one("#agent-activity", AgentActivityWidget)
            if think_type == "call":
                activity.update_agent(agent, tool=tool or text)
            elif think_type == "think" and text:
                activity.update_agent(agent, text=text)
        except Exception:
            pass

        # Determine which tab this goes to
        if "dj_treta" in agent or "mixer" in agent:
            tab = self._log_dj
            color = "bright_blue"
        elif "planner" in agent or "library" in agent or "producer" in agent:
            tab = self._log_planner
            color = "magenta"
        elif "consciousness" in agent or "treta" in agent:
            tab = self._log_treta
            color = "bright_cyan"
        else:
            tab = self._log_treta
            color = "dim"

        if think_type == "think":
            if "dj_treta" in agent:
                # Ring buffer entries kept short to keep /brain output scannable.
                self._last_dj_decision = text[:300]
                self._last_dj_decision_time = time.time()
                buf = getattr(self, "_recent_dj_thoughts", None)
                if buf is None:
                    buf = []
                    self._recent_dj_thoughts = buf
                buf.append(text[:300])
                if len(buf) > 30:
                    del buf[:-30]
            if text and len(text.strip()) > 5:
                # Full text — RichLog has wrap=True so it word-wraps naturally.
                line = f"[{color}]  {agent}:[/{color}] [italic]{text}[/italic]"
                tab.write(line)
                # Mirror to All tab so the user has a single chronological feed.
                self.log_widget.write(line)

        elif think_type == "call":
            tool_name = tool or text or "?"
            # Full args — RichLog wraps. Truncating cut off important JSON
            # context (planner reasoning summaries, schedule_transition params).
            line = f"[bold {color}]  {agent}:[/bold {color}] [cyan]{tool_name}({args})[/cyan]"
            tab.write(line)
            # Mirror to All tab so the user has a single chronological feed.
            self.log_widget.write(line)

        # Debug panel gets everything raw
        debug_visible = self.query_one("#debug-log").has_class("visible")
        if debug_visible:
            if think_type == "think" and text:
                self.debug_widget.write(f"[bold bright_white]  {agent}:[/bold bright_white] [italic]{text[:300]}[/italic]")
            elif think_type == "call":
                self.debug_widget.write(f"[cyan]  {agent} -> {tool or text}({args[:100]})[/cyan]")

    # ── End WebSocket ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="decks"):
            yield DeckWidget(1, id="deck1")
            yield DeckWidget(2, id="deck2")
        yield MixerWidget(id="mixer")
        yield BrainWidget(id="brain")
        with Horizontal(id="main-area"):
            with TabbedContent(id="log-tabs"):
                with TabPane("All", id="tab-all"):
                    yield RichLog(id="log-all", highlight=True, markup=True, wrap=True)
                with TabPane("Treta", id="tab-treta"):
                    yield RichLog(id="log-treta", highlight=True, markup=True, wrap=True)
                with TabPane("DJ", id="tab-dj"):
                    yield RichLog(id="log-dj", highlight=True, markup=True, wrap=True)
                with TabPane("Planner", id="tab-planner"):
                    yield RichLog(id="log-planner", highlight=True, markup=True, wrap=True)
            with Vertical(id="right-panel"):
                yield AgentActivityWidget(id="agent-activity")
                with ScrollableContainer(id="playlist-scroll"):
                    yield PlaylistWidget(id="playlist")
        yield RichLog(id="debug-log", highlight=True, markup=True, wrap=True)
        yield Input(placeholder="Talk to DJ Treta... (or /help)", id="prompt-input")
        yield Footer()

    def on_mount(self) -> None:
        # Register the state source for module-level mixxx_get / mixxx_post
        # so they route Mixxx HTTP through the daemon's WS proxy.
        set_active_state_source(self.state_source)
        self.log_widget = self.query_one("#log-all", RichLog)
        self._log_dj = self.query_one("#log-dj", RichLog)
        self._log_planner = self.query_one("#log-planner", RichLog)
        self._log_treta = self.query_one("#log-treta", RichLog)
        self.debug_widget = self.query_one("#debug-log", RichLog)
        self.log_widget.write("[dim]DJ Treta Console. Type anything to talk, /help for commands.[/dim]\n")
        self.log_widget.write(
            f"[dim]  state: {self.state_source.label}[/dim]\n"
        )
        # Cached snapshots / mode flags consumed by render funcs
        self._ws_state = None
        self._last_dj_decision = ""
        self._last_dj_decision_time = 0.0
        self._last_state_source_connected: bool | None = None
        # Periodic UI refresh from the state source's caches (drained, not
        # polled from disk). The state source itself runs a single WebSocket
        # in its own thread.
        self.set_interval(1.0, self.refresh_status)
        self.set_interval(0.5, self.drain_ws_logs)
        self.set_interval(0.5, self.drain_ws_thinking)
        self.set_interval(2.0, self.refresh_connection_status)

    def _switch_tab(self, tab_id: str):
        tabs = self.query_one("#log-tabs", TabbedContent)
        tabs.active = tab_id

    def action_tab_all(self): self._switch_tab("tab-all")
    def action_tab_treta(self): self._switch_tab("tab-treta")
    def action_tab_dj(self): self._switch_tab("tab-dj")
    def action_tab_planner(self): self._switch_tab("tab-planner")

    def action_fullscreen(self):
        """Toggle fullscreen tabs — hide decks/mixer/brain for focused view."""
        decks = self.query_one("#decks")
        mixer = self.query_one("#mixer")
        brain = self.query_one("#brain")
        right = self.query_one("#right-panel")

        if decks.display:
            # Enter fullscreen
            decks.display = False
            mixer.display = False
            brain.display = False
            right.display = False
            self.log_widget.write("[yellow]  Fullscreen mode — F11 to exit[/yellow]")
        else:
            # Exit fullscreen
            decks.display = True
            mixer.display = True
            brain.display = True
            right.display = True

    def action_toggle_remote(self) -> None:
        """Toggle between LOCAL daemon and REMOTE relay — by swapping the
        state-source URL. Same WebSocket code path either way.

        Read-side (/ws/state) is public and needs no auth. The optional token
        only authenticates write commands (mood/skip/talk/…) sent to the VM's
        /ws/command endpoint — read-only viewing works without it.
        """
        if self._is_remote():
            # Switch to LOCAL daemon.
            try:
                self.state_source.close()
            except Exception:
                pass
            self.state_source = WebSocketRemoteStateSource(url=DEFAULT_LOCAL_WS_URL)
            set_active_state_source(self.state_source)
            self.log_widget.write(
                f"[green]  → Switched to LOCAL ({DEFAULT_LOCAL_WS_URL})[/green]"
            )
            self.log_widget.write(
                "[dim]  (if no local daemon is running, start one with /start)[/dim]"
            )
        else:
            url = self._remote_url or DEFAULT_REMOTE_URL
            token = self._remote_token or _load_remote_token()
            try:
                try:
                    self.state_source.close()
                except Exception:
                    pass
                self.state_source = WebSocketRemoteStateSource(
                    url=url, command_token=token
                )
                set_active_state_source(self.state_source)
                self._remote_url = url
                self._remote_token = token
                self.log_widget.write(f"[cyan]  → Switched to REMOTE → {url}[/cyan]")
                if not token:
                    self.log_widget.write(
                        "[yellow]  (read-only: no command token. Set DJTRETA_COMMAND_TOKEN "
                        f"or write it to {REMOTE_TOKEN_FILE} to enable writes.)[/yellow]"
                    )
            except Exception as exc:
                self.log_widget.write(f"[red]  Remote connect failed: {exc}[/red]")

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

    def refresh_connection_status(self) -> None:
        """Surface state-source connect/disconnect transitions in the log."""
        try:
            now = self.state_source.connected
        except Exception:
            now = False
        prev = self._last_state_source_connected
        if prev is None:
            self._last_state_source_connected = now
            if now:
                self.log_widget.write(
                    f"[green]  ⚡ {self.state_source.label} connected[/green]"
                )
            return
        if now != prev:
            self._last_state_source_connected = now
            if now:
                self.log_widget.write(
                    f"[green]  ⚡ {self.state_source.label} reconnected[/green]"
                )
            else:
                detail = self.state_source.status_detail or "disconnected"
                self.log_widget.write(
                    f"[yellow]  • {self.state_source.label}: {detail}[/yellow]"
                )

    def refresh_status(self) -> None:
        remote = self._is_remote()
        # In remote mode, the VM owns Mixxx — don't hit localhost. Locally,
        # we still query Mixxx directly for the rich live data (VU meters,
        # beat phase) that isn't worth round-tripping through the WebSocket.
        status = None if remote else mixxx_get("/api/status")
        live = None if remote else mixxx_get("/api/live")
        state = self.state_source.read_state()
        # Update the cached agent_busy/planner_status flags from the latest
        # WS state — preserves the old _apply_ws_state side effect cheaply.
        if state:
            self._apply_ws_state(state)

        # In remote mode, synthesize a minimal deck/live payload from
        # current_track so the deck widgets have something to render.
        if remote and state:
            status, live = _synthesize_mixxx_from_state(state)

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

    def drain_ws_logs(self) -> None:
        """Pull buffered ``log`` events from the state source and render."""
        try:
            lines = self.state_source.drain_logs()
        except Exception:
            lines = []
        for line in lines:
            try:
                self._apply_ws_log(line)
            except Exception:
                pass

    def _route_to_agent_tab(self, agent: str):
        """Get the right tab log for an agent name."""
        if "dj_treta" in agent or "mixer" in agent:
            return self._log_dj, "bright_blue"
        elif "planner" in agent or "library" in agent or "producer" in agent:
            return self._log_planner, "magenta"
        elif "consciousness" in agent or "treta" in agent:
            return self._log_treta, "bright_cyan"
        return self._log_treta, "dim"

    def drain_ws_thinking(self) -> None:
        """Pull buffered ``thinking`` events from the state source and render."""
        try:
            events = self.state_source.drain_thinking()
        except Exception:
            events = []
        for evt in events:
            try:
                self._apply_ws_thinking(evt)
            except Exception:
                pass

    def poll_thinking_log(self) -> None:
        """Deprecated: agent thinking now arrives via the WebSocket
        ``thinking`` event drained by ``drain_ws_thinking``. Kept as a
        no-op stub for any external caller still referencing it.
        """
        return

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
                "  [cyan]<message>[/cyan]              Talk to DJ Treta\n"
                "  [cyan]/play[/cyan] [mood] [min]     Start a set (LOCAL only — remote runs continuously)\n"
                "  [cyan]/stop[/cyan]                  Stop the set (LOCAL only)\n"
                "  [cyan]/mood[/cyan] <name>           Change mood\n"
                "  [cyan]/skip[/cyan]                  Skip (smooth transition)\n"
                "  [bold]Mixer[/bold]\n"
                "  [cyan]/pause[/cyan] [deck]          Pause a deck\n"
                "  [cyan]/resume[/cyan] [deck]         Resume (play) a deck\n"
                "  [cyan]/volume[/cyan] <deck> <0-1>   Deck volume\n"
                "  [cyan]/crossfade[/cyan] <0-1>       Crossfader (0=deck1, 1=deck2)\n"
                "  [cyan]/eq[/cyan] <deck> <band> <v>  EQ band (hi/mid/lo, 0-4)\n"
                "  [cyan]/filter[/cyan] <deck> <0-1>   Filter (0.5 = neutral)\n"
                "  [bold]Library[/bold]\n"
                "  [cyan]/load[/cyan] <d> <name>       Load track by name fuzzy\n"
                "  [cyan]/search[/cyan] <query>        Search library (remote: SQLite FTS)\n"
                "  [cyan]/request[/cyan] <artist> <title>  Request a track to fetch\n"
                "  [cyan]/tracks[/cyan]                List library (LOCAL only)\n"
                "  [bold]Set + state[/bold]\n"
                "  [cyan]/set[/cyan]                   Show current set info\n"
                "  [cyan]/set history[/cyan]           Show all sets from DB\n"
                "  [cyan]/queue[/cyan]                 Show next track + planner plan\n"
                "  [cyan]/brain[/cyan]                 Show last DJ decisions\n"
                "  [cyan]/stats[/cyan]                 Show library stats\n"
                "  [cyan]/relay[/cyan]                 Show relay status\n"
                "  [cyan]/like[/cyan]  /dislike        Feedback on current track\n"
                "  [cyan]/sources[/cyan] <src> on|off  Toggle music sources\n"
                "  [cyan]/cost[/cyan]                  Show billing details\n"
                "  [cyan]/start[/cyan] /kill           Start / kill local daemon\n"
                "  [cyan]/help[/cyan]                  This help\n"
                "  [cyan]/remote[/cyan] [URL]          Connect to remote VM daemon via MCP\n"
                "  [cyan]/local[/cyan]                 Switch back to LOCAL file mode\n"
                "  [dim]Ctrl+Q quit | F2 debug | Ctrl+S skip | Ctrl+R toggle LOCAL/REMOTE[/dim]\n"
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
            try:
                deck = int(args[0]) if args else 1
            except ValueError:
                self.log_widget.write("[red]  Usage: /pause [1|2][/red]")
                return
            self.log_widget.write(f"[yellow]  Pausing Deck {deck}...[/yellow]")
            self._dispatch_mixxx_async("/api/pause", {"deck": deck}, f"Paused Deck {deck}")
        elif cmd == "resume":
            try:
                deck = int(args[0]) if args else 1
            except ValueError:
                self.log_widget.write("[red]  Usage: /resume [1|2][/red]")
                return
            self.log_widget.write(f"[yellow]  Resuming Deck {deck}...[/yellow]")
            self._dispatch_mixxx_async("/api/play", {"deck": deck}, f"Playing Deck {deck}")
        elif cmd == "volume" and len(args) >= 2:
            try:
                deck = int(args[0])
                vol = float(args[1])
            except ValueError:
                self.log_widget.write("[red]  Usage: /volume <deck> <0.0-1.0>[/red]")
                return
            self._dispatch_mixxx_async(
                # Mixxx /api/volume expects "level" not "volume"
                "/api/volume", {"deck": deck, "level": vol},
                f"Deck {deck} volume → {vol:.2f}",
            )
        elif cmd == "crossfade" and args:
            try:
                pos = float(args[0])
            except ValueError:
                self.log_widget.write("[red]  Usage: /crossfade <0.0-1.0>[/red]")
                return
            self._dispatch_mixxx_async(
                "/api/crossfade", {"position": pos},
                f"Crossfader → {pos:.2f}",
            )
        elif cmd == "eq" and len(args) >= 3:
            try:
                deck = int(args[0])
                band = args[1].lower()
                val = float(args[2])
            except ValueError:
                self.log_widget.write("[red]  Usage: /eq <deck> <hi|mid|lo> <0.0-4.0>[/red]")
                return
            self._dispatch_mixxx_async(
                # Mixxx /api/eq uses band name as JSON key (hi/mid/lo)
                "/api/eq", {"deck": deck, band: val},
                f"Deck {deck} EQ {band} → {val:.2f}",
            )
        elif cmd == "filter" and len(args) >= 2:
            try:
                deck = int(args[0])
                val = float(args[1])
            except ValueError:
                self.log_widget.write("[red]  Usage: /filter <deck> <0.0-1.0>[/red]")
                return
            self._dispatch_mixxx_async(
                "/api/filter", {"deck": deck, "value": val},
                f"Deck {deck} filter → {val:.2f}",
            )
        elif cmd == "search" and args:
            self._search_library(" ".join(args))
        elif cmd == "request" and len(args) >= 2:
            # /request <artist> | <title>  or  /request artist title
            rest = " ".join(args)
            if "|" in rest:
                artist, _, title = rest.partition("|")
            else:
                # Heuristic: first token = artist, remainder = title
                artist, title = args[0], " ".join(args[1:])
            self._request_track_remote(artist.strip(), title.strip())
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
        elif cmd in ("remote", "local"):
            # Explicit toggle — /remote or /local or /remote URL
            if cmd == "remote" and args and args[0].startswith(("http://", "https://")):
                self._remote_url = args[0]
            # If already in requested mode, no-op; otherwise toggle.
            want_remote = (cmd == "remote")
            if want_remote != self._is_remote():
                self.action_toggle_remote()
            else:
                self.log_widget.write(f"[dim]  already in {cmd.upper()} mode[/dim]")
        else:
            self.log_widget.write(f"[red]Unknown: {text}[/red] — /help")

    @work(thread=True)
    def handle_talk(self, message: str) -> None:
        self.log_widget.write(f"\n[bold cyan]You:[/bold cyan] {message}")
        self.log_widget.write("[dim]  thinking...[/dim]")
        try:
            response = self.state_source.send_command(
                "talk", {"message": message}, timeout=300.0
            )
        except Exception as exc:
            response = f"[error] {exc}"
        self.log_widget.write(f"[bold magenta]DJ Treta:[/bold magenta] {response}\n")

    @work(thread=True)
    def run_brain_command(self, cmd: str, args: dict) -> None:
        # play / stop / reset target a daemon lifecycle, only meaningful
        # against the local daemon that ``start_brain`` controls.
        if self._is_remote() and cmd in BRAIN_CMD_LOCAL_ONLY:
            self.log_widget.write(
                f"[yellow]  /{cmd} not available against remote daemon (local-only)[/yellow]"
            )
            return
        try:
            response = self.state_source.send_command(cmd, args)
        except Exception as exc:
            response = f"[error] {exc}"
        self.log_widget.write(f"[green]  {response}[/green]")

    def _dispatch_mixxx(self, path: str, payload: dict) -> dict:
        """LOCAL → direct HTTP to localhost Mixxx; REMOTE → mixer command over
        /ws/command on the VM. Returns a best-effort result dict (never
        raises). Safe to call from the TUI thread.
        """
        if not self._is_remote():
            res = mixxx_post(path, payload) or {}
            return {"ok": True, "message": str(res) if res else f"posted {path}"}

        # Remote: wrap the Mixxx path + payload in a mixer command. The VM
        # server forwards this to /tmp/dj-treta-command.json with a special
        # "mixer" verb the daemon dispatches directly to Mixxx HTTP.
        try:
            out = self.state_source.send_command(
                "mixer", {"path": path, "payload": payload}, timeout=10.0
            )
        except Exception as exc:
            return {"ok": False, "message": f"(remote) mixer {path} failed: {exc}"}
        return {"ok": True, "message": str(out) if out else f"posted {path}"}

    @work(thread=True)
    def _dispatch_mixxx_async(self, path: str, payload: dict, success_msg: str) -> None:
        out = self._dispatch_mixxx(path, payload)
        if out.get("ok") is False:
            self.log_widget.write(f"[red]  {out.get('message', 'failed')}[/red]")
        else:
            self.log_widget.write(f"[green]  {success_msg}[/green]")

    def load_track(self, deck: int, query: str):
        """Resolve a fuzzy track name to a playable path and load it onto a
        deck. LOCAL mode walks ~/Music/DJTreta. REMOTE mode uses
        dj_search_library on the VM.
        """
        if self._is_remote():
            self._load_track_remote(deck, query)
            return
        for genre_dir in sorted(MUSIC_DIR.iterdir()):
            if not genre_dir.is_dir():
                continue
            for f in sorted(genre_dir.iterdir()):
                if query in f.stem.lower():
                    mixxx_post("/api/load", {"deck": deck, "track": str(f)})
                    self.log_widget.write(f"[green]  Loaded on Deck {deck}: {f.stem}[/green]")
                    return
        self.log_widget.write(f"[red]  No track matching '{query}'[/red]")

    @work(thread=True)
    def _load_track_remote(self, deck: int, query: str) -> None:
        """Forward a load-by-query to the VM daemon. The daemon resolves the
        query against its own library DB and drives Mixxx locally."""
        try:
            response = self.state_source.send_command(
                "load_track", {"deck": deck, "query": query}, timeout=15.0
            )
        except Exception as exc:
            self.log_widget.write(f"[red]  (remote) load failed: {exc}[/red]")
            return
        self.log_widget.write(f"[green]  {response}[/green]")

    @work(thread=True)
    def _search_library(self, query: str, limit: int = 8) -> None:
        """Search library — REMOTE forwards to daemon; LOCAL has /tracks."""
        try:
            response = self.state_source.send_command(
                "search_library", {"query": query, "limit": limit}, timeout=15.0
            )
        except Exception as exc:
            self.log_widget.write(f"[red]  search failed: {exc}[/red]")
            return
        self.log_widget.write(f"\n[bold]Search: {query}[/bold]\n  {response}")

    @work(thread=True)
    def _request_track_remote(self, artist: str, title: str) -> None:
        try:
            response = self.state_source.send_command(
                "request_track", {"artist": artist, "title": title}, timeout=15.0
            )
        except Exception as exc:
            self.log_widget.write(f"[red]  (remote) request failed: {exc}[/red]")
            return
        self.log_widget.write(f"[green]  {response}[/green]")

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
        state = self.state_source.read_state()
        status = None if self._is_remote() else mixxx_get("/api/status")
        if self._is_remote() and state:
            status, _ = _synthesize_mixxx_from_state(state)

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
        b = self.state_source.read_billing()
        if not b:
            self.log_widget.write("[dim]No billing data yet[/dim]")
            return

        try:
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
        state = self.state_source.read_state()
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
        state = self.state_source.read_state()
        lines = ["\n[bold]QUEUE[/bold]"]

        # Next track from state
        if state:
            nt = state.get("next_track")
            if nt and nt.get("title"):
                lines.append(f"  [cyan]Next (deck {nt.get('deck', '?')}):[/cyan] {nt['title']}")
            else:
                lines.append("  [dim]No track on idle deck[/dim]")

        # Planner output: surfaced via WS state when the planner publishes
        # one. (Legacy ``playlist.json`` file was never written by the
        # current daemon; removed to keep the TUI WebSocket-only.)
        st = self.state_source.read_state() or {}
        plan_output = (st.get("planner") or {}).get("output", "")
        if plan_output:
            lines.append("\n  [bold]Planner Plan:[/bold]")
            for pline in plan_output.split("\n")[:15]:
                if pline.strip():
                    lines.append(f"  [dim]{pline.strip()[:80]}[/dim]")
        else:
            lines.append("  [dim]No planner output yet[/dim]")
        lines.append("")
        self.log_widget.write("\n".join(lines))

    def show_brain_decisions(self):
        """Show last DJ decisions captured from the WebSocket ``thinking``
        stream. We keep a small ring buffer in ``_recent_dj_thoughts``
        populated by ``_apply_ws_thinking``."""
        lines = ["\n[bold]DJ DECISIONS[/bold]"]
        thoughts = getattr(self, "_recent_dj_thoughts", [])
        if thoughts:
            for t in thoughts[-10:]:
                snippet = t if len(t) <= 120 else t[:120] + "..."
                lines.append(f"  [italic]{snippet}[/italic]")
        else:
            lines.append("  [dim]No decisions yet[/dim]")
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
        """Render the planner plan if state has surfaced one. Local
        ``playlist.json`` is no longer consulted — planner_output, when
        produced, is broadcast via the WebSocket ``state`` event under
        ``state.planner.output``.
        """
        st = self.state_source.read_state() or {}
        output = (st.get("planner") or {}).get("output", "")
        if not output:
            return
        try:
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
        state = self.state_source.read_state()
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


def main(*, remote: bool = False, remote_url: str | None = None, remote_token: str | None = None):
    """Launch the TUI.

    All TUI data flows over WebSocket — same code path locally and remotely;
    only the URL changes.

    Args:
        remote: if True, connect to the public relay (defaults to
            ``DEFAULT_REMOTE_URL`` = ``wss://dj.treta.life/ws/state``).
            If False, connect to the local daemon's own ws_server at
            ``DEFAULT_LOCAL_WS_URL`` = ``ws://localhost:7779/ws/state``.
        remote_url: explicit WS state URL to use when ``remote`` is True
            (or anytime the user passed ``--remote URL``).
        remote_token: optional command token for the /ws/command auth.
            Falls back to env (DJTRETA_COMMAND_TOKEN / DJTRETA_RELAY_TOKEN /
            DJTRETA_MCP_TOKEN) or ~/.djtreta-command-token. Read-side works
            without it; writes require it on the public relay. Locally the
            daemon's ws_server currently doesn't enforce a token.
    """
    token = remote_token or _load_remote_token()
    if remote:
        url = remote_url or DEFAULT_REMOTE_URL
        if not token:
            print(
                "info: remote read-only (no command token set).\n"
                "  Set DJTRETA_COMMAND_TOKEN, pass --token TOK, or write the token "
                f"to {REMOTE_TOKEN_FILE} to enable writes.",
                file=sys.stderr,
            )
    else:
        # Local daemon — ws_server.py listens at ws://localhost:7779/ws/state.
        url = remote_url or DEFAULT_LOCAL_WS_URL

    state_source: StateSource = WebSocketRemoteStateSource(
        url=url, command_token=token
    )

    app = DJTretaApp(
        state_source=state_source,
        remote_url=remote_url or DEFAULT_REMOTE_URL,  # ctrl+R will fall back to this
        remote_token=token,
    )
    app.run()


if __name__ == "__main__":
    # Minimal CLI so `python tui.py --remote ...` works directly too.
    import argparse
    ap = argparse.ArgumentParser(description="DJ Treta TUI")
    ap.add_argument("--remote", nargs="?", const=DEFAULT_REMOTE_URL, default=None,
                    help="Connect to remote VM over WebSocket (default URL if flag alone)")
    ap.add_argument("--token", default=None, help="Command token for /ws/command (optional; read is public)")
    ns = ap.parse_args()
    main(remote=ns.remote is not None, remote_url=ns.remote, remote_token=ns.token)
