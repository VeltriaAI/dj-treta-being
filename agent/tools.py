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
import unicodedata
from pathlib import Path

import httpx
from smolagents import tool

from .config import Config, load_config

_SELF_DIR = Path(__file__).parent.parent


def _music_dir() -> Path:
    return load_config().library.music_path


def _roots(cfg: Config) -> list[Path]:
    return [_SELF_DIR.resolve(), cfg.library.music_path.expanduser().resolve()]


def _is_under_allowed_roots(cfg: Config, path: Path) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in _roots(cfg):
        try:
            rp.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _resolve_tool_path(cfg: Config, file_path: str) -> Path | None:
    raw = Path(file_path).expanduser()
    if not raw.is_absolute():
        path = (_SELF_DIR / raw).resolve()
    else:
        path = raw.resolve()
    if not _is_under_allowed_roots(cfg, path):
        return None
    return path


def _normalize_for_search(s: str) -> str:
    """Normalize unicode for fuzzy matching — strip emoji, normalize dashes/special chars."""
    s = ''.join(c for c in s if unicodedata.category(c) not in ('So', 'Sk', 'Sm'))
    s = s.replace('–', '-').replace('—', '-').replace('｜', '|').replace('：', ':')
    s = unicodedata.normalize('NFKC', s)
    return s.lower().strip()


def _mixxx_failed(d: dict) -> str | None:
    if d.get("_request_failed"):
        return d.get("_detail", "Mixxx request failed")
    return None


def _mixxx_get(path: str) -> dict:
    cfg = load_config()
    try:
        r = httpx.get(f"{cfg.mixxx.url}{path}", timeout=cfg.mixxx.timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_request_failed": True, "_detail": str(e)}


def _mixxx_post(path: str, data: dict | None = None) -> dict:
    cfg = load_config()
    try:
        r = httpx.post(f"{cfg.mixxx.url}{path}", json=data or {}, timeout=cfg.mixxx.timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_request_failed": True, "_detail": str(e)}


def _dj_get(path: str) -> dict:
    r = _mixxx_get(path)
    if err := _mixxx_failed(r):
        return {"error": err}
    return r


def _dj_post(path: str, data: dict | None = None) -> dict:
    r = _mixxx_post(path, data)
    if err := _mixxx_failed(r):
        return {"error": err}
    return r


# ═══════════════════════════════════════════════════════════════════════
# DJ CONTROLS — Mixxx API
# ═══════════════════════════════════════════════════════════════════════

@tool
def get_dj_status() -> dict:
    """Get full DJ status — both decks, crossfader, BPM, key, remaining time, what's playing."""
    data = _mixxx_get("/api/status")
    if err := _mixxx_failed(data):
        return {"error": err, "_request_failed": True}
    return data


@tool
def get_deck_info(deck: int) -> dict:
    """Get detailed info for a specific deck — track title, BPM, key, position, remaining time.

    Args:
        deck: The deck number, either 1 or 2.
    """
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return {"error": err}
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
        query = _normalize_for_search(track_path)
        found = None
        for genre_dir in sorted(_music_dir().iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
                continue
            for f in sorted(genre_dir.iterdir()):
                if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                    if query in _normalize_for_search(f.stem) or query in _normalize_for_search(f.name):
                        found = str(f)
                        break
            if found:
                break

        if not found:
            return f"ERROR: Track not found in library: '{track_path}'. Use list_library_tracks to see available tracks."

        track_path = found

    result = _mixxx_post("/api/load", {"deck": deck, "track": track_path})
    if err := _mixxx_failed(result):
        return f"ERROR: Mixxx load failed: {err}"
    if result and result.get("ok"):
        return f"Loaded on Deck {deck}: {Path(track_path).stem}"
    return f"ERROR: Mixxx rejected load: {result}"


@tool
def play_deck(deck: int) -> dict:
    """Start playback on a deck.

    Args:
        deck: The deck number to play, either 1 or 2.
    """
    return _dj_post("/api/play", {"deck": deck})


@tool
def pause_deck(deck: int) -> dict:
    """Pause playback on a deck.

    Args:
        deck: The deck number to pause, either 1 or 2.
    """
    return _dj_post("/api/pause", {"deck": deck})


@tool
def set_volume(deck: int, volume: float) -> dict:
    """Set deck volume level.

    Args:
        deck: The deck number, either 1 or 2.
        volume: Volume level from 0.0 (silent) to 1.0 (full).
    """
    return _dj_post("/api/volume", {"deck": deck, "volume": volume})


@tool
def set_crossfader(position: float) -> dict:
    """Set crossfader position between decks.

    Args:
        position: 0.0 = full Deck 1, 0.5 = center, 1.0 = full Deck 2.
    """
    # Clamp to valid range
    position = max(0.0, min(1.0, position))
    return _dj_post("/api/crossfade", {"position": position})


@tool
def set_eq(deck: int, band: str, value: float) -> dict:
    """Set EQ band on a deck.

    Args:
        deck: The deck number, either 1 or 2.
        band: EQ band name — 'hi', 'mid', or 'lo'.
        value: EQ value from 0.0 (cut) to 4.0 (boost), 1.0 is neutral.
    """
    return _dj_post("/api/eq", {"deck": deck, "band": band, "value": value})


@tool
def set_filter(deck: int, value: float) -> dict:
    """Set quick-effect filter on a deck.

    Args:
        deck: The deck number, either 1 or 2.
        value: Filter from 0.0 (full high-pass) through 0.5 (neutral) to 1.0 (full low-pass).
    """
    return _dj_post("/api/filter", {"deck": deck, "value": value})


@tool
def set_sync(deck: int, enabled: bool) -> dict:
    """Enable or disable beat sync on a deck.

    Args:
        deck: The deck number, either 1 or 2.
        enabled: True to enable sync, False to disable.
    """
    return _dj_post("/api/sync", {"deck": deck, "enabled": enabled})


@tool
def get_live_data() -> dict:
    """Get real-time data — VU meters, beat position, crossfader. For feeling the music."""
    return _dj_get("/api/live")


@tool
def get_track_info(deck: int) -> dict:
    """Get deep track metadata from Mixxx — title, artist, BPM, key, duration, waveform, cue points, beat grid.

    Args:
        deck: The deck number, either 1 or 2.
    """
    return _dj_get(f"/api/deck/{deck}/track_info")


# ═══════════════════════════════════════════════════════════════════════
# BPM / RATE CONTROL
# ═══════════════════════════════════════════════════════════════════════

@tool
def set_rate(deck: int, rate: float = 0.0) -> str:
    """Set the playback rate/pitch of a deck. Use to change BPM.

    rate=0.0 means original BPM (reset to file's native tempo).
    Positive = faster, negative = slower. Range roughly -0.5 to 0.5.

    To reset to original BPM: set_rate(deck, 0.0)
    To speed up by 3%: set_rate(deck, 0.03)

    Args:
        deck: Deck number (1 or 2).
        rate: Rate adjustment. 0.0 = original BPM.
    """
    # Also disable sync so rate change sticks
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "sync_enabled", "value": 0})
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "rate", "value": rate})

    # Read back actual BPM
    status = _mixxx_get("/api/status")
    if status and not _mixxx_failed(status):
        bpm = status.get(f"deck{deck}", {}).get("bpm", 0)
        file_bpm = status.get(f"deck{deck}", {}).get("file_bpm", 0)
        return f"Deck {deck}: rate={rate}, BPM now {bpm:.1f} (file: {file_bpm:.0f})"
    return f"Deck {deck}: rate set to {rate}"


@tool
def reset_bpm(deck: int) -> str:
    """Reset a deck to its original BPM — undoes any sync or rate changes.

    Args:
        deck: Deck number (1 or 2).
    """
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "sync_enabled", "value": 0})
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "rate", "value": 0})
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "rate_set_default", "value": 1})

    status = _mixxx_get("/api/status")
    if status and not _mixxx_failed(status):
        bpm = status.get(f"deck{deck}", {}).get("bpm", 0)
        file_bpm = status.get(f"deck{deck}", {}).get("file_bpm", 0)
        return f"Deck {deck}: BPM reset to original {file_bpm:.0f} (was {bpm:.1f})"
    return f"Deck {deck}: BPM reset to original"


# ═══════════════════════════════════════════════════════════════════════
# BEAT ALIGNMENT — Phase matching like a human DJ
# ═══════════════════════════════════════════════════════════════════════

@tool
def align_beats(deck: int) -> str:
    """Align the beats of a deck to match the other playing deck.
    This is like a human DJ nudging the jog wheel to get kicks landing together.
    Call this AFTER loading and syncing a track, BEFORE or DURING a transition.

    Args:
        deck: The deck to align (1 or 2). Its beats will snap to the other deck's grid.
    """
    # beatsync_phase = align phase without changing BPM
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "beatsync_phase", "value": 1})
    # Also enable quantize so future actions stay on-grid
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "quantize", "value": 1})
    return f"Beats aligned on Deck {deck} — phase matched and quantize enabled"


@tool
def nudge_track(deck: int, direction: str = "forward", strength: float = 0.5) -> str:
    """Nudge a track forward or backward slightly — like touching the jog wheel.
    Use this to fine-tune beat alignment during a mix.

    Args:
        deck: Deck to nudge (1 or 2).
        direction: 'forward' to speed up momentarily, 'backward' to slow down.
        strength: Nudge strength 0.0 to 1.0 (0.5 = gentle, 1.0 = strong push).
    """
    import time as _time
    value = strength if direction == "forward" else -strength
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "wheel", "value": value})
    _time.sleep(0.1)
    _mixxx_post("/api/control", {"group": f"[Channel{deck}]", "key": "wheel", "value": 0})
    return f"Nudged Deck {deck} {direction} (strength {strength})"


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
    if not status or _mixxx_failed(status):
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
    if not tinfo or _mixxx_failed(tinfo) or tinfo.get("error"):
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

        cfg = load_config()
        from litellm import completion as _completion
        resp = _completion(
            model=cfg.llm.model,
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
            api_base=cfg.llm.api_base,
            api_key=cfg.llm.api_key,
            temperature=0.5,
            timeout=30,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Gemini audio error: {e}"


@tool
def analyze_track(track_path: str) -> str:
    """Deep analysis of a FULL track — sends entire audio to Gemini.
    Returns structured JSON: BPM, key, energy, timeline, mix points.
    Results cached in SQLite DB — analyzing twice returns cached instantly.

    Args:
        track_path: Full file path OR partial name (will search library).
    """
    import base64
    import subprocess as _sp
    import time as _time

    from .db import get_track_by_path, upsert_track

    # Resolve path
    path = Path(track_path)
    if not path.is_absolute() or not path.exists():
        query = _normalize_for_search(track_path)
        for genre_dir in sorted(_music_dir().iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
                continue
            for f in sorted(genre_dir.iterdir()):
                if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                    if query in _normalize_for_search(f.stem):
                        path = f
                        break
            if path.exists() and path != Path(track_path):
                break
        if not path.exists():
            return f"Track not found: {track_path}"

    title = path.stem

    # Check DB cache
    existing = get_track_by_path(str(path))
    if existing and existing.get("analyzed_at"):
        return json.dumps({
            "title": existing.get("title"), "bpm": existing.get("bpm"),
            "key": existing.get("key_musical"), "energy_peak": existing.get("energy_peak"),
            "mood": existing.get("mood"), "mix_in_seconds": existing.get("mix_in_seconds"),
            "mix_out_seconds": existing.get("mix_out_seconds"),
            "timeline": existing.get("timeline"), "verdict": existing.get("verdict"),
            "similar": existing.get("similar"),
        }, indent=2)

    # Convert full track to low-quality WAV (mono 8kHz — small for API)
    wav_path = "/tmp/dj-treta-full-analysis.wav"
    try:
        _sp.run(
            ["ffmpeg", "-y", "-i", str(path), "-ac", "1", "-ar", "8000",
             "-acodec", "pcm_s16le", wav_path],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        return f"Audio conversion failed: {e}"

    if not Path(wav_path).exists():
        return "Audio conversion failed"

    # Send full track to Gemini — ask for JSON (Flash is great at structured output)
    try:
        with open(wav_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()

        cfg = load_config()
        from litellm import completion as _completion
        resp = _completion(
            model=cfg.llm.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        f"Analyze this track: '{title}'. Listen to the ENTIRE track.\n"
                        "Return JSON only, no other text:\n"
                        '{"bpm": <number>, "key": "<Dm/Am/etc>", "energy_peak": <1-10>, '
                        '"mood": "<2-3 words>", "genre": "<genre>", '
                        '"mix_in_seconds": <best time to start mixing in>, '
                        '"mix_out_seconds": <best time to start mixing out>, '
                        '"timeline": [{"start": 0, "end": 45, "section": "intro", "energy": 3}, ...], '
                        '"verdict": "<one sentence>", "similar": "<3 artists>"}'
                    )},
                    {"type": "image_url", "image_url": {"url": f"data:audio/wav;base64,{audio_b64}"}},
                ],
            }],
            api_base=cfg.llm.api_base,
            api_key=cfg.llm.api_key,
            temperature=0.3,
            timeout=60,
        )

        raw = resp.choices[0].message.content.strip()

        # Parse JSON from Gemini response (may have markdown code fences)
        json_str = raw
        if "```" in raw:
            # Extract JSON from code block
            lines = raw.split("\n")
            in_block = False
            json_lines = []
            for line in lines:
                if line.strip().startswith("```"):
                    in_block = not in_block
                    continue
                if in_block:
                    json_lines.append(line)
            json_str = "\n".join(json_lines)

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            # Fallback: store raw text
            upsert_track(
                path=str(path), title=title,
                analysis_text=raw, analyzed_at=_time.time()
            )
            return raw

        # Parse key to Camelot
        from .camelot import KEY_TO_CAMELOT
        key_musical = data.get("key", "")
        key_camelot = KEY_TO_CAMELOT.get(key_musical, "")

        # Get real duration from ffprobe — Gemini hallucinates timelines beyond track length
        real_duration = None
        try:
            dur_result = _sp.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            real_duration = float(json.loads(dur_result.stdout)["format"]["duration"])
        except Exception:
            pass

        # Clamp timeline to real duration
        timeline = data.get("timeline", [])
        if real_duration and timeline:
            clamped = []
            for s in timeline:
                if s.get("start", 0) >= real_duration:
                    break
                if s.get("end", 0) > real_duration:
                    s["end"] = round(real_duration)
                clamped.append(s)
            timeline = clamped
            data["timeline"] = timeline

        # Clamp mix_out
        mix_out = data.get("mix_out_seconds")
        if real_duration and mix_out and mix_out > real_duration - 10:
            mix_out = max(real_duration - 30, real_duration * 0.7)
            data["mix_out_seconds"] = mix_out

        # Store in DB
        timeline_json = json.dumps(timeline)
        upsert_track(
            path=str(path), title=title,
            bpm=data.get("bpm"),
            key_musical=key_musical,
            key_camelot=key_camelot,
            energy_peak=data.get("energy_peak"),
            mood=data.get("mood"),
            mix_in_seconds=data.get("mix_in_seconds"),
            mix_out_seconds=data.get("mix_out_seconds"),
            duration_seconds=real_duration or data.get("duration_seconds"),
            timeline=timeline_json,
            analysis_text=raw,
            similar=data.get("similar"),
            verdict=data.get("verdict"),
            genre=data.get("genre"),
            analyzed_at=_time.time(),
        )

        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Analysis error: {e}"


@tool
def preview_track(track_path: str, position: int = 30, duration: int = 10) -> str:
    """Preview any track WITHOUT loading it on a deck — like a DJ listening in headphones.
    Extracts audio from the file and analyzes it with Gemini. Use this to evaluate
    a track BEFORE deciding to load it.

    Args:
        track_path: Full file path OR partial name (will search library).
        position: Position in seconds to start listening from (default: 30s in, past the intro).
        duration: Seconds of audio to analyze (5-15).
    """
    import base64
    import subprocess as _sp

    # Resolve partial name to full path
    path = Path(track_path)
    if not path.is_absolute() or not path.exists():
        query = track_path.lower()
        found = None
        for genre_dir in sorted(_music_dir().iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
                continue
            for f in sorted(genre_dir.iterdir()):
                if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                    if query in f.stem.lower():
                        found = f
                        break
            if found:
                break
        if not found:
            return f"Track not found: {track_path}"
        path = found

    title = path.stem
    duration = max(5, min(15, duration))

    # Extract audio snippet with ffmpeg
    snippet = "/tmp/dj-treta-preview.wav"
    try:
        _sp.run(
            ["ffmpeg", "-y", "-ss", str(position), "-t", str(duration),
             "-i", str(path), "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", snippet],
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

        cfg = load_config()
        from litellm import completion as _completion
        resp = _completion(
            model=cfg.llm.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        f"You are DJ Treta previewing '{title}' at {position}s. "
                        "Describe: BPM estimate, key, mood, energy (1-10), genre, "
                        "structure (intro/groove/breakdown/buildup/drop), "
                        "notable elements. Would this track work well in a DJ set? "
                        "2-3 sentences."
                    )},
                    {"type": "image_url", "image_url": {"url": f"data:audio/wav;base64,{audio_b64}"}},
                ],
            }],
            api_base=cfg.llm.api_base,
            api_key=cfg.llm.api_key,
            temperature=0.5,
            timeout=30,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Preview error: {e}"


# ═══════════════════════════════════════════════════════════════════════
# TRANSITIONS — Brain-controlled mixing
# ═══════════════════════════════════════════════════════════════════════

@tool
def do_transition(to_deck: int, duration: int = 60) -> str:
    """Execute a smooth crossfade transition to a deck.
    Uses Mixxx's C++ engine (20fps S-curve). After transition completes,
    the outgoing deck is paused and EQ/volume reset.

    The brain picks compatible tracks. This tool just executes the transition.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Transition duration in seconds (10-120).
    """
    import time as _time

    duration = max(10, min(120, duration))
    out_deck = 1 if to_deck == 2 else 2

    # Pre-flight: ABORT if incoming deck has no playable track
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return f"ABORTED: cannot reach Mixxx: {err}"
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded! Load a track first with load_track."
        remaining = float(deck_state.get("remaining_seconds", 0) or 0)
        if remaining < 30:
            return f"ABORTED: Deck {to_deck} track has only {remaining:.0f}s left — load a fresh track first."

    # Sync + play + phase align (let Mixxx handle BPM matching naturally)
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.3)
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "beatsync_phase", "value": 1})
    _mixxx_post("/api/control", {"group": f"[Channel{to_deck}]", "key": "quantize", "value": 1})
    _time.sleep(0.1)

    # Verify it actually started playing
    status2 = _mixxx_get("/api/status")
    if err2 := _mixxx_failed(status2):
        return f"ABORTED: lost Mixxx during transition prep: {err2}"
    if status2:
        deck_state2 = status2.get(f"deck{to_deck}", {})
        if not deck_state2.get("playing", False):
            return f"ABORTED: Deck {to_deck} failed to start playing."

    # Mixxx C++ S-curve transition (20fps, smooth)
    _mixxx_post("/api/transition", {"deck": to_deck, "duration": duration})
    _time.sleep(duration + 2)

    # Post-flight cleanup — crossfader + pause + reset EQ
    xf = 0.0 if to_deck == 1 else 1.0
    _mixxx_post("/api/crossfade", {"position": xf})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/volume", {"deck": out_deck, "volume": 1.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "volume": 1.0})
    for band in ["hi", "mid", "lo"]:
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
        _mixxx_post("/api/eq", {"deck": to_deck, band: 1.0})
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.5})

    # Eject outgoing deck — prevents "loaded but finished" state
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Transitioned to Deck {to_deck} over {duration}s. Deck {out_deck} ejected."


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

    # Pre-flight: ABORT if incoming deck has no playable track
    status = _mixxx_get("/api/status")
    if err := _mixxx_failed(status):
        return f"ABORTED: cannot reach Mixxx: {err}"
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded! Load a track first."
        remaining = float(deck_state.get("remaining_seconds", 0) or 0)
        if remaining < 30:
            return f"ABORTED: Deck {to_deck} track has only {remaining:.0f}s left — load a fresh track first."

    fps = 10
    total = int(duration * fps)

    # Move crossfader to center so both decks are audible through it
    _mixxx_post("/api/crossfade", {"position": 0.5})

    # Sync + play incoming with bass killed and volume at 0
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/eq", {"deck": to_deck, "lo": 0.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "volume": 0.0})
    _mixxx_post("/api/play", {"deck": to_deck})

    # Phase 1 (0-40%): Bring in incoming volume (bass still cut)
    # Phase 2 (40-60%): Swap bass — cut outgoing bass, restore incoming bass
    # Phase 3 (60-100%): Fade out outgoing volume
    for i in range(total + 1):
        t = i / total
        if t <= 0.4:
            blend = t / 0.4
            _mixxx_post("/api/volume", {"deck": to_deck, "volume": round(blend, 2)})
        elif t <= 0.6:
            swap_t = (t - 0.4) / 0.2
            _mixxx_post("/api/eq", {"deck": out_deck, "lo": round(1.0 - swap_t, 2)})
            _mixxx_post("/api/eq", {"deck": to_deck, "lo": round(swap_t, 2)})
        else:
            fade = 1.0 - ((t - 0.6) / 0.4)
            _mixxx_post("/api/volume", {"deck": out_deck, "volume": round(fade, 2)})
        _time.sleep(1.0 / fps)

    # Cleanup — smooth crossfader to final position, pause + reset
    xf_target = 0.0 if to_deck == 1 else 1.0
    # Glide crossfader over 1s instead of snapping
    steps = 10
    xf_start = 0.5
    for s in range(steps + 1):
        xf = xf_start + (xf_target - xf_start) * (s / steps)
        _mixxx_post("/api/crossfade", {"position": round(xf, 2)})
        _time.sleep(0.1)

    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/volume", {"deck": out_deck, "volume": 1.0})
    _mixxx_post("/api/volume", {"deck": to_deck, "volume": 1.0})
    for band in ["hi", "mid", "lo"]:
        _mixxx_post("/api/eq", {"deck": out_deck, band: 1.0})
        _mixxx_post("/api/eq", {"deck": to_deck, band: 1.0})

    # Eject outgoing deck
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Bass-swapped to Deck {to_deck} over {duration}s. Deck {out_deck} ejected."


@tool
def do_filter_sweep(to_deck: int, duration: int = 45) -> str:
    """Filter sweep transition — gradually reveal incoming track through a low-pass filter.
    Best for: progressive, melodic, atmospheric tracks.

    The incoming track starts muffled (low-pass filtered), then the filter opens
    while the outgoing track fades. Creates a smooth, evolving reveal.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: Transition duration in seconds (20-90).
    """
    import time as _time

    duration = max(20, min(90, duration))
    out_deck = 1 if to_deck == 2 else 2

    # Pre-flight
    status = _mixxx_get("/api/status")
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded!"
        remaining = float(deck_state.get("remaining_seconds", 0) or 0)
        if remaining < 30:
            return f"ABORTED: Deck {to_deck} track has only {remaining:.0f}s left."

    # Start incoming with filter closed (muffled), sync handles BPM
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.0})  # fully closed
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})
    _time.sleep(0.3)

    # Gradually open filter on incoming + fade out outgoing
    fps = 10
    total = int(duration * fps)
    for i in range(total + 1):
        t = i / total  # 0.0 → 1.0

        # Open incoming filter: 0.0 → 0.5 (neutral)
        _mixxx_post("/api/filter", {"deck": to_deck, "value": t * 0.5})

        # Close outgoing filter: 0.5 → 0.0
        _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5 * (1 - t)})

        # Crossfader follows
        xf = t if to_deck == 2 else (1 - t)
        _mixxx_post("/api/crossfade", {"position": xf})

        _time.sleep(1 / fps)

    # Cleanup
    xf = 0.0 if to_deck == 1 else 1.0
    _mixxx_post("/api/crossfade", {"position": xf})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/filter", {"deck": to_deck, "value": 0.5})
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Filter-swept to Deck {to_deck} over {duration}s. Deck {out_deck} ejected."


@tool
def do_hard_cut(to_deck: int) -> str:
    """Hard cut — instant switch to the other deck. No blend, no crossfade.
    Best for: genre changes, drop moments, high energy transitions.

    Args:
        to_deck: Deck to switch TO (1 or 2).
    """
    out_deck = 1 if to_deck == 2 else 2

    status = _mixxx_get("/api/status")
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded!"

    # Instant switch
    _mixxx_post("/api/play", {"deck": to_deck})
    xf = 0.0 if to_deck == 1 else 1.0
    _mixxx_post("/api/crossfade", {"position": xf})
    _mixxx_post("/api/pause", {"deck": out_deck})
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Hard-cut to Deck {to_deck}. Deck {out_deck} ejected."


@tool
def do_echo_out(to_deck: int, duration: int = 30) -> str:
    """Echo out — fade outgoing track with delay/echo tail, then drop incoming.
    Best for: energy shifts, mood changes, dramatic moments.

    The outgoing track fades with its echo reverberating, creating space,
    then the incoming track drops in clean.

    Args:
        to_deck: Deck to transition TO (1 or 2).
        duration: How long the echo fade takes in seconds (10-45).
    """
    import time as _time

    duration = max(10, min(45, duration))
    out_deck = 1 if to_deck == 2 else 2

    status = _mixxx_get("/api/status")
    if status:
        deck_state = status.get(f"deck{to_deck}", {})
        if not deck_state.get("track_loaded", False):
            return f"ABORTED: Deck {to_deck} has no track loaded!"

    # Move crossfader to center so both decks are audible
    _mixxx_post("/api/crossfade", {"position": 0.5})

    # Start incoming silently — it will be revealed after outgoing fades
    _mixxx_post("/api/volume", {"deck": to_deck, "volume": 0.0})
    _mixxx_post("/api/sync", {"deck": to_deck})
    _mixxx_post("/api/play", {"deck": to_deck})

    # Fade out outgoing with filter closing (simulates echo decay)
    fps = 10
    total = int(duration * fps)
    for i in range(total + 1):
        t = i / total

        # Close filter on outgoing (muffled decay)
        _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5 * (1 - t)})

        # Fade volume on outgoing
        _mixxx_post("/api/volume", {"deck": out_deck, "volume": 1.0 - t})

        _time.sleep(1 / fps)

    # Outgoing silent — bring in incoming clean
    _mixxx_post("/api/pause", {"deck": out_deck})
    # Quick volume rise on incoming (0.5s clean drop-in)
    for s in range(5):
        _mixxx_post("/api/volume", {"deck": to_deck, "volume": round((s + 1) / 5, 2)})
        _time.sleep(0.1)

    # Glide crossfader to final position
    xf_target = 0.0 if to_deck == 1 else 1.0
    for s in range(10):
        xf = 0.5 + (xf_target - 0.5) * ((s + 1) / 10)
        _mixxx_post("/api/crossfade", {"position": round(xf, 2)})
        _time.sleep(0.1)

    # Reset
    _mixxx_post("/api/volume", {"deck": out_deck, "volume": 1.0})
    _mixxx_post("/api/filter", {"deck": out_deck, "value": 0.5})
    _mixxx_post("/api/control", {"group": f"[Channel{out_deck}]", "key": "eject", "value": 1})

    return f"Echo-out to Deck {to_deck} over {duration}s. Deck {out_deck} ejected."


@tool
def schedule_transition(to_deck: int, at_position: int, technique: str = "crossfade", duration: int = 45) -> str:
    """Schedule a transition at a specific track position. Returns immediately —
    Python executes the transition in the background when the track reaches at_position.

    Call this when you've decided the right moment to transition based on
    the track timeline (e.g., at a breakdown or outro).

    Args:
        to_deck: Deck to transition TO (1 or 2).
        at_position: Track position in seconds to START the transition.
        technique: "crossfade" (smooth blend), "bass_swap" (EQ swap, techno), "filter_sweep" (progressive reveal), "echo_out" (fade with echo, mood shift), "hard_cut" (instant switch, genre change).
        duration: Transition duration in seconds (10-90). Ignored for hard_cut.
    """
    duration = max(10, min(120, duration))

    # Get current track position
    status = _mixxx_get("/api/status")
    if not status or _mixxx_failed(status):
        return "ERROR: Mixxx not responding"

    active_deck = 1 if to_deck == 2 else 2
    d_active = status.get(f"deck{active_deck}", {})
    current_pos = float(d_active.get("position_seconds", 0) or 0)
    track_duration = float(d_active.get("duration", 0) or 0)

    # Safety: clamp at_position so transition completes before track ends
    if track_duration > 0:
        max_start = track_duration - duration - 5  # 5s safety margin
        if at_position > max_start:
            at_position = max(current_pos + 5, max_start)

    delay = max(0, at_position - current_pos)

    # If track is almost over, transition immediately
    remaining = float(d_active.get("remaining_seconds", 0) or 0)
    if remaining < duration + 10:
        at_position = current_pos + 2  # start in 2 seconds
        delay = 2

    # Write schedule — Python (_execute_scheduled_transition in main.py) picks this up
    scheduled = {
        "toDeck": to_deck,
        "atPosition": at_position,
        "technique": technique,
        "duration": duration,
        "activeDeck": active_deck,
        "scheduledAt": current_pos,
        "executesIn": round(delay),
    }
    Path("/tmp/dj-treta-scheduled-transition.json").write_text(
        json.dumps(scheduled, indent=2)
    )

    return (
        f"Scheduled {technique} to deck {to_deck} at position {at_position}s "
        f"(in {round(delay)}s). Python will execute it — you're free now."
    )


# ═══════════════════════════════════════════════════════════════════════
# MUSIC DISCOVERY — Search & Download
# ═══════════════════════════════════════════════════════════════════════

@tool
def search_music(query: str, limit: int = 10) -> list:
    """Search YouTube for individual music tracks (NOT mixes or DJ sets).

    Returns only tracks between 2-10 minutes. Longer results (mixes, sets, compilations)
    are automatically filtered out.

    Args:
        query: Search query — artist name, track title, genre. Add 'official' or 'original mix' for better results.
        limit: Number of raw results to fetch before filtering (1-20).
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
            dur = info.get("duration", 0) or 0
            title = info.get("title", "Unknown")

            # Skip mixes, sets, compilations, non-music, tutorials
            skip_words = ["mix 20", "full set", "compilation", "hour mix", "live set",
                          "dj set", "mixtape", "nonstop", "megamix", "interview",
                          "podcast", "test", "quiz", "reaction", "how to", "tutorial",
                          "copyright free", "royalty free", "free download", "free music",
                          "top 10", "best of", "playlist", "radio", "review",
                          "unboxing", "vlog", "behind the scene"]
            title_lower = title.lower()
            if any(w in title_lower for w in skip_words):
                continue

            # Only individual tracks: 2-10 minutes
            if dur < 120 or dur > 600:
                continue

            mins = int(dur // 60)
            secs = int(dur % 60)
            results.append({
                "title": title,
                "url": info.get("url", info.get("webpage_url", "")),
                "id": info.get("id", ""),
                "duration": f"{mins}:{secs:02d}",
                "duration_seconds": dur,
                "uploader": info.get("uploader", info.get("channel", "Unknown")),
            })
        except json.JSONDecodeError:
            continue

    if not results:
        return [{"info": "No individual tracks found (2-10 min). Try searching with artist name + 'original mix' or 'official audio'."}]

    return results


@tool
def download_track(url: str, genre: str = "deep") -> str:
    """Download a track from YouTube into the music library.

    Args:
        url: YouTube URL to download.
        genre: Genre folder to save into (e.g., dark-techno, melodic-techno, deep, minimal, progressive, vocal, psychill).
    """
    genre_dir = _music_dir() / genre
    genre_dir.mkdir(parents=True, exist_ok=True)

    # Track files before download to find what was added
    before = set(genre_dir.glob("*.mp3"))

    result = subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "320",
         "-o", str(genre_dir / "%(uploader)s - %(title)s.%(ext)s"),
         "--no-playlist", "--no-overwrites", url],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return f"Download failed: {result.stderr[:200]}"

    # Find the actual downloaded file (yt-dlp may change chars in filename)
    after = set(genre_dir.glob("*.mp3"))
    new_files = after - before
    if new_files:
        actual_path = next(iter(new_files))

        # Insert into DB + auto-analyze in background
        from .db import upsert_track
        upsert_track(path=str(actual_path), title=actual_path.stem, genre=genre)

        import threading
        def _bg_analyze():
            try:
                analyze_track(str(actual_path))
            except Exception:
                pass
        threading.Thread(target=_bg_analyze, daemon=True).start()

        return f"Downloaded: {actual_path}"

    # No new file — yt-dlp skipped (already exists)
    if "--no-overwrites" in result.stdout or "has already been downloaded" in result.stdout:
        return "ALREADY EXISTS: This track was already downloaded. Search for a DIFFERENT track."

    return f"Downloaded to {genre}/ folder"


# ═══════════════════════════════════════════════════════════════════════
# MUSIC PRODUCTION (Lyria 3)
# ═══════════════════════════════════════════════════════════════════════

GENERATION_QUEUE_FILE = Path("/tmp/dj-treta-generation-jobs.json")


@tool
def generate_track_async(prompt: str, bpm: int = 128, key: str = "C minor",
                         genre: str = "dark-techno", duration: str = "full",
                         name: str = "") -> str:
    """Start generating a track in the BACKGROUND. Returns IMMEDIATELY.
    The track auto-loads on the idle deck when ready. Use this when DJing live
    so you stay free to DJ while the track produces.

    Args:
        prompt: Describe the track — mood, style, instruments, energy.
        bpm: Tempo in BPM (60-200).
        key: Musical key.
        genre: Genre folder.
        duration: "full" for ~3 min, "clip" for 30s.
        name: Track name. If provided, skips AI naming.
    """
    import time as _time
    job_id = f"gen-{_time.strftime('%H%M%S')}-{id(prompt) % 10000}"
    job = {
        "id": job_id,
        "prompt": prompt, "bpm": bpm, "key": key,
        "genre": genre, "duration": duration, "name": name,
        "status": "pending", "requested_at": _time.time(),
    }
    # Append to jobs file — daemon's generation worker picks it up
    jobs = []
    if GENERATION_QUEUE_FILE.exists():
        try:
            jobs = json.loads(GENERATION_QUEUE_FILE.read_text())
        except Exception:
            pass
    jobs.append(job)
    GENERATION_QUEUE_FILE.write_text(json.dumps(jobs, indent=2))

    display = name or "a new track"
    return f"Producing '{display}'... will auto-load on idle deck when ready. Keep DJing."


@tool
def generate_track(prompt: str, bpm: int = 128, key: str = "C minor",
                   genre: str = "dark-techno", duration: str = "full",
                   name: str = "") -> str:
    """Generate an original AI track using Google Lyria 3. The track is saved
    to the music library and auto-analyzed, ready to be loaded and mixed.

    Args:
        prompt: Describe the track — mood, style, instruments, energy. Be specific.
               Example: "Dark driving techno with pulsing bassline, metallic percussion,
               atmospheric pads, building tension, no vocals"
        bpm: Tempo in BPM (60-200). Default 128.
        key: Musical key. Example: "C minor", "F# major", "A minor".
        genre: Genre folder to save into (e.g., dark-techno, melodic-techno, deep, minimal, progressive, ai-generated).
        duration: "full" for ~3 min track (lyria-3-pro), "clip" for 30s clip (lyria-3-clip).
        name: Track name. If provided, skips AI naming. If empty, Gemini names it.
    """
    from google import genai
    from google.genai import types
    import time as _time

    cfg = load_config()
    pc = cfg.producer

    # Build the music prompt with DJ-specific instructions
    music_prompt = (
        f"{prompt}\n\n"
        f"Tempo: {bpm} BPM\n"
        f"Key: {key}\n"
        f"Style: {genre.replace('-', ' ')}\n"
        f"Instrumental only, no vocals.\n"
        f"DJ-friendly structure: clear intro (16 bars), main groove, breakdown, build-up, drop, outro (16 bars).\n"
        f"Designed for DJ mixing with beatmatched intro and outro."
    )

    # Pick model based on duration
    use_clip = (duration == "clip")

    try:
        import warnings as _warnings
        _warnings.filterwarnings('ignore', message='.*Interactions.*')

        client = genai.Client(
            vertexai=True,
            project=pc.vertex_project,
            location=pc.vertex_location,
        )

        audio_data = None
        description = ""

        if use_clip:
            # Clip model (30s) uses generate_content API
            response = client.models.generate_content(
                model="lyria-3-clip-preview",
                contents=music_prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO", "TEXT"],
                ),
            )
            if response.parts:
                for part in response.parts:
                    if part.text is not None:
                        description = part.text
                    elif part.inline_data is not None:
                        audio_data = part.inline_data.data
        else:
            # Pro model (~3 min) uses interactions API
            # API is flaky — sometimes returns empty outputs on 'completed'. Retry up to 3 times.
            import base64 as _b64
            for _attempt in range(3):
                interaction = client.interactions.create(
                    model="lyria-3-pro-preview",
                    response_modalities=["audio", "text"],
                    input=music_prompt,
                )
                # Try direct attribute access first, fall back to model_dump()
                raw_outputs = interaction.outputs
                if raw_outputs:
                    for out in raw_outputs:
                        otype = getattr(out, "type", None)
                        if otype == "text" and not description:
                            description = getattr(out, "text", "")
                        elif otype == "audio":
                            data = getattr(out, "data", None)
                            if data:
                                audio_data = _b64.b64decode(data) if isinstance(data, str) else data
                else:
                    # SDK bug: outputs attr is None but data exists in dump
                    _warnings.filterwarnings('ignore', category=UserWarning)
                    outputs = interaction.model_dump().get("outputs") or []
                    for out in outputs:
                        if out.get("type") == "text" and not description:
                            description = out.get("text", "")
                        elif out.get("type") == "audio" and out.get("data"):
                            audio_data = _b64.b64decode(out["data"])
                if audio_data:
                    break
    except Exception as e:
        return f"Lyria generation failed: {e}"

    if not audio_data:
        return f"No audio generated. Model response: {description[:200]}"

    # Step 1: Save to temp file
    genre_dir = _music_dir() / genre
    genre_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _time.strftime("%H%M%S")
    temp_path = genre_dir / f"_generating_{timestamp}.mp3"

    try:
        with open(temp_path, "wb") as f:
            f.write(audio_data)
    except Exception as e:
        return f"Failed to save audio: {e}"

    # Step 2: Analyze with librosa (real signal processing, no LLM guessing)
    from .audio_analysis import analyze_audio
    audio_data_analysis = {}
    try:
        audio_data_analysis = analyze_audio(str(temp_path))
    except Exception:
        pass

    real_bpm = audio_data_analysis.get("bpm", bpm)
    real_key = audio_data_analysis.get("key", key)
    real_duration = audio_data_analysis.get("duration_seconds")
    energy_peak = audio_data_analysis.get("energy_peak", 5)
    timeline = audio_data_analysis.get("timeline", [])
    mix_in = audio_data_analysis.get("mix_in_seconds", 0)
    mix_out = audio_data_analysis.get("mix_out_seconds")

    # Step 3: Name the track — use provided name or Gemini fallback
    track_name = None
    if name:
        track_name = re.sub(r'[<>:"/\\|?*]', '', name.strip())[:50]
    if not track_name:
        try:
            from litellm import completion as _name_completion
            _name_resp = _name_completion(
                model=cfg.llm.model,
                messages=[{"role": "user", "content":
                    f"You are DJ Treta naming your new track. Reply with ONLY a creative "
                    f"2-4 word name. No explanation, no quotes, just the name. "
                    f"Make it evocative and unique — never generic.\n"
                    f"Style: {genre}, BPM: {real_bpm:.0f}, Key: {real_key}, Energy: {energy_peak}/10\n"
                    f"Description: {prompt[:200]}"}],
                api_base=cfg.llm.api_base, api_key=cfg.llm.api_key,
                temperature=0.9, timeout=10,
            )
            track_name = _name_resp.choices[0].message.content.strip().strip('"\'')[:50]
            track_name = re.sub(r'[<>:"/\\|?*]', '', track_name).strip()
        except Exception:
            pass
    if not track_name:
        track_name = f"Untitled {timestamp}"

    # Step 3b: Get mood + similar artists from Gemini (creative, not structural)
    mood_text = ""
    similar = ""
    verdict = ""
    try:
        from litellm import completion as _mood_completion
        _mood_resp = _mood_completion(
            model=cfg.llm.model,
            messages=[{"role": "user", "content":
                f"A {genre} track at {real_bpm:.0f} BPM in {real_key}, energy {energy_peak}/10.\n"
                f"Prompt was: {prompt[:150]}\n"
                f"Reply JSON only: "
                '{"mood": "<2-3 mood words>", "similar": "<3 similar artists>", '
                '"verdict": "<one sentence about this track>"}'}],
            api_base=cfg.llm.api_base, api_key=cfg.llm.api_key,
            temperature=0.5, timeout=10,
        )
        _raw = _mood_resp.choices[0].message.content.strip()
        if "```" in _raw:
            _raw = _raw.split("```")[1].split("```")[0].strip()
            if _raw.startswith("json"):
                _raw = _raw[4:].strip()
        _mood_data = json.loads(_raw)
        mood_text = _mood_data.get("mood", "")
        similar = _mood_data.get("similar", "")
        verdict = _mood_data.get("verdict", "")
    except Exception:
        pass

    # Step 4: Rename to final path
    filename = f"DJ Treta - {track_name}.mp3"
    filepath = genre_dir / filename
    if filepath.exists():
        filename = f"DJ Treta - {track_name} ({timestamp}).mp3"
        filepath = genre_dir / filename
    temp_path.rename(filepath)

    # Step 5: Write ID3 tags with full metadata
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TBPM, TKEY, TCON, TALB, COMM
        tags = ID3()
        tags.add(TIT2(encoding=3, text=[track_name]))
        tags.add(TPE1(encoding=3, text=["DJ Treta"]))
        tags.add(TBPM(encoding=3, text=[str(int(real_bpm))]))
        tags.add(TKEY(encoding=3, text=[real_key]))
        tags.add(TCON(encoding=3, text=[genre.replace('-', ' ').title()]))
        tags.add(TALB(encoding=3, text=["Treta Originals"]))
        tags.add(COMM(encoding=3, lang='eng', desc='prompt', text=[prompt[:200]]))
        tags.save(filepath)
    except Exception:
        pass

    # Step 6: Insert full metadata into DB
    from .db import upsert_track
    from .camelot import KEY_TO_CAMELOT
    key_camelot = KEY_TO_CAMELOT.get(real_key, "")
    timeline_json = json.dumps(timeline)

    upsert_track(
        path=str(filepath), title=track_name, artist="DJ Treta",
        genre=genre, bpm=float(real_bpm), key_musical=real_key,
        key_camelot=key_camelot, energy_peak=energy_peak,
        mood=mood_text,
        duration_seconds=real_duration,
        mix_in_seconds=mix_in,
        mix_out_seconds=mix_out,
        timeline=timeline_json,
        similar=similar,
        verdict=verdict,
        analyzed_at=_time.time(),
    )

    result = f"Generated: {filepath} | {track_name} | {real_bpm:.0f} BPM | {real_key} | Energy: {energy_peak} | Mood: {mood_text}"
    return result


# ═══════════════════════════════════════════════════════════════════════
# LIBRARY MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

@tool
def list_library_tracks() -> list:
    """List all tracks in the music library with file path, filename, and genre folder."""
    tracks = []
    for genre_dir in sorted(_music_dir().iterdir()):
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
    """Read a file under the DJ Treta repo or configured music library only.

    Args:
        file_path: Path relative to the repo (e.g. 'config.yaml', '.beings/SOUL.md') or absolute path under those roots.
    """
    cfg = load_config()
    path = _resolve_tool_path(cfg, file_path)
    if path is None:
        return "ERROR: Path not allowed (must be under the DJ Treta repo or library.music_dir)."
    if not path.exists():
        return f"File not found: {path}"
    content = path.read_text()
    if len(content) > 10000:
        return content[:10000] + f"\n\n... (truncated, {len(content)} chars total)"
    return content


@tool
def write_file(file_path: str, content: str) -> str:
    """Write a file under the DJ Treta repo or configured music library only.

    Args:
        file_path: Path relative to the repo or absolute under allowed roots.
        content: The full content to write.
    """
    cfg = load_config()
    path = _resolve_tool_path(cfg, file_path)
    if path is None:
        return "ERROR: Path not allowed (must be under the DJ Treta repo or library.music_dir)."
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"Written {len(content)} chars to {path}"


@tool
def list_files(directory: str = ".") -> list:
    """List files in a directory under the repo or music library.

    Args:
        directory: Path relative to the repo or absolute under allowed roots.
    """
    cfg = load_config()
    path = _resolve_tool_path(cfg, directory)
    if path is None:
        return ["ERROR: Path not allowed (must be under the DJ Treta repo or library.music_dir)."]
    if not path.exists():
        return [f"Directory not found: {path}"]
    if not path.is_dir():
        return [f"Not a directory: {path}"]
    return [str(f.relative_to(path)) for f in sorted(path.iterdir()) if not f.name.startswith('.')]


@tool
def run_shell(command: str) -> str:
    """Run a shell command (disabled unless capabilities.allow_shell is true in config).

    Args:
        command: Shell command to execute.
    """
    if not load_config().capabilities.allow_shell:
        return (
            "ERROR: Shell is disabled. Set capabilities.allow_shell: true in config.yaml "
            "(trusted machines only)."
        )
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
    from .db import save_learning_db
    save_learning_db(topic, content)
    return f"Saved learning about '{topic}'."


@tool
def recall_learnings(topic: str = "") -> list:
    """Recall past learnings. Optionally filter by topic.

    Args:
        topic: Optional topic filter (matches substring). Empty = return all.
    """
    from .db import recall_learnings_db
    return recall_learnings_db(topic)
