"""AI music generation -- Lyria 3 track production."""

import json
import re
from pathlib import Path

from .helpers import _music_dir, load_config


def generate_track(prompt: str, bpm: int = 128, key: str = "C minor",
                   genre: str = "dark-techno", duration: str = "full",
                   name: str = "") -> str:
    """Generate an original AI track using Google Lyria 3. The track is saved
    to the music library and auto-analyzed, ready to be loaded and mixed.

    Args:
        prompt: Describe the track -- mood, style, instruments, energy. Be specific.
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
            # API is flaky -- sometimes returns empty outputs on 'completed'. Retry up to 3 times.
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
    from ..audio_analysis import analyze_audio
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

    # Step 3: Name the track -- use provided name or Gemini fallback
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
                    f"Make it evocative and unique -- never generic.\n"
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
    from ..db import upsert_track
    from ..camelot import KEY_TO_CAMELOT
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
