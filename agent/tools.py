"""smolagents @tool functions — DJ Treta's hands, ears, and voice.

These are ALL the capabilities the Being has:
- DJ controls (Mixxx API)
- Music discovery (YouTube search + download)
- Library management
- Self-awareness (read own code, config, memory)
- Self-improvement (write code, update config, save learnings)
"""

import json
import os
import re
import subprocess
from pathlib import Path

import httpx
from smolagents import tool

from .config import load_config

_cfg = load_config()
_MIXXX = _cfg.mixxx.url
_MUSIC_DIR = _cfg.library.music_path
_TIMEOUT = _cfg.mixxx.timeout
_SELF_DIR = Path(__file__).parent.parent  # ~/beings/dj-treta


def _mixxx_get(path: str) -> dict:
    resp = httpx.get(f"{_MIXXX}{path}", timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _mixxx_post(path: str, data: dict | None = None) -> dict:
    resp = httpx.post(f"{_MIXXX}{path}", json=data or {}, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════
# DJ CONTROLS — Mixxx API
# ═══════════════════════════════════════════════════════════════════════

@tool
def get_dj_status() -> dict:
    """Get full DJ status — both decks, crossfader, BPM, key, remaining time, what's playing."""
    return _mixxx_get("/api/status")


@tool
def get_deck_info(deck: int) -> dict:
    """Get detailed info for a specific deck — track title, BPM, key, position, remaining time.

    Args:
        deck: The deck number, either 1 or 2.
    """
    status = _mixxx_get("/api/status")
    return status[f"deck{deck}"]


@tool
def load_track(deck: int, track_path: str) -> dict:
    """Load a track file onto a deck. Track path must be absolute.

    Args:
        deck: The deck number to load onto, either 1 or 2.
        track_path: Absolute file path to the audio track.
    """
    return _mixxx_post("/api/load", {"deck": deck, "track": track_path})


@tool
def play_deck(deck: int) -> dict:
    """Start playback on a deck.

    Args:
        deck: The deck number to play, either 1 or 2.
    """
    return _mixxx_post("/api/play", {"deck": deck})


@tool
def pause_deck(deck: int) -> dict:
    """Pause playback on a deck.

    Args:
        deck: The deck number to pause, either 1 or 2.
    """
    return _mixxx_post("/api/pause", {"deck": deck})


@tool
def set_volume(deck: int, volume: float) -> dict:
    """Set deck volume level.

    Args:
        deck: The deck number, either 1 or 2.
        volume: Volume level from 0.0 (silent) to 1.0 (full).
    """
    return _mixxx_post("/api/volume", {"deck": deck, "volume": volume})


@tool
def set_crossfader(position: float) -> dict:
    """Set crossfader position between decks.

    Args:
        position: Position from -1.0 (full deck 1) through 0.0 (center) to 1.0 (full deck 2).
    """
    return _mixxx_post("/api/crossfade", {"position": position})


@tool
def set_eq(deck: int, band: str, value: float) -> dict:
    """Set EQ band on a deck.

    Args:
        deck: The deck number, either 1 or 2.
        band: EQ band name — 'hi', 'mid', or 'lo'.
        value: EQ value from 0.0 (cut) to 4.0 (boost), 1.0 is neutral.
    """
    return _mixxx_post("/api/eq", {"deck": deck, "band": band, "value": value})


@tool
def set_filter(deck: int, value: float) -> dict:
    """Set quick-effect filter on a deck.

    Args:
        deck: The deck number, either 1 or 2.
        value: Filter from 0.0 (full high-pass) through 0.5 (neutral) to 1.0 (full low-pass).
    """
    return _mixxx_post("/api/filter", {"deck": deck, "value": value})


@tool
def set_sync(deck: int, enabled: bool) -> dict:
    """Enable or disable beat sync on a deck.

    Args:
        deck: The deck number, either 1 or 2.
        enabled: True to enable sync, False to disable.
    """
    return _mixxx_post("/api/sync", {"deck": deck, "enabled": enabled})


@tool
def get_live_data() -> dict:
    """Get real-time data — VU meters, beat position, crossfader. For feeling the music."""
    return _mixxx_get("/api/live")


@tool
def get_track_info(deck: int) -> dict:
    """Get deep track metadata from Mixxx — title, artist, BPM, key, duration, waveform, cue points, beat grid.

    Args:
        deck: The deck number, either 1 or 2.
    """
    return _mixxx_get(f"/api/deck/{deck}/track_info")


# ═══════════════════════════════════════════════════════════════════════
# MUSIC DISCOVERY — Search & Download
# ═══════════════════════════════════════════════════════════════════════

@tool
def search_music(query: str, limit: int = 5) -> list:
    """Search YouTube for music tracks. Returns titles, URLs, and durations.

    Args:
        query: Search query — artist name, track title, genre, anything.
        limit: Number of results to return (1-20).
    """
    result = subprocess.run(
        ["yt-dlp", f"ytsearch{limit}:{query}", "--dump-json", "--no-download", "--flat-playlist"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return [{"error": result.stderr[:200]}]

    results = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            info = json.loads(line)
            results.append({
                "title": info.get("title", "Unknown"),
                "url": info.get("url", info.get("webpage_url", "")),
                "id": info.get("id", ""),
                "duration": info.get("duration", 0),
                "uploader": info.get("uploader", info.get("channel", "Unknown")),
            })
        except json.JSONDecodeError:
            continue
    return results


@tool
def download_track(url: str, genre: str = "deep") -> str:
    """Download a track from YouTube into the music library.

    Args:
        url: YouTube URL to download.
        genre: Genre folder to save into (e.g., dark-techno, melodic-techno, deep, minimal, progressive, vocal, psychill).
    """
    genre_dir = _MUSIC_DIR / genre
    genre_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "320",
         "-o", str(genre_dir / "%(uploader)s - %(title)s.%(ext)s"),
         "--no-playlist", "--no-overwrites", url],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return f"Download failed: {result.stderr[:200]}"

    return f"Downloaded to {genre}/ folder"


# ═══════════════════════════════════════════════════════════════════════
# LIBRARY MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

@tool
def list_library_tracks() -> list:
    """List all tracks in the music library with file path, filename, and genre folder."""
    tracks = []
    for genre_dir in sorted(_MUSIC_DIR.iterdir()):
        if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
            continue
        genre = genre_dir.name
        for f in sorted(genre_dir.iterdir()):
            if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                tracks.append({
                    "path": str(f),
                    "filename": f.stem,
                    "genre": genre,
                })
    return tracks


@tool
def get_set_history() -> list:
    """Get the list of tracks played in the current set with titles and timestamps."""
    state_file = Path("/tmp/dj-treta-state.json")
    if state_file.exists():
        data = json.loads(state_file.read_text())
        return data.get("tracks_played", [])
    return []


# ═══════════════════════════════════════════════════════════════════════
# SELF-AWARENESS — Read own code, config, identity
# ═══════════════════════════════════════════════════════════════════════

@tool
def read_file(file_path: str) -> str:
    """Read any file — your own code, config, identity files, DJ knowledge, anything.

    Args:
        file_path: Path relative to ~/beings/dj-treta/ (e.g., 'agent/brain.py', 'config.yaml', '.beings/SOUL.md') or absolute path.
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = _SELF_DIR / file_path
    if not path.exists():
        return f"File not found: {path}"
    content = path.read_text()
    if len(content) > 10000:
        return content[:10000] + f"\n\n... (truncated, {len(content)} chars total)"
    return content


@tool
def write_file(file_path: str, content: str) -> str:
    """Write to a file — update your own code, config, save learnings, create new files.

    Args:
        file_path: Path relative to ~/beings/dj-treta/ (e.g., 'agent/tools.py', '.beings/MEMORY.md') or absolute path.
        content: The full content to write.
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = _SELF_DIR / file_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"Written {len(content)} chars to {path}"


@tool
def list_files(directory: str = ".") -> list:
    """List files in a directory. Defaults to ~/beings/dj-treta/.

    Args:
        directory: Path relative to ~/beings/dj-treta/ or absolute.
    """
    path = Path(directory)
    if not path.is_absolute():
        path = _SELF_DIR / directory
    if not path.exists():
        return [f"Directory not found: {path}"]
    return [str(f.relative_to(path)) for f in sorted(path.iterdir()) if not f.name.startswith('.')]


@tool
def run_shell(command: str) -> str:
    """Run a shell command. Use for: git, pip install, checking processes, anything system-level.

    Args:
        command: Shell command to execute.
    """
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=30, cwd=str(_SELF_DIR),
        )
        output = result.stdout + result.stderr
        if len(output) > 5000:
            output = output[:5000] + "\n... (truncated)"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out (30s limit)"


# ═══════════════════════════════════════════════════════════════════════
# SELF-IMPROVEMENT — Save learnings, update memory
# ═══════════════════════════════════════════════════════════════════════

@tool
def save_learning(topic: str, content: str) -> str:
    """Save something you learned — a mixing technique that worked, a track combination,
    a preference, anything worth remembering for future sets.

    Args:
        topic: Short topic name (e.g., 'transition-timing', 'track-pairing', 'eq-technique').
        content: What you learned.
    """
    memory_dir = _SELF_DIR / ".beings" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    memory_file = memory_dir / "learnings.json"
    learnings = []
    if memory_file.exists():
        try:
            learnings = json.loads(memory_file.read_text())
        except json.JSONDecodeError:
            learnings = []

    import time
    learnings.append({
        "topic": topic,
        "content": content,
        "timestamp": time.time(),
    })

    memory_file.write_text(json.dumps(learnings, indent=2))
    return f"Saved learning about '{topic}'. Total learnings: {len(learnings)}"


@tool
def recall_learnings(topic: str = "") -> list:
    """Recall past learnings. Optionally filter by topic.

    Args:
        topic: Optional topic filter (matches substring). Empty = return all.
    """
    memory_file = _SELF_DIR / ".beings" / "memory" / "learnings.json"
    if not memory_file.exists():
        return []

    try:
        learnings = json.loads(memory_file.read_text())
    except json.JSONDecodeError:
        return []

    if topic:
        learnings = [l for l in learnings if topic.lower() in l.get("topic", "").lower()
                     or topic.lower() in l.get("content", "").lower()]

    return learnings[-20:]  # last 20
