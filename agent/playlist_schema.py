"""PlaylistV1 schema — structured JSON the planner agent emits to session.playlist.

Replaces the v7 markdown blob that Python ignored. Validation is strict so a
malformed LLM response never silently corrupts downstream state: the caller
gets a ValueError and can retry or fall back to the last valid playlist.

Schema:
    {
      "planned_at": <unix_timestamp_float>,
      "mood_snapshot": "<canonical_slug>",
      "reasoning_summary": "<one paragraph>",
      "tracks": [
        {
          "rank": 1,
          "path": "/full/path.mp3",
          "title": "Artist - Song",
          "bpm": 124.5,
          "key_camelot": "8A",
          "energy": 7,
          "reason": "why this fits next",
          "transition_hint": {
            "technique": "crossfade",     # crossfade|bass_swap|filter_sweep|echo_out|hard_cut
            "duration": 45,               # seconds, 10-90
            "at_section": "breakdown"     # breakdown|outro|build|drop|intro
          }
        },
        ...
      ]
    }

We use dict-based validation (no pydantic dependency) to stay lightweight.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("dj-treta")

_VALID_TECHNIQUES = {"crossfade", "bass_swap", "filter_sweep", "echo_out", "hard_cut"}


class PlaylistValidationError(ValueError):
    """Raised when a playlist dict fails schema validation."""


def validate_playlist(data: Any) -> dict:
    """Validate and normalize a playlist dict. Raises PlaylistValidationError.

    Returns a cleaned dict with canonicalized fields — extra unknown keys are
    preserved (forward-compat), missing optional fields get defaults.
    """
    if not isinstance(data, dict):
        raise PlaylistValidationError(
            f"playlist must be a JSON object, got {type(data).__name__}"
        )

    planned_at = data.get("planned_at")
    if not isinstance(planned_at, (int, float)):
        raise PlaylistValidationError("planned_at must be a number (unix timestamp)")

    mood_snapshot = data.get("mood_snapshot", "")
    if not isinstance(mood_snapshot, str):
        raise PlaylistValidationError("mood_snapshot must be a string")

    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        raise PlaylistValidationError("tracks must be a list")
    if not tracks:
        raise PlaylistValidationError("tracks list is empty")

    clean_tracks = []
    seen_ranks = set()
    seen_paths = set()
    for i, t in enumerate(tracks):
        if not isinstance(t, dict):
            raise PlaylistValidationError(f"track #{i} must be an object")
        clean_tracks.append(_validate_track(t, i, seen_ranks, seen_paths))

    return {
        "planned_at": float(planned_at),
        "mood_snapshot": mood_snapshot,
        "reasoning_summary": str(data.get("reasoning_summary", "")),
        "tracks": clean_tracks,
    }


def _validate_track(t: dict, idx: int, seen_ranks: set, seen_paths: set) -> dict:
    rank = t.get("rank")
    if not isinstance(rank, int) or rank < 1:
        raise PlaylistValidationError(f"track #{idx}: rank must be positive int, got {rank!r}")
    if rank in seen_ranks:
        raise PlaylistValidationError(f"track #{idx}: duplicate rank {rank}")
    seen_ranks.add(rank)

    path = t.get("path")
    if not isinstance(path, str) or not path.strip():
        raise PlaylistValidationError(f"track #{idx}: path must be a non-empty string")
    if path in seen_paths:
        raise PlaylistValidationError(f"track #{idx}: duplicate path {path!r}")
    seen_paths.add(path)

    # Optional numeric fields — coerce if present, default if missing.
    bpm = t.get("bpm")
    if bpm is not None:
        try:
            bpm = float(bpm)
        except (TypeError, ValueError):
            raise PlaylistValidationError(f"track #{idx}: bpm must be a number, got {bpm!r}")

    energy = t.get("energy")
    if energy is not None:
        try:
            energy = int(energy)
        except (TypeError, ValueError):
            raise PlaylistValidationError(f"track #{idx}: energy must be an int, got {energy!r}")
        if not 1 <= energy <= 10:
            raise PlaylistValidationError(f"track #{idx}: energy {energy} out of 1-10 range")

    transition = t.get("transition_hint")
    if transition is not None:
        transition = _validate_transition(transition, idx)

    return {
        "rank": rank,
        "path": path.strip(),
        "title": str(t.get("title", "") or ""),
        "bpm": bpm,
        "key_camelot": str(t.get("key_camelot", "") or ""),
        "energy": energy,
        "reason": str(t.get("reason", "") or ""),
        "transition_hint": transition,
    }


def _validate_transition(hint: Any, track_idx: int) -> dict:
    if not isinstance(hint, dict):
        raise PlaylistValidationError(
            f"track #{track_idx}: transition_hint must be an object"
        )
    technique = hint.get("technique", "crossfade")
    if technique not in _VALID_TECHNIQUES:
        log.warning(
            f"track #{track_idx}: unknown technique {technique!r}, coercing to crossfade"
        )
        technique = "crossfade"
    duration = hint.get("duration", 45)
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 45
    duration = max(10, min(90, duration))
    return {
        "technique": technique,
        "duration": duration,
        "at_section": str(hint.get("at_section", "outro") or "outro"),
    }


def pick_next_candidate(
    playlist: dict | None,
    exclude_paths: set,
    played_titles: list,
) -> dict | None:
    """Return the highest-rank track from `playlist` not already played or on deck.

    Returns None if the playlist is empty / all candidates excluded.
    """
    if not playlist or not isinstance(playlist.get("tracks"), list):
        return None
    played_titles_set = set(played_titles or [])
    ranked = sorted(playlist["tracks"], key=lambda t: t.get("rank", 999))
    for track in ranked:
        if track.get("path") in exclude_paths:
            continue
        title = track.get("title", "")
        if title and title in played_titles_set:
            continue
        return track
    return None
