"""Music discovery -- YouTube search and download with full metadata."""

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from .helpers import _music_dir

log = logging.getLogger("dj-treta")


def search_music(query: str, limit: int = 10) -> list:
    """Search YouTube for individual music tracks (NOT mixes or DJ sets).

    Returns only tracks between 2-10 minutes. Longer results (mixes, sets, compilations)
    are automatically filtered out.

    Args:
        query: Search query -- artist name, track title, genre. Add 'official' or 'original mix' for better results.
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


def _parse_artist_title(filename: str) -> tuple[str, str]:
    """Parse 'Uploader - Artist - Title' or 'Artist - Title' into (artist, title)."""
    parts = filename.split(" - ", 2)
    if len(parts) >= 3:
        return parts[1].strip(), parts[2].strip()
    elif len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", filename.strip()


def _write_id3_tags(filepath: Path, artist: str, title: str, bpm: float,
                    key: str, genre: str, energy: int = 0):
    """Write ID3 tags to MP3 file using mutagen."""
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TBPM, TKEY, TCON, TALB, ID3NoHeaderError
        try:
            tags = ID3(str(filepath))
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TIT2(encoding=3, text=[title]))
        tags.add(TPE1(encoding=3, text=[artist]))
        if bpm > 0:
            tags.add(TBPM(encoding=3, text=[str(int(bpm))]))
        if key:
            tags.add(TKEY(encoding=3, text=[key]))
        tags.add(TCON(encoding=3, text=[genre.replace('-', ' ').title()]))
        tags.add(TALB(encoding=3, text=["DJ Treta Library"]))
        tags.save(str(filepath))
    except Exception:
        pass


def _enrich_track(filepath: Path, genre: str, yt_meta: dict = None):
    """Background: analyze with librosa + YouTube metadata, write ID3 tags, update DB."""
    yt_meta = yt_meta or {}
    try:
        from ..audio_analysis import analyze_audio
        from ..db import upsert_track
        from ..camelot import KEY_TO_CAMELOT

        # Librosa analysis — BPM, key, energy, sections
        analysis = analyze_audio(str(filepath))

        bpm = analysis.get("bpm", 0)
        key = analysis.get("key", "")
        key_camelot = KEY_TO_CAMELOT.get(key, "")
        energy_peak = analysis.get("energy_peak", 5)
        duration = analysis.get("duration_seconds", 0)
        timeline = analysis.get("timeline", [])
        mix_in = analysis.get("mix_in_seconds", 0)
        mix_out = analysis.get("mix_out_seconds", 0)

        # Artist/title — prefer YouTube metadata, fallback to filename parsing
        yt_title = yt_meta.get("track") or yt_meta.get("title", "")
        yt_artist = yt_meta.get("artist") or yt_meta.get("creator") or yt_meta.get("uploader", "")
        yt_album = yt_meta.get("album", "")
        yt_description = yt_meta.get("description", "")[:300]

        if yt_artist and yt_title:
            artist, title = yt_artist, yt_title
        else:
            artist, title = _parse_artist_title(filepath.stem)

        # Get mood + similar artists from Gemini (creative context, not structural)
        mood_text = ""
        similar = ""
        verdict = ""
        try:
            from ..config import load_config
            from litellm import completion as _completion
            cfg = load_config()
            resp = _completion(
                model=cfg.llm.model,
                messages=[{"role": "user", "content":
                    f"A {genre} track: '{artist} - {title}' at {bpm:.0f} BPM in {key}, energy {energy_peak}/10.\n"
                    f"YouTube description: {yt_description}\n"
                    f"Reply JSON only: "
                    '{"mood": "<2-3 mood words>", "similar": "<3 similar artists>", '
                    '"verdict": "<one sentence about this track>"}'}],
                api_base=cfg.llm.api_base, api_key=cfg.llm.api_key,
                temperature=0.5, timeout=15,
            )
            raw = resp.choices[0].message.content.strip()
            if "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()
            mood_data = json.loads(raw)
            mood_text = mood_data.get("mood", "")
            similar = mood_data.get("similar", "")
            verdict = mood_data.get("verdict", "")
        except Exception:
            pass

        # Write ID3 tags
        _write_id3_tags(filepath, artist, title, bpm, key, genre, energy_peak)

        # Update DB with full metadata
        upsert_track(
            path=str(filepath), title=filepath.stem, artist=artist,
            genre=genre, bpm=bpm, key_musical=key, key_camelot=key_camelot,
            energy_peak=energy_peak, duration_seconds=duration,
            mix_in_seconds=mix_in, mix_out_seconds=mix_out,
            timeline=json.dumps(timeline), mood=mood_text,
            similar=similar, verdict=verdict,
            analyzed_at=time.time(),
        )
        log.info(f"Enriched: {artist} - {title} | {bpm:.0f} BPM | {key} | Energy: {energy_peak} | {mood_text}")

    except Exception as e:
        # Fallback: still try Gemini-only analysis
        try:
            from .perception import analyze_track
            analyze_track(str(filepath))
        except Exception:
            pass


def download_track(url: str, genre: str = "deep") -> str:
    """Download a track from YouTube into the music library.
    Auto-analyzes with librosa (BPM, key, energy, sections) and writes ID3 tags.

    Args:
        url: YouTube URL to download.
        genre: Genre folder to save into (e.g., dark-techno, melodic-techno, deep, minimal, progressive, vocal, psychill).
    """
    genre_dir = _music_dir() / genre
    genre_dir.mkdir(parents=True, exist_ok=True)

    # Track files before download to find what was added
    before = set(genre_dir.glob("*.mp3"))

    # Get metadata from YouTube BEFORE downloading
    yt_meta = {}
    try:
        meta_result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-download", url],
            capture_output=True, text=True, timeout=30,
        )
        if meta_result.returncode == 0 and meta_result.stdout.strip():
            yt_meta = json.loads(meta_result.stdout.strip())
    except Exception:
        pass

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

        # Insert into DB immediately (basic info)
        from ..db import upsert_track
        upsert_track(path=str(actual_path), title=actual_path.stem, genre=genre)

        # Background: full analysis + ID3 tags + DB update (pass YouTube metadata)
        threading.Thread(target=_enrich_track, args=(actual_path, genre, yt_meta), daemon=True).start()

        return f"Downloaded: {actual_path}"

    # No new file -- yt-dlp skipped (already exists)
    if "--no-overwrites" in result.stdout or "has already been downloaded" in result.stdout:
        return "ALREADY EXISTS: This track was already downloaded. Search for a DIFFERENT track."

    return f"Downloaded to {genre}/ folder"
