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

# NS-001: JSON-schema response_format for the planner call. DELIBERATELY
# LOOSER than validate_playlist: planned_at is NOT required (server-stamped
# in planner_loop), transition_hint / bpm / energy / v9 fields optional.
# Non-strict (no additionalProperties:false) so unsupported gateways degrade
# gracefully; extract_json fallback + coercion stay in place.
PLAYLIST_V1_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "playlist_v1",
        "schema": {
            "type": "object",
            "properties": {
                "mood_snapshot": {"type": "string"},
                "reasoning_summary": {"type": "string"},
                "tracks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rank": {"type": "integer"},
                            "path": {"type": "string"},
                            "title": {"type": "string"},
                            "bpm": {"type": ["number", "null"]},
                            "key_camelot": {"type": "string"},
                            "energy": {"type": ["integer", "null"]},
                            "reason": {"type": "string"},
                            "transition_hint": {
                                "type": ["object", "null"],
                                "properties": {
                                    "technique": {"type": "string"},
                                    "duration": {"type": "integer"},
                                    "at_section": {"type": "string"},
                                },
                            },
                            "downloaded": {"type": "boolean"},
                            "video_id": {"type": "string"},
                            "mbid": {"type": "string"},
                        },
                        "required": ["rank", "path", "title", "reason"],
                    },
                },
            },
            "required": ["mood_snapshot", "reasoning_summary", "tracks"],
        },
    },
}


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
    # Empty tracks list is ALLOWED — planner uses it to signal "library is
    # thin, need to download more". reasoning_summary should explain.

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

    # v9: tracks may be undownloaded (knowledge-dataset picks). downloaded
    # defaults to True for backward-compat with v8 playlists.
    downloaded = bool(t.get("downloaded", True))
    video_id = str(t.get("video_id", "") or "")
    mbid = str(t.get("mbid", "") or "")

    path = t.get("path", "")
    if not isinstance(path, str):
        raise PlaylistValidationError(f"track #{idx}: path must be a string, got {type(path).__name__}")
    path = path.strip()

    if downloaded:
        if not path:
            raise PlaylistValidationError(
                f"track #{idx}: path must be non-empty when downloaded=true"
            )
        if path in seen_paths:
            raise PlaylistValidationError(f"track #{idx}: duplicate path {path!r}")
        seen_paths.add(path)
    else:
        # v9 undownloaded: need at least one of video_id or mbid to resolve later.
        if not video_id and not mbid:
            raise PlaylistValidationError(
                f"track #{idx}: downloaded=false requires video_id or mbid"
            )
        # Paths are optional for undownloaded — but still dedup if present.
        if path:
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
        "path": path,
        "title": str(t.get("title", "") or ""),
        "bpm": bpm,
        "key_camelot": str(t.get("key_camelot", "") or ""),
        "energy": energy,
        "reason": str(t.get("reason", "") or ""),
        "transition_hint": transition,
        # v9 fields
        "downloaded": downloaded,
        "video_id": video_id,
        "mbid": mbid,
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


# DJ Treta-generated originals are written by generation.py as
# "DJ Treta - {name}.mp3" with ID3 artist 'DJ Treta'. The validated playlist
# track dict carries only path/title (no artist), so the path-basename prefix
# is the reliable original-detection signal here.
_ORIGINAL_FILENAME_PREFIX = "DJ Treta - "


def _is_original(track: dict) -> bool:
    """True if `track` is a DJ Treta-generated original (filename-based)."""
    path = track.get("path") or ""
    return path.rsplit("/", 1)[-1].startswith(_ORIGINAL_FILENAME_PREFIX)


def pick_next_candidate(
    playlist: dict | None,
    exclude_paths: set,
    played_titles: list,
    played_paths: set | None = None,
    *,
    downloaded_only: bool = True,
    exclude_originals: bool = False,
) -> dict | None:
    """Return the highest-rank track from `playlist` not already played or on deck.

    `downloaded_only=True` (default) skips knowledge-dataset candidates that
    haven't been fetched yet — DJ can't load_track(path="").
    Returns None if the playlist is empty / all candidates excluded.

    `exclude_originals=True` is a defensive belt-and-braces filter for GH #68:
    when the treta_originals source is off, no DJ Treta-generated original can
    be picked even if one leaked into the playlist. Defaults to False so
    behavior is unchanged when treta_originals is on.

    Uses three dedup layers (any one excludes a candidate):
      1. exclude_paths — currently loaded on a deck
      2. played_titles — title-matched already-played
      3. played_paths — path-basename-matched already-played (more reliable
         than title; Mixxx-reported titles often differ from DB titles —
         the BUG-17 comment in heartbeat.py notes the same gap)
    """
    if not playlist or not isinstance(playlist.get("tracks"), list):
        return None
    played_titles_set = set(played_titles or [])
    # Build basename set from played_paths for path-format-tolerant match.
    played_basenames = set()
    if played_paths:
        for p in played_paths:
            if p:
                played_basenames.add(p.rsplit("/", 1)[-1])
    ranked = sorted(playlist["tracks"], key=lambda t: t.get("rank", 999))
    for track in ranked:
        if downloaded_only and not track.get("downloaded", True):
            continue
        if exclude_originals and _is_original(track):
            continue
        path = track.get("path") or ""
        if path in exclude_paths:
            continue
        # Path basename match — catches replays the title dedup misses.
        if played_basenames and path.rsplit("/", 1)[-1] in played_basenames:
            continue
        title = track.get("title", "")
        if title and title in played_titles_set:
            continue
        return track
    return None


def first_undownloaded_candidate(playlist: dict | None) -> dict | None:
    """Return the highest-rank undownloaded track — surfaces `library_need`.

    v9 planner uses this to tell the library agent what to fetch next.
    """
    if not playlist or not isinstance(playlist.get("tracks"), list):
        return None
    ranked = sorted(playlist["tracks"], key=lambda t: t.get("rank", 999))
    for track in ranked:
        if not track.get("downloaded", True):
            return track
    return None
