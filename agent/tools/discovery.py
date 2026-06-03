"""Music discovery -- YouTube search and download with full metadata."""

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .helpers import _music_dir

log = logging.getLogger("dj-treta")


_YTMUSIC_CLIENT = None


def _ytmusic():
    """Lazy YTMusic client. Anonymous mode — no auth needed for search."""
    global _YTMUSIC_CLIENT
    if _YTMUSIC_CLIENT is None:
        from ytmusicapi import YTMusic
        _YTMUSIC_CLIENT = YTMusic()
    return _YTMUSIC_CLIENT


def _parse_duration(dur_str: str | None, dur_s: int | None) -> int:
    """ytmusicapi returns duration as 'M:SS' string + duration_seconds int.
    Prefer the int, fall back to parsing the string."""
    if isinstance(dur_s, int) and dur_s > 0:
        return dur_s
    if not dur_str:
        return 0
    parts = dur_str.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        pass
    return 0


def search_music(query: str = "", artist: str = "", title: str = "",
                 limit: int = 10, min_duration: int = 120,
                 max_duration: int = 720) -> list:
    """Search YouTube Music for individual songs.

    Three usage shapes — pass whatever signal you have:
      - Broad mood/genre browse:  search_music(query="hypnotic deep techno")
      - Artist discography:       search_music(artist="ARTBAT")
      - Specific track lookup:    search_music(artist="ARTBAT", title="Horizon")
      - Mixed:                    search_music(query="atmospheric", artist="Anyma")

    Backed by YouTube Music's `songs` filter — but YT Music tags 30-min
    DJ mixes and 30-min "sleep music" as 'Songs', so a duration window
    is applied client-side. Default is 2-12 min (typical track length).

    Returns:
        List of dicts:
          {video_id, url, title, artist, album, duration_seconds, duration,
           year, is_explicit}
        Empty list when no results — caller should treat this as "try a
        different query / different artist", NOT as a tool error.

    Args:
        query: Free-form text — mood, genre, vibe, or any combo.
        artist: Optional artist filter. Combined with title → precise.
        title: Optional song title. Combined with artist → precise.
        limit: 1-30, default 10.
        min_duration: Minimum seconds (default 120 — drops jingles/snippets).
        max_duration: Maximum seconds (default 720 — drops mixes/sets).
            Pass higher (e.g. 1800) for ambient/long-form moods explicitly.
    """
    limit = max(1, min(30, limit))

    # Compose search string from whatever signals the caller passed.
    parts = []
    if artist:
        parts.append(artist.strip())
    if title:
        parts.append(title.strip())
    if query and not (artist and title):
        # Mix the broad query in unless we already have a precise A+T.
        parts.append(query.strip())
    q = " ".join(parts).strip()
    if not q:
        return [{"info": "search_music called with no query/artist/title — pass at least one"}]

    try:
        raw = _ytmusic().search(q, filter="songs", limit=limit)
    except Exception as e:
        log.warning(f"ytmusicapi search failed: {e}")
        return [{"error": f"ytmusicapi: {type(e).__name__}: {str(e)[:120]}"}]

    results = []
    dropped_long = 0
    dropped_short = 0
    for r in raw:
        vid = r.get("videoId") or ""
        if not vid:
            continue
        artists = r.get("artists") or []
        artist_names = ", ".join(a.get("name", "") for a in artists if a.get("name"))
        album = (r.get("album") or {}).get("name", "") if r.get("album") else ""
        dur_s = _parse_duration(r.get("duration"), r.get("duration_seconds"))
        # Duration window — YT Music tags 30-min mixes + 30-min "sleep
        # music" as 'Songs', so client-side filtering is required even
        # with filter='songs'. min_duration drops snippets/intros;
        # max_duration drops mixes/long-form. Caller can widen.
        if dur_s and dur_s > max_duration:
            dropped_long += 1
            continue
        if dur_s and dur_s < min_duration:
            dropped_short += 1
            continue
        results.append({
            "video_id": vid,
            "url": f"https://music.youtube.com/watch?v={vid}",
            "title": r.get("title") or "",
            "artist": artist_names,
            "album": album,
            "duration": r.get("duration") or "",
            "duration_seconds": dur_s,
            "year": r.get("year") or "",
            "is_explicit": bool(r.get("isExplicit")),
        })
        if len(results) >= limit:
            break

    if dropped_long or dropped_short:
        log.info(
            f"search_music({q!r}): {len(results)} kept, "
            f"{dropped_long} too long (>{max_duration}s), "
            f"{dropped_short} too short (<{min_duration}s)"
        )
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
            from ..billing_rates import bill_from_response
            bill_from_response(resp, "library")
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


def download_track(url: str, genre: str = "deep") -> dict:
    """Download a track from YouTube into the music library.

    Three-layer dedup to avoid duplicates:
      1. URL match against tracks.source_url / track_aliases (free, instant)
      2. LLM canonical-identity match against library (one cheap LLM call)
      3. Download with canonical filename + store canonical fields in DB

    Auto-analyzes in background (BPM, key, energy, sections, ID3 tags).

    Returns:
        dict with shape:
          {ok: bool, path: str | None, message: str}
        - ok=True with `path` set: track is on disk at that absolute path.
          Pass `path` directly into play_specific_track — DO NOT construct
          paths from your head, ever. The returned path IS authoritative.
        - ok=False: download failed. `message` explains why.

        Note: "ALREADY EXISTS" is treated as ok=True with the existing
        path returned, so the caller can play it without re-downloading.

    Args:
        url: YouTube URL to download.
        genre: Genre folder (dark-techno, melodic-techno, bollyafro, etc.).
               Normalized to lowercase to prevent case-drift duplicates.
    """
    from ..db import (
        upsert_track, find_track_by_source_url,
        find_track_by_canonical, add_track_alias,
    )
    from ..canonicalize import llm_canonicalize, canonical_filename

    # Layer 1: URL already in library?
    existing = find_track_by_source_url(url)
    if existing:
        existing_path = existing.get("path") or ""
        # Resolve to absolute path so play_specific_track can use it
        # directly without _resolve_track_path lookup gymnastics.
        if existing_path and not os.path.isabs(existing_path):
            existing_path = str(_music_dir() / existing_path)
        return {
            "ok": True,
            "path": existing_path,
            "message": (
                f"ALREADY EXISTS (URL match): "
                f"{existing.get('title') or existing_path}. "
                f"Use this path with play_specific_track."
            ),
        }

    # Fetch YouTube metadata WITHOUT downloading
    yt_meta = {}
    try:
        meta_result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-download", url],
            capture_output=True, text=True, timeout=30,
        )
        if meta_result.returncode == 0 and meta_result.stdout.strip():
            yt_meta = json.loads(meta_result.stdout.strip())
    except Exception as e:
        log.warning(f"yt-dlp metadata fetch failed: {e}")

    yt_title = yt_meta.get("title", "")
    yt_uploader = yt_meta.get("uploader") or yt_meta.get("channel", "")
    yt_duration = yt_meta.get("duration", 0) or 0

    # Layer 2: canonical-identity check via LLM
    canon = llm_canonicalize(yt_title, yt_uploader, yt_duration)

    existing = find_track_by_canonical(
        canon["canonical_artist"], canon["canonical_song"],
        canon["canonical_version"], canon["remixer"],
    )
    if existing:
        add_track_alias(
            track_id=existing["id"], source_url=url,
            original_title=yt_title, original_uploader=yt_uploader,
        )
        existing_path = existing.get("path") or ""
        if existing_path and not os.path.isabs(existing_path):
            existing_path = str(_music_dir() / existing_path)
        return {
            "ok": True,
            "path": existing_path,
            "message": (
                f"ALREADY EXISTS (canonical match): "
                f"{canon['canonical_artist']} - {canon['canonical_song']} "
                f"({canon.get('canonical_version') or 'Original'}). "
                f"URL recorded as alias. Use this path with play_specific_track."
            ),
        }

    # Layer 3: download with canonical filename
    genre_norm = (genre or "").strip().lower() or "unsorted"
    genre_dir = _music_dir() / genre_norm
    genre_dir.mkdir(parents=True, exist_ok=True)

    fname_stem = canonical_filename(canon, fallback=yt_title or "track")
    before = set(genre_dir.glob("*.mp3"))

    result = subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "320",
         "-o", str(genre_dir / f"{fname_stem}.%(ext)s"),
         "--no-playlist", "--no-overwrites", url],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return {
            "ok": False,
            "path": None,
            "message": f"Download failed: {result.stderr[:200]}",
        }

    # Find the actual file — yt-dlp may sanitize chars in filename
    after = set(genre_dir.glob("*.mp3"))
    new_files = after - before
    if not new_files:
        if "has already been downloaded" in result.stdout:
            return {
                "ok": False,
                "path": None,
                "message": (
                    "File with this name already on disk but not in DB — "
                    "run rescan_library or manual cleanup."
                ),
            }
        return {
            "ok": False,
            "path": None,
            "message": f"Downloaded to {genre_norm}/ folder (couldn't identify new file)",
        }

    actual_path = next(iter(new_files))

    # Insert with full canonical identity
    upsert_track(
        path=str(actual_path),
        title=actual_path.stem,
        genre=genre_norm,
        source_url=url,
        original_title=yt_title,
        canonical_artist=canon["canonical_artist"],
        canonical_song=canon["canonical_song"],
        canonical_version=canon["canonical_version"],
        remixer=canon["remixer"],
        canonical_confidence=canon["canonical_confidence"],
    )

    # Background: audio analysis (BPM/key/energy) + mood/similar/ID3 tags
    threading.Thread(
        target=_enrich_track, args=(actual_path, genre_norm, yt_meta), daemon=True
    ).start()

    return {
        "ok": True,
        "path": str(actual_path),
        "message": (
            f"Downloaded: {canon['canonical_artist']} - {canon['canonical_song']}"
            f" [{genre_norm}] (confidence {canon['canonical_confidence']:.2f})"
        ),
    }
