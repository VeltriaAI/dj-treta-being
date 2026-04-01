"""Music discovery -- YouTube search and download."""

import json
import subprocess
from pathlib import Path

from .helpers import _music_dir


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
        from ..db import upsert_track
        upsert_track(path=str(actual_path), title=actual_path.stem, genre=genre)

        import threading
        from .perception import analyze_track
        def _bg_analyze():
            try:
                analyze_track(str(actual_path))
            except Exception:
                pass
        threading.Thread(target=_bg_analyze, daemon=True).start()

        return f"Downloaded: {actual_path}"

    # No new file -- yt-dlp skipped (already exists)
    if "--no-overwrites" in result.stdout or "has already been downloaded" in result.stdout:
        return "ALREADY EXISTS: This track was already downloaded. Search for a DIFFERENT track."

    return f"Downloaded to {genre}/ folder"
