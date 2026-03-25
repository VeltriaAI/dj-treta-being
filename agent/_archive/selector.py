"""Two-stage track selector.

Stage 1 (deterministic): Filter by BPM ±6, Camelot compatible, energy ±2, not played.
Stage 2 (LLM): Top candidates → brain ranks → final pick with reasoning.
"""

from pathlib import Path

from .camelot import key_compatibility_score, mixxx_key_to_musical, KEY_TO_CAMELOT
from .state import TrackState


def scan_library(music_dir: Path) -> list[dict]:
    """Scan music directory for tracks."""
    tracks = []
    if not music_dir.exists():
        return tracks

    for genre_dir in sorted(music_dir.iterdir()):
        if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
            continue
        for f in sorted(genre_dir.iterdir()):
            if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                tracks.append({
                    "path": str(f),
                    "filename": f.stem,
                    "genre": genre_dir.name,
                })
    return tracks


def filter_candidates(
    tracks: list[dict],
    current_bpm: float,
    current_key: str,
    current_energy: int,
    played_paths: set[str],
    bpm_tolerance: float = 6.0,
    energy_tolerance: int = 2,
) -> list[dict]:
    """Stage 1: Deterministic filter.

    Returns candidates sorted by compatibility score (best first).
    """
    candidates = []

    for track in tracks:
        path = track["path"]

        # Skip already played
        if path in played_paths:
            continue

        # BPM filter (if BPM known)
        track_bpm = track.get("bpm", 0)
        if track_bpm > 0 and current_bpm > 0:
            if abs(track_bpm - current_bpm) > bpm_tolerance:
                # Check half/double time
                if abs(track_bpm * 2 - current_bpm) > bpm_tolerance and \
                   abs(track_bpm / 2 - current_bpm) > bpm_tolerance:
                    continue

        # Energy filter
        track_energy = track.get("energy", 5)
        if abs(track_energy - current_energy) > energy_tolerance:
            continue

        # Key compatibility score
        track_key = track.get("key", "")
        key_score = key_compatibility_score(current_key, track_key)
        track["_key_score"] = key_score
        track["_bpm_diff"] = abs(track_bpm - current_bpm) if track_bpm > 0 else 999

        candidates.append(track)

    # Sort: key score desc, then BPM diff asc
    candidates.sort(key=lambda t: (-t.get("_key_score", 0), t.get("_bpm_diff", 999)))

    return candidates[:20]  # top 20 for brain to rank


def suggest_technique(
    current_genre: str,
    next_genre: str,
    key_score: int,
    bpm_diff: float,
) -> str:
    """Suggest transition technique based on genre and compatibility."""

    # Same genre, compatible key → long blend
    if current_genre == next_genre and key_score >= 8:
        if current_genre in ("psychill", "ambient"):
            return "blend"
        elif current_genre in ("melodic-techno", "progressive"):
            return "filter_sweep"
        elif current_genre in ("dark-techno", "minimal"):
            return "bass_swap"
        else:
            return "blend"

    # Different genre → depends on how different
    if key_score >= 5:
        return "filter_sweep"
    elif bpm_diff > 4:
        return "hard_cut"
    else:
        return "bass_swap"
