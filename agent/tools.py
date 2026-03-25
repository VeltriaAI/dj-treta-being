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
def load_track(deck: int, track_path: str) -> str:
    """Load a track onto a deck. Accepts full path OR partial name (will search library).

    Args:
        deck: The deck number to load onto, either 1 or 2.
        track_path: Full file path OR partial track name to search for.
    """
    path = Path(track_path)

    # If not a valid absolute path, search the library
    if not path.is_absolute() or not path.exists():
        query = track_path.lower()
        found = None
        for genre_dir in sorted(_MUSIC_DIR.iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
                continue
            for f in sorted(genre_dir.iterdir()):
                if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                    if query in f.stem.lower() or query in f.name.lower():
                        found = str(f)
                        break
            if found:
                break

        if not found:
            return f"ERROR: Track not found in library: '{track_path}'. Use list_library_tracks to see available tracks."

        track_path = found

    result = _mixxx_post("/api/load", {"deck": deck, "track": track_path})
    if result and result.get("ok"):
        return f"Loaded on Deck {deck}: {Path(track_path).stem}"
    return f"ERROR: Mixxx rejected load: {result}"


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
# AUDIO PERCEPTION — Hear the music through Gemini
# ═══════════════════════════════════════════════════════════════════════

@tool
def hear_music(deck: int = 0, duration: int = 10) -> str:
    """Listen to what's currently playing. Extracts audio from the track file
    at the current playback position and analyzes it with Gemini's audio model.

    Returns a description of: mood, energy (1-10), structure (breakdown/buildup/groove/drop),
    and notable audio elements.

    Args:
        deck: Deck to listen to (1 or 2). 0 = auto-detect active deck.
        duration: Seconds of audio to analyze (5-15).
    """
    import base64
    import subprocess as _sp

    duration = max(5, min(15, duration))

    # Get current state
    status = _mixxx_get("/api/status")
    if not status:
        return "Can't hear — Mixxx not responding"

    # Find active deck
    if deck == 0:
        d1 = status.get("deck1", {})
        d2 = status.get("deck2", {})
        if d1.get("playing"):
            deck = 1
        elif d2.get("playing"):
            deck = 2
        else:
            return "Can't hear — nothing playing"

    deck_status = status.get(f"deck{deck}", {})
    if not deck_status.get("playing"):
        return f"Can't hear — Deck {deck} not playing"

    pos = deck_status.get("position_seconds", 0)

    # Get file path
    tinfo = _mixxx_get(f"/api/deck/{deck}/track_info")
    if not tinfo or tinfo.get("error"):
        return "Can't hear — no track info"

    file_path = tinfo.get("file_path", "")
    if not file_path or not Path(file_path).exists():
        return f"Can't hear — file not found: {file_path}"

    title = tinfo.get("title", Path(file_path).stem)

    # Extract audio snippet with ffmpeg (mono 16kHz wav — small enough for API)
    snippet = "/tmp/dj-treta-snippet.wav"
    try:
        _sp.run(
            ["ffmpeg", "-y", "-ss", str(max(0, pos - 2)), "-t", str(duration),
             "-i", file_path, "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", snippet],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        return f"Can't extract audio: {e}"

    if not Path(snippet).exists():
        return "Audio extraction failed"

    # Send to Gemini
    try:
        with open(snippet, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()

        from litellm import completion as _completion
        resp = _completion(
            model=_cfg.llm.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        f"You are DJ Treta listening to '{title}' at {pos:.0f}s. "
                        "Describe what you hear in 2-3 sentences: mood, energy level (1-10), "
                        "structure (breakdown/buildup/groove/drop/intro/outro), "
                        "and any notable elements (vocals, synths, bass, percussion)."
                    )},
                    {"type": "image_url", "image_url": {"url": f"data:audio/wav;base64,{audio_b64}"}},
                ],
            }],
            api_base=_cfg.llm.api_base,
            api_key=_cfg.llm.api_key,
            temperature=0.5,
            timeout=30,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Gemini audio error: {e}"


# ═══════════════════════════════════════════════════════════════════════
# TRANSITIONS — Brain-controlled mixing
# ═══════════════════════════════════════════════════════════════════════

@tool
def do_transition(to_deck: int, duration: int = 60) -> str:
    """Execute a smooth crossfade transition to a deck.
    Uses Mixxx's C++ engine (20fps S-curve). After transition completes,
    the outgoing deck is paused and EQ/volume reset.

    IMPORTANT: The incoming deck must have a track loaded before calling this.
    Enable sync first with set_sync.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Transition duration in seconds (10-120).
    """
    import time as _time

    duration = max(10, min(120, duration))
    out_deck = 1 if to_deck == 2 else 2

    # Pre-flight: ABORT if incoming deck has no track loaded
    status = _mixxx_get("/api/status")
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded! Load a track first with load_track."

    # Ensure incoming is playing + synced
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.3)

    # Verify it actually started playing
    status2 = _mixxx_get("/api/status")
    if status2:
        deck_state2 = status2.get(f"deck{to_deck}", {})
        if not deck_state2.get("playing", False):
            return f"ABORTED: Deck {to_deck} failed to start playing. Track may not be loaded correctly."

    # Use Mixxx's server-side transition (C++, 20fps, non-blocking)
    _mixxx_post("/api/transition", {"deck": to_deck, "duration": duration})

    # Wait for it to complete
    _time.sleep(duration + 2)

    # Post-flight cleanup
    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/volume", {"deck": out_deck, "volume": 1.0})
    for band in ["hi", "mid", "lo"]:
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.5})

    return f"Transitioned to Deck {to_deck} over {duration}s. Deck {out_deck} paused and cleaned up."


@tool
def do_bass_swap(to_deck: int, duration: int = 60) -> str:
    """Execute a bass-swap transition (techno style).
    Phase 1: Bring incoming with bass cut. Phase 2: Swap bass. Phase 3: Fade out old.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Total transition duration in seconds (20-120).
    """
    import time as _time

    duration = max(20, min(120, duration))
    out_deck = 1 if to_deck == 2 else 2
    fps = 10
    total = int(duration * fps)

    # Sync + play incoming with bass killed
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/eq", {"deck": to_deck, "lo": 0.0})
    _mixxx_post("/api/play", {"deck": to_deck})

    for i in range(total + 1):
        t = i / total
        if t <= 0.4:
            # Phase 1: bring in incoming volume
            blend = t / 0.4
            _mixxx_post("/api/volume", {"deck": to_deck, "volume": round(blend, 2)})
        elif t <= 0.6:
            # Phase 2: bass swap
            swap_t = (t - 0.4) / 0.2
            _mixxx_post("/api/eq", {"deck": out_deck, "lo": round(1.0 - swap_t, 2)})
            _mixxx_post("/api/eq", {"deck": to_deck, "lo": round(swap_t, 2)})
        else:
            # Phase 3: fade out old
            fade = 1.0 - ((t - 0.6) / 0.4)
            _mixxx_post("/api/volume", {"deck": out_deck, "volume": round(fade, 2)})
        _time.sleep(1.0 / fps)

    # Cleanup
    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/volume", {"deck": out_deck, "volume": 1.0})
    for band in ["hi", "mid", "lo"]:
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
        _mixxx_post("/api/eq", {"deck": to_deck, band: 1.0})

    return f"Bass-swapped to Deck {to_deck} over {duration}s. Deck {out_deck} paused."


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
