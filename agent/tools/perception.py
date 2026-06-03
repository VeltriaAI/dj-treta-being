"""Audio perception tools -- hear, analyze, and preview tracks."""

import json
from pathlib import Path

from ..audio_files import is_audio_file
from .helpers import (
    _music_dir, _normalize_for_search,
    _mixxx_failed, _mixxx_get,
    load_config,
)


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
        return "Can't hear -- Mixxx not responding"

    # Find active deck
    if deck == 0:
        d1 = status.get("deck1", {})
        d2 = status.get("deck2", {})
        if d1.get("playing"):
            deck = 1
        elif d2.get("playing"):
            deck = 2
        else:
            return "Can't hear -- nothing playing"

    deck_status = status.get(f"deck{deck}", {})
    if not deck_status.get("playing"):
        return f"Can't hear -- Deck {deck} not playing"

    pos = deck_status.get("position_seconds", 0)

    # Get file path
    tinfo = _mixxx_get(f"/api/deck/{deck}/track_info")
    if not tinfo or _mixxx_failed(tinfo) or tinfo.get("error"):
        return "Can't hear -- no track info"

    file_path = tinfo.get("file_path", "")
    if not file_path or not Path(file_path).exists():
        return f"Can't hear -- file not found: {file_path}"

    title = tinfo.get("title", Path(file_path).stem)

    # Extract audio snippet with ffmpeg (mono 16kHz wav -- small enough for API)
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
        from ..billing_rates import bill_from_response
        bill_from_response(resp, "dj_treta")
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Gemini audio error: {e}"


def analyze_track(track_path: str) -> str:
    """Deep analysis of a FULL track -- sends entire audio to Gemini.
    Returns structured JSON: BPM, key, energy, timeline, mix points.
    Results cached in SQLite DB -- analyzing twice returns cached instantly.

    Args:
        track_path: Full file path OR partial name (will search library).
    """
    import base64
    import subprocess as _sp
    import time as _time

    from ..db import get_track_by_path, upsert_track

    # Resolve path
    path = Path(track_path)
    if not path.is_absolute() or not path.exists():
        query = _normalize_for_search(track_path)
        for genre_dir in sorted(_music_dir().iterdir()):
            if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
                continue
            for f in sorted(genre_dir.iterdir()):
                if is_audio_file(f):
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

    # Try librosa first -- accurate signal processing, no LLM cost
    try:
        from ..audio_analysis import analyze_audio
        import time as _time
        analysis = analyze_audio(str(path))
        from ..camelot import KEY_TO_CAMELOT
        key_camelot = KEY_TO_CAMELOT.get(analysis["key"], "")
        timeline_json = json.dumps(analysis["timeline"])
        upsert_track(
            path=str(path), title=title,
            bpm=analysis["bpm"], key_musical=analysis["key"],
            key_camelot=key_camelot, energy_peak=analysis["energy_peak"],
            duration_seconds=analysis["duration_seconds"],
            mix_in_seconds=analysis["mix_in_seconds"],
            mix_out_seconds=analysis["mix_out_seconds"],
            timeline=timeline_json,
            analyzed_at=_time.time(),
        )
        return json.dumps({
            "title": title, "bpm": analysis["bpm"], "key": analysis["key"],
            "energy_peak": analysis["energy_peak"],
            "duration_seconds": analysis["duration_seconds"],
            "mix_in_seconds": analysis["mix_in_seconds"],
            "mix_out_seconds": analysis["mix_out_seconds"],
            "timeline": analysis["timeline"],
        }, indent=2)
    except Exception:
        pass  # Fall through to Gemini

    # Fallback: Gemini multimodal audio (slower, costs tokens)
    # Convert full track to low-quality WAV (mono 8kHz -- small for API)
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

    # Send full track to Gemini -- ask for JSON (Flash is great at structured output)
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
        from ..billing_rates import bill_from_response
        bill_from_response(resp, "dj_treta")

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
        from ..camelot import KEY_TO_CAMELOT
        key_musical = data.get("key", "")
        key_camelot = KEY_TO_CAMELOT.get(key_musical, "")

        # Get real duration from ffprobe -- Gemini hallucinates timelines beyond track length
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


def preview_track(track_path: str, position: int = 30, duration: int = 10) -> str:
    """Preview any track WITHOUT loading it on a deck -- like a DJ listening in headphones.
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
                if is_audio_file(f):
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
        from ..billing_rates import bill_from_response
        bill_from_response(resp, "dj_treta")
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Preview error: {e}"
